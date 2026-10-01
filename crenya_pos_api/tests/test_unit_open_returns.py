"""Returns without an invoice: profile flag, batch requirement and incoming rates (no site needed)."""

import unittest
from unittest import mock

import frappe

from crenya_pos_api.sync import batches, open_returns
from crenya_pos_api.sync.batches import lines_missing_batch, require_line_batches
from crenya_pos_api.sync.errors import SyncError
from crenya_pos_api.sync.open_returns import (
	ALLOW_FIELD,
	allows_return_without_invoice,
	assert_allowed,
	set_valuation_incoming_rates,
)
from crenya_pos_api.sync.validation import validate_invoice_payload
from crenya_pos_api.tests.test_unit_validation import make_open_return


def line(line_no, item_code, batch_no=None):
	return {"line_no": line_no, "item_code": item_code, "batch_no": batch_no}


class TestProfileFlag(unittest.TestCase):
	def test_flag_values(self):
		for value, expected in ((None, False), (0, False), ("0", False), (1, True), ("1", True)):
			with self.subTest(value=value):
				profile = frappe._dict({"name": "Till", ALLOW_FIELD: value})
				self.assertIs(allows_return_without_invoice(profile), expected)

	def test_missing_field_is_off(self):
		self.assertFalse(allows_return_without_invoice(frappe._dict({"name": "Till"})))

	def test_assert_allowed(self):
		assert_allowed(frappe._dict({"name": "Till", ALLOW_FIELD: 1}))
		with self.assertRaises(SyncError) as ctx:
			assert_allowed(frappe._dict({"name": "Till", ALLOW_FIELD: 0}))
		self.assertEqual(ctx.exception.code, "validation")
		self.assertIn("Till does not allow returns without an invoice", ctx.exception.message)


class TestBatchRequirement(unittest.TestCase):
	def test_lines_missing_batch(self):
		lines = [
			line(1, "PARA", "B1"),
			line(2, "PARA"),
			line(3, "MILK"),
			line(4, "PARA", ""),
		]
		self.assertEqual([x["line_no"] for x in lines_missing_batch(lines, {"PARA"})], [2, 4])
		self.assertEqual(lines_missing_batch(lines, set()), [])

	def test_payload_lines(self):
		payload = make_open_return()
		data = validate_invoice_payload(payload)
		self.assertEqual(lines_missing_batch(data["items"], {"MILK-1L"}), data["items"])
		payload["items"][0]["batch_no"] = "MILK-B1"
		data = validate_invoice_payload(payload)
		self.assertEqual(lines_missing_batch(data["items"], {"MILK-1L"}), [])

	def test_require_line_batches(self):
		with mock.patch.object(batches, "_batch_tracked_items", return_value={"PARA"}):
			require_line_batches([line(1, "PARA", "B1"), line(2, "MILK")])
			with self.assertRaises(SyncError) as ctx:
				require_line_batches([line(1, "MILK"), line(2, "PARA")])
		self.assertEqual(ctx.exception.code, "validation")
		self.assertIn("Line 2 (PARA): batch_no is required", ctx.exception.message)


STOCK_ITEMS = {"PARA": 1, "MILK": 1, "SERVICE": 0}


class TestIncomingRates(unittest.TestCase):
	def make_doc(self, rows, update_stock=1):
		return frappe._dict(
			{
				"doctype": "Sales Invoice",
				"name": None,
				"company": "Crenya LLC",
				"posting_date": "2026-10-01",
				"posting_time": "10:00:00",
				"update_stock": update_stock,
				"items": [
					frappe._dict({"warehouse": "Stores", "conversion_factor": 1, "incoming_rate": 0, **row})
					for row in rows
				],
			}
		)

	def run_rates(self, doc, rates):
		calls = []

		def rate(args, raise_error_if_no_rate=True, fallbacks=True):
			calls.append((args["item_code"], args["batch_no"], args["qty"], raise_error_if_no_rate))
			return rates.get((args["item_code"], args["batch_no"]), 0)

		with (
			mock.patch.object(open_returns, "_rate_function", return_value=rate),
			mock.patch.object(frappe, "get_cached_value", side_effect=lambda dt, name, f: STOCK_ITEMS[name]),
			# no rounding: without a site frappe's rounding settings are not available
			mock.patch.object(frappe, "get_precision", return_value=None),
		):
			set_valuation_incoming_rates(doc)
		return calls

	def test_batch_rate_then_warehouse_rate(self):
		doc = self.make_doc(
			[
				{"item_code": "PARA", "batch_no": "B1", "qty": -2, "conversion_factor": 12},
				{"item_code": "PARA", "batch_no": "EMPTY", "qty": -1},
				{"item_code": "MILK", "qty": -1},
				{"item_code": "SERVICE", "qty": -1},
			]
		)
		calls = self.run_rates(doc, {("PARA", "B1"): 0.3, ("PARA", None): 0.25, ("MILK", None): 0.4})
		self.assertEqual([row.incoming_rate for row in doc["items"]], [0.3, 0.25, 0.4, 0])
		self.assertEqual(
			calls,
			[
				("PARA", "B1", -24, False),
				("PARA", "EMPTY", -1, False),
				("PARA", None, -1, False),
				("MILK", None, -1, False),
			],
		)

	def test_zero_is_left_to_erpnext(self):
		doc = self.make_doc([{"item_code": "MILK", "qty": -1, "incoming_rate": 0}])
		self.run_rates(doc, {})
		self.assertEqual(doc["items"][0].incoming_rate, 0)

	def test_without_stock_update_nothing_is_set(self):
		doc = self.make_doc([{"item_code": "MILK", "qty": -1}], update_stock=0)
		self.assertEqual(self.run_rates(doc, {("MILK", None): 0.4}), [])
		self.assertEqual(doc["items"][0].incoming_rate, 0)


if __name__ == "__main__":
	unittest.main()
