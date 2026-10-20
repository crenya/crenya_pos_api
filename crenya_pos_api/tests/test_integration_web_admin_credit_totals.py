"""Restaurant credit 0.14.0 (Task 4): the House Account page's totals for a date range (CC8), who a
restaurant outlet lists (RC16), a customer's payment history and the payment receipt PDF (CC9)
(crenya_pos_api/api/web_admin/credit.py `credit_customers`, `customer_payments`,
`payment_receipt_pdf`; print format "crenya Payment Receipt").

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_web_admin_credit_totals
"""

from decimal import Decimal
from unittest import mock

import frappe
from frappe.utils import add_days, get_fullname, nowdate

from crenya_pos_api.api import web_admin
from crenya_pos_api.sync.credit import CREDIT_ENABLED_FIELD
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.test_integration_credit import make_customer
from crenya_pos_api.tests.test_integration_web_admin_credit import (
	ACCOUNTS,
	REST_MANAGER,
	REST_MANAGER_B,
	RESTAURANT_A,
	RESTAURANT_B,
	SETTLE_BANK,
	rows_by_customer,
)
from crenya_pos_api.tests.test_integration_web_admin_settle import SettleCase

PAYMENT_ENTRY = "Payment Entry"


class TotalsCase(SettleCase):
	def row(self, customer, outlet=RESTAURANT_A, **range_args):
		return rows_by_customer(web_admin.credit_customers(outlet, **range_args)).get(customer)

	def move(self, doctype, name, days):
		frappe.db.set_value(doctype, name, "posting_date", add_days(nowdate(), days), update_modified=False)
		frappe.db.commit()  # nosemgrep


class TestRangeTotals(TotalsCase):
	def test_customer_rows_carry_range_totals(self):
		"""All time by default; a one-week range counts only the bills and payments posted in it."""
		customer = make_customer("cc8range", limit=None)
		old, new = self.bills(customer, "10.000", "4.000")
		self.move("Sales Invoice", old, -30)
		frappe.set_user(ACCOUNTS)
		paid_now = self.settle(customer, "3.000", invoices=[new])["payment_entry"]
		self.settle(customer, "2.000", invoices=[old], posting_date=add_days(nowdate(), -30))
		frappe.set_user(REST_MANAGER)
		row = self.row(customer)
		self.assertEqual(
			(row["credit_sales"], row["payments"], row["outstanding"]), ("14.000", "5.000", "9.000")
		)
		week = self.row(customer, from_date=add_days(nowdate(), -6), to_date=nowdate())
		self.assertEqual(
			(week["credit_sales"], week["payments"], week["outstanding"]), ("4.000", "3.000", "9.000")
		)
		# open on one side
		since = self.row(customer, from_date=add_days(nowdate(), -6))
		self.assertEqual((since["credit_sales"], since["payments"]), ("4.000", "3.000"))
		until = self.row(customer, to_date=add_days(nowdate(), -7))
		self.assertEqual((until["credit_sales"], until["payments"]), ("10.000", "2.000"))
		self.assertTrue(paid_now)

	def test_credit_sales_is_on_account_part_net_of_cancels(self):
		"""An 18.000 bill with 5.000 paid in cash counts 13.000; a bill cancelled on the desk counts 0,
		and so does a bill whose House Account part a return credited back to the account."""
		customer = make_customer("cc8part", limit=None)
		self.post(self.on_account_sale(customer, "18.000", cash="5.000"))
		frappe.set_user(REST_MANAGER)
		self.assertEqual(self.row(customer)["credit_sales"], "13.000")
		frappe.set_user("Administrator")
		cancelled = self.sell(customer, "7.000")
		frappe.get_doc("Sales Invoice", cancelled).cancel()
		frappe.db.commit()  # nosemgrep
		sale = self.on_account_sale(customer, "18.000", cash="5.000")
		self.post(sale)
		ret = self.return_payload(sale, [(fixtures.MILK, "-2", "9.000", 1)], credit_offset="13.000")
		ret["payments"] = [{"mode_of_payment": fixtures.CASH, "amount": "-5.000"}]
		self.post(ret)
		frappe.set_user(REST_MANAGER)
		row = self.row(customer)
		self.assertEqual((row["credit_sales"], row["outstanding"]), ("13.000", "13.000"))

	def test_grand_total_row_sums_rows_omr(self):
		first = make_customer("cc8sum1", limit=None)
		second = make_customer("cc8sum2", limit=None)
		self.bills(first, "1.234")
		self.bills(second, "2.002", "0.006")
		frappe.set_user(ACCOUNTS)
		self.settle(second, "0.333")
		answer = web_admin.credit_customers(RESTAURANT_A)
		self.assertEqual(answer["currency"]["precision"], 3)
		sums = {
			key: sum((Decimal(row[key]) for row in answer["rows"]), Decimal(0))
			for key in ("credit_sales", "payments", "outstanding")
		}
		self.assertEqual(answer["totals"], {key: f"{value:.3f}" for key, value in sums.items()})
		rows = rows_by_customer(answer)
		self.assertEqual((rows[first]["credit_sales"], rows[second]["credit_sales"]), ("1.234", "2.008"))
		self.assertEqual((rows[second]["payments"], rows[second]["outstanding"]), ("0.333", "1.675"))

	def test_outstanding_not_range_bound(self):
		customer = make_customer("cc8bal", limit=None)
		self.bills(customer, "6.500")
		frappe.set_user(REST_MANAGER)
		past = self.row(customer, from_date="2001-01-01", to_date="2001-01-31")
		self.assertEqual(
			(past["credit_sales"], past["payments"], past["outstanding"]), ("0.000", "0.000", "6.500")
		)

	def test_restaurant_lists_enabled_or_owing(self):
		"""RC16: enabled with no balance is listed, owing without the facility is listed, neither is
		not (an own credit limit does not list a customer on a restaurant)."""
		enabled = make_customer("rc16on", limit=None)
		owing = make_customer("rc16owe", limit=None)
		neither = make_customer("rc16none", limit=75)
		frappe.db.set_value("Customer", enabled, {CREDIT_ENABLED_FIELD: 1, "mobile_no": "+96891234567"})
		frappe.db.commit()  # nosemgrep
		self.bills(owing, "3.000")
		frappe.set_user(REST_MANAGER)
		rows = rows_by_customer(web_admin.credit_customers(RESTAURANT_A))
		self.assertIn(enabled, rows)
		self.assertIn(owing, rows)
		self.assertNotIn(neither, rows)
		self.assertEqual(
			(rows[enabled]["credit_enabled"], rows[enabled]["outstanding"], rows[enabled]["mobile_no"]),
			(True, "0.000", "+96891234567"),
		)
		self.assertIs(rows[owing]["credit_enabled"], False)
		# the shop keeps its rule: a limit lists the customer, the flag alone does not
		frappe.set_user("Administrator")
		shop_rows = rows_by_customer(web_admin.credit_customers(self.profile.name))
		self.assertIn(neither, shop_rows)
		self.assertNotIn(enabled, shop_rows)


class TestPaymentsAndReceipt(TotalsCase):
	def setUp(self):
		super().setUp()
		saved = {key: frappe.local.response.get(key) for key in ("filename", "filecontent", "type")}

		def restore():
			for key, value in saved.items():
				if value is None:
					frappe.local.response.pop(key, None)
				else:
					frappe.local.response[key] = value

		self.addCleanup(restore)

	def receipt(self, outlet, entry, lang="en"):
		"""The receipt's HTML as `get_pdf` is given it, and the response."""
		seen = []

		def fake_pdf(html, *args, **kwargs):
			seen.append(html)
			return b"%PDF-1.4 receipt"

		with mock.patch("frappe.utils.pdf.get_pdf", side_effect=fake_pdf):
			self.assertIsNone(web_admin.payment_receipt_pdf(outlet, entry, lang))
		self.assertEqual(len(seen), 1)
		return seen[0], frappe.local.response

	def test_customer_payments_lists_receive_entries_with_bills(self):
		customer = make_customer("cc9list", limit=None)
		first, second = self.bills(customer, "6.000", "10.000")
		self.pay(self.payment(customer, "2.000"))
		frappe.set_user(REST_MANAGER)
		web = self.settle(customer, "7.000", invoices=[second], reference="CHQ-77")
		rows = web_admin.customer_payments(RESTAURANT_A, customer)
		self.assertEqual(len(rows), 2)
		by_entry = {row["payment_entry"]: row for row in rows}
		row = by_entry[web["payment_entry"]]
		self.assertEqual(
			{key: row[key] for key in ("payment_type", "amount", "mode_of_payment", "reference", "invoices")},
			{
				"payment_type": "Receive",
				"amount": "7.000",
				"mode_of_payment": SETTLE_BANK,
				"reference": "CHQ-77",
				"invoices": [second],
			},
		)
		self.assertEqual(row["recorded_by"], get_fullname(REST_MANAGER))
		self.assertEqual(row["posting_date"], nowdate())
		till = next(r for r in rows if r["payment_entry"] != web["payment_entry"])
		# a till's cash receipt without a typed reference: none shown (not the till's id)
		self.assertEqual((till["amount"], till["reference"], till["invoices"]), ("2.000", None, [first]))
		self.assertEqual(
			web_admin.customer_payments(RESTAURANT_A, customer, from_date="2001-01-01", to_date="2001-01-02"),
			[],
		)
		with self.assertRaises(frappe.PermissionError):
			web_admin.customer_payments(RESTAURANT_A, self.profile.customer)
		frappe.set_user(REST_MANAGER_B)
		with self.assertRaises(frappe.PermissionError):
			web_admin.customer_payments(RESTAURANT_A, customer)

	def test_payment_receipt_pdf_for_permitted_outlet(self):
		customer = make_customer("cc9pdf", limit=None)
		first, second = self.bills(customer, "6.000", "10.000")
		frappe.set_user(REST_MANAGER)
		answer = self.settle(customer, "12.000", invoices=[first, second], reference="TRF-9")
		html, response = self.receipt(RESTAURANT_A, answer["payment_entry"])
		self.assertEqual(
			(response.type, response.filecontent, response.filename),
			("pdf", b"%PDF-1.4 receipt", f"{answer['payment_entry']}.pdf"),
		)
		for words in (
			"PAYMENT RECEIVED",
			answer["payment_entry"],
			"TRF-9",
			"12.000",
			"Bills cleared",
			first,
			"Part paid",
			second,
			"4.000",
			"Remaining balance",
			get_fullname(REST_MANAGER),
			'dir="ltr"',
		):
			self.assertIn(words, html)
		self.assertNotIn("Kept as advance", html)
		# an advance kept is printed; an accountant prints any outlet's of the company
		more = self.settle(customer, "5.000", invoices=[second])
		frappe.set_user(ACCOUNTS)
		html, _response = self.receipt(RESTAURANT_B, more["payment_entry"])
		self.assertIn("Kept as advance", html)
		self.assertIn("1.000", html)

	def test_payment_receipt_pdf_refuses_other_outlet_and_pay_entries(self):
		customer = make_customer("cc9refuse", limit=None)
		self.bills(customer, "10.000")
		frappe.set_user(REST_MANAGER)
		entry = self.settle(customer, "1.000")["payment_entry"]
		frappe.set_user(REST_MANAGER_B)
		with self.assertRaises(frappe.PermissionError):
			self.receipt(RESTAURANT_A, entry)
		# another company's entry, asked for through an outlet the user does manage
		frappe.set_user("Administrator")
		company = frappe.db.get_value(PAYMENT_ENTRY, entry, "company")
		frappe.db.set_value(
			PAYMENT_ENTRY, entry, "company", "_Test Crenya Other Company", update_modified=False
		)
		frappe.db.commit()  # nosemgrep
		try:
			frappe.set_user(REST_MANAGER)
			with self.assertRaises(frappe.PermissionError) as caught:
				self.receipt(RESTAURANT_A, entry)
			self.assertIn("not on this outlet's House Account", str(caught.exception))
		finally:
			frappe.set_user("Administrator")
			frappe.db.set_value(PAYMENT_ENTRY, entry, "company", company, update_modified=False)
			frappe.db.commit()  # nosemgrep
		frappe.set_user(REST_MANAGER)
		self.assertIn(entry, self.receipt(RESTAURANT_A, entry)[0])
		frappe.set_user(REST_MANAGER)
		for name in ("No such entry", ""):
			with self.assertRaises(frappe.PermissionError) as caught:
				self.receipt(RESTAURANT_A, name)
			self.assertIn("not on this outlet's House Account", str(caught.exception))
		frappe.set_user("Administrator")
		frappe.db.set_value(PAYMENT_ENTRY, entry, "payment_type", "Pay", update_modified=False)
		frappe.db.commit()  # nosemgrep
		frappe.set_user(REST_MANAGER)
		with self.assertRaises(frappe.PermissionError):
			self.receipt(RESTAURANT_A, entry)
		frappe.set_user("Administrator")
		frappe.db.set_value(
			PAYMENT_ENTRY, entry, {"payment_type": "Receive", "docstatus": 2}, update_modified=False
		)
		frappe.db.commit()  # nosemgrep
		frappe.set_user(REST_MANAGER)
		with self.assertRaises(frappe.PermissionError):
			self.receipt(RESTAURANT_A, entry)

	def test_payment_receipt_arabic_renders(self):
		customer = make_customer("cc9ar", limit=None)
		(bill,) = self.bills(customer, "10.000")
		frappe.set_user(REST_MANAGER)
		entry = self.settle(customer, "10.000", invoices=[bill])["payment_entry"]
		html, response = self.receipt(RESTAURANT_A, entry, "ar")
		self.assertEqual(response.type, "pdf")
		for words in (
			'dir="rtl"',
			'lang="ar"',
			"سند قبض",
			"الفواتير المسددة",
			"المبلغ المستلم",
			"الرصيد المتبقي",
			bill,
		):
			self.assertIn(words, html)
		self.assertNotIn("PAYMENT RECEIVED", html)
		self.assertIn("سند قبض", self.receipt(RESTAURANT_A, entry, " AR ")[0])
		with self.assertRaises(frappe.ValidationError):
			self.receipt(RESTAURANT_A, entry, "fr")

	def test_print_format_is_installed_standard(self):
		row = frappe.db.get_value(
			"Print Format",
			"crenya Payment Receipt",
			["doc_type", "module", "standard", "print_format_type", "disabled"],
			as_dict=True,
		)
		self.assertEqual(
			(row.doc_type, row.module, row.standard, row.print_format_type, row.disabled),
			(PAYMENT_ENTRY, "Crenya Pos Api", "Yes", "Jinja", 0),
		)


class TestOwingAndDisabled(TotalsCase):
	def test_ordinary_unpaid_invoice_shows_no_tab_on_a_credit_off_outlet(self):
		"""F2: a desk invoice that is unpaid does not make the House Account tab appear; a till bill
		left on account does, and so does a customer with the facility on."""
		from crenya_pos_api.api.web_admin import credit as web_credit

		frappe.set_user("Administrator")
		restaurant = RESTAURANT_A
		saved = frappe.db.get_value("POS Profile", restaurant, "crenya_credit_sales_enabled")
		self.addCleanup(
			lambda: (
				frappe.db.set_value("POS Profile", restaurant, "crenya_credit_sales_enabled", saved or 0),
				frappe.db.commit(),  # nosemgrep
			)
		)
		frappe.db.set_value("POS Profile", restaurant, "crenya_credit_sales_enabled", 0)
		frappe.clear_document_cache("POS Profile", restaurant)
		frappe.db.commit()  # nosemgrep
		# everything uncommitted below is undone at the end of the test
		self.addCleanup(frappe.db.rollback)
		frappe.db.sql(
			"update `tabSales Invoice` set pos_profile = NULL where company = %s", (fixtures.COMPANY,)
		)
		frappe.db.sql("update `tabCustomer` set crenya_credit_enabled = 0")
		desk = make_customer("deskowes", limit=None)
		doc = self.desk_invoice(desk)
		doc.insert(ignore_permissions=True)
		doc.submit()
		self.assertGreater(frappe.db.get_value("Sales Invoice", doc.name, "outstanding_amount"), 0)
		self.assertEqual(web_credit.owing_companies({fixtures.COMPANY}), set())
		frappe.set_user(ACCOUNTS)
		answer = web_admin.credit_shops("restaurant")
		self.assertNotIn(restaurant, [shop["name"] for shop in answer["shops"]])
		# a customer with the facility on brings the tab
		frappe.db.set_value("Customer", desk, CREDIT_ENABLED_FIELD, 1)
		self.assertEqual(web_credit.owing_companies({fixtures.COMPANY}), {fixtures.COMPANY})
		frappe.db.set_value("Customer", desk, CREDIT_ENABLED_FIELD, 0)
		self.assertEqual(web_credit.owing_companies({fixtures.COMPANY}), set())
		# a till bill still outstanding brings it too
		frappe.db.set_value("Sales Invoice", doc.name, "pos_profile", restaurant)
		self.assertEqual(web_credit.owing_companies({fixtures.COMPANY}), {fixtures.COMPANY})

	def test_disabled_customer_who_owes_stays_listed_and_counted(self):
		"""F7: disabling a customer who still owes does not remove them from the page or the Grand
		total; a disabled customer who owes nothing is not listed."""
		owing = make_customer("disowes", limit=None)
		clear = make_customer("disclear", limit=None)
		self.bills(owing, "30.000")
		frappe.db.set_value("Customer", clear, CREDIT_ENABLED_FIELD, 1)
		frappe.db.commit()  # nosemgrep
		frappe.set_user(REST_MANAGER)
		before = web_admin.credit_customers(RESTAURANT_A)
		frappe.set_user("Administrator")
		for name in (owing, clear):
			frappe.db.set_value("Customer", name, "disabled", 1)
		frappe.db.commit()  # nosemgrep
		self.addCleanup(self.enable, owing, clear)
		frappe.set_user(REST_MANAGER)
		after = web_admin.credit_customers(RESTAURANT_A)
		rows = rows_by_customer(after)
		self.assertIn(owing, rows)
		self.assertIs(rows[owing]["disabled"], True)
		self.assertEqual(rows[owing]["outstanding"], "30.000")
		self.assertNotIn(clear, rows)
		self.assertIn(clear, rows_by_customer(before))
		self.assertEqual(
			Decimal(after["totals"]["outstanding"]),
			sum((Decimal(row["outstanding"]) for row in after["rows"]), Decimal(0)),
		)
		self.assertGreaterEqual(Decimal(after["totals"]["outstanding"]), Decimal("30.000"))
		# a shop keeps its rule
		frappe.set_user("Administrator")
		self.assertNotIn(owing, rows_by_customer(web_admin.credit_customers(self.profile.name)))

	def enable(self, *names):
		for name in names:
			frappe.db.set_value("Customer", name, "disabled", 0)
		frappe.db.commit()  # nosemgrep
