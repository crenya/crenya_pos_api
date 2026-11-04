"""The Shops Reports tab and the Z additions (0.17.0), the pure row builders (no site): items netted
with returns, cashiers with their refunds, discounts, voids and no-sales, the shift's discounts with
their approvers and its refunds, and the control events' labels. OMR, 3 decimals."""

import unittest
from datetime import datetime
from decimal import Decimal

from crenya_pos_api.api.web_admin.day_close import CONTROL_EVENT_KEYS, control_event_rows
from crenya_pos_api.web.sales_reports import cashier_rows, item_rows
from crenya_pos_api.web.shift_audit import MAX_ROWS, discount_rows, refund_rows


class TestItemRows(unittest.TestCase):
	def test_item_sums_formatted_at_3_decimals(self):
		# the database's sums (Decimals from MariaDB, floats on some drivers): returns already netted
		raw = [
			{
				"item_code": "BREAD",
				"item_name": "Bread",
				"item_group": "Bakery",
				"qty": Decimal("4.000"),
				"amount": Decimal("1.6"),
				"discounts": Decimal("0.4"),
			},
			{
				"item_code": "MILK",
				"item_name": None,
				"item_group": "Dairy",
				"qty": 2.0,
				"amount": 1.2,
				"discounts": 0,
			},
		]
		rows = item_rows(raw, 3)
		self.assertEqual(
			rows[0],
			{
				"item_code": "BREAD",
				"item_name": "Bread",
				"item_group": "Bakery",
				"qty": "4",
				"amount": "1.600",
				"discounts": "0.400",
			},
		)
		self.assertEqual(
			(rows[1]["item_name"], rows[1]["qty"], rows[1]["amount"], rows[1]["discounts"]),
			("MILK", "2", "1.200", "0.000"),
		)


class TestCashierRows(unittest.TestCase):
	def test_cashiers_with_refunds_discounts_and_controls(self):
		raw = [
			{
				"cashier": "a@x",
				"bills": 2,
				"refunds_count": 1,
				"refunds": Decimal("2"),
				"sales": Decimal("15.5"),
				"discounts": 1.0,
			}
		]
		controls = {
			"a@x": {"line_voids_count": 2, "no_sales_count": 1},
			"b@x": {"line_voids_count": 1, "no_sales_count": 0},
		}
		rows = cashier_rows(raw, controls, {"a@x": "Aisha", "b@x": "Badr"}, 3)
		self.assertEqual(
			rows[0],
			{
				"cashier": "a@x",
				"cashier_name": "Aisha",
				"bills": 2,
				"refunds_count": 1,
				"refunds": "2.000",
				"sales": "15.500",
				"net_total": "13.500",
				"discounts": "1.000",
				"line_voids_count": 2,
				"no_sales_count": 1,
			},
		)
		# a cashier with only voids is listed
		self.assertEqual(
			(rows[1]["cashier_name"], rows[1]["bills"], rows[1]["line_voids_count"]), ("Badr", 0, 1)
		)


class TestShiftAudit(unittest.TestCase):
	def test_discounts_from_invoices_with_approver(self):
		# OMR: invoice discount 1.000 + a line discount 0.500 + a price override 2.000 from list 2.500
		invoices = [{"name": "SINV-1", "discount_amount": 1.0}, {"name": "SINV-2", "discount_amount": 0}]
		items = [
			{"parent": "SINV-1", "item_code": "MILK", "qty": 1, "price_list_rate": 1.0, "rate": 0.5},
			{"parent": "SINV-2", "item_code": "CHEESE", "qty": 1, "price_list_rate": 2.5, "rate": 2.0},
			{"parent": "SINV-2", "item_code": "BREAD", "qty": 2, "price_list_rate": 0.5, "rate": 0.5},
		]
		events = [
			{
				"event_type": "approval_used",
				"action": "price_change",
				"item_code": "CHEESE",
				"approver_name": "Manager M",
			}
		]
		block = discount_rows(invoices, items, 3, events)
		self.assertEqual((block["count"], block["amount"]), (3, "2.000"))
		override = block["rows"][2]
		self.assertEqual(
			override,
			{
				"invoice": "SINV-2",
				"item_code": "CHEESE",
				"qty": "1",
				"list_rate": "2.500",
				"rate": "2.000",
				"discount": "0.500",
				"approver": "Manager M",
			},
		)
		self.assertIsNone(block["rows"][0]["item_code"])
		self.assertIsNone(block["rows"][1]["approver"])

	def test_rows_capped_count_is_all(self):
		invoices = [{"name": f"S{i}", "discount_amount": 0.1} for i in range(MAX_ROWS + 5)]
		block = discount_rows(invoices, [], 3)
		self.assertEqual((block["count"], len(block["rows"])), (MAX_ROWS + 5, MAX_ROWS))

	def test_refunds_positive(self):
		block = refund_rows(
			[{"name": "RET-1", "return_against": "SINV-1", "grand_total": -2.0, "cashier": "a@x"}],
			{"a@x": "Aisha"},
			3,
		)
		self.assertEqual(
			block,
			{
				"count": 1,
				"amount": "2.000",
				"rows": [
					{
						"invoice": "RET-1",
						"return_against": "SINV-1",
						"amount": "2.000",
						"cashier_name": "Aisha",
					}
				],
			},
		)


class TestControlEventRows(unittest.TestCase):
	def test_labels_names_and_unknown_type(self):
		events = [
			{
				"event_type": "line_voided",
				"at": datetime(2026, 11, 4, 8, 0, 1),
				"actor": "a@x",
				"approver": "m@x",
				"amount": 1.25,
				"item_code": "MILK",
				"subject": None,
				"outside_shift": False,
				"action": "line_void",
			},
			{
				"event_type": "drawer_kicked",
				"at": datetime(2026, 11, 4, 8, 0, 2),
				"actor": "gone@x",
				"approver": None,
				"amount": None,
				"item_code": None,
				"subject": "x",
				"outside_shift": True,
				"action": None,
			},
		]
		rows = control_event_rows(events, {"a@x": "Aisha", "m@x": "Manager M"}, 3)
		first = {key: rows[0][key] for key in CONTROL_EVENT_KEYS}
		self.assertEqual(
			first,
			{
				"at": "2026-11-04 08:00:01",
				"event_type": "line_voided",
				"label": "Line removed",
				"label_ar": "حذف صنف",
				"actor_name": "Aisha",
				"approver_name": "Manager M",
				"amount": "1.250",
				"item_code": "MILK",
				"subject": None,
				"outside_shift": False,
			},
		)
		self.assertEqual(
			(rows[1]["label"], rows[1]["label_ar"], rows[1]["actor_name"], rows[1]["amount"]),
			("drawer_kicked", None, "gone@x", None),
		)


if __name__ == "__main__":
	unittest.main()
