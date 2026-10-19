"""Restaurant credit 0.14.0 (Task 4): a customer's open bills and a non-cash Settle on app.crenya.com
(crenya_pos_api/api/web_admin/credit.py `customer_open_invoices`, `settle_customer`), for both
products: the till's booking function and allocation, no cash (D5, D7), a reference always, one
Payment Entry per request id, the customer's lock shared with the till, the switch not required.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_web_admin_settle
"""

import json
import os
import threading
import uuid
from decimal import Decimal
from unittest import mock

import frappe
from frappe.utils import add_days, nowdate

from crenya_pos_api.api import web_admin
from crenya_pos_api.api.web_admin import credit as web_credit
from crenya_pos_api.sync import events
from crenya_pos_api.sync.events import customer_lock_key, lock_name
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import money
from crenya_pos_api.tests.test_integration_credit import make_customer
from crenya_pos_api.tests.test_integration_customer_payment import PaymentCase, outstanding
from crenya_pos_api.tests.test_integration_web_admin_credit import (
	ACCOUNTS,
	CASHIER,
	REST_MANAGER,
	REST_MANAGER_B,
	RESTAURANT_A,
	RESTAURANT_B,
	SETTLE_BANK,
	restore_restaurant_product,
	setup_restaurant_credit,
)

PAYMENT_ENTRY = "Payment Entry"
GOLDEN = os.path.join(os.path.dirname(__file__), "golden", "credit_allocations.json")


def golden(name):
	with open(GOLDEN) as f:
		return next(case for case in json.load(f) if case["name"] == name)


class SettleCase(PaymentCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.previous_product = setup_restaurant_credit(cls.profile.customer)

	@classmethod
	def tearDownClass(cls):
		restore_restaurant_product(cls.previous_product)
		super().tearDownClass()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def bills(self, customer, *totals):
		return [self.sell(customer, total) for total in totals]

	def settle(self, customer, amount, outlet=RESTAURANT_A, **over):
		args = {
			"outlet": outlet,
			"customer": customer,
			"mode_of_payment": SETTLE_BANK,
			"amount": amount,
			"invoices": None,
			"reference": "TRF-1001",
			"posting_date": nowdate(),
			"request_id": str(uuid.uuid4()),
		}
		args.update(over)
		return web_admin.settle_customer(**args)

	def entries_of(self, customer):
		return frappe.get_all(
			PAYMENT_ENTRY, filters={"party": customer, "docstatus": 1}, pluck="name", order_by="creation asc"
		)

	def references(self, name):
		entry = frappe.get_doc(PAYMENT_ENTRY, name)
		return [(row.reference_name, Decimal(money(row.allocated_amount))) for row in entry.references]


class TestOpenInvoices(SettleCase):
	def test_open_bills_with_the_outlets_non_cash_modes(self):
		customer = make_customer("webopen", limit=None)
		first, second = self.bills(customer, "6.000", "10.000")
		frappe.set_user(REST_MANAGER)
		answer = web_admin.customer_open_invoices(RESTAURANT_A, customer)
		self.assertEqual([row["name"] for row in answer["invoices"]], [first, second])
		self.assertEqual(
			(answer["outstanding"], answer["advance"], answer["more"]), ("16.000", "0.000", False)
		)
		# the bills of every outlet of the company (Q1): these were sold at the shop
		self.assertEqual({row["outlet"] for row in answer["invoices"]}, {self.profile.name})
		# Cash is on the outlet, never offered on the web (D5, D7)
		self.assertEqual(answer["modes"], [{"mode_of_payment": SETTLE_BANK, "type": "Bank"}])

	def test_open_bills_refuse_the_walk_in_and_other_outlets(self):
		frappe.set_user(REST_MANAGER)
		with self.assertRaises(frappe.PermissionError):
			web_admin.customer_open_invoices(RESTAURANT_A, self.profile.customer)
		with self.assertRaises(frappe.PermissionError):
			web_admin.customer_open_invoices(RESTAURANT_A, "No such customer")
		with self.assertRaises(frappe.PermissionError):
			web_admin.customer_open_invoices(RESTAURANT_B, make_customer("webopenb", limit=None))


class TestSettle(SettleCase):
	def test_settle_refuses_cash_mode(self):
		"""D5 and D7: a cash-type mode is refused by the server even when the outlet takes it."""
		customer = make_customer("webcash", limit=None)
		self.bills(customer, "10.000")
		frappe.set_user(REST_MANAGER)
		with self.assertRaises(frappe.ValidationError) as caught:
			self.settle(customer, "5.000", mode_of_payment=fixtures.CASH)
		self.assertIn("Cash is taken at the till", str(caught.exception))
		self.assertEqual(self.entries_of(customer), [])

	def test_settle_refuses_mode_not_on_outlet(self):
		customer = make_customer("webmode", limit=None)
		self.bills(customer, "10.000")
		other = "_Test Crenya Settle Other Bank"
		fixtures.ensure_mode_of_payment(other, "Bank", frappe.get_doc("Company", fixtures.COMPANY))
		frappe.db.commit()  # nosemgrep
		frappe.set_user(REST_MANAGER)
		for mode in (other, "No such mode", ""):
			with self.assertRaises(frappe.ValidationError, msg=mode):
				self.settle(customer, "5.000", mode_of_payment=mode)
		self.assertEqual(self.entries_of(customer), [])

	def test_settle_needs_reference(self):
		customer = make_customer("webref", limit=None)
		self.bills(customer, "10.000")
		frappe.set_user(REST_MANAGER)
		for reference in (None, "", "   ", "x" * 141):
			with self.assertRaises(frappe.ValidationError, msg=repr(reference)):
				self.settle(customer, "5.000", reference=reference)
		self.assertEqual(self.entries_of(customer), [])

	def test_settle_checks_amount_and_request_id(self):
		customer = make_customer("webamount", limit=None)
		self.bills(customer, "10.000")
		frappe.set_user(REST_MANAGER)
		for amount in ("0", "-1.000", "1.0001", "ten", ""):
			with self.assertRaises(frappe.ValidationError, msg=repr(amount)):
				self.settle(customer, amount)
		for request_id in (None, "", "not-a-uuid"):
			with self.assertRaises(frappe.ValidationError, msg=repr(request_id)):
				self.settle(customer, "1.000", request_id=request_id)
		self.assertEqual(self.entries_of(customer), [])

	def test_settle_ticks_and_advance(self):
		"""The golden case spec_15_3_ticks_2_and_3 through the web: bills 6, 10, 13; ticks the 2nd and
		the 3rd, pays 15: the 2nd cleared, 5 on the 3rd (8 left), the 1st untouched. Then more than
		the ticked bill owes stays an advance. By the outlet's manager (Q2)."""
		case = golden("spec_15_3_ticks_2_and_3")
		customer = make_customer("webticks", limit=None)
		names = dict(
			zip(
				[row["key"] for row in case["open"]],
				self.bills(customer, *[row["outstanding"] for row in case["open"]]),
				strict=True,
			)
		)
		frappe.set_user(REST_MANAGER)
		answer = self.settle(customer, case["amount"], invoices=[names[key] for key in case["ticked"]])
		expect = case["expect"]
		self.assertEqual(
			self.references(answer["payment_entry"]),
			[(names[row["key"]], Decimal(row["allocated"])) for row in expect["rows"]],
		)
		for row in expect["rows"]:
			self.assertEqual(outstanding(names[row["key"]]), Decimal(row["left"]))
		self.assertEqual(outstanding(names["B1"]), Decimal("6.000"))
		self.assertEqual(
			{key: answer[key] for key in ("allocated", "unallocated", "invoices", "skipped")},
			{
				"allocated": "15.000",
				"unallocated": expect["advance"],
				"invoices": [names[row["key"]] for row in expect["rows"]],
				"skipped": [],
			},
		)
		self.assertEqual((answer["outstanding"], answer["advance"]), ("14.000", "0.000"))
		# the entry: the web user's, named in its remarks and in a comment on the customer
		frappe.set_user("Administrator")
		entry = frappe.get_doc(PAYMENT_ENTRY, answer["payment_entry"])
		self.assertEqual(
			(entry.owner, entry.mode_of_payment, entry.reference_no), (REST_MANAGER, SETTLE_BANK, "TRF-1001")
		)
		self.assertTrue(
			entry.remarks.startswith(f"Settled on app.crenya.com by {REST_MANAGER}"), entry.remarks
		)
		self.assertTrue(
			frappe.get_all(
				"Comment",
				filters={
					"reference_doctype": "Customer",
					"reference_name": customer,
					"content": ["like", f"%{answer['payment_entry']}%app.crenya.com by {REST_MANAGER}%"],
				},
			)
		)
		# more than the ticked bill owes: the rest is an advance, never spread over the 1st
		frappe.set_user(REST_MANAGER)
		more = self.settle(customer, "9.000", invoices=[names["B3"]])
		self.assertEqual((more["allocated"], more["unallocated"]), ("8.000", "1.000"))
		self.assertEqual(outstanding(names["B1"]), Decimal("6.000"))
		self.assertEqual((more["outstanding"], more["advance"]), ("5.000", "1.000"))
		# a bill paid meanwhile is skipped, the money stays an advance
		gone = self.settle(customer, "2.000", invoices=[names["B2"]])
		self.assertEqual((gone["skipped"], gone["unallocated"]), ([names["B2"]], "2.000"))

	def test_settle_no_ticks_oldest_first(self):
		customer = make_customer("webnoticks", limit=None)
		first, second = self.bills(customer, "6.000", "10.000")
		frappe.set_user(ACCOUNTS)
		answer = self.settle(customer, "7.000", invoices=[])
		self.assertEqual(
			self.references(answer["payment_entry"]), [(first, Decimal("6.000")), (second, Decimal("1.000"))]
		)
		# a form post sends the list as JSON
		again = self.settle(customer, "1.000", invoices=json.dumps([second]))
		self.assertEqual(again["invoices"], [second])

	def test_settle_request_id_idempotent(self):
		"""Review Focus 3: the dialog's Settle sent twice with one request id books one entry and
		answers it again; the id of another customer's payment is refused."""
		customer = make_customer("webonce", limit=None)
		(bill,) = self.bills(customer, "10.000")
		request_id = str(uuid.uuid4())
		frappe.set_user(REST_MANAGER)
		first = self.settle(customer, "4.000", request_id=request_id)
		second = self.settle(customer, "4.000", request_id=request_id.upper())
		self.assertEqual(first, second)
		self.assertEqual(len(self.entries_of(customer)), 1)
		self.assertEqual(
			frappe.db.get_value(PAYMENT_ENTRY, first["payment_entry"], "crenya_local_id"), request_id
		)
		self.assertEqual(outstanding(bill), Decimal("6.000"))
		frappe.set_user("Administrator")
		other = make_customer("webonceother", limit=None)
		self.bills(other, "3.000")
		frappe.set_user(REST_MANAGER)
		with self.assertRaises(frappe.PermissionError):
			self.settle(other, "1.000", request_id=request_id)
		self.assertEqual(self.entries_of(other), [])

	def test_settle_when_outlet_credit_off(self):
		"""Review Focus 1: switched off, a customer who still owes is settled on the web."""
		saved = frappe.db.get_value("POS Profile", RESTAURANT_A, "crenya_credit_sales_enabled")
		frappe.db.set_value("POS Profile", RESTAURANT_A, "crenya_credit_sales_enabled", 0)
		frappe.clear_document_cache("POS Profile", RESTAURANT_A)
		frappe.db.commit()  # nosemgrep
		try:
			customer = make_customer("weboff", limit=None)
			(bill,) = self.bills(customer, "10.000")
			frappe.set_user(REST_MANAGER)
			self.assertEqual(len(web_admin.customer_open_invoices(RESTAURANT_A, customer)["invoices"]), 1)
			answer = self.settle(customer, "10.000", invoices=[bill])
			self.assertEqual((answer["allocated"], answer["outstanding"]), ("10.000", "0.000"))
			self.assertEqual(outstanding(bill), Decimal("0.000"))
		finally:
			frappe.set_user("Administrator")
			frappe.db.set_value("POS Profile", RESTAURANT_A, "crenya_credit_sales_enabled", saved or 0)
			frappe.clear_document_cache("POS Profile", RESTAURANT_A)
			frappe.db.commit()  # nosemgrep

	def test_web_settle_takes_customer_lock(self):
		"""The customer's lock held elsewhere (a till's receipt of the customer): the web answers
		"try again" and books nothing."""
		customer = make_customer("weblock", limit=None)
		self.bills(customer, "10.000")
		site, sites_path = frappe.local.site, frappe.local.sites_path
		name = lock_name(customer_lock_key(customer))
		held, release, problems = threading.Event(), threading.Event(), []

		def holder():
			frappe.init(site=site, sites_path=sites_path)
			frappe.connect()
			try:
				frappe.db.sql("select get_lock(%s, 5)", (name,))
				held.set()
				release.wait(60)
				frappe.db.sql("select release_lock(%s)", (name,))
			except Exception as exc:  # the main thread reports it
				problems.append(exc)
				held.set()
			finally:
				frappe.destroy()

		thread = threading.Thread(target=holder)
		thread.start()
		try:
			self.assertTrue(held.wait(30))
			self.assertEqual(problems, [])
			request_id = str(uuid.uuid4())
			frappe.set_user(REST_MANAGER)
			with mock.patch.object(events, "LOCK_WAIT_SECONDS", 1):
				with self.assertRaises(frappe.ValidationError) as caught:
					self.settle(customer, "5.000", request_id=request_id)
			self.assertIn("try again", str(caught.exception))
		finally:
			release.set()
			thread.join(60)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.get_all(PAYMENT_ENTRY, filters={"crenya_local_id": request_id}), [])
		# the lock gone, the same request books
		frappe.set_user(REST_MANAGER)
		self.assertEqual(self.settle(customer, "5.000", request_id=request_id)["allocated"], "5.000")

	def test_settle_needs_web_session(self):
		customer = make_customer("webkey", limit=None)
		self.bills(customer, "10.000")
		frappe.set_user(REST_MANAGER)
		with fixtures.signed_with_key():
			with self.assertRaises(frappe.PermissionError):
				self.settle(customer, "5.000")
			with self.assertRaises(frappe.PermissionError):
				web_admin.customer_open_invoices(RESTAURANT_A, customer)
		self.assertEqual(self.entries_of(customer), [])

	def test_settle_future_date_refused(self):
		customer = make_customer("webdate", limit=None)
		self.bills(customer, "10.000")
		frappe.set_user(REST_MANAGER)
		with self.assertRaises(frappe.ValidationError):
			self.settle(customer, "5.000", posting_date=add_days(nowdate(), 1))
		with self.assertRaises(frappe.ValidationError):
			self.settle(customer, "5.000", posting_date="19/10/2026")
		with mock.patch.object(
			web_credit, "frozen_till", return_value=frappe.utils.getdate(add_days(nowdate(), -3))
		):
			with self.assertRaises(frappe.ValidationError):
				self.settle(customer, "5.000", posting_date=add_days(nowdate(), -3))
			answer = self.settle(customer, "5.000", posting_date=add_days(nowdate(), -2))
		self.assertEqual(
			str(frappe.db.get_value(PAYMENT_ENTRY, answer["payment_entry"], "posting_date")),
			add_days(nowdate(), -2),
		)
		self.assertEqual(len(self.entries_of(customer)), 1)

	def test_who_may_settle(self):
		"""Q2: the outlet's manager and accountants; not another outlet's manager, not a cashier."""
		customer = make_customer("webwho", limit=None)
		self.bills(customer, "10.000")
		for user in (REST_MANAGER_B, CASHIER):
			frappe.set_user(user)
			with self.assertRaises(frappe.PermissionError, msg=user):
				self.settle(customer, "1.000")
		frappe.set_user(ACCOUNTS)
		self.assertEqual(self.settle(customer, "1.000", outlet=RESTAURANT_B)["allocated"], "1.000")
		# and on a shop, by the shop's own rule (the shop takes no bank mode: refused for the mode)
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.ValidationError):
			self.settle(customer, "1.000", outlet=self.profile.name)
