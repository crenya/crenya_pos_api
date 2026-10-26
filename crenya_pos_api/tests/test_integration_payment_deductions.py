"""A Payment Entry booking with a deduction (a provider's commission), the service provider field
and ticked credit notes (0.15.0, provider settlement spec PS2, 7.4) on an ERPNext site: the
received amount plus the commission is allocated over the ticked orders, the ledger balances
exactly (Dr bank received, Dr commission expense, Cr the party received + commission), a refund
ticked with its order is allocated negative first, and a booking without the new fields is the
0.14.0 entry.

The tests make their own customer ("a provider's customer"), commission account and provider; they
never use another app's fixtures.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_payment_deductions
"""

import uuid
from decimal import Decimal

import frappe
from frappe.utils import nowdate

from crenya_pos_api.sync.customer_payment import PaymentBooking, post_payment_entry
from crenya_pos_api.sync.errors import VALIDATION, SyncError
from crenya_pos_api.sync.invoice_builder import mode_of_payment_account
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import money, new_id
from crenya_pos_api.tests.test_integration_credit import make_customer
from crenya_pos_api.tests.test_integration_customer_payment import PaymentCase, outstanding

PAYMENT_ENTRY = "Payment Entry"
PROVIDER_DOCTYPE = "Crenya Service Provider"
COMMISSION_ACCOUNT = f"_Test Crenya Service Provider Commission - {fixtures.ABBR}"


def ensure_commission_account():
	if not frappe.db.exists("Account", COMMISSION_ACCOUNT):
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "_Test Crenya Service Provider Commission",
				"parent_account": f"Indirect Expenses - {fixtures.ABBR}",
				"company": fixtures.COMPANY,
				"account_currency": fixtures.CURRENCY,
				"account_type": "Expense Account",
			}
		).insert(ignore_permissions=True)
	return COMMISSION_ACCOUNT


def D(value):
	return Decimal(money(value))


class DeductionCase(PaymentCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_commission_account()
		frappe.db.commit()  # nosemgrep

	def provider_customer(self, label="talabat"):
		return make_customer(f"provider {label}", limit=None)

	def orders(self, customer, *totals):
		return [self.sell(customer, total) for total in totals]

	def commission(self, amount):
		return {
			"account": COMMISSION_ACCOUNT,
			"cost_center": fixtures.COST_CENTER,
			"amount": Decimal(amount),
			"description": "Provider commission",
		}

	def book(self, customer, amount, invoices=None, **over):
		booking = PaymentBooking(
			company=fixtures.COMPANY,
			customer=customer,
			mode_of_payment=fixtures.CASH,
			amount=Decimal(amount),
			currency=fixtures.CURRENCY,
			posting_date=nowdate(),
			local_id=new_id(),
			reference="TT-1",
			remarks="Provider settlement on app.crenya.com by Administrator",
			invoices=invoices,
			**over,
		)
		return post_payment_entry(booking, [])

	def gl(self, name):
		"""{(account, party): debit - credit} of a voucher's live GL rows."""
		rows = frappe.get_all(
			"GL Entry",
			filters={"voucher_type": PAYMENT_ENTRY, "voucher_no": name, "is_cancelled": 0},
			fields=["account", "party", "debit", "credit"],
		)
		balances = {}
		for row in rows:
			key = (row.account, row.party or None)
			balances[key] = balances.get(key, Decimal(0)) + D(row.debit) - D(row.credit)
		return balances

	def references(self, doc):
		return [(row.reference_name, D(row.allocated_amount)) for row in doc.references]


class TestDeductionBooking(DeductionCase):
	def test_booking_without_deductions_unchanged(self):
		"""A booking without the new fields books the 0.14.0 entry: no deduction, no provider, the
		paid amount allocated, nothing left over."""
		customer = self.provider_customer("plain")
		first, second = self.orders(customer, "6.300", "6.300")
		doc = self.book(customer, "10.000", [first, second])
		self.assertEqual(doc.deductions, [])
		self.assertFalse(doc.get("crenya_service_provider"))
		self.assertEqual(self.references(doc), [(first, D("6.300")), (second, D("3.700"))])
		self.assertEqual(
			(D(doc.paid_amount), D(doc.unallocated_amount), D(doc.difference_amount)),
			(D("10"), D("0"), D("0")),
		)
		explicit = self.book(customer, "2.600", [second], deductions=None, extra=None, credit_notes=None)
		self.assertEqual(self.references(explicit), [(second, D("2.600"))])
		self.assertEqual(explicit.deductions, [])

	def test_deduction_allocates_received_plus_commission(self):
		"""Golden spec_10_full: ten orders of 6.300, received 51.000, commission 12.000."""
		customer = self.provider_customer()
		names = self.orders(customer, *(["6.300"] * 10))
		doc = self.book(customer, "51.000", names, deductions=[self.commission("12.000")])
		doc.reload()
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual((D(doc.paid_amount), D(doc.received_amount)), (D("51.000"), D("51.000")))
		self.assertEqual(
			[(row.account, row.cost_center, D(row.amount), row.description) for row in doc.deductions],
			[(COMMISSION_ACCOUNT, fixtures.COST_CENTER, D("12.000"), "Provider commission")],
		)
		self.assertEqual(self.references(doc), [(name, D("6.300")) for name in names])
		self.assertEqual(D(doc.total_allocated_amount), D("63.000"))
		self.assertEqual((D(doc.unallocated_amount), D(doc.difference_amount)), (D("0"), D("0")))
		self.assertEqual({outstanding(name) for name in names}, {D("0")})

	def test_gl_bank_received_expense_deduction(self):
		customer = self.provider_customer("gl")
		names = self.orders(customer, *(["6.300"] * 10))
		doc = self.book(customer, "51.000", names, deductions=[self.commission("12.000")])
		receivable = doc.paid_from
		bank = mode_of_payment_account(fixtures.CASH, fixtures.COMPANY)
		self.assertEqual(
			self.gl(doc.name),
			{
				(bank, None): D("51.000"),
				(COMMISSION_ACCOUNT, None): D("12.000"),
				(receivable, customer): D("-63.000"),
			},
		)
		self.assertEqual(sum(self.gl(doc.name).values()), D("0"))
		cost_centers = frappe.get_all(
			"GL Entry",
			filters={"voucher_no": doc.name, "account": COMMISSION_ACCOUNT, "is_cancelled": 0},
			pluck="cost_center",
		)
		self.assertEqual(cost_centers, [fixtures.COST_CENTER])

	def test_deduction_rounding_exact_omr(self):
		"""0.001 steps: whatever the orders' totals, received = their outstanding - 0.001 with a
		0.001 commission clears them exactly; 0.001 more received is an advance of 0.001."""
		customer = self.provider_customer("round")
		names = self.orders(customer, "3.334", "1.112", "0.006")
		total = sum(outstanding(name) for name in names)
		doc = self.book(customer, str(total - D("0.001")), names, deductions=[self.commission("0.001")])
		self.assertEqual((D(doc.unallocated_amount), D(doc.difference_amount)), (D("0"), D("0")))
		self.assertEqual({outstanding(name) for name in names}, {D("0")})
		more = self.orders(customer, "2.222")
		doc = self.book(customer, "2.222", more, deductions=[self.commission("0.001")])
		self.assertEqual(self.references(doc), [(more[0], D("2.222"))])
		self.assertEqual((D(doc.unallocated_amount), D(doc.difference_amount)), (D("0.001"), D("0")))
		self.assertEqual(sum(self.gl(doc.name).values()), D("0"))

	def test_extra_unknown_field_refused(self):
		customer = self.provider_customer("extra")
		(order,) = self.orders(customer, "6.300")
		before = frappe.db.count(PAYMENT_ENTRY, {"party": customer})
		with self.assertRaises(SyncError) as caught:
			self.book(customer, "6.300", [order], extra={"paid_amount": 1})
		self.assertEqual(caught.exception.code, VALIDATION)
		self.assertEqual(frappe.db.count(PAYMENT_ENTRY, {"party": customer}), before)
		self.assertEqual(outstanding(order), D("6.300"))

	def test_extra_sets_provider_field(self):
		meta = frappe.get_meta(PAYMENT_ENTRY).get_field("crenya_service_provider")
		self.assertTrue(meta.read_only)
		if frappe.db.exists("DocType", PROVIDER_DOCTYPE):
			self.assertEqual((meta.fieldtype, meta.options), ("Link", PROVIDER_DOCTYPE))
			provider = frappe.get_doc(
				{
					"doctype": PROVIDER_DOCTYPE,
					"provider_name": f"_Test Crenya Provider {uuid.uuid4().hex[:6]}",
					"company": fixtures.COMPANY,
					"commission_type": "Percentage",
				}
			).insert(ignore_permissions=True)
			value = provider.name
		else:
			self.assertEqual(meta.fieldtype, "Data")
			value = "Talabat"
		customer = self.provider_customer("provider")
		(order,) = self.orders(customer, "6.300")
		doc = self.book(
			customer,
			"5.040",
			[order],
			deductions=[self.commission("1.260")],
			extra={"crenya_service_provider": value},
		)
		self.assertEqual(frappe.db.get_value(PAYMENT_ENTRY, doc.name, "crenya_service_provider"), value)
		self.assertEqual(outstanding(order), D("0"))


class TestCreditNotes(DeductionCase):
	def refund(self, invoice):
		"""A full return of `invoice` that keeps its own negative outstanding (shape A, spec R1)."""
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

		doc = make_sales_return(invoice)
		doc.update_outstanding_for_self = 1
		doc.insert(ignore_permissions=True)
		doc.submit()
		return doc.name

	def test_credit_notes_allocated_negative_first(self):
		customer = self.provider_customer("refund")
		first, second = self.orders(customer, "6.300", "6.300")
		refund = self.refund(first)
		self.assertEqual((outstanding(refund), outstanding(first)), (D("-6.300"), D("6.300")))
		doc = self.book(
			customer, "5.040", [first, second], credit_notes=[refund], deductions=[self.commission("1.260")]
		)
		self.assertEqual(
			self.references(doc), [(refund, D("-6.300")), (first, D("6.300")), (second, D("6.300"))]
		)
		self.assertEqual((D(doc.unallocated_amount), D(doc.difference_amount)), (D("0"), D("0")))
		self.assertEqual(
			(outstanding(refund), outstanding(first), outstanding(second)), (D("0"), D("0"), D("0"))
		)
		self.assertEqual(sum(self.gl(doc.name).values()), D("0"))
		self.assertEqual(self.gl(doc.name)[(doc.paid_from, customer)], D("-6.300"))

	def test_a_credit_note_not_a_return_of_the_customer_is_skipped(self):
		customer = self.provider_customer("refund-skip")
		other = self.provider_customer("refund-other")
		(order,) = self.orders(customer, "6.300")
		(foreign,) = self.orders(other, "6.300")
		foreign_refund = self.refund(foreign)
		doc = self.book(customer, "6.300", [order], credit_notes=[foreign_refund, order, "NO-SUCH"])
		self.assertEqual(self.references(doc), [(order, D("6.300"))])
		self.assertEqual(doc.flags.crenya_skipped, [foreign_refund, order, "NO-SUCH"])
		self.assertEqual(outstanding(foreign_refund), D("-6.300"))

	def test_refunds_more_than_the_orders_take_refused(self):
		customer = self.provider_customer("refund-over")
		first, second = self.orders(customer, "6.300", "1.000")
		refund = self.refund(first)
		with self.assertRaises(SyncError) as caught:
			self.book(customer, "0.500", [second], credit_notes=[refund])
		self.assertEqual(caught.exception.code, VALIDATION)
		self.assertEqual(outstanding(refund), D("-6.300"))
