"""Batch payload keys, batch feed records, return splitting and the stock cursor (no site needed)."""

import base64
import copy
import json
import unittest
from datetime import date, datetime
from decimal import Decimal
from unittest import mock

from crenya_pos_api.sync.batches import (
	BatchSplitError,
	build_batch_record,
	build_batch_records,
	floor_to_step,
	single_batch,
	split_return_qty,
)
from crenya_pos_api.sync.context import qty_precision
from crenya_pos_api.sync.cursor import Cursor, InvalidCursor, decode_cursor, encode_cursor
from crenya_pos_api.sync.errors import SyncError
from crenya_pos_api.sync.validation import validate_invoice_payload
from crenya_pos_api.tests.test_unit_validation import SALE, make_return
from crenya_pos_api.utils.decimal import format_number

STEP = Decimal("0.001")


def sale_with_item(**item_overrides):
	payload = copy.deepcopy(SALE)
	payload["items"][0].update(item_overrides)
	return payload


def split(qty, batches, step=STEP):
	return [
		(batch_no, format_number(part)) for batch_no, part in split_return_qty(Decimal(qty), batches, step)
	]


def d(*values):
	return [Decimal(value) for value in values]


class TestBatchPayload(unittest.TestCase):
	def test_batch_no_is_optional(self):
		line = validate_invoice_payload(copy.deepcopy(SALE))["items"][0]
		self.assertIsNone(line["batch_no"])
		for empty in (None, "", "   "):
			line = validate_invoice_payload(sale_with_item(batch_no=empty))["items"][0]
			self.assertIsNone(line["batch_no"])

	def test_batch_no_is_kept_trimmed(self):
		line = validate_invoice_payload(sale_with_item(batch_no="  PARA-B1 "))["items"][0]
		self.assertEqual(line["batch_no"], "PARA-B1")

	def test_batch_no_on_a_return_is_accepted(self):
		payload = make_return()
		payload["items"][0]["batch_no"] = "PARA-B1"
		self.assertEqual(validate_invoice_payload(payload)["items"][0]["batch_no"], "PARA-B1")

	def test_bad_batch_no_is_validation(self):
		for value in ("B" * 141, 12, ["PARA-B1"], {"batch": "x"}):
			with self.assertRaises(SyncError, msg=repr(value)) as ctx:
				validate_invoice_payload(sale_with_item(batch_no=value))
			self.assertEqual(ctx.exception.code, "validation")
			self.assertIn("items[0].batch_no", ctx.exception.message)

	def test_batch_no_of_140_characters_is_allowed(self):
		line = validate_invoice_payload(sale_with_item(batch_no="B" * 140))["items"][0]
		self.assertEqual(len(line["batch_no"]), 140)


BATCH_ROW = {
	"name": "PARA-B1",
	"item": "PARA-500",
	"expiry_date": date(2027, 3, 31),
	"manufacturing_date": date(2025, 4, 1),
	"disabled": 0,
	"modified": datetime(2026, 9, 29, 14, 3, 15, 120000),
}


class TestBatchRecords(unittest.TestCase):
	ROW = BATCH_ROW

	def test_record_fields(self):
		record = build_batch_record(self.ROW, 12.0, 3)
		self.assertEqual(
			record,
			{
				"name": "PARA-B1",
				"item_code": "PARA-500",
				"expiry_date": "2027-03-31",
				"manufacturing_date": "2025-04-01",
				"disabled": 0,
				"qty": "12",
				"modified": "2026-09-29 14:03:15.120000",
			},
		)

	def test_missing_dates_and_stock(self):
		row = dict(self.ROW, expiry_date=None, manufacturing_date=None, disabled=1)
		record = build_batch_record(row, None, 3)
		self.assertIsNone(record["expiry_date"])
		self.assertIsNone(record["manufacturing_date"])
		self.assertEqual(record["disabled"], 1)
		self.assertEqual(record["qty"], "0")

	def test_quantities_are_rounded_to_precision(self):
		self.assertEqual(build_batch_record(self.ROW, 2.0000001, 3)["qty"], "2")
		self.assertEqual(build_batch_record(self.ROW, 1.25, 1)["qty"], "1.3")
		self.assertEqual(build_batch_record(self.ROW, -3.5, 3)["qty"], "-3.5")

	def test_records_pick_their_own_quantity(self):
		rows = [self.ROW, dict(self.ROW, name="PARA-B2")]
		records = build_batch_records(rows, {"PARA-B2": 4.5}, 3)
		self.assertEqual([(r["name"], r["qty"]) for r in records], [("PARA-B1", "0"), ("PARA-B2", "4.5")])

	def test_single_batch(self):
		self.assertEqual(single_batch([("B1", 5.0)]), "B1")
		self.assertEqual(single_batch([("B1", 2.0), ("B1", 3.0)]), "B1")
		self.assertIsNone(single_batch([("B1", 2.0), ("B2", 3.0)]))
		self.assertIsNone(single_batch([]))
		self.assertIsNone(single_batch(None))


class TestSplitReturnQty(unittest.TestCase):
	def test_whole_row_goes_back_as_sold(self):
		batches = [("A", *d("3", "3")), ("B", *d("2", "2"))]
		self.assertEqual(split("5", batches), [("A", "3"), ("B", "2")])

	def test_share_is_proportional_to_what_was_sold(self):
		batches = [("A", *d("6", "6")), ("B", *d("2", "2"))]
		self.assertEqual(split("4", batches), [("A", "3"), ("B", "1")])

	def test_rounding_goes_to_the_largest_fraction(self):
		batches = [("A", *d("3", "3")), ("B", *d("2", "2"))]
		# ideal 1.2 / 0.8 in whole units
		self.assertEqual(split("2", batches, Decimal(1)), [("A", "1"), ("B", "1")])
		# ideal 0.6 / 0.4
		self.assertEqual(split("1", batches, Decimal(1)), [("A", "1")])

	def test_ties_go_to_the_earlier_batch(self):
		batches = [("A", *d("1", "1")), ("B", *d("1", "1"))]
		self.assertEqual(split("1", batches, Decimal(1)), [("A", "1")])

	def test_fractional_quantities(self):
		batches = [("A", *d("1", "1")), ("B", *d("1", "1")), ("C", *d("1", "1"))]
		parts = split_return_qty(Decimal("1"), batches, STEP)
		self.assertEqual(sum(part for _b, part in parts), Decimal("1"))
		self.assertEqual([format_number(part) for _b, part in parts], ["0.334", "0.333", "0.333"])

	def test_capped_by_what_was_already_returned(self):
		# A sold 3 but 3 are back already; B sold 2, nothing returned
		batches = [("A", *d("3", "0")), ("B", *d("2", "2"))]
		self.assertEqual(split("2", batches), [("B", "2")])

	def test_capped_share_moves_to_the_other_batches(self):
		# ideal 1.8 / 1.2; A can take only 1
		batches = [("A", *d("3", "1")), ("B", *d("2", "2"))]
		self.assertEqual(split("3", batches, Decimal(1)), [("A", "1"), ("B", "2")])

	def test_capped_share_spreads_proportionally(self):
		batches = [("A", *d("4", "0.5")), ("B", *d("4", "4")), ("C", *d("2", "2"))]
		self.assertEqual(split("4", batches), [("A", "0.5"), ("B", "2.333"), ("C", "1.167")])

	def test_more_than_the_batches_can_take_is_refused(self):
		batches = [("A", *d("3", "1")), ("B", *d("2", "1"))]
		with self.assertRaises(BatchSplitError) as ctx:
			split_return_qty(Decimal("3"), batches, STEP)
		self.assertEqual(ctx.exception.available, Decimal("2"))
		self.assertIn("2", str(ctx.exception))

	def test_returnable_is_floored_to_the_step(self):
		batches = [("A", *d("2", "1.5")), ("B", *d("2", "2"))]
		with self.assertRaises(BatchSplitError):
			split_return_qty(Decimal("4"), batches, Decimal(1))
		self.assertEqual(split("3", batches, Decimal(1)), [("A", "1"), ("B", "2")])

	def test_batches_without_sales_take_nothing(self):
		batches = [("A", *d("0", "5")), ("B", *d("2", "2"))]
		self.assertEqual(split("2", batches), [("B", "2")])

	def test_repeated_batch_entries_are_merged(self):
		batches = [("A", *d("1", "1")), ("B", *d("2", "2")), ("A", *d("1", "1"))]
		self.assertEqual(split("4", batches), [("A", "2"), ("B", "2")])

	def test_total_is_always_exact(self):
		batches = [("A", *d("7", "7")), ("B", *d("5", "5")), ("C", *d("3", "2"))]
		for qty in ("0.001", "1", "2.5", "7.777", "13", "14"):
			parts = split_return_qty(Decimal(qty), batches, STEP)
			self.assertEqual(sum(part for _b, part in parts), Decimal(qty), qty)
			for batch_no, part in parts:
				cap = {"A": 7, "B": 5, "C": 2}[batch_no]
				self.assertLessEqual(part, cap)
				self.assertEqual(part, floor_to_step(part, STEP))

	def test_floor_to_step(self):
		self.assertEqual(floor_to_step(Decimal("1.2349"), STEP), Decimal("1.234"))
		self.assertEqual(floor_to_step(Decimal("2.9"), Decimal(1)), Decimal(2))
		self.assertEqual(floor_to_step(0.5, Decimal("0.25")), Decimal("0.5"))


class TestStockCursor(unittest.TestCase):
	def test_round_trip_with_stock_position(self):
		cursor = Cursor(
			entity="batch",
			modified="2026-09-29 14:03:15.123456",
			name="PARA-B1",
			tomb_creation="2026-09-29 14:00:00.000000",
			tomb_name="abc",
			stock_modified="2026-09-29 14:05:00.000001",
			stock_name="SLE-0001",
		)
		self.assertEqual(decode_cursor(encode_cursor(cursor), "batch"), cursor)

	def test_other_entities_do_not_carry_stock_keys(self):
		token = encode_cursor(Cursor(entity="item", modified="2026-09-29 14:03:15.000000", name="A"))
		data = json.loads(base64.urlsafe_b64decode(token))
		self.assertNotIn("sm", data)
		self.assertNotIn("sn", data)
		self.assertIsNone(decode_cursor(token, "item").stock_modified)

	def test_stock_position_without_name_starts_at_the_timestamp(self):
		raw = base64.b64encode(json.dumps({"e": "batch", "sm": "2026-09-29 14:05:00"}).encode()).decode()
		cursor = decode_cursor(raw, "batch")
		self.assertEqual(cursor.stock_modified, "2026-09-29 14:05:00")
		self.assertEqual(cursor.stock_name, "")

	def test_rejects_bad_stock_position(self):
		for data in ({"sm": "yesterday", "sn": "X"}, {"sn": "X"}, {"sm": 5}):
			raw = base64.b64encode(json.dumps(data).encode()).decode()
			with self.assertRaises(InvalidCursor, msg=repr(data)):
				decode_cursor(raw, "batch")


class TestQtyPrecision(unittest.TestCase):
	def test_precision_of_the_sales_invoice_item_qty_field(self):
		with mock.patch("frappe.get_precision", return_value="4") as get_precision:
			self.assertEqual(qty_precision(), 4)
		get_precision.assert_called_once_with("Sales Invoice Item", "qty")


if __name__ == "__main__":
	unittest.main()
