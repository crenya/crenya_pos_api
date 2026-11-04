"""H2 (retail P1 slice 1 review, 0.16.3): `invoice_builder.reconcile_payments` on a sale partly on
account or a return partly credited to the account. A till/server difference within tolerance (10
minor units) is absorbed into the account part, with a note, the payment rows staying what the drawer
took; beyond it the invoice is refused. Pure: the doc is a stand-in and the currency's precision and
tolerance are patched (no site)."""

import unittest
from decimal import Decimal
from unittest import mock

import frappe

from crenya_pos_api.sync import invoice_builder
from crenya_pos_api.sync.errors import TOTAL_MISMATCH, SyncError
from crenya_pos_api.utils.decimal import smallest_unit_tolerance


class Doc:
	"""The part of a Sales Invoice `reconcile_payments` reads and writes."""

	def __init__(self, payable, payments, currency="OMR"):
		self.currency = currency
		self.grand_total = float(payable)
		self.rounded_total = float(payable)
		self.payments = [
			frappe._dict(
				mode_of_payment=mode, amount=float(amount), default=0, type="Cash", account="Cash - T"
			)
			for mode, amount in payments
		]

	def get(self, key, default=None):
		return getattr(self, key, default)

	def is_rounded_total_disabled(self):
		return True

	def remove(self, row):
		self.payments.remove(row)

	def append(self, key, row):
		row = frappe._dict(row)
		self.payments.append(row)
		return row


def plain_flt(value, precision=None):
	"""frappe's `flt` without a site (its rounding method is a System Setting)."""
	value = float(value or 0)
	return value if precision is None else round(value, precision)


def reconcile(doc, precision=3, **parts):
	notes = []
	with (
		mock.patch.object(invoice_builder, "flt", plain_flt),
		mock.patch.object(invoice_builder, "money_precision", return_value=precision),
		mock.patch.object(
			invoice_builder, "get_total_tolerance", side_effect=lambda p: smallest_unit_tolerance(p, 10)
		),
	):
		answer = invoice_builder.reconcile_payments(doc, "Cash", notes, **parts)
	return answer, notes


def amounts(doc):
	return [round(row.amount, 3) for row in doc.payments]


class TestOnAccountTolerance(unittest.TestCase):
	"""A sale of 100.000 (OMR) with 40.000 in cash, the rest on account."""

	def test_exact_split_is_unchanged(self):
		doc = Doc("100.000", [("Cash", "40.000")])
		(on_account, credit), notes = reconcile(doc, on_account=Decimal("60.000"))
		self.assertEqual((on_account, credit), (Decimal("60.000"), Decimal(0)))
		self.assertEqual((amounts(doc), notes), ([40.0], []))

	def test_one_and_ten_minor_units_go_to_the_account_part(self):
		for till, posted in (
			("60.001", "60.000"),
			("59.999", "60.000"),
			("60.010", "60.000"),
			("59.990", "60.000"),
		):
			doc = Doc("100.000", [("Cash", "40.000")])
			(on_account, _credit), notes = reconcile(doc, on_account=Decimal(till))
			self.assertEqual(on_account, Decimal(posted), msg=till)
			# the drawer's cash is untouched
			self.assertEqual(amounts(doc), [40.0], msg=till)
			self.assertEqual(len(notes), 1)
			self.assertIn("absorbed into the on-account part", notes[0])
			self.assertIn(f"({till} -> {posted})", notes[0])

	def test_eleven_minor_units_are_refused(self):
		for till in ("60.011", "59.989"):
			doc = Doc("100.000", [("Cash", "40.000")])
			with self.assertRaises(SyncError) as caught:
				reconcile(doc, on_account=Decimal(till))
			self.assertEqual(caught.exception.code, TOTAL_MISMATCH)
			self.assertIn(f"{till} on account", caught.exception.message)

	def test_wholly_on_account(self):
		doc = Doc("100.000", [])
		(on_account, _credit), notes = reconcile(doc, on_account=Decimal("100.001"))
		self.assertEqual(on_account, Decimal("100.000"))
		self.assertEqual(doc.payments, [])
		self.assertIn("(100.001 -> 100.000)", notes[0])

	def test_a_part_that_would_go_negative_leaves_it_to_the_cash_row(self):
		doc = Doc("99.995", [("Cash", "100.000")])
		(on_account, _credit), notes = reconcile(doc, on_account=Decimal("0.003"))
		self.assertEqual(on_account, Decimal("0.003"))
		self.assertEqual(amounts(doc), [99.992])
		self.assertIn("absorbed into Cash", notes[0])

	def test_two_decimals(self):
		doc = Doc("100.00", [("Cash", "40.00")], currency="INR")
		(on_account, _credit), notes = reconcile(doc, precision=2, on_account=Decimal("60.10"))
		self.assertEqual(on_account, Decimal("60.00"))
		self.assertIn("(60.10 -> 60.00)", notes[0])
		with self.assertRaises(SyncError):
			reconcile(Doc("100.00", [("Cash", "40.00")], "INR"), precision=2, on_account=Decimal("60.11"))


class TestCreditOffsetTolerance(unittest.TestCase):
	"""A return of 50.000 (OMR, -50.000 payable), 20.000 refunded in cash, the rest credited."""

	def test_exact_split_is_unchanged(self):
		doc = Doc("-50.000", [("Cash", "-20.000")])
		(on_account, credit), notes = reconcile(doc, credit_offset=Decimal("30.000"))
		self.assertEqual((on_account, credit), (Decimal(0), Decimal("30.000")))
		self.assertEqual((amounts(doc), notes), ([-20.0], []))

	def test_one_and_ten_minor_units_go_to_the_credit(self):
		for till in ("30.001", "29.999", "30.010", "29.990"):
			doc = Doc("-50.000", [("Cash", "-20.000")])
			(_on_account, credit), notes = reconcile(doc, credit_offset=Decimal(till))
			self.assertEqual(credit, Decimal("30.000"), msg=till)
			self.assertEqual(amounts(doc), [-20.0], msg=till)
			self.assertIn("absorbed into the credit to the account", notes[0])
			self.assertIn(f"({till} -> 30.000)", notes[0])

	def test_a_whole_credit_at_the_tills_total(self):
		doc = Doc("-50.000", [])
		(_on_account, credit), _notes = reconcile(doc, credit_offset=Decimal("50.001"))
		self.assertEqual(credit, Decimal("50.000"))
		self.assertEqual(doc.payments, [])

	def test_a_credit_that_would_go_negative_leaves_it_to_the_cash_row(self):
		"""Within tolerance as on a plain return: the refund row takes it, the credit stays >= 0."""
		doc = Doc("-50.000", [("Cash", "-50.001")])
		(_on_account, credit), notes = reconcile(doc, credit_offset=Decimal("0.002"))
		self.assertEqual(credit, Decimal("0.002"))
		self.assertEqual(amounts(doc), [-49.998])
		self.assertIn("absorbed into Cash", notes[0])

	def test_eleven_minor_units_are_refused(self):
		for till in ("30.011", "29.989"):
			with self.assertRaises(SyncError) as caught:
				reconcile(Doc("-50.000", [("Cash", "-20.000")]), credit_offset=Decimal(till))
			self.assertEqual(caught.exception.code, TOTAL_MISMATCH)
			self.assertIn("credited to the account", caught.exception.message)


class TestCashTolerance(unittest.TestCase):
	def test_a_cash_sale_still_absorbs_into_the_cash_row(self):
		doc = Doc("100.000", [("Cash", "99.999")])
		(on_account, credit), notes = reconcile(doc)
		self.assertEqual((on_account, credit), (Decimal(0), Decimal(0)))
		self.assertEqual(amounts(doc), [100.0])
		self.assertIn("absorbed into Cash", notes[0])


if __name__ == "__main__":
	unittest.main()
