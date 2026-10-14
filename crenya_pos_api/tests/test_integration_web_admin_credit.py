"""Shops web admin: credit customers, the credit limit edit and the Credit sales / Expenses settings
(crenya_pos_api/api/web_admin/credit.py, settings.py), retail P1 task 12.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_web_admin_credit
"""

from decimal import Decimal
from unittest import mock

import frappe

from crenya_pos_api.api import web_admin
from crenya_pos_api.expenses import booking
from crenya_pos_api.sync.approvals import APPROVER_ROLE
from crenya_pos_api.sync.context import POS_USER_ROLE
from crenya_pos_api.sync.credit import CREDIT_SALES_FIELD
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import new_id
from crenya_pos_api.tests.test_integration_credit import CreditCase, make_customer
from crenya_pos_api.tests.web_admin_case import (
	CASHIER,
	MANAGER,
	SHOP2,
	SHOP2_CASHIER,
	SHOP2_MANAGER,
	ensure_shop2,
	ensure_user,
)
from crenya_pos_api.utils.dates import format_db_datetime


def credit_limit_rows(customer):
	return frappe.get_all(
		"Customer Credit Limit",
		filters={"parent": customer, "parenttype": "Customer", "company": fixtures.COMPANY},
		fields=["credit_limit"],
	)


def rows_by_customer(answer):
	return {row["customer"]: row for row in answer["rows"]}


class WebCreditCase(CreditCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(MANAGER, [POS_USER_ROLE, APPROVER_ROLE])
		ensure_user(CASHIER, [POS_USER_ROLE])
		ensure_user(SHOP2_CASHIER, [POS_USER_ROLE])
		ensure_user(SHOP2_MANAGER, [POS_USER_ROLE, APPROVER_ROLE])
		ensure_shop2([SHOP2_CASHIER, SHOP2_MANAGER])
		frappe.db.commit()  # nosemgrep

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def sell(self, customer, total):
		return self.post(self.on_account_sale(customer, total=total))["name"]

	def as_manager(self):
		frappe.set_user(MANAGER)


class TestCreditCustomers(WebCreditCase):
	def test_credit_customers_flags_over_limit(self):
		over = make_customer("webover", limit=100)
		under = make_customer("webunder", limit=500)
		free = make_customer("webfree", limit=None)
		idle = make_customer("webidle", limit=250)
		for customer, total in ((over, "150.000"), (under, "120.000"), (free, "80.000")):
			self.sell(customer, total)
		self.sell(under, "30.000")
		self.as_manager()
		answer = web_admin.credit_customers(self.profile.name)
		self.assertEqual(answer["outlet"], self.profile.name)
		self.assertEqual(answer["currency"]["precision"], 3)
		rows = rows_by_customer(answer)
		self.assertEqual(
			(rows[over]["credit_limit"], rows[over]["outstanding"], rows[over]["available"]),
			("100.000", "150.000", "0.000"),
		)
		self.assertIs(rows[over]["over_limit"], True)
		self.assertEqual(
			(rows[under]["outstanding"], rows[under]["available"], rows[under]["open_invoices"]),
			("150.000", "350.000", 2),
		)
		self.assertIs(rows[under]["over_limit"], False)
		# no limit: "No limit" on the web
		self.assertEqual(
			(rows[free]["credit_limit"], rows[free]["available"], rows[free]["over_limit"]),
			(None, None, False),
		)
		# a limit and no balance is listed too
		self.assertEqual((rows[idle]["credit_limit"], rows[idle]["outstanding"]), ("250.000", "0.000"))
		# over the limit first
		names = [row["customer"] for row in answer["rows"]]
		self.assertLess(names.index(over), names.index(under))
		self.assertLess(names.index(over), names.index(free))

	def test_cashier_and_other_shops_manager_refused(self):
		frappe.set_user(CASHIER)
		with self.assertRaises(frappe.PermissionError):
			web_admin.credit_customers(self.profile.name)
		# shop 2 lists its own users: this shop's manager does not manage it
		self.as_manager()
		with self.assertRaises(frappe.PermissionError):
			web_admin.credit_customers(SHOP2)

	def test_a_key_is_refused(self):
		customer = make_customer("webkey", limit=100)
		self.as_manager()
		with mock.patch("crenya_pos_api.web.context.request_uses_api_key", return_value=True):
			calls = (
				lambda: web_admin.credit_customers(self.profile.name),
				lambda: web_admin.save_credit_limit(self.profile.name, customer, "50.000", "x"),
				lambda: web_admin.ensure_cash_supplier(self.profile.name),
				lambda: web_admin.expenses_not_booked(self.profile.name),
				lambda: web_admin.till_expense("EXP-0001"),
				lambda: web_admin.retry_expense_booking("EXP-0001"),
			)
			for call in calls:
				with self.assertRaises(frappe.PermissionError):
					call()
		self.assertEqual(credit_limit_rows(customer)[0].credit_limit, 100)


class TestSaveCreditLimit(WebCreditCase):
	def modified(self, customer):
		self.as_manager()
		return rows_by_customer(web_admin.credit_customers(self.profile.name))[customer]["modified"]

	def test_save_credit_limit_writes_company_row_and_comments(self):
		customer = make_customer("webset", limit=None)
		self.sell(customer, "40.000")
		modified = self.modified(customer)
		self.assertEqual(credit_limit_rows(customer), [])
		row = web_admin.save_credit_limit(self.profile.name, customer, "250.500", modified)
		self.assertEqual(
			[Decimal(str(r.credit_limit)) for r in credit_limit_rows(customer)], [Decimal("250.5")]
		)
		self.assertEqual(
			(row["customer"], row["credit_limit"], row["outstanding"], row["available"]),
			(customer, "250.500", "40.000", "210.500"),
		)
		comments = frappe.get_all(
			"Comment",
			filters={
				"reference_doctype": "Customer",
				"reference_name": customer,
				"content": ["like", f"%app.crenya.com by {MANAGER}%"],
			},
			pluck="content",
		)
		self.assertTrue(comments)
		self.assertIn("250.500", comments[0])
		# a change of the same row, then 0: no limit (the company's row goes)
		again = web_admin.save_credit_limit(self.profile.name, customer, "100", row["modified"])
		self.assertEqual(again["credit_limit"], "100.000")
		self.assertEqual(len(credit_limit_rows(customer)), 1)
		cleared = web_admin.save_credit_limit(self.profile.name, customer, "0", again["modified"])
		self.assertEqual(credit_limit_rows(customer), [])
		self.assertEqual((cleared["credit_limit"], cleared["available"]), (None, None))

	def test_blank_is_no_limit_too(self):
		customer = make_customer("webblank", limit=75)
		row = web_admin.save_credit_limit(self.profile.name, customer, "", self.modified(customer))
		self.assertIsNone(row["credit_limit"])
		self.assertEqual(credit_limit_rows(customer), [])

	def test_save_credit_limit_needs_web_session(self):
		customer = make_customer("websession", limit=75)
		modified = self.modified(customer)
		with mock.patch("crenya_pos_api.web.context.request_uses_api_key", return_value=True):
			with self.assertRaises(frappe.PermissionError):
				web_admin.save_credit_limit(self.profile.name, customer, "10", modified)
		self.assertEqual(credit_limit_rows(customer)[0].credit_limit, 75)

	def test_save_credit_limit_stale_modified_refused(self):
		customer = make_customer("webstale", limit=75)
		self.as_manager()
		with self.assertRaises(frappe.TimestampMismatchError):
			web_admin.save_credit_limit(self.profile.name, customer, "10", "2001-01-01 00:00:00.000000")
		self.assertEqual(credit_limit_rows(customer)[0].credit_limit, 75)

	def test_credit_limit_precision_omr(self):
		customer = make_customer("webprec", limit=75)
		modified = self.modified(customer)
		for value in ("10.0001", "-1", "ten"):
			with self.assertRaises(frappe.ValidationError, msg=value):
				web_admin.save_credit_limit(self.profile.name, customer, value, modified)
		row = web_admin.save_credit_limit(self.profile.name, customer, "10.125", modified)
		self.assertEqual(row["credit_limit"], "10.125")

	def test_only_a_manager_of_the_shop_and_only_its_customers(self):
		customer = make_customer("webscope", limit=75)
		modified = self.modified(customer)
		frappe.set_user(CASHIER)
		with self.assertRaises(frappe.PermissionError):
			web_admin.save_credit_limit(self.profile.name, customer, "10", modified)
		self.as_manager()
		with self.assertRaises(frappe.PermissionError):
			web_admin.save_credit_limit(SHOP2, customer, "10", modified)
		# a customer outside the shop's customer groups
		self.as_manager()
		outside = fixtures.make_customer(f"_TC web outside {new_id()[:6]}")
		other_group = "_Test Crenya Other Web Customers"
		if not frappe.db.exists("Customer Group", other_group):
			from frappe.utils.nestedset import get_root_of

			fixtures._insert(
				{
					"doctype": "Customer Group",
					"customer_group_name": other_group,
					"parent_customer_group": get_root_of("Customer Group"),
				}
			)
		saved = [row.customer_group for row in self.profile.customer_groups]
		profile = frappe.get_doc("POS Profile", self.profile.name)
		profile.set("customer_groups", [{"customer_group": other_group}])
		profile.flags.ignore_validate = True
		profile.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep
		frappe.clear_document_cache("POS Profile", self.profile.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				web_admin.save_credit_limit(
					self.profile.name,
					outside,
					"10",
					format_db_datetime(frappe.db.get_value("Customer", outside, "modified")),
				)
		finally:
			profile = frappe.get_doc("POS Profile", self.profile.name)
			profile.set("customer_groups", [{"customer_group": g} for g in saved])
			profile.flags.ignore_validate = True
			profile.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep
			frappe.clear_document_cache("POS Profile", self.profile.name)


class TestCreditAndExpenseSettings(WebCreditCase):
	def fields(self, answer):
		return {f["fieldname"]: f for section in answer["sections"] for f in section["fields"]}

	def test_settings_form_has_credit_and_expenses_sections(self):
		self.as_manager()
		answer = web_admin.outlet_settings(self.profile.name)
		titles = [section["title"] for section in answer["sections"]]
		self.assertIn("Expenses", titles)
		self.assertIn("Credit sales", titles)
		fields = self.fields(answer)
		for name in (
			booking.ENABLED_FIELD,
			booking.CASH_SUPPLIER_FIELD,
			booking.SUPPLIER_GROUP_FIELD,
			booking.TAX_TEMPLATE_FIELD,
			booking.ADD_PIN_FIELD,
			CREDIT_SALES_FIELD,
		):
			self.assertIn(name, fields, msg=name)
		# the accounting links are a System Manager's; the switches are the manager's
		for name in (booking.CASH_SUPPLIER_FIELD, booking.SUPPLIER_GROUP_FIELD, booking.TAX_TEMPLATE_FIELD):
			self.assertTrue(fields[name]["read_only"], msg=name)
		for name in (booking.ENABLED_FIELD, booking.ADD_PIN_FIELD, CREDIT_SALES_FIELD):
			self.assertFalse(fields[name]["read_only"], msg=name)
		# options for the new links, and the live checks
		self.assertIn("linkOptions", fields[booking.SUPPLIER_GROUP_FIELD])
		self.assertIn("linkOptions", fields[booking.CASH_SUPPLIER_FIELD])
		self.assertIn("linkOptions", fields[booking.TAX_TEMPLATE_FIELD])
		self.assertTrue(answer["expense_setup_checks"])
		self.assertIn("module", {row["key"] for row in answer["expense_setup_checks"]})
		self.assertIn("credit_setup_checks", answer)

	def test_manager_saves_the_switches_but_not_the_links(self):
		self.keep_profile(CREDIT_SALES_FIELD, booking.ENABLED_FIELD, booking.CASH_SUPPLIER_FIELD)
		self.as_manager()
		answer = web_admin.outlet_settings(self.profile.name)
		saved = web_admin.save_outlet_settings(
			self.profile.name, {CREDIT_SALES_FIELD: 1, booking.ENABLED_FIELD: 0}, answer["modified"]
		)
		fields = self.fields(saved)
		self.assertEqual(
			(fields[CREDIT_SALES_FIELD]["value"], fields[booking.ENABLED_FIELD]["value"]), (1, 0)
		)
		with self.assertRaises(frappe.PermissionError):
			web_admin.save_outlet_settings(
				self.profile.name, {booking.CASH_SUPPLIER_FIELD: ""}, saved["modified"]
			)
		# a System Manager sets the links
		frappe.set_user("Administrator")
		again = web_admin.outlet_settings(self.profile.name)
		done = web_admin.save_outlet_settings(
			self.profile.name, {booking.TAX_TEMPLATE_FIELD: ""}, again["modified"]
		)
		self.assertEqual(self.fields(done)[booking.TAX_TEMPLATE_FIELD]["value"], "")

	def test_credit_setup_check_follows_the_switch(self):
		self.keep_profile(CREDIT_SALES_FIELD)
		self.as_manager()
		answer = web_admin.outlet_settings(self.profile.name)
		on = web_admin.save_outlet_settings(self.profile.name, {CREDIT_SALES_FIELD: 1}, answer["modified"])
		self.assertEqual([row["key"] for row in on["credit_setup_checks"]], ["credit_receivable"])
		off = web_admin.save_outlet_settings(self.profile.name, {CREDIT_SALES_FIELD: 0}, on["modified"])
		self.assertEqual(off["credit_setup_checks"], [])

	def keep_profile(self, *fields):
		saved = frappe.db.get_value("POS Profile", self.profile.name, list(fields), as_dict=True)

		def restore():
			frappe.db.set_value("POS Profile", self.profile.name, saved, update_modified=False)
			frappe.clear_document_cache("POS Profile", self.profile.name)
			frappe.db.commit()  # nosemgrep

		self.addCleanup(restore)
