"""`api.credit.get_open_invoices` and the `settlements` of `get_invoice_outstanding` (restaurant
credit, 0.14.0, Task 3) on an ERPNext site: a customer's open bills of the company oldest first, each
naming its outlet, the advance on the account, the 200 / 1000 row limits, the device and outlet gates,
OMR amounts, and how a bill was settled (a till's cash receipt, a web bank settlement, a desk entry).

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_open_invoices
"""

from decimal import Decimal
from unittest import mock

import frappe
from frappe.utils import nowdate

from crenya_pos_api.api import credit as credit_api
from crenya_pos_api.api import device as device_api
from crenya_pos_api.sync import credit as credit_service
from crenya_pos_api.sync.customer_payment import PaymentBooking, post_payment_entry
from crenya_pos_api.sync.errors import InvalidRequestError
from crenya_pos_api.tests import extension_dummy as dummy
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import new_id
from crenya_pos_api.tests.test_integration_credit import make_customer
from crenya_pos_api.tests.test_integration_customer_payment import PaymentCase


def sale_with_ids(case, customer, total, **over):
	"""(the invoice's name, the sale payload) of a sale wholly on account."""
	payload = case.on_account_sale(customer, total=total, **over)
	return case.post(payload)["name"], payload


class OpenInvoicesCase(PaymentCase):
	def hooked_device(self, device_type="restaurant_pos"):
		device = new_id()
		device_api.register_device(
			device_id=device, device_name="Dining", pos_profile=self.profile.name, device_type=device_type
		)
		return device

	def open_invoices(self, customer, **kwargs):
		return credit_api.get_open_invoices(device_id=self.device_id, customer=customer, **kwargs)


class TestOpenInvoices(OpenInvoicesCase):
	def test_open_invoices_oldest_first_company_wide(self):
		"""Two outlets of one company: both outlets' bills, oldest first, each naming its outlet (Q1)."""
		customer = make_customer("oldestfirst", limit=None)
		first, first_payload = sale_with_ids(self, customer, "30.000")
		second = self.sell(customer, "50.000")
		third = self.sell(customer, "40.000")
		frappe.db.set_value("Sales Invoice", second, "pos_profile", "Another Outlet")
		answer = self.open_invoices(customer)
		self.assertEqual([row["name"] for row in answer["invoices"]], [first, second, third])
		self.assertEqual(
			[row["outlet"] for row in answer["invoices"]],
			[self.profile.name, "Another Outlet", self.profile.name],
		)
		head = answer["invoices"][0]
		self.assertEqual(head["local_id"], first_payload["local_id"])
		self.assertEqual(
			head["posting_date"], str(frappe.db.get_value("Sales Invoice", first, "posting_date"))
		)
		self.assertEqual((head["grand_total"], head["outstanding"]), ("30.000", "30.000"))
		self.assertEqual(answer["customer"], customer)
		self.assertIs(answer["more"], False)
		self.assertTrue(answer["as_of"])

	def test_paid_and_return_invoices_excluded(self):
		customer = make_customer("excluded", limit=None)
		paid = self.sell(customer, "20.000")
		open_one = self.sell(customer, "30.000")
		self.assertOk(self.pay(self.payment(customer, "20.000")))
		self.assertEqual(frappe.db.get_value("Sales Invoice", paid, "outstanding_amount"), 0)
		self.assertEqual([row["name"] for row in self.open_invoices(customer)["invoices"]], [open_one])

	def test_advance_is_unallocated_receive_entries(self):
		customer = make_customer("advance", limit=None)
		self.sell(customer, "30.000")
		result = self.assertOk(self.pay(self.payment(customer, "40.000")))
		self.assertEqual(result["payment"]["unallocated"], "10.000")
		answer = self.open_invoices(customer)
		self.assertEqual(answer["advance"], "10.000")
		self.assertEqual(answer["invoices"], [])
		# a customer who paid nothing ahead has none
		self.assertEqual(self.open_invoices(make_customer("noadvance", limit=None))["advance"], "0.000")

	def test_more_flag_at_limit(self):
		customer = make_customer("more", limit=None)
		names = [self.sell(customer, "10.000") for _ in range(3)]
		cut = self.open_invoices(customer, limit=2)
		self.assertEqual([row["name"] for row in cut["invoices"]], names[:2])
		self.assertIs(cut["more"], True)
		everything = self.open_invoices(customer, limit=3)
		self.assertEqual(len(everything["invoices"]), 3)
		self.assertIs(everything["more"], False)
		# the default is 200 rows and the most one answer gives is 1000 (the till's "Show more")
		for asked, expected in ((None, 201), ("1000", 1001), (5000, 1001), (0, 201)):
			with mock.patch("crenya_pos_api.sync.customer_payment.open_invoices", return_value=[]) as read:
				self.open_invoices(customer, limit=asked)
			self.assertEqual(read.call_args.args[2], expected, asked)

	def test_two_hundred_and_one_rows_answer_two_hundred_and_more(self):
		rows = [
			{
				"name": f"SINV-{number:04d}",
				"total": Decimal("1.500"),
				"outstanding": Decimal("1.500"),
				"pos_profile": "O",
				"posting_date": "2026-10-01",
				"local_id": None,
				"offline_number": None,
				"due_date": "2026-10-01",
			}
			for number in range(201)
		]
		with mock.patch("crenya_pos_api.sync.customer_payment.open_invoices", return_value=rows):
			answer = credit_service.open_invoices_answer("CUST", fixtures.COMPANY, 3, 200)
		self.assertEqual(len(answer["invoices"]), 200)
		self.assertIs(answer["more"], True)
		with mock.patch("crenya_pos_api.sync.customer_payment.open_invoices", return_value=rows):
			everything = credit_service.open_invoices_answer("CUST", fixtures.COMPANY, 3, 1000)
		self.assertEqual(len(everything["invoices"]), 201)
		self.assertIs(everything["more"], False)

	def test_refused_for_waiter_kds_and_switched_off_outlet(self):
		customer = make_customer("refused", limit=None)
		self.sell(customer, "10.000")
		for device_type in ("waiter", "kds"):
			device = self.hooked_device(device_type)
			with self.assertRaises(InvalidRequestError):
				credit_api.get_open_invoices(device_id=device, customer=customer)
		# a type an app lists for credit is answered; a type it does not list is refused
		device = self.hooked_device("restaurant_pos")
		with self.assertRaises(InvalidRequestError):
			credit_api.get_open_invoices(device_id=device, customer=customer)
		with dummy.fake_hooks({"crenya_pos_credit_device_types": ["restaurant_pos"]}):
			answer = credit_api.get_open_invoices(device_id=device, customer=customer)
		self.assertEqual(len(answer["invoices"]), 1)
		self.set_switch(0)
		try:
			with self.assertRaises(InvalidRequestError):
				self.open_invoices(customer)
		finally:
			self.set_switch(1)

	def test_walk_in_refused(self):
		with self.assertRaises(InvalidRequestError):
			self.open_invoices(self.walk_in)
		with self.assertRaises(InvalidRequestError):
			self.open_invoices("No Such Customer")
		with self.assertRaises(InvalidRequestError):
			self.open_invoices(None)

	def test_amounts_at_outlet_precision_omr(self):
		customer = make_customer("omr", limit=None)
		invoice = self.sell(customer, "12.500")
		self.assertOk(self.pay(self.payment(customer, "4.250")))
		row = self.open_invoices(customer)["invoices"][0]
		self.assertEqual(row["name"], invoice)
		self.assertEqual((row["grand_total"], row["outstanding"]), ("12.500", "8.250"))
		answer = self.open_invoices(customer)
		self.assertEqual(answer["outstanding"], "8.250")
		self.assertEqual(answer["advance"], "0.000")


BANK = "_Test Crenya Open Bank"


class TestSettlements(OpenInvoicesCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# a Bank-type mode with an account for the company (the card mode of the terminal fixtures may
		# not be set up when this module runs alone)
		company = frappe.get_doc("Company", fixtures.COMPANY)
		fixtures.ensure_mode_of_payment(BANK, "Bank", company)

	def web_settlement(self, customer, invoice, amount):
		booking = PaymentBooking(
			company=fixtures.COMPANY,
			customer=customer,
			mode_of_payment=BANK,
			amount=Decimal(amount),
			currency=fixtures.CURRENCY,
			posting_date=nowdate(),
			local_id=new_id(),
			reference="WEB-BANK-1",
			remarks="Settled on app.crenya.com by Administrator",
			invoices=[invoice],
		)
		return post_payment_entry(booking, []).name

	def desk_entry(self, invoice, amount):
		"""A Payment Entry as a desk user makes it from the invoice: no till id, its own remarks."""
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		entry = get_payment_entry("Sales Invoice", invoice, party_amount=float(amount))
		entry.mode_of_payment = fixtures.CASH
		entry.reference_no = "DESK-1"
		entry.reference_date = nowdate()
		entry.remarks = "Desk receipt"
		entry.insert(ignore_permissions=True)
		entry.submit()
		return entry.name

	def test_invoice_outstanding_lists_settlements(self):
		customer = make_customer("settled", limit=None)
		invoice = self.sell(customer, "20.000")
		till = self.assertOk(self.pay(self.payment(customer, "5.000")))["name"]
		web = self.web_settlement(customer, invoice, "3.000")
		desk = self.desk_entry(invoice, "2.000")
		answer = credit_api.get_invoice_outstanding(device_id=self.device_id, invoice=invoice)
		self.assertEqual(answer["outstanding"], "10.000")
		self.assertEqual(
			[
				(
					row["payment_entry"],
					row["mode_of_payment"],
					row["mode_type"],
					row["from_till"],
					row["allocated"],
				)
				for row in answer["settlements"]
			],
			[
				(till, fixtures.CASH, "Cash", True, "5.000"),
				(web, BANK, "Bank", False, "3.000"),
				(desk, fixtures.CASH, "Cash", False, "2.000"),
			],
		)

	def test_unsettled_bill_has_no_settlements_and_cancelled_entry_is_not_listed(self):
		customer = make_customer("unsettled", limit=None)
		invoice = self.sell(customer, "20.000")
		answer = credit_api.get_invoice_outstanding(device_id=self.device_id, invoice=invoice)
		self.assertEqual(answer["settlements"], [])
		entry = self.web_settlement(customer, invoice, "4.000")
		self.assertEqual(len(credit_service.invoice_settlements(invoice)), 1)
		frappe.get_doc("Payment Entry", entry).cancel()
		self.assertEqual(credit_service.invoice_settlements(invoice), [])
