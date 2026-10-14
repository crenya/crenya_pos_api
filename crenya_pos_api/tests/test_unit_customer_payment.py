"""A customer's payment taken at the till (`Customer Payment`, retail P1 task 8): the payload check
and the oldest-first allocation, without a site."""

import unittest
from decimal import Decimal

from crenya_pos_api.sync.customer_payment import (
	AGGREGATE_CUSTOMER_PAYMENT,
	CustomerPaymentHandler,
	allocate,
	validate_customer_payment_payload,
)
from crenya_pos_api.sync.errors import VALIDATION, SyncError


def payload(**over):
	p = {
		"local_id": "PAY-1",
		"pos_profile": "Shop",
		"company": "Company",
		"shift_local_id": "SH-1",
		"business_date": "2026-10-14",
		"cashier": "cashier@example.com",
		"customer_local_id": "CUST-L1",
		"customer": None,
		"mode_of_payment": "Cash",
		"amount": "40.000",
		"currency": "OMR",
		"reference": None,
		"note": None,
		"received_at": "2026-10-14T08:00:00Z",
	}
	p.update(over)
	return p


def refused(test, **over):
	with test.assertRaises(SyncError) as caught:
		validate_customer_payment_payload(payload(**over))
	test.assertEqual(caught.exception.code, VALIDATION)
	return caught.exception.message


class TestPayload(unittest.TestCase):
	def test_a_payment_is_read_with_its_amount_as_a_decimal(self):
		data = validate_customer_payment_payload(payload())
		self.assertEqual(data["amount"], Decimal("40.000"))
		self.assertEqual(data["customer_local_id"], "CUST-L1")
		self.assertEqual(data["business_date"], "2026-10-14")
		self.assertEqual(data["received_at"].isoformat(), "2026-10-14T08:00:00+00:00")

	def test_optional_keys_may_be_absent(self):
		minimal = {
			"local_id": "PAY-2",
			"business_date": "2026-10-14",
			"customer": "CUST-0001",
			"mode_of_payment": "Cash",
			"amount": "1.250",
			"currency": "OMR",
		}
		data = validate_customer_payment_payload(minimal)
		self.assertIsNone(data["reference"])
		self.assertIsNone(data["received_at"])
		self.assertEqual(data["customer"], "CUST-0001")

	def test_what_is_refused(self):
		self.assertIn("amount", refused(self, amount="0.000"))
		self.assertIn("amount", refused(self, amount="-5.000"))
		# money is a decimal string, never a float
		self.assertIn("amount", refused(self, amount=40.5))
		self.assertIn("customer", refused(self, customer_local_id=None, customer=None))
		self.assertIn("business_date", refused(self, business_date="14/10/2026"))
		self.assertIn("received_at", refused(self, received_at="yesterday"))
		self.assertIn("mode_of_payment", refused(self, mode_of_payment=None))
		self.assertIn("local_id", refused(self, local_id=""))

	def test_the_handler_is_a_built_in_submit_only_payment_entry_aggregate(self):
		handler = CustomerPaymentHandler()
		self.assertEqual(handler.aggregate_type, AGGREGATE_CUSTOMER_PAYMENT)
		self.assertEqual(handler.operations, ("submit",))
		self.assertEqual(handler.result_doctype, "Payment Entry")
		self.assertEqual(handler.local_id_field, "crenya_local_id")
		# an empty id matches nothing: entries not made at a till keep NULL, never ""
		self.assertIsNone(handler.find_by_local_id(""))
		self.assertIsNone(handler.find_by_local_id(None))


def debts(*amounts):
	return [
		{"name": f"INV-{index}", "total": amount, "outstanding": amount, "due_date": None}
		for index, amount in enumerate(amounts, start=1)
	]


class TestAllocate(unittest.TestCase):
	def test_oldest_first_up_to_each_outstanding_amount(self):
		rows, left = allocate("60.000", debts("30.000", "50.000", "40.000"), 3)
		self.assertEqual(
			[(row["name"], row["allocated"]) for row in rows],
			[("INV-1", Decimal("30.000")), ("INV-2", Decimal("30.000"))],
		)
		self.assertEqual(left, Decimal("0.000"))

	def test_more_money_than_debt_leaves_the_rest_unallocated(self):
		rows, left = allocate("40.000", debts("25.000"), 3)
		self.assertEqual([row["allocated"] for row in rows], [Decimal("25.000")])
		self.assertEqual(left, Decimal("15.000"))

	def test_no_debt_is_all_advance(self):
		rows, left = allocate("12.500", [], 3)
		self.assertEqual((rows, left), ([], Decimal("12.500")))

	def test_exact_in_omr_with_no_float_error(self):
		rows, left = allocate("0.300", debts("0.100", "0.200", "0.001"), 3)
		self.assertEqual([row["allocated"] for row in rows], [Decimal("0.100"), Decimal("0.200")])
		self.assertEqual(left, Decimal("0.000"))
		rows, left = allocate("0.301", debts("0.100", "0.200", "0.001"), 3)
		self.assertEqual(sum(row["allocated"] for row in rows), Decimal("0.301"))
		self.assertEqual(left, Decimal("0.000"))

	def test_an_invoice_with_nothing_outstanding_is_skipped(self):
		rows, left = allocate("10.000", debts("0.000", "4.000"), 3)
		self.assertEqual([row["name"] for row in rows], ["INV-2"])
		self.assertEqual(left, Decimal("6.000"))


if __name__ == "__main__":
	unittest.main()
