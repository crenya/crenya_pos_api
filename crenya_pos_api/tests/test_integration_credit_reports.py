"""House Account Phase 2 (0.16.0, Task 6): the Outstanding report with ageing
(crenya_pos_api/api/web_admin/credit_reports.py `credit_outstanding`) on a restaurant outlet:
ERPNext's own Accounts Receivable and customer balance as the references, OMR's 3 decimals, the
company as the boundary and the outlet's manager as the reader.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_credit_reports
"""

import uuid
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
	REST_MANAGER_B,
	RESTAURANT_A,
	RESTAURANT_B,
	SETTLE_BANK,
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

	group = REPORT_GROUP

	def customer(self, label, limit=None):
		"""A customer of the outlet's report group."""
		name = make_customer(label, limit=limit)
		frappe.db.set_value("Customer", name, "customer_group", self.group)
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


class TestOutstandingFixRound1(OutstandingCase):
	def use_group(self, group):
		self.group = group
		profile = frappe.get_doc("POS Profile", RESTAURANT_A)
		profile.set("customer_groups", [{"customer_group": group}])
		profile.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep

	def test_session_survives_the_elevated_report(self):
		"""The report runs AR as Administrator; sid, session data and form dict come back identical, also
		when AR raises (a cookie request would otherwise write wiped data to the real sid)."""
		customer = self.customer("sess")
		self.enable(customer)
		frappe.set_user(REST_MANAGER)
		data = frappe._dict(user=REST_MANAGER, csrf_token="csrf-token-1")
		form = frappe._dict(cmd="x")
		frappe.local.session.sid = "fake-sid-1"
		frappe.local.session.data = data
		frappe.local.form_dict = form

		def check():
			self.assertEqual(frappe.session.user, REST_MANAGER)
			self.assertEqual(frappe.session.sid, "fake-sid-1")
			self.assertIs(frappe.local.session.data, data)
			self.assertEqual(dict(data), {"user": REST_MANAGER, "csrf_token": "csrf-token-1"})
			self.assertIs(frappe.local.form_dict, form)
			self.assertEqual(dict(form), {"cmd": "x"})

		self.assertIn(customer, rows_by_customer(report()))
		check()
		with mock.patch(
			"erpnext.accounts.report.accounts_receivable.accounts_receivable.execute",
			side_effect=RuntimeError("boom"),
		):
			with self.assertRaises(RuntimeError):
				report()
		check()

	def test_enabled_disabled_customer_who_owes_is_listed(self):
		"""The page keeps a disabled customer who still owes; so does the report (no sale in range)."""
		owing = self.customer("dis_owes")
		quiet = self.customer("dis_quiet")
		for name in (owing, quiet):
			self.enable(name)
		self.ledger(owing, [("invoice", 40, "20.000")])
		frappe.db.set_value("Customer", owing, "disabled", 1)
		frappe.db.set_value("Customer", quiet, "disabled", 1)
		frappe.db.commit()  # nosemgrep
		rows = rows_by_customer(self.as_manager_report(from_date=add_days(nowdate(), -3)))
		self.assertEqual(rows[owing]["closing"], "20.000")
		self.assertEqual(rows[owing]["opening"], "20.000")
		self.assertNotIn(quiet, rows)

	def test_on_account_customer_found_among_more_than_cap_cash_customers(self):
		"""Fully paid named cash sales do not use the cap; the one customer on account is listed."""
		# a group of its own: the shared one holds the on-account customers of the other tests
		group = f"_Test Crenya Report Group Cap {uuid.uuid4().hex[:6]}"
		if not frappe.db.exists("Customer Group", group):
			frappe.get_doc(
				{
					"doctype": "Customer Group",
					"customer_group_name": group,
					"parent_customer_group": "All Customer Groups",
					"is_group": 0,
				}
			).insert(ignore_permissions=True)
		profile = frappe.get_doc("POS Profile", RESTAURANT_A)
		profile.set("customer_groups", [{"customer_group": group}])
		profile.save(ignore_permissions=True)
		self.addCleanup(self.use_group, REPORT_GROUP)
		self.group = group
		cash = [self.customer(f"cash{n}") for n in range(4)]
		for name in cash:
			self.post(self.on_account_sale(name, "5.000", cash="5.000"))
		owing = self.customer("zzowing")
		self.bills(owing, "3.000")
		with mock.patch.object(credit_reports, "MAX_REPORT_ROWS", 3):
			frappe.set_user(REST_MANAGER)
			answer = report()
		rows = rows_by_customer(answer)
		self.assertIn(owing, rows)
		self.assertEqual(rows[owing]["closing"], "3.000")
		self.assertFalse(set(cash) & set(rows))
		self.assertIs(answer["more"], False)

	def test_more_only_when_the_on_account_set_exceeds_the_cap(self):
		names = [self.customer(f"acct{n}") for n in range(3)]
		for name in names:
			self.bills(name, "1.000")
		with mock.patch.object(credit_reports, "MAX_REPORT_ROWS", 2):
			frappe.set_user(REST_MANAGER)
			answer = report()
		self.assertIs(answer["more"], True)
		self.assertEqual(len(answer["rows"]), 2)

	def test_oldest_unpaid_is_from_sales_invoices_only(self):
		customer = self.customer("oldest")
		self.enable(customer)
		self.ledger(customer, [("journal", 90, "4.000"), ("invoice", 20, "2.000")])
		row = self.row(customer)
		self.assertEqual(row["oldest_unpaid"], add_days(nowdate(), -20))


def statement(outlet=RESTAURANT_A, customer=None, **args):
	args.setdefault("from_date", add_days(nowdate(), -30))
	args.setdefault("to_date", nowdate())
	return web_admin.credit_statement(outlet, customer, **args)


class TestStatement(OutstandingCase):
	def stmt(self, customer, **args):
		frappe.set_user(REST_MANAGER)
		return statement(customer=customer, **args)

	def test_statement_closing_equals_outstanding(self):
		from erpnext.selling.doctype.customer.customer import get_customer_outstanding

		customer = self.customer("stmt", limit=None)
		self.enable(customer)
		self.ledger(customer, [("invoice", 60, "8.000")])
		self.ledger(
			customer, [("invoice", 5, "12.000"), ("payment", 4, "5.000"), ("credit_note", 3, "1.000")]
		)
		answer = self.stmt(customer, from_date=add_days(nowdate(), -10))
		self.assertEqual(answer["opening"], "8.000")
		self.assertEqual([r["balance"] for r in answer["rows"]], ["20.000", "15.000", "14.000"])
		self.assertEqual([r["description"] for r in answer["rows"]], ["sale", "payment", "credit_note"])
		self.assertEqual(answer["closing"], "14.000")
		frappe.set_user("Administrator")
		self.assertEqual(answer["closing"], f"{get_customer_outstanding(customer, fixtures.COMPANY):.3f}")
		self.assertEqual((answer["company"], answer["currency"]["precision"]), (fixtures.COMPANY, 3))

	def test_opening_zero_before_first_entry(self):
		customer = self.customer("stmt0", limit=None)
		self.ledger(customer, [("invoice", 5, "4.000")])
		answer = self.stmt(customer)
		self.assertEqual((answer["opening"], answer["closing"]), ("0.000", "4.000"))
		empty = self.stmt(customer, from_date=add_days(nowdate(), -90), to_date=add_days(nowdate(), -60))
		self.assertEqual((empty["rows"], empty["opening"], empty["closing"]), ([], "0.000", "0.000"))

	def test_cash_pos_sale_shows_debit_and_credit_same_voucher(self):
		"""A11: a till sale part cash, part on account posts receivable Dr 100 and Cr 40 in one voucher."""
		customer = self.customer("stmtpos", limit=None)
		self.enable(customer)
		sale = self.on_account_sale(customer, "100.000", cash="40.000")
		name = self.post(sale)["name"]
		answer = self.stmt(customer)
		lines = [r for r in answer["rows"] if r["voucher_no"] == name]
		self.assertEqual(len(lines), 1)
		self.assertEqual(
			(lines[0]["debit"], lines[0]["credit"], lines[0]["balance"]), ("100.000", "40.000", "60.000")
		)
		self.assertEqual(lines[0]["bill_no"], sale["offline_number"])
		self.assertEqual(lines[0]["description"], "sale")

	def test_cancelled_entries_excluded(self):
		customer = self.customer("stmtcxl", limit=None)
		names = self.ledger(customer, [("invoice", 5, "4.000"), ("invoice", 4, "3.000")])
		frappe.set_user("Administrator")
		frappe.get_doc("Sales Invoice", names[0]).cancel()
		frappe.db.commit()  # nosemgrep
		answer = self.stmt(customer)
		self.assertEqual([r["voucher_no"] for r in answer["rows"]], [names[1]])
		self.assertEqual(answer["closing"], "3.000")

	def test_journal_opening_entry_appears(self):
		customer = self.customer("stmtjv", limit=None)
		self.ledger(customer, [("journal", 5, "9.000")])
		answer = self.stmt(customer)
		self.assertEqual([(r["description"], r["debit"]) for r in answer["rows"]], [("journal", "9.000")])
		self.assertEqual(answer["closing"], "9.000")

	def test_statement_of_other_outlet_customer_refused(self):
		customer = self.customer("stmtx", limit=None)
		frappe.set_user(REST_MANAGER)
		with self.assertRaises(frappe.PermissionError):
			statement(customer=self.profile.customer)
		with self.assertRaises(frappe.PermissionError):
			statement(customer="No such customer")
		with mock.patch.object(credit_reports, "excluded_customers", return_value={customer}):
			with self.assertRaises(frappe.PermissionError):
				statement(customer=customer)
		frappe.set_user(CASHIER)
		with self.assertRaises(frappe.PermissionError):
			statement(customer=customer)

	def test_dates_required(self):
		customer = self.customer("stmtd", limit=None)
		frappe.set_user(REST_MANAGER)
		for args in (
			{"from_date": None},
			{"to_date": ""},
			{"from_date": "2026-02-02", "to_date": "2026-02-01"},
			{"from_date": "02/02/2026"},
		):
			with self.assertRaises(frappe.ValidationError):
				statement(customer=customer, **args)


def transactions(outlet=RESTAURANT_A, **args):
	args.setdefault("from_date", add_days(nowdate(), -30))
	args.setdefault("to_date", nowdate())
	return web_admin.credit_transactions(outlet, **args)


class TestTransactions(OutstandingCase):
	"""Task 8: the House Account transactions by day and shift."""

	def person(self, label):
		from crenya_pos_api.sync.context import POS_USER_ROLE
		from crenya_pos_api.tests.test_integration_device_users import ensure_user as ensure_person

		return ensure_person(f"tx.{label}@example.com", [POS_USER_ROLE, "Sales User"])

	def txn(self, outlet=RESTAURANT_A, **args):
		"""The report as the outlet's manager. The test till sells under its own POS Profile, so the
		sales this test posted and the till's device are moved to the restaurant outlet first."""
		for name in getattr(self, "posted", []):
			frappe.db.set_value("Sales Invoice", name, "pos_profile", RESTAURANT_A)
		frappe.db.set_value("Crenya POS Device", self.device_id, "pos_profile", RESTAURANT_A)
		self.addCleanup(
			frappe.db.set_value, "Crenya POS Device", self.device_id, "pos_profile", self.profile.name
		)
		frappe.set_user(REST_MANAGER)
		return transactions(outlet, **args)

	def till_sale(self, customer, total, cash="0.000", shift=None, cashier=None):
		payload = self.on_account_sale(customer, total, cash=cash)
		if shift:
			payload["shift_local_id"] = shift
		if cashier:
			payload["cashier"] = cashier
		sold = self.post(payload)
		self.remember(sold["name"])
		return payload, sold

	def remember(self, name):
		self.__dict__.setdefault("posted", []).append(name)

	def till_payment(self, customer, amount, shift=None, cashier=None):
		over = {}
		if shift:
			over["shift_local_id"] = shift
		if cashier:
			over["cashier"] = cashier
		self.assertOk(self.pay(self.payment(customer, amount, **over)))

	def listed(self):
		customer = self.customer("tx", limit=None)
		self.enable(customer)
		return customer

	def test_sale_and_collection_under_shift_and_cashier(self):
		"""Spec 10 step 6c: a till sale on account and a till payment in shift S1 by cashier C."""
		customer, cashier, shift = self.listed(), self.person("c1"), f"S1-{uuid.uuid4().hex[:6]}"
		shift_event = self.shift_payload(local_id=shift, shift_number="SH-TX-1", cashier=cashier)
		self.assertOk(self.push_one(self.event(shift_event, "Crenya POS Shift")))
		self.till_sale(customer, "13.000", shift=shift, cashier=cashier)
		self.till_payment(customer, "5.000", shift=shift, cashier=cashier)
		answer = self.txn(shift=shift)
		self.assertEqual(
			# ordered by posting date, shift number and voucher number (ACC-PAY before ACC-SINV)
			sorted((r["kind"], r["amount"], r["source"]) for r in answer["rows"]),
			[("collection", "5.000", "till"), ("sale", "13.000", "till")],
		)
		for row in answer["rows"]:
			self.assertEqual(
				(row["shift_id"], row["shift_number"], row["cashier"], row["customer"]),
				(shift, "SH-TX-1", cashier, customer),
			)
			self.assertTrue(row["cashier_name"])
		self.assertEqual(
			answer["shifts"],
			[
				{
					"shift_id": shift,
					"shift_number": "SH-TX-1",
					"cashier": cashier,
					"cashier_name": answer["rows"][0]["cashier_name"],
					"sales": "13.000",
					"collections": "5.000",
				}
			],
		)
		self.assertEqual(
			answer["days"], [{"posting_date": nowdate(), "sales": "13.000", "collections": "5.000"}]
		)
		self.assertEqual(answer["totals"], {"sales": "13.000", "collections": "5.000"})
		self.assertEqual((answer["outlet"], answer["currency"]["precision"]), (RESTAURANT_A, 3))

	def test_shift_not_on_server_has_null_number(self):
		customer, shift = self.listed(), f"S-{uuid.uuid4().hex[:6]}"
		self.till_sale(customer, "4.000", shift=shift)
		row = self.txn(shift=shift)["rows"][0]
		self.assertEqual((row["shift_id"], row["shift_number"]), (shift, None))

	def test_split_sale_only_house_account_part(self):
		customer, shift = self.listed(), f"S-{uuid.uuid4().hex[:6]}"
		self.till_sale(customer, "18.000", cash="5.000", shift=shift)
		rows = self.txn(shift=shift)["rows"]
		self.assertEqual([(r["kind"], r["amount"]) for r in rows], [("sale", "13.000")])

	def test_cash_sale_not_listed(self):
		customer, shift = self.listed(), f"S-{uuid.uuid4().hex[:6]}"
		self.till_sale(customer, "10.000", cash="10.000", shift=shift)
		self.assertEqual(self.txn(shift=shift)["rows"], [])

	def test_cancel_negative(self):
		customer, shift = self.listed(), f"S-{uuid.uuid4().hex[:6]}"
		sale, _sold = self.till_sale(customer, "100.000", cash="40.000", shift=shift)
		ret = self.return_payload(sale, [(fixtures.MILK, "-1", "50.000", 1)], credit_offset="50.000")
		ret["payments"] = []
		ret["shift_local_id"] = shift
		self.remember(self.assertOk(self.push_one(self.event(ret)))["name"])
		answer = self.txn(shift=shift)
		self.assertEqual(
			[(r["kind"], r["amount"]) for r in answer["rows"]], [("sale", "60.000"), ("cancel", "-50.000")]
		)
		self.assertEqual(answer["totals"], {"sales": "10.000", "collections": "0.000"})

	def test_web_settlement_listed_as_web_desk(self):
		customer = self.listed()
		self.sell(customer, "10.000")
		frappe.set_user(REST_MANAGER)
		self.settle(customer, "4.000")
		answer = self.txn()
		mine = [r for r in answer["rows"] if r["customer"] == customer and r["kind"] == "collection"]
		self.assertEqual(len(mine), 1)
		self.assertEqual(
			(
				mine[0]["amount"],
				mine[0]["source"],
				mine[0]["shift_id"],
				mine[0]["shift_number"],
				mine[0]["cashier"],
			),
			("4.000", "web_desk", None, None, None),
		)
		self.assertEqual(mine[0]["mode_of_payment"], SETTLE_BANK)

	def test_old_payment_without_shift_shows_dash(self):
		customer = self.listed()
		self.ledger(customer, [("payment", 1, "2.000")])
		mine = [r for r in self.txn()["rows"] if r["customer"] == customer]
		self.assertEqual(
			[(r["kind"], r["source"], r["shift_id"], r["amount"]) for r in mine],
			[("collection", "web_desk", None, "2.000")],
		)

	def test_other_outlet_sales_excluded(self):
		customer, shift = self.listed(), f"S-{uuid.uuid4().hex[:6]}"
		_sale, sold = self.till_sale(customer, "6.000", shift=shift)
		self.assertEqual(len(self.txn(shift=shift)["rows"]), 1)
		frappe.db.set_value("Sales Invoice", sold["name"], "pos_profile", RESTAURANT_B)
		self.posted.remove(sold["name"])
		self.assertEqual(self.txn(shift=shift)["rows"], [])
		# the same sale is the other outlet's (its own manager reads it)
		frappe.set_user(REST_MANAGER_B)
		self.assertEqual(
			[r["voucher_no"] for r in transactions(RESTAURANT_B, shift=shift)["rows"]], [sold["name"]]
		)

	def test_other_customers_desk_payment_not_listed(self):
		outsider = make_customer("txout", limit=None)
		self.ledger(outsider, [("payment", 1, "3.000")])
		self.assertEqual([r for r in self.txn()["rows"] if r["customer"] == outsider], [])

	def test_shift_filter(self):
		customer, one, two = self.listed(), f"S1-{uuid.uuid4().hex[:6]}", f"S2-{uuid.uuid4().hex[:6]}"
		self.till_sale(customer, "3.000", shift=one)
		self.till_sale(customer, "4.000", shift=two)
		self.till_payment(customer, "1.000", shift=two)
		self.ledger(customer, [("payment", 0, "2.000")])
		rows = self.txn(shift=two)["rows"]
		self.assertEqual(
			sorted((r["kind"], r["amount"]) for r in rows), [("collection", "1.000"), ("sale", "4.000")]
		)

	def test_cashier_filter(self):
		customer, first, second = self.listed(), self.person("f1"), self.person("f2")
		shift = f"S-{uuid.uuid4().hex[:6]}"
		self.till_sale(customer, "3.000", shift=shift, cashier=first)
		self.till_sale(customer, "7.000", shift=shift, cashier=second)
		self.till_payment(customer, "2.000", shift=shift, cashier=second)
		self.ledger(customer, [("payment", 0, "9.000")])
		rows = self.txn(cashier=second, shift=shift)["rows"]
		self.assertEqual(
			sorted((r["kind"], r["amount"]) for r in rows), [("collection", "2.000"), ("sale", "7.000")]
		)
		answer = self.txn(cashier=second)
		# a web_desk row has no cashier: it drops out under a filter
		self.assertNotIn("web_desk", {r["source"] for r in answer["rows"]})

	def test_range_over_92_days_refused(self):
		frappe.set_user(REST_MANAGER)
		today = nowdate()
		with self.assertRaises(frappe.ValidationError):
			transactions(from_date=add_days(today, -92), to_date=today)
		transactions(from_date=add_days(today, -91), to_date=today)
		for args in (
			{"from_date": None},
			{"to_date": ""},
			{"from_date": today, "to_date": add_days(today, -1)},
		):
			with self.assertRaises(frappe.ValidationError):
				transactions(**args)

	def test_outlet_manager_reads(self):
		frappe.set_user(REST_MANAGER)
		self.assertIn("rows", transactions())
		frappe.set_user(CASHIER)
		with self.assertRaises(frappe.PermissionError):
			transactions()
		frappe.set_user(REST_MANAGER)
		with self.assertRaises(frappe.PermissionError):
			transactions(RESTAURANT_B)
