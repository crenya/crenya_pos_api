"""Retail expenses on the server (0.13.0): the retail till pushes POS Expenses through the built-in
handler of crenya_pos_api, they book as paid Purchase Invoices, the pulls and the bootstrap follow
the outlet's switch, and a restaurant till (crenya_restaurant 0.15.6, whose hook entries this app
ignores) goes through the same handler with its own wording and approvers.

Every call into the booking engine passes the product explicitly (book and rebook swallow a missing
product into "Not booked"): the cases assert it. The cases need erptrue_app's POS Expense (CI's
`expenses` job installs it); without it they are skipped and `TestRetailExpensesOff` runs.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_expenses_retail
"""

import types
import unittest
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import frappe
from frappe.utils import add_to_date, now_datetime, nowdate

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.expenses import booking
from crenya_pos_api.expenses import sync as expense_sync
from crenya_pos_api.expenses.product import RESTAURANT, RETAIL
from crenya_pos_api.sync.hashing import payload_hash
from crenya_pos_api.tests import fixtures as core
from crenya_pos_api.tests.stubs import restaurant_constants, stub_modules
from crenya_pos_api.tests.sync_case import SyncCase, money, new_id
from crenya_pos_api.tests.test_integration_expenses_booking import (
	CASH_SUPPLIER,
	GAS,
	VAT_TEMPLATE,
	VEG_SHOP,
	ExpenseCase,
	cash_account,
)

AGGREGATE = "POS Expense"
MANAGER = "_test_crenya_retail_exp_manager@example.com"
CASHIER = "_test_crenya_retail_exp_cashier@example.com"


def utc(moment):
	return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_user(email, roles):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	user.enabled = 1
	user.set("roles", [{"role": role} for role in roles])
	user.save(ignore_permissions=True)
	return email


class RetailExpenseCase(ExpenseCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.manager = ensure_user(MANAGER, ["Crenya POS User", "Crenya POS Manager"])
		cls.cashier = ensure_user(CASHIER, ["Crenya POS User"])
		frappe.db.commit()  # nosemgrep

	def payload(self, local_id=None, **over):
		p = {
			"local_id": local_id or f"EXP-{uuid.uuid4().hex[:10]}",
			"pos_profile": self.profile.name,
			"company": core.COMPANY,
			"shift_local_id": over.pop("shift_local_id", None) or new_id(),
			"business_date": nowdate(),
			"user": "Administrator",
			"approved_by": None,
			"edited_at": utc(now_datetime()),
			"supplier": None,
			"payee_name": "Gas man",
			"bill_no": None,
			"bill_date": nowdate(),
			"mode_of_payment": core.CASH,
			"currency": core.CURRENCY,
			"lines": [{"category": GAS, "description": "Cylinder", "amount": "7.000"}],
			"tax_template": VAT_TEMPLATE,
			"taxes": [{"account_head": core.VAT_ACCOUNT, "rate": "5", "amount": "0.350"}],
			"net": "7.000",
			"tax": "0.350",
			"total": "7.350",
			"notes": None,
			"reason": None,
		}
		p.update(over)
		return p

	def expense_event(self, payload, operation="submit", event_id=None):
		return {
			"event_id": event_id or new_id(),
			"aggregate_type": AGGREGATE,
			"operation": operation,
			"local_id": payload["local_id"],
			"sequence_no": 1,
			"payload_hash": payload_hash(payload),
			"payload": payload,
		}

	def send(self, payload, operation="submit", event_id=None, ok=True, device_id=None):
		result = sync_api.push_batch(
			device_id=device_id or self.device_id, events=[self.expense_event(payload, operation, event_id)]
		)[0]
		if ok:
			self.assertIn(result["status"], ("ok", "duplicate"), msg=result.get("error"))
		return result

	def expense(self, local_id):
		name = frappe.db.get_value(AGGREGATE, {"local_id": local_id}, "name")
		return frappe.get_doc(AGGREGATE, name) if name else None

	def push_shift(self, shift_id, closed_at):
		payload = self.shift_payload(
			local_id=shift_id, opened_at=utc(add_to_date(closed_at, hours=-8)), closed_at=utc(closed_at)
		)
		result = self.push_one(self.event(payload, "Crenya POS Shift"))
		self.assertOk(result)


class TestRetailExpenses(RetailExpenseCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		if not booking.expenses_supported():
			raise unittest.SkipTest("erptrue_app's POS Expense with the till fields is not installed")
		super().setUpClass()

	def test_expense_submit_idempotent_by_event_and_local_id(self):
		p = self.payload()
		event_id = new_id()
		with mock.patch.object(booking, "book", wraps=booking.book) as book:
			first = self.send(p, event_id=event_id)
		# the engine is always told the product: the device's, a retail till's
		book.assert_called_once()
		self.assertIs(book.call_args.kwargs["product"], RETAIL)
		self.assertEqual(first["status"], "ok")
		self.assertEqual(first["doctype"], AGGREGATE)
		self.assertEqual(first["expense"]["status"], "Booked")
		# the answer was lost: the same event again, then a new event of the same expense
		again = self.send(p, event_id=event_id)
		self.assertEqual(again["status"], "duplicate")
		third = self.send(p)
		self.assertEqual({first["name"], again["name"], third["name"]}, {first["name"]})
		self.assertEqual(frappe.db.count(AGGREGATE, {"local_id": p["local_id"]}), 1)
		doc = self.expense(p["local_id"])
		self.assertEqual(
			frappe.db.count(
				"Purchase Invoice", {"remarks": ["like", f"%POS Expense {doc.name}%"], "docstatus": 1}
			),
			1,
		)
		self.assertEqual(first["expense"]["purchase_invoice"], doc.purchase_invoice)
		self.assertEqual([row.action for row in doc.log], ["add", "booked"])

	def test_retail_till_expense_books_paid_purchase_invoice(self):
		p = self.payload()
		self.send(p)
		doc = self.expense(p["local_id"])
		self.assertEqual(doc.status, "Booked", msg=doc.posting_error)
		pi = frappe.get_doc("Purchase Invoice", doc.purchase_invoice)
		cash = cash_account()
		self.assertEqual((pi.docstatus, pi.is_paid, pi.update_stock), (1, 1, 0))
		# OMR 7.350 with 5% VAT: the invoice is exactly what left the drawer
		self.assertEqual(Decimal(money(pi.grand_total)), Decimal("7.350"))
		self.assertEqual(Decimal(money(pi.paid_amount)), Decimal("7.350"))
		self.assertEqual(pi.cash_bank_account, cash)
		self.assertEqual(pi.supplier, CASH_SUPPLIER)
		self.assertEqual(self.gl(pi.name, cash), (Decimal("0.000"), Decimal("7.350")))
		self.assertEqual(self.gl(pi.name, core.VAT_ACCOUNT), (Decimal("0.350"), Decimal("0.000")))
		self.assertEqual(
			(doc.status, doc.company, doc.outlet, doc.device),
			("Booked", core.COMPANY, self.profile.name, self.device_id),
		)

	def test_retail_expense_needs_manager_role_to_amend(self):
		p = self.payload()
		self.send(p)
		amended = dict(
			p,
			edited_at=utc(now_datetime()),
			lines=[{"category": GAS, "description": "Two", "amount": "14.000"}],
			taxes=[{"account_head": core.VAT_ACCOUNT, "rate": "5", "amount": "0.700"}],
			net="14.000",
			tax="0.700",
			total="14.700",
		)
		plain = self.send(dict(amended, approved_by=self.cashier), "amend", ok=False)
		self.assertEqual(plain["status"], "error")
		self.assertEqual(plain["error"]["code"], "permission")
		nobody = self.send(dict(amended, approved_by=None), "amend", ok=False)
		self.assertEqual(nobody["error"]["code"], "validation")
		with mock.patch.object(booking, "rebook", wraps=booking.rebook) as rebook:
			done = self.send(dict(amended, approved_by=self.manager), "amend")
		self.assertIs(rebook.call_args.kwargs["product"], RETAIL)
		self.assertEqual(done["expense"]["status"], "Booked")
		self.assertEqual(self.expense(p["local_id"]).amount, 14.7)

	def test_an_add_that_needs_a_pin_names_a_manager(self):
		frappe.db.set_value("POS Profile", self.profile.name, "crenya_expense_add_needs_pin", 1)
		frappe.clear_document_cache("POS Profile", self.profile.name)
		try:
			refused = self.send(self.payload(), ok=False)
			self.assertEqual(refused["error"]["code"], "permission")
			self.send(self.payload(approved_by=self.manager))
		finally:
			frappe.db.set_value("POS Profile", self.profile.name, "crenya_expense_add_needs_pin", 0)
			frappe.clear_document_cache("POS Profile", self.profile.name)
			frappe.db.commit()  # nosemgrep

	def test_the_outlets_applicable_users_limit_who_approves(self):
		profile = frappe.get_doc("POS Profile", self.profile.name)
		saved = [row.user for row in profile.applicable_for_users]
		p = self.payload()
		self.send(p)
		try:
			profile.set("applicable_for_users", [{"user": "Administrator"}])
			profile.flags.ignore_validate = True
			profile.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep
			frappe.clear_document_cache("POS Profile", self.profile.name)
			refused = self.send(
				dict(p, approved_by=self.manager, edited_at=utc(now_datetime())), "amend", ok=False
			)
			self.assertEqual(refused["error"]["code"], "permission")
			self.assertIn("not a manager of outlet", refused["error"]["message"])
		finally:
			profile = frappe.get_doc("POS Profile", self.profile.name)
			profile.set("applicable_for_users", [{"user": user} for user in saved])
			profile.flags.ignore_validate = True
			profile.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep
			frappe.clear_document_cache("POS Profile", self.profile.name)

	def test_amend_after_shift_close_refused_before_close_accepted(self):
		shift_id = new_id()
		p = self.payload(shift_local_id=shift_id)
		self.send(p)
		closed = now_datetime().replace(microsecond=0)
		self.push_shift(shift_id, closed)
		after = dict(p, approved_by=self.manager, edited_at=utc(closed + timedelta(minutes=1)), notes="late")
		refused = self.send(after, "amend", ok=False)
		self.assertEqual(refused["status"], "error")
		self.assertEqual(refused["error"]["code"], "validation")
		self.assertFalse(refused["error"]["retryable"])
		self.assertIn("day is closed", refused["error"]["message"])
		# made on the till before the close, synced after it
		before = dict(
			p, approved_by=self.manager, edited_at=utc(closed - timedelta(minutes=5)), notes="on time"
		)
		self.send(before, "amend")
		self.assertEqual(self.expense(p["local_id"]).description, "on time")

	def test_failed_booking_keeps_expense_not_booked(self):
		# a mode of the outlet without an account for the company: the PI cannot be made
		mode = f"_Test Crenya Petty {uuid.uuid4().hex[:6]}"
		frappe.get_doc(
			{"doctype": "Mode of Payment", "mode_of_payment": mode, "type": "Cash", "enabled": 1}
		).insert(ignore_permissions=True)
		profile = frappe.get_doc("POS Profile", self.profile.name)
		profile.append("payments", {"mode_of_payment": mode})
		profile.flags.ignore_validate = True
		profile.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep
		try:
			p = self.payload(mode_of_payment=mode)
			result = self.send(p)
			# the cash left the drawer: the event succeeds, the expense waits Not booked
			self.assertEqual(result["status"], "ok")
			self.assertEqual(result["expense"]["status"], "Not booked")
			self.assertIsNone(result["expense"]["purchase_invoice"])
			self.assertIn(mode, result["expense"]["posting_error"])
			doc = self.expense(p["local_id"])
			self.assertEqual([row.action for row in doc.log], ["add", "booking failed"])
			mop = frappe.get_doc("Mode of Payment", mode)
			mop.append("accounts", {"company": core.COMPANY, "default_account": cash_account()})
			mop.save(ignore_permissions=True)
			# the owner's retry passes the product of the web request, never a guess
			self.assertEqual(booking.retry(doc, product=RETAIL), booking.BOOKED)
			doc.reload()
			self.assertEqual(doc.status, "Booked")
		finally:
			profile = frappe.get_doc("POS Profile", self.profile.name)
			profile.payments = [row for row in profile.payments if row.mode_of_payment != mode]
			profile.flags.ignore_validate = True
			profile.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep

	def test_cancel_cancels_the_invoice_and_keeps_the_row(self):
		p = self.payload()
		self.send(p)
		pi = self.expense(p["local_id"]).purchase_invoice
		refused = self.send(dict(p, approved_by=self.manager), "cancel", ok=False)
		self.assertEqual(refused["error"]["code"], "validation")
		self.send(dict(p, approved_by=self.manager, reason="Entered twice"), "cancel")
		doc = self.expense(p["local_id"])
		self.assertEqual(doc.status, "Cancelled")
		self.assertEqual(frappe.db.get_value("Purchase Invoice", pi, "docstatus"), 2)

	def test_supplier_outside_the_outlet_refused(self):
		other = f"_Test Crenya Other Supplier {uuid.uuid4().hex[:6]}"
		frappe.get_doc(
			{"doctype": "Supplier", "supplier_name": other, "supplier_group": "All Supplier Groups"}
		).insert(ignore_permissions=True)
		refused = self.send(self.payload(supplier=other), ok=False)
		self.assertEqual(refused["error"]["code"], "validation")
		self.assertIn("not an enabled expense supplier", refused["error"]["message"])
		self.send(self.payload(supplier=VEG_SHOP))

	def test_old_till_gets_no_expense_entities(self):
		"""A till (or an outlet) that has not switched Expenses on is sent no category or supplier,
		and its bootstrap says why; switching on sends them once."""
		self.assertTrue(self._pull("expense_category"))
		frappe.db.set_value("POS Profile", self.profile.name, "crenya_expenses_enabled", 0)
		frappe.clear_document_cache("POS Profile", self.profile.name)
		try:
			self.assertEqual(self._pull("expense_category"), [])
			self.assertEqual(self._pull("expense_supplier"), [])
			doc = device_api.get_bootstrap(device_id=self.device_id)
			self.assertEqual(doc["features"]["retail_expenses"], 1)
			self.assertIs(doc["expenses"]["enabled"], False)
			self.assertIn("Shops → Settings → Expenses", doc["expenses"]["reason"])
		finally:
			frappe.db.set_value("POS Profile", self.profile.name, "crenya_expenses_enabled", 1)
			frappe.clear_document_cache("POS Profile", self.profile.name)
			frappe.db.commit()  # nosemgrep
		names = {row["id"] for row in self._pull("expense_supplier")}
		self.assertLessEqual({CASH_SUPPLIER, VEG_SHOP}, names)
		cash = [row for row in self._pull("expense_supplier") if row["is_cash_supplier"]]
		self.assertEqual([row["id"] for row in cash], [CASH_SUPPLIER])

	def _pull(self, entity):
		return self._pull_all(entity, 100)[0]

	def test_bootstrap_has_the_expenses_block_for_a_retail_till(self):
		doc = device_api.get_bootstrap(device_id=self.device_id)
		self.assertEqual(doc["features"]["retail_expenses"], 1)
		block = doc["expenses"]
		self.assertIs(block["enabled"], True)
		self.assertIsNone(block["reason"])
		self.assertEqual(block["cash_supplier"], CASH_SUPPLIER)
		self.assertEqual(block["tax"]["template"], VAT_TEMPLATE)
		self.assertEqual(block["tax"]["rows"][0]["account_head"], core.VAT_ACCOUNT)
		self.assertEqual(block["photo"], {"max_side": 1600, "max_bytes": 1024 * 1024})

	def test_a_restaurant_till_uses_the_same_handler_with_its_own_wording_and_approvers(self):
		"""crenya_restaurant 0.15.6 is not registered as a product: its approver roles come from the
		installed app (stubbed here), and its failures name its own settings page."""
		restaurant = new_id()
		device_api.register_device(
			device_id=restaurant,
			device_name="Restaurant till",
			pos_profile=self.profile.name,
			app_version="0.7.0",
			platform="linux",
			device_type="restaurant_pos",
		)
		stub = restaurant_constants(("Crenya POS Manager",))
		p = self.payload()
		with stub_modules(stub):
			with mock.patch.object(booking, "book", wraps=booking.book) as book:
				self.send(p, device_id=restaurant)
			self.assertIs(book.call_args.kwargs["product"], RESTAURANT)
			amended = dict(p, edited_at=utc(now_datetime()), notes="amended")
			refused = self.send(
				dict(amended, approved_by=self.cashier), "amend", ok=False, device_id=restaurant
			)
			self.assertEqual(refused["error"]["code"], "permission")
			with mock.patch.object(booking, "rebook", wraps=booking.rebook) as rebook:
				self.send(dict(amended, approved_by=self.manager), "amend", device_id=restaurant)
			self.assertIs(rebook.call_args.kwargs["product"], RESTAURANT)
		doc = self.expense(p["local_id"])
		self.assertEqual((doc.status, doc.device), ("Booked", restaurant))
		self.assertIs(
			expense_sync.product_of_device(types.SimpleNamespace(device_type="restaurant_pos")), RESTAURANT
		)


class TestRetailExpensesOff(SyncCase):
	"""A site without erptrue_app's POS Expense (or before its migrate): no capability, nothing to book."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		if booking.expenses_supported():
			raise unittest.SkipTest("erptrue_app's POS Expense is installed on this site")
		super().setUpClass()

	def test_site_without_pos_expense_hides_capability(self):
		doc = device_api.get_bootstrap(device_id=self.device_id)
		self.assertNotIn("retail_expenses", doc["features"])
		self.assertNotIn("expenses", doc)
		p = {
			"local_id": f"EXP-{uuid.uuid4().hex[:8]}",
			"shift_local_id": new_id(),
			"business_date": nowdate(),
			"user": "Administrator",
			"edited_at": utc(now_datetime()),
			"mode_of_payment": core.CASH,
			"currency": core.CURRENCY,
			"lines": [{"category": GAS, "amount": "7.000"}],
			"net": "7.000",
			"tax": "0.000",
			"total": "7.000",
		}
		event = {
			"event_id": new_id(),
			"aggregate_type": AGGREGATE,
			"operation": "submit",
			"local_id": p["local_id"],
			"sequence_no": 1,
			"payload_hash": payload_hash(p),
			"payload": p,
		}
		result = self.push_one(event)
		self.assertError(result, "validation", False)
		self.assertIn(booking.UNSUPPORTED, result["error"]["message"])
