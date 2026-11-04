"""M2 (retail P1 slice 1 review, 0.16.3): an outlet manager without a System Manager or Accounts
Manager role lists, reads and settles on the credit page only the customers on the House Account of
the outlet's company (the facility on, or sold on account at the company's tills), never the
company's desk (B2B) debtors; an accountant reads and settles every debtor
(crenya_pos_api/api/web_admin/credit.py `house_account_refusal`, the rule the House Account reports
share).

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_web_admin_desk_debtors
"""

import uuid
from unittest import mock

import frappe

from crenya_pos_api.api import web_admin
from crenya_pos_api.api.web_admin import credit as web_credit
from crenya_pos_api.sync.credit import CREDIT_ENABLED_FIELD
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.test_integration_credit import make_customer
from crenya_pos_api.tests.test_integration_web_admin_credit import (
	ACCOUNTS,
	REST_MANAGER,
	RESTAURANT_A,
	SETTLE_BANK,
	rows_by_customer,
)
from crenya_pos_api.tests.test_integration_web_admin_settle import SettleCase
from crenya_pos_api.tests.web_admin_case import MANAGER


class TestDeskDebtors(SettleCase):
	def setUp(self):
		super().setUp()
		# a long-lived test site keeps every test's customers: list them all
		for name in ("MAX_CREDIT_ROWS", "MAX_CANDIDATES"):
			patch = mock.patch.object(web_credit, name, 100000)
			patch.start()
			self.addCleanup(patch.stop)

	def desk_debtor(self, label, enabled=False):
		"""A customer whose only bill is a desk Sales Invoice of 9.000 (no till, no POS Profile)."""
		customer = make_customer(label, limit=None)
		frappe.set_user("Administrator")
		fixtures.house_account_ledger(fixtures.COMPANY, customer, [("invoice", 1, "9.000")])
		if enabled:
			frappe.db.set_value("Customer", customer, CREDIT_ENABLED_FIELD, 1)
		frappe.db.commit()  # nosemgrep
		return customer

	def listed(self, user, outlet=RESTAURANT_A):
		frappe.set_user(user)
		return rows_by_customer(web_admin.credit_customers(outlet))

	def settle_as(self, user, customer, amount="1.000"):
		frappe.set_user(user)
		return self.settle(customer, amount, request_id=str(uuid.uuid4()))

	def test_manager_neither_sees_nor_settles_a_desk_debtor(self):
		debtor = self.desk_debtor("deskonly")
		self.assertNotIn(debtor, self.listed(REST_MANAGER))
		frappe.set_user(REST_MANAGER)
		for call in (
			lambda: web_admin.customer_open_invoices(RESTAURANT_A, debtor),
			lambda: web_admin.customer_payments(RESTAURANT_A, debtor),
			lambda: self.settle(debtor, "9.000"),
		):
			with self.assertRaises(frappe.PermissionError):
				call()
		self.assertEqual(self.entries_of(debtor), [])
		# an accountant's settlement of it: its receipt is not the manager's either
		receipt = self.settle_as(ACCOUNTS, debtor)["payment_entry"]
		frappe.set_user(REST_MANAGER)
		with self.assertRaises(frappe.PermissionError):
			web_admin.payment_receipt_pdf(RESTAURANT_A, receipt)
		# nor on a shop's Credit page (a shop's manager, no accounts role)
		self.assertNotIn(debtor, self.listed(MANAGER, self.profile.name))
		frappe.set_user(MANAGER)
		with self.assertRaises(frappe.PermissionError):
			web_admin.customer_open_invoices(self.profile.name, debtor)

	def test_manager_sees_and_settles_a_house_account_member(self):
		# sold on account at a till of the company
		sold = make_customer("deskmember", limit=None)
		self.bills(sold, "10.000")
		# the facility on, its only bill a desk one
		enabled = self.desk_debtor("deskenabled", enabled=True)
		rows = self.listed(REST_MANAGER)
		self.assertEqual(rows[sold]["outstanding"], "10.000")
		self.assertEqual(rows[enabled]["outstanding"], "9.000")
		for customer, owed in ((sold, "10.000"), (enabled, "9.000")):
			frappe.set_user(REST_MANAGER)
			answer = web_admin.customer_open_invoices(RESTAURANT_A, customer)
			self.assertEqual(answer["outstanding"], owed)
			settled = self.settle_as(REST_MANAGER, customer)
			self.assertEqual(settled["allocated"], "1.000")
			frappe.set_user(REST_MANAGER)
			self.assertTrue(web_admin.customer_payments(RESTAURANT_A, customer))
		# the shop's manager lists the till's credit customer too
		self.assertIn(sold, self.listed(MANAGER, self.profile.name))

	def test_a_limit_alone_is_no_membership(self):
		"""A shop customer with a credit limit and no till sale on account is listed for an accountant,
		not for the shop's manager (a desk customer may carry a limit for the company too)."""
		idle = make_customer("desklimit", limit=250)
		self.assertNotIn(idle, self.listed(MANAGER, self.profile.name))
		row = self.listed(ACCOUNTS, self.profile.name)[idle]
		self.assertEqual((row["credit_limit"], row["outstanding"]), ("250.000", "0.000"))

	def test_accounts_manager_sees_and_settles_every_debtor(self):
		debtor = self.desk_debtor("deskacct")
		for user in (ACCOUNTS, "Administrator"):
			self.assertEqual(self.listed(user)[debtor]["outstanding"], "9.000", msg=user)
		frappe.set_user(ACCOUNTS)
		self.assertEqual(web_admin.customer_open_invoices(RESTAURANT_A, debtor)["outstanding"], "9.000")
		settled = self.settle_as(ACCOUNTS, debtor, "9.000")
		self.assertEqual(settled["allocated"], "9.000")
		frappe.set_user(ACCOUNTS)
		self.assertEqual(len(web_admin.customer_payments(RESTAURANT_A, debtor)), 1)
		self.assertEqual(
			frappe.db.get_value("Payment Entry", settled["payment_entry"], "mode_of_payment"), SETTLE_BANK
		)
