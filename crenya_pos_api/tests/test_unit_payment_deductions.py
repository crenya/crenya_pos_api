"""A Payment Entry booking that carries deductions (a provider's commission), extra fields (the
service provider) and ticked credit notes (0.15.0, provider settlement spec PS2, 7.4), without a
site's ledger: ERPNext's reads and the document are faked, so what `post_payment_entry` writes is
compared field for field. A booking without the new fields writes exactly what 0.14.0 wrote."""

import contextlib
import unittest
from decimal import Decimal
from unittest import mock

import frappe

from crenya_pos_api.setup import install
from crenya_pos_api.sync import customer_payment as payments
from crenya_pos_api.sync.customer_payment import (
	EXTRA_FIELDS,
	PaymentBooking,
	deduction_total,
	post_payment_entry,
)
from crenya_pos_api.sync.errors import VALIDATION, SyncError

PARTY_ACCOUNT = "Debtors - MT"
CASH_ACCOUNT = "Bank - MT"
COMMISSION = "Service Provider Commission - MT"
COST_CENTER = "Main - MT"


class FakeDoc:
	"""What `post_payment_entry` does to a new Payment Entry: the fields it sets and the rows it
	appends, in order."""

	def __init__(self, doctype):
		self.doctype = doctype
		self.fields = {}
		self.rows = []
		self.flags = frappe._dict()
		self.name = "ACC-PAY-TEST-0001"
		self.inserted = self.submitted = False

	def update(self, values):
		self.fields.update(values)

	def append(self, table, row):
		self.rows.append((table, dict(row)))

	def insert(self, **_kwargs):
		self.inserted = True

	def submit(self):
		self.submitted = True


def booking(**over):
	values = {
		"company": "Muscat Test LLC",
		"customer": "Talabat Customer",
		"mode_of_payment": "Bank",
		"amount": Decimal("51.000"),
		"currency": "OMR",
		"posting_date": "2026-10-26",
		"local_id": "REQ-1",
		"reference": "TT-778",
		"remarks": "Provider settlement Talabat on app.crenya.com by owner@example.com",
		"invoices": ["O1", "O2"],
	}
	values.update(over)
	return PaymentBooking(**values)


def invoice(name, outstanding, total=None):
	return {
		"name": name,
		"total": Decimal(total or outstanding),
		"outstanding": Decimal(outstanding),
		"due_date": "2026-10-20",
	}


ACCOUNTS = {
	("Account", PARTY_ACCOUNT, "account_currency"): "OMR",
	("Account", COMMISSION, ("company", "is_group")): frappe._dict(company="Muscat Test LLC", is_group=0),
	("Cost Center", COST_CENTER, ("company", "is_group")): frappe._dict(
		company="Muscat Test LLC", is_group=0
	),
	("Company", "Muscat Test LLC", "cost_center"): COST_CENTER,
}


def fake_get_value(doctype, name, fieldname=None, *args, **kwargs):
	key = fieldname if isinstance(fieldname, str) else tuple(fieldname or ())
	return ACCOUNTS.get((doctype, name, key))


def post(data, candidates=None, returns=None, skipped=(), returns_skipped=()):
	"""(the fake Payment Entry, notes) of `post_payment_entry(data)` with the ticked bills
	`candidates` and the ticked credit notes `returns` as ERPNext would read them now."""
	doc = FakeDoc("Payment Entry")
	notes = []
	with (
		mock.patch.object(payments, "money_precision", return_value=3),
		mock.patch.object(payments, "mode_of_payment_account", return_value=CASH_ACCOUNT),
		mock.patch("erpnext.accounts.party.get_party_account", return_value=PARTY_ACCOUNT),
		mock.patch.object(payments.frappe.db, "get_value", side_effect=fake_get_value),
		mock.patch.object(payments.frappe, "get_cached_value", side_effect=fake_get_value),
		mock.patch.object(payments.frappe, "new_doc", return_value=doc),
		mock.patch.object(payments, "ticked_invoices", return_value=(list(candidates or []), list(skipped))),
		mock.patch.object(
			payments,
			"ticked_credit_notes",
			return_value=(list(returns or []), list(returns_skipped)),
			create=True,
		),
		mock.patch.object(payments, "open_invoices", return_value=list(candidates or [])),
		mock.patch.object(payments, "_as_administrator", contextlib.nullcontext),
		mock.patch.object(payments.frappe.db, "set_value"),
	):
		post_payment_entry(data, notes)
	return doc, notes


def refused(test, data, **kwargs):
	with test.assertRaises(SyncError) as caught:
		post(data, **kwargs)
	test.assertEqual(caught.exception.code, VALIDATION)
	return caught.exception.message


# what 0.14.0's `post_payment_entry` wrote for `booking()` over O1 6.300 and O2 6.300 with 51.000
# (captured from d9b0f0b): the settlement's fields must stay these, field for field
FIELDS_0_14_0 = {
	"payment_type": "Receive",
	"party_type": "Customer",
	"party": "Talabat Customer",
	"company": "Muscat Test LLC",
	"posting_date": "2026-10-26",
	"mode_of_payment": "Bank",
	"paid_from": PARTY_ACCOUNT,
	"paid_to": CASH_ACCOUNT,
	"paid_amount": 51.0,
	"received_amount": 51.0,
	"source_exchange_rate": 1,
	"target_exchange_rate": 1,
	"crenya_local_id": "REQ-1",
	"reference_no": "TT-778",
	"reference_date": "2026-10-26",
	"custom_remarks": 1,
	"remarks": "Provider settlement Talabat on app.crenya.com by owner@example.com",
}
ROWS_0_14_0 = [
	(
		"references",
		{
			"reference_doctype": "Sales Invoice",
			"reference_name": "O1",
			"total_amount": Decimal("6.300"),
			"outstanding_amount": Decimal("6.300"),
			"allocated_amount": 6.3,
			"due_date": "2026-10-20",
		},
	),
	(
		"references",
		{
			"reference_doctype": "Sales Invoice",
			"reference_name": "O2",
			"total_amount": Decimal("6.300"),
			"outstanding_amount": Decimal("6.300"),
			"allocated_amount": 6.3,
			"due_date": "2026-10-20",
		},
	),
]


class TestDeductionTotal(unittest.TestCase):
	def test_deduction_total_pure_inr(self):
		self.assertEqual(deduction_total(None, 2), Decimal("0.00"))
		self.assertEqual(deduction_total([], 2), Decimal("0.00"))
		rows = [
			{"account": "Commission - IN", "amount": Decimal("12.50")},
			{"account": "Bank Charges - IN", "amount": "0.05"},
			{"account": "Commission - IN", "amount": 3},
		]
		self.assertEqual(deduction_total(rows, 2), Decimal("15.55"))
		self.assertEqual(str(deduction_total(rows, 2)), "15.55")
		# OMR, three decimals: no float drift over many 0.001 steps
		self.assertEqual(deduction_total([{"amount": "0.001"}] * 1000, 3), Decimal("1.000"))


class TestBookingWithoutDeductions(unittest.TestCase):
	def test_booking_without_deductions_unchanged(self):
		"""Review Focus 5: a retail (or 0.14.0 web) booking writes exactly what 0.14.0 wrote, field
		for field, with the new fields left out or given as None."""
		candidates = [invoice("O1", "6.300"), invoice("O2", "6.300")]
		for data in (
			booking(amount=Decimal("51.000")),
			booking(amount=Decimal("51.000"), deductions=None, extra=None, credit_notes=None),
			booking(amount=Decimal("51.000"), deductions=[], extra={}, credit_notes=[]),
		):
			doc, notes = post(data, candidates)
			self.assertEqual(doc.fields, FIELDS_0_14_0)
			self.assertEqual(doc.rows, ROWS_0_14_0)
			self.assertEqual(notes, [])
			self.assertTrue(doc.inserted and doc.submitted)

	def test_the_three_new_fields_trail_with_none_defaults(self):
		self.assertEqual(PaymentBooking._fields[-3:], ("deductions", "extra", "credit_notes"))
		self.assertEqual(
			PaymentBooking._field_defaults, {"deductions": None, "extra": None, "credit_notes": None}
		)


class TestDeductions(unittest.TestCase):
	def commission(self, amount="12.000", **over):
		row = {
			"account": COMMISSION,
			"cost_center": COST_CENTER,
			"amount": Decimal(amount),
			"description": "Talabat commission",
		}
		row.update(over)
		return row

	def test_deduction_allocates_received_plus_commission(self):
		"""Golden spec_10_full: ten orders of 6.300, received 51.000, commission 12.000."""
		names = [f"O{n}" for n in range(1, 11)]
		data = booking(invoices=names, deductions=[self.commission()])
		doc, _notes = post(data, [invoice(name, "6.300") for name in names])
		self.assertEqual((doc.fields["paid_amount"], doc.fields["received_amount"]), (51.0, 51.0))
		references = [row for table, row in doc.rows if table == "references"]
		self.assertEqual([row["reference_name"] for row in references], names)
		self.assertEqual(sum(Decimal(str(row["allocated_amount"])) for row in references), Decimal("63.000"))
		deductions = [row for table, row in doc.rows if table == "deductions"]
		self.assertEqual(
			deductions,
			[
				{
					"account": COMMISSION,
					"cost_center": COST_CENTER,
					"amount": 12.0,
					"description": "Talabat commission",
				}
			],
		)
		# the deduction rows come after the references
		self.assertEqual([table for table, _row in doc.rows][-1], "deductions")

	def test_deduction_cost_center_defaults_to_the_company(self):
		data = booking(deductions=[{"account": COMMISSION, "amount": Decimal("1.000")}])
		doc, _notes = post(data, [invoice("O1", "6.300")])
		deductions = [row for table, row in doc.rows if table == "deductions"]
		self.assertEqual(
			deductions,
			[{"account": COMMISSION, "cost_center": COST_CENTER, "amount": 1.0, "description": None}],
		)

	def test_deduction_rounding_exact_omr(self):
		"""0.001 steps: 0.001 + 0.002 of commission on 0.003 received clears 0.006 exactly."""
		data = booking(
			amount=Decimal("0.003"),
			deductions=[self.commission("0.001"), self.commission("0.002")],
		)
		doc, _notes = post(data, [invoice("O1", "0.001"), invoice("O2", "0.002"), invoice("O3", "0.004")])
		references = [
			(row["reference_name"], row["allocated_amount"])
			for table, row in doc.rows
			if table == "references"
		]
		self.assertEqual(references, [("O1", 0.001), ("O2", 0.002), ("O3", 0.003)])
		self.assertEqual([row["amount"] for table, row in doc.rows if table == "deductions"], [0.001, 0.002])

	def test_a_deduction_is_checked(self):
		self.assertIn("decimals", refused(self, booking(deductions=[self.commission("1.0005")])))
		self.assertIn("more than 0", refused(self, booking(deductions=[self.commission("0.000")])))
		self.assertIn("more than 0", refused(self, booking(deductions=[self.commission("-1.000")])))
		self.assertIn(
			"Other - MT", refused(self, booking(deductions=[self.commission(account="Other - MT")]))
		)
		self.assertIn("CC - MT", refused(self, booking(deductions=[self.commission(cost_center="CC - MT")])))
		self.assertIn("account", refused(self, booking(deductions=[{"amount": Decimal("1.000")}])))
		self.assertIn("list", refused(self, booking(deductions={"account": COMMISSION})))


class TestExtra(unittest.TestCase):
	def test_extra_fields(self):
		self.assertEqual(EXTRA_FIELDS, frozenset({"crenya_service_provider"}))

	def test_extra_unknown_field_refused(self):
		for extra in ({"remarks": "x"}, {"crenya_service_provider": "Talabat", "paid_amount": 1}, ["x"]):
			self.assertTrue(refused(self, booking(extra=extra)))

	def test_extra_sets_provider_field(self):
		doc, _notes = post(booking(extra={"crenya_service_provider": "Talabat"}), [invoice("O1", "6.300")])
		self.assertEqual(doc.fields["crenya_service_provider"], "Talabat")
		self.assertEqual(set(doc.fields) - set(FIELDS_0_14_0), {"crenya_service_provider"})


class TestCreditNotes(unittest.TestCase):
	def test_credit_notes_allocated_negative_first(self):
		"""A refund of 6.300 ticked with its order: its reference is allocated -6.300 first, and the
		pot over the orders grows by 6.300."""
		data = booking(
			amount=Decimal("5.040"),
			invoices=["O1", "O2"],
			credit_notes=["R1"],
			deductions=[{"account": COMMISSION, "cost_center": COST_CENTER, "amount": Decimal("1.260")}],
		)
		doc, _notes = post(
			data,
			[invoice("O1", "6.300"), invoice("O2", "6.300")],
			returns=[invoice("R1", "-6.300")],
		)
		references = [
			(row["reference_name"], row["allocated_amount"])
			for table, row in doc.rows
			if table == "references"
		]
		self.assertEqual(references, [("R1", -6.3), ("O1", 6.3), ("O2", 6.3)])
		self.assertEqual(doc.fields["paid_amount"], 5.04)

	def test_skipped_credit_notes_join_the_skipped(self):
		data = booking(credit_notes=["R9"])
		doc, _notes = post(data, [invoice("O1", "6.300")], returns_skipped=["R9"])
		self.assertTrue(doc.fields["remarks"].endswith("; skipped ticked invoices: R9"))
		self.assertEqual(doc.flags.crenya_skipped, ["R9"])

	def test_refunds_more_than_the_orders_take_refused(self):
		data = booking(amount=Decimal("1.000"), invoices=["O1"], credit_notes=["R1"])
		message = refused(self, data, candidates=[invoice("O1", "2.000")], returns=[invoice("R1", "-6.300")])
		self.assertIn("6.300", message)


class TestProviderField(unittest.TestCase):
	def field(self):
		(row,) = [
			f for f in install.CUSTOM_FIELDS["Payment Entry"] if f["fieldname"] == "crenya_service_provider"
		]
		return row

	def test_provider_field_is_a_read_only_link(self):
		row = self.field()
		self.assertEqual(
			(row["fieldtype"], row["options"], row["read_only"], row["insert_after"]),
			("Link", "Crenya Service Provider", 1, "crenya_local_id"),
		)

	def test_link_kept_when_the_bench_has_the_doctype(self):
		fields = install.bench_custom_fields(install.CUSTOM_FIELDS, lambda doctype: True)
		(row,) = [f for f in fields["Payment Entry"] if f["fieldname"] == "crenya_service_provider"]
		self.assertEqual((row["fieldtype"], row["options"]), ("Link", "Crenya Service Provider"))
		self.assertEqual(fields, install.CUSTOM_FIELDS)

	def test_data_when_the_bench_lacks_the_doctype(self):
		"""A retail bench (no crenya_restaurant): the field is Data, everything else unchanged."""
		missing = {"Crenya Service Provider"}
		fields = install.bench_custom_fields(install.CUSTOM_FIELDS, lambda doctype: doctype not in missing)
		(row,) = [f for f in fields["Payment Entry"] if f["fieldname"] == "crenya_service_provider"]
		self.assertEqual((row["fieldtype"], row["options"]), ("Data", ""))
		self.assertEqual(
			{k: v for k, v in row.items() if k not in ("fieldtype", "options")},
			{k: v for k, v in self.field().items() if k not in ("fieldtype", "options")},
		)
		# the table itself is not changed, and no other field moved
		self.assertEqual(self.field()["fieldtype"], "Link")
		others = {
			dt: [f for f in rows if f["fieldname"] != "crenya_service_provider"]
			for dt, rows in fields.items()
		}
		expected = {
			dt: [f for f in rows if f["fieldname"] != "crenya_service_provider"]
			for dt, rows in install.CUSTOM_FIELDS.items()
		}
		self.assertEqual(others, expected)
