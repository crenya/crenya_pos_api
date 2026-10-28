"""House Account Phase 2 (0.16.0, Task 6): the Outstanding report with ageing
(crenya_pos_api/api/web_admin/credit_reports.py `credit_outstanding`) on a restaurant outlet:
ERPNext's own Accounts Receivable and customer balance as the references, OMR's 3 decimals, the
company as the boundary and the outlet's manager as the reader.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_credit_reports
"""

from unittest import mock

import frappe
from frappe.utils import add_days, nowdate

from crenya_pos_api.api import web_admin
from crenya_pos_api.api.web_admin import credit_reports
from crenya_pos_api.sync.credit import CREDIT_ENABLED_FIELD
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.test_integration_credit import make_customer
from crenya_pos_api.tests.test_integration_web_admin_credit import (
	CASHIER,
	REST_MANAGER,
	RESTAURANT_A,
	RESTAURANT_B,
	rows_by_customer,
)
from crenya_pos_api.tests.test_integration_web_admin_settle import SettleCase

OTHER_COMPANY = "_Test Crenya Other Company"
REPORT_GROUP = "_Test Crenya Report Group"


def report(outlet=RESTAURANT_A, **args):
	return web_admin.credit_outstanding(outlet, **args)


def ar_rows(customer, as_on=None):
	"""ERPNext's own Accounts Receivable run for the customer, with the report's filters."""
	from erpnext.accounts.report.accounts_receivable.accounts_receivable import execute

	frappe.set_user("Administrator")
	return execute(
		frappe._dict(
			company=fixtures.COMPANY,
			report_date=as_on or nowdate(),
			ageing_based_on="Due Date",
			range="30, 60, 90",
			party_type="Customer",
			party=[customer],
		)
	)[1]


class OutstandingCase(SettleCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# a long-lived test site holds thousands of customers and till sales of earlier runs; the
		# outlet lists only its own customer group, so the report sees this class's customers alone
		if not frappe.db.exists("Customer Group", REPORT_GROUP):
			frappe.get_doc(
				{
					"doctype": "Customer Group",
					"customer_group_name": REPORT_GROUP,
					"parent_customer_group": "All Customer Groups",
					"is_group": 0,
				}
			).insert(ignore_permissions=True)
		profile = frappe.get_doc("POS Profile", RESTAURANT_A)
		cls.saved_groups = [row.customer_group for row in profile.customer_groups]
		profile.set("customer_groups", [{"customer_group": REPORT_GROUP}])
		profile.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep

	@classmethod
	def tearDownClass(cls):
		profile = frappe.get_doc("POS Profile", RESTAURANT_A)
		profile.set("customer_groups", [{"customer_group": group} for group in cls.saved_groups])
		profile.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep
		super().tearDownClass()

	def customer(self, label, limit=None):
		"""A customer of the outlet's report group."""
		name = make_customer(label, limit=limit)
		frappe.db.set_value("Customer", name, "customer_group", REPORT_GROUP)
		frappe.db.commit()  # nosemgrep
		return name

	def enable(self, customer):
		frappe.db.set_value("Customer", customer, CREDIT_ENABLED_FIELD, 1)
		frappe.db.commit()  # nosemgrep

		def switch_off():
			frappe.db.set_value("Customer", customer, CREDIT_ENABLED_FIELD, 0)
			frappe.db.commit()  # nosemgrep

		self.addCleanup(switch_off)

	def ledger(self, customer, rows):
		frappe.set_user("Administrator")
		names = fixtures.house_account_ledger(fixtures.COMPANY, customer, rows)
		frappe.db.commit()  # nosemgrep
		return names

	def as_manager_report(self, **args):
		frappe.set_user(REST_MANAGER)
		return report(**args)

	def row(self, customer, outlet=RESTAURANT_A, **args):
		frappe.set_user(REST_MANAGER)
		return rows_by_customer(report(outlet, **args)).get(customer)


class TestOutstandingFigures(OutstandingCase):
	def test_ageing_matches_accounts_receivable(self):
		"""Review Focus 5: bills 10, 40, 70 and 100 days old fall in the four buckets, each equal to
		ERPNext's own Accounts Receivable run with the same filters."""
		customer = self.customer("ageing", limit=None)
		self.enable(customer)
		self.ledger(
			customer,
			[
				("invoice", 100, "4.000"),
				("invoice", 70, "3.000"),
				("invoice", 40, "2.000"),
				("invoice", 10, "1.000"),
			],
		)
		row = self.row(customer)
		rows = ar_rows(customer)
		expected = [
			sum(float(r.get(key) or 0) for r in rows) for key in ("range1", "range2", "range3", "range4")
		]
		self.assertEqual(
			[row[k] for k in ("range_0_30", "range_31_60", "range_61_90", "range_90_plus")],
			[f"{value:.3f}" for value in expected],
		)
		self.assertEqual(
			[row[k] for k in ("range_0_30", "range_31_60", "range_61_90", "range_90_plus")],
			["1.000", "2.000", "3.000", "4.000"],
		)
		self.assertEqual(row["oldest_unpaid"], add_days(nowdate(), -100))
		self.assertEqual(row["overdue"], "10.000")

	def test_opening_charged_paid_closing(self):
		customer = self.customer("flow", limit=None)
		self.enable(customer)
		self.ledger(customer, [("invoice", 60, "8.000")])
		self.ledger(
			customer,
			[("invoice", 5, "12.000"), ("payment", 4, "5.000"), ("credit_note", 3, "1.000")],
		)
		row = self.row(customer, from_date=add_days(nowdate(), -10))
		self.assertEqual(
			(row["opening"], row["charged"], row["paid"], row["closing"]),
			("8.000", "12.000", "6.000", "14.000"),
		)
		total = sum(float(r["outstanding"]) for r in ar_rows(customer))
		self.assertEqual(float(row["closing"]), total)

	def test_closing_equals_customer_outstanding_today(self):
		from erpnext.selling.doctype.customer.customer import get_customer_outstanding

		customer = self.customer("today", limit=None)
		self.enable(customer)
		self.bills(customer, "6.500", "3.250")
		row = self.row(customer)
		self.assertEqual(row["closing"], f"{get_customer_outstanding(customer, fixtures.COMPANY):.3f}")
		self.assertEqual(row["closing"], "9.750")

	def test_overdue_equals_the_credit_page_row(self):
		"""F8: on the day, the report's overdue is the House Account page's overdue."""
		customer = self.customer("overdue", limit=None)
		self.enable(customer)
		self.ledger(
			customer, [("invoice", 40, "7.000"), ("invoice", 0, "2.000"), ("credit_note", 20, "1.000")]
		)
		frappe.set_user(REST_MANAGER)
		page = rows_by_customer(web_admin.credit_customers(RESTAURANT_A))[customer]
		mine = rows_by_customer(report(as_on=nowdate()))[customer]
		self.assertEqual(mine["overdue"], page["overdue"])
		self.assertEqual(mine["oldest_unpaid"], page["oldest_unpaid"])
		self.assertEqual(mine["credit_days"], page["credit_days"])

	def test_as_on_before_first_invoice_all_zero(self):
		customer = self.customer("early", limit=None)
		plain = self.customer("early2", limit=None)
		self.enable(customer)
		self.bills(customer, "5.000")
		self.bills(plain, "5.000")
		long_ago = add_days(nowdate(), -400)
		rows = rows_by_customer(self.as_manager_report(as_on=long_ago, from_date=add_days(long_ago, -5)))
		self.assertIn(customer, rows)
		self.assertNotIn(plain, rows)
		zero = rows[customer]
		self.assertEqual(
			[zero[k] for k in ("opening", "charged", "paid", "closing", "range_0_30", "overdue")],
			["0.000"] * 6,
		)
		self.assertIsNone(zero["oldest_unpaid"])

	def test_defaults_and_shape(self):
		customer = self.customer("shape", limit=None)
		self.enable(customer)
		frappe.set_user(REST_MANAGER)
		answer = report()
		self.assertEqual(answer["as_on"], nowdate())
		self.assertEqual(answer["from_date"], nowdate()[:8] + "01")
		self.assertEqual(answer["outlet"], RESTAURANT_A)
		self.assertEqual(answer["company"], fixtures.COMPANY)
		self.assertEqual(answer["currency"]["precision"], 3)
		self.assertIs(answer["more"], False)
		self.assertEqual(
			set(rows_by_customer(answer)[customer]),
			{
				"customer",
				"name",
				"credit_days",
				"opening",
				"charged",
				"paid",
				"closing",
				"range_0_30",
				"range_31_60",
				"range_61_90",
				"range_90_plus",
				"overdue",
				"oldest_unpaid",
			},
		)
		self.assertEqual(
			set(answer["totals"]),
			{
				"opening",
				"charged",
				"paid",
				"closing",
				"range_0_30",
				"range_31_60",
				"range_61_90",
				"range_90_plus",
				"overdue",
			},
		)

	def test_totals_sum_the_rows(self):
		first = self.customer("tot1", limit=None)
		second = self.customer("tot2", limit=None)
		self.enable(first)
		self.enable(second)
		self.ledger(first, [("invoice", 1, "1.234")])
		self.ledger(second, [("invoice", 1, "2.002"), ("payment", 0, "0.333")])
		frappe.set_user(REST_MANAGER)
		answer = report(from_date=add_days(nowdate(), -3))
		for key in ("opening", "charged", "paid", "closing", "overdue"):
			self.assertEqual(answer["totals"][key], f"{sum(float(r[key]) for r in answer['rows']):.3f}")


class TestOutstandingScope(OutstandingCase):
	def test_outstanding_is_per_company(self):
		"""Review Focus 2: the customer owes the other company too; the outlet shows its own company's."""
		customer = self.customer("percompany", limit=None)
		self.enable(customer)
		self.bills(customer, "5.000")
		entry = frappe.get_doc(
			{
				"doctype": "GL Entry",
				"posting_date": nowdate(),
				"company": OTHER_COMPANY,
				"account": f"Debtors - {fixtures.ABBR}",
				"party_type": "Customer",
				"party": customer,
				"debit": 99,
				"debit_in_account_currency": 99,
				"voucher_type": "Journal Entry",
				"voucher_no": "_Test Crenya Other Voucher",
				"is_cancelled": 0,
			}
		)
		entry.flags.ignore_links = True
		entry.flags.ignore_validate = True
		entry.db_insert()
		self.addCleanup(lambda: frappe.db.delete("GL Entry", {"company": OTHER_COMPANY, "party": customer}))
		row = self.row(customer)
		self.assertEqual(row["closing"], "5.000")

	def test_customer_filter(self):
		first = self.customer("only1", limit=None)
		second = self.customer("only2", limit=None)
		self.enable(first)
		self.enable(second)
		frappe.set_user(REST_MANAGER)
		answer = report(customer=first)
		self.assertEqual([row["customer"] for row in answer["rows"]], [first])
		self.assertEqual(len(report()["rows"]) >= 2, True)

	def test_customer_filter_refuses_walk_in_and_excluded(self):
		frappe.set_user(REST_MANAGER)
		with self.assertRaises(frappe.PermissionError):
			report(customer=self.profile.customer)
		with self.assertRaises(frappe.PermissionError):
			report(customer="No such customer")
		excluded = self.customer("excl", limit=None)
		with mock.patch.object(credit_reports, "excluded_customers", return_value={excluded}):
			with self.assertRaises(frappe.PermissionError):
				report(customer=excluded)

	def test_from_after_as_on_refused(self):
		frappe.set_user(REST_MANAGER)
		with self.assertRaises(frappe.ValidationError):
			report(as_on="2026-01-01", from_date="2026-01-02")
		with self.assertRaises(frappe.ValidationError):
			report(as_on="01/01/2026")

	def test_walk_in_and_provider_not_listed(self):
		provider = self.customer("provider", limit=None)
		self.enable(provider)
		self.bills(provider, "3.000")
		frappe.db.set_value("Customer", self.profile.customer, CREDIT_ENABLED_FIELD, 1)
		frappe.db.commit()  # nosemgrep
		self.addCleanup(
			lambda: frappe.db.set_value("Customer", self.profile.customer, CREDIT_ENABLED_FIELD, 0)
		)
		with mock.patch.object(credit_reports, "excluded_customers", return_value={provider}):
			frappe.set_user(REST_MANAGER)
			rows = rows_by_customer(report())
		self.assertNotIn(provider, rows)
		self.assertNotIn(self.profile.customer, rows)

	def test_desk_debtors_are_not_listed(self):
		"""F9: a desk invoice on account to a customer that is not enabled is not a House Account."""
		desk = self.customer("desk", limit=None)
		self.ledger(desk, [("invoice", 1, "9.000")])
		till = self.customer("till", limit=None)
		self.bills(till, "4.000")
		rows = rows_by_customer(self.as_manager_report(from_date=add_days(nowdate(), -3)))
		self.assertNotIn(desk, rows)
		self.assertEqual(rows[till]["closing"], "4.000")

	def test_outlet_manager_not_system_manager_reads(self):
		"""Anti-goal A4: a Restaurant Manager with no Accounts role reads; a cashier does not; and the
		session user is back afterwards."""
		customer = self.customer("mgr", limit=None)
		self.enable(customer)
		self.bills(customer, "1.000")
		frappe.set_user(REST_MANAGER)
		self.assertIn(customer, rows_by_customer(report()))
		self.assertEqual(frappe.session.user, REST_MANAGER)
		frappe.set_user(CASHIER)
		with self.assertRaises(frappe.PermissionError):
			report()
		self.assertEqual(frappe.session.user, CASHIER)

	def test_session_user_restored_when_the_report_fails(self):
		frappe.set_user(REST_MANAGER)
		with mock.patch(
			"erpnext.accounts.report.accounts_receivable.accounts_receivable.execute",
			side_effect=RuntimeError("boom"),
		):
			customer = self.customer("boom", limit=None)
			self.enable(customer)
			with self.assertRaises(RuntimeError):
				report()
		self.assertEqual(frappe.session.user, REST_MANAGER)

	def test_user_permission_on_other_company_refused(self):
		permission = frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": REST_MANAGER,
				"allow": "Company",
				"for_value": OTHER_COMPANY,
			}
		)
		permission.flags.ignore_links = True
		permission.insert(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep
		self.addCleanup(
			lambda: (frappe.delete_doc("User Permission", permission.name, force=True), frappe.db.commit())
		)
		frappe.set_user(REST_MANAGER)
		with self.assertRaises(frappe.PermissionError):
			report()

	def test_another_outlets_manager_refused(self):
		frappe.set_user(REST_MANAGER)
		with self.assertRaises(frappe.PermissionError):
			report(RESTAURANT_B)
