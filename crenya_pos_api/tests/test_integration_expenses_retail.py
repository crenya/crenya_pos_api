"""Retail expenses on the server (0.13.0): the retail till pushes POS Expenses through the built-in
handler of crenya_pos_api, they book as paid Purchase Invoices, the pulls and the bootstrap follow
the outlet's switch, and a restaurant till (crenya_restaurant 0.15.6, whose hook entries this app
ignores) goes through the same handler with its own wording and approvers.

Every call into the booking engine passes the product explicitly (book and rebook swallow a missing
product into "Not booked"): the cases assert it. On the erpnext branch this app ships POS
Expense itself: the cases run on every migrated site (`TestRetailExpensesOff` only before the migrate).

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_expenses_retail
"""

import base64
import hashlib
import types
import unittest
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import frappe
from frappe.utils import add_days, add_to_date, now_datetime, nowdate

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import expenses as expenses_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.expenses import booking, reports
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
	jpeg,
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
		# erpnext branch: this app ships POS Expense, so a migrated test site always has it
		if not booking.expenses_supported():
			raise AssertionError("POS Expense with the till fields is missing: run bench migrate")
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

	def test_contention_is_retried_not_kept_not_booked(self):
		"""A deadlock or a stale timestamp while booking undoes the event (retryable): the till
		sends it again and it books. (The restaurant's own test of this patches its copy of the
		engine; this one goes through the built-in handler.)"""
		for error in (frappe.QueryDeadlockError, frappe.TimestampMismatchError):
			payload = self.payload()
			with mock.patch.object(booking, "_post", side_effect=error("busy")):
				result = self.send(payload, ok=False)
			self.assertEqual(result["status"], "error", msg=error)
			self.assertTrue(result["error"]["retryable"], msg=error)
			self.assertIsNone(self.expense(payload["local_id"]))
			self.assertEqual(self.send(payload)["expense"]["status"], "Booked")

	def test_a_delete_the_server_could_not_finish_stays_not_booked_and_out_of_the_z(self):
		shift_id = new_id()
		kept = self.payload(shift_local_id=shift_id)
		gone = self.payload(shift_local_id=shift_id)
		for payload in (kept, gone):
			self.send(payload)
		with mock.patch.object(booking, "_cancel_pi", side_effect=frappe.ValidationError("linked")):
			self.send(dict(gone, approved_by=self.manager, reason="Not ours"), "cancel")
		doc = self.expense(gone["local_id"])
		self.assertEqual(doc.status, "Not booked")
		self.assertEqual(frappe.db.get_value("Purchase Invoice", doc.purchase_invoice, "docstatus"), 1)
		shift = frappe._dict(local_id=shift_id, pos_profile=self.profile.name)
		rows = reports.shift_expenses(shift, {core.CASH})
		self.assertEqual(len(rows), 1)

	def test_supplier_outside_the_outlet_refused(self):
		other = f"_Test Crenya Other Supplier {uuid.uuid4().hex[:6]}"
		frappe.get_doc(
			{"doctype": "Supplier", "supplier_name": other, "supplier_group": "All Supplier Groups"}
		).insert(ignore_permissions=True)
		refused = self.send(self.payload(supplier=other), ok=False)
		self.assertEqual(refused["error"]["code"], "validation")
		self.assertIn("not an enabled expense supplier", refused["error"]["message"])
		self.send(self.payload(supplier=VEG_SHOP))

	def test_pulls_are_empty_while_the_outlet_switch_is_off(self):
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
			self.assertIn("POS Profile → Expenses", doc["expenses"]["reason"])
		finally:
			frappe.db.set_value("POS Profile", self.profile.name, "crenya_expenses_enabled", 1)
			frappe.clear_document_cache("POS Profile", self.profile.name)
			frappe.db.commit()  # nosemgrep
		names = {row["id"] for row in self._pull("expense_supplier")}
		self.assertLessEqual({CASH_SUPPLIER, VEG_SHOP}, names)
		cash = [row for row in self._pull("expense_supplier") if row["is_cash_supplier"]]
		self.assertEqual([row["id"] for row in cash], [CASH_SUPPLIER])

	def test_a_cursor_taken_while_off_sends_everything_once_switched_on(self):
		"""The cursor scope changes with the switch: a till that pulled while it was off gets the
		categories and suppliers the moment it is on, without a manual re-pull."""
		cursors = {}
		frappe.db.set_value("POS Profile", self.profile.name, "crenya_expenses_enabled", 0)
		frappe.clear_document_cache("POS Profile", self.profile.name)
		try:
			for entity in ("expense_category", "expense_supplier"):
				page = sync_api.pull_changes(device_id=self.device_id, entity=entity, limit=1)
				self.assertEqual((page["records"], page["has_more"]), ([], False))
				cursors[entity] = page["next_cursor"]
		finally:
			frappe.db.set_value("POS Profile", self.profile.name, "crenya_expenses_enabled", 1)
			frappe.clear_document_cache("POS Profile", self.profile.name)
			frappe.db.commit()  # nosemgrep
		for entity, cursor in cursors.items():
			records, _cursor, _pages = self._pull_all(entity, 100, cursor=cursor)
			self.assertTrue(records, msg=entity)
		ids = {
			row["id"]
			for row in self._pull_all("expense_category", 100, cursor=cursors["expense_category"])[0]
		}
		self.assertIn(GAS, ids)

	def test_the_booking_pull_carries_a_web_retry_to_the_till(self):
		"""A failed booking pulls as `Not booked` with its error; after the owner's Retry on the
		web the same expense pulls again as `Booked` with its invoice and no error. Another
		outlet's expense, one made on the desk (no local_id) and an expense past the window are
		not sent."""
		mode = f"_Test Crenya Pullpetty {uuid.uuid4().hex[:6]}"
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
			self.send(p)
			doc = self.expense(p["local_id"])
			first, cursor, _pages = self._pull_all("expense_booking", 100)
			mine = [row for row in first if row["id"] == p["local_id"]]
			self.assertEqual(len(mine), 1)
			self.assertEqual(
				(mine[0]["name"], mine[0]["status"], mine[0]["purchase_invoice"]),
				(doc.name, "Not booked", None),
			)
			self.assertIn(mode, mine[0]["posting_error"])

			mop = frappe.get_doc("Mode of Payment", mode)
			mop.append("accounts", {"company": core.COMPANY, "default_account": cash_account()})
			mop.save(ignore_permissions=True)
			self.assertEqual(booking.retry(doc, product=RETAIL), booking.BOOKED)
			doc.reload()
			again, _cursor, _pages = self._pull_all("expense_booking", 100, cursor=cursor)
			mine = [row for row in again if row["id"] == p["local_id"]]
			self.assertEqual(len(mine), 1, "the retry is a change after the till's cursor")
			self.assertEqual(
				(mine[0]["status"], mine[0]["purchase_invoice"], mine[0]["posting_error"]),
				("Booked", doc.purchase_invoice, None),
			)

			# only this outlet's till expenses, and not the old ones
			frappe.db.set_value(AGGREGATE, doc.name, "expense_date", add_days(nowdate(), -400))
			old, _cursor, _pages = self._pull_all("expense_booking", 100)
			self.assertNotIn(p["local_id"], [row["id"] for row in old])
			frappe.db.set_value(AGGREGATE, doc.name, {"expense_date": nowdate(), "outlet": "Elsewhere"})
			other, _cursor, _pages = self._pull_all("expense_booking", 100)
			self.assertNotIn(p["local_id"], [row["id"] for row in other])
		finally:
			profile = frappe.get_doc("POS Profile", self.profile.name)
			profile.payments = [row for row in profile.payments if row.mode_of_payment != mode]
			profile.flags.ignore_validate = True
			profile.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep

	def test_the_booking_pull_is_empty_while_the_outlet_switch_is_off(self):
		self.send(self.payload())
		self.assertTrue(self._pull("expense_booking"))
		frappe.db.set_value("POS Profile", self.profile.name, "crenya_expenses_enabled", 0)
		frappe.clear_document_cache("POS Profile", self.profile.name)
		try:
			self.assertEqual(self._pull("expense_booking"), [])
		finally:
			frappe.db.set_value("POS Profile", self.profile.name, "crenya_expenses_enabled", 1)
			frappe.clear_document_cache("POS Profile", self.profile.name)
			frappe.db.commit()  # nosemgrep

	def _device_of_type(self, device_type):
		device_id = new_id()
		device_api.register_device(
			device_id=device_id,
			device_name=f"{device_type} device",
			pos_profile=self.profile.name,
			app_version="0.7.0",
			platform="linux",
			device_type=device_type,
		)
		return device_id

	def test_a_device_without_an_expense_product_is_sent_no_expense_record(self):
		"""A kitchen display or waiter phone of the outlet is sent no category, supplier or booking."""
		self.send(self.payload())
		for device_type in ("kds", "waiter_phone"):
			device_id = self._device_of_type(device_type)
			for entity in ("expense_category", "expense_supplier", "expense_booking"):
				records = self._pull_all(entity, 100, device_id=device_id)[0]
				self.assertEqual(records, [], msg=f"{device_type} {entity}")

	def test_a_restaurant_till_is_sent_the_booking_of_its_outlets_expenses(self):
		restaurant = self._device_of_type("restaurant_pos")
		p = self.payload()
		with stub_modules(restaurant_constants(("Crenya POS Manager",))):
			self.send(p, device_id=restaurant)
		rows = [
			row
			for row in self._pull_all("expense_booking", 100, device_id=restaurant)[0]
			if row["id"] == p["local_id"]
		]
		self.assertEqual([(row["status"]) for row in rows], ["Booked"])
		self.assertTrue(self._pull_all("expense_category", 100, device_id=restaurant)[0])

	def test_the_booking_pull_looks_back_62_days_of_the_business_date(self):
		inside, outside = self.payload(), self.payload()
		self.send(inside)
		self.send(outside)
		for p, days in ((inside, 62), (outside, 63)):
			name = frappe.db.get_value(AGGREGATE, {"local_id": p["local_id"]}, "name")
			frappe.db.set_value(AGGREGATE, name, "expense_date", add_days(nowdate(), -days))
		ids = {row["id"] for row in self._pull("expense_booking")}
		self.assertIn(inside["local_id"], ids)
		self.assertNotIn(outside["local_id"], ids)

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


class TestRetailExpensePhoto(RetailExpenseCase):
	"""The bill photo method (`api.expenses.attach_photo`): its limits and its idempotency."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		# erpnext branch: this app ships POS Expense, so a migrated test site always has it
		if not booking.expenses_supported():
			raise AssertionError("POS Expense with the till fields is missing: run bench migrate")
		super().setUpClass()

	def attach(self, local_id, content, sha=None, device_id=None):
		return expenses_api.attach_photo(
			device_id=device_id or self.device_id,
			local_id=local_id,
			photo=base64.b64encode(content).decode(),
			sha256=sha or hashlib.sha256(content).hexdigest(),
		)

	def booked(self):
		payload = self.payload()
		self.send(payload)
		return payload["local_id"]

	def files(self, local_id):
		name = frappe.db.get_value(AGGREGATE, {"local_id": local_id}, "name")
		return frappe.get_all(
			"File",
			filters={"attached_to_doctype": AGGREGATE, "attached_to_name": name, "is_private": 1},
			pluck="name",
		)

	def test_a_photo_is_attached_once_and_the_same_photo_again_changes_nothing(self):
		local_id = self.booked()
		content = jpeg()
		first = self.attach(local_id, content)
		self.assertIs(first["already"], False)
		self.assertTrue(first["file"].startswith("/private/files/"))
		again = self.attach(local_id, content)
		self.assertEqual((again["already"], again["file"]), (True, first["file"]))
		self.assertEqual(len(self.files(local_id)), 1)
		# it is on the Purchase Invoice too, once
		doc = self.expense(local_id)
		self.assertEqual(
			frappe.db.count(
				"File",
				{
					"file_url": first["file"],
					"attached_to_doctype": "Purchase Invoice",
					"attached_to_name": doc.purchase_invoice,
				},
			),
			1,
		)

	def test_another_photo_replaces_it(self):
		local_id = self.booked()
		first = self.attach(local_id, jpeg())
		second = self.attach(local_id, jpeg())
		self.assertIs(second["already"], False)
		self.assertNotEqual(second["file"], first["file"])
		self.assertEqual(self.expense(local_id).photo, second["file"])

	def test_a_photo_over_one_mib_is_refused(self):
		local_id = self.booked()
		big = b"\xff\xd8\xff" + b"0" * (booking.PHOTO_MAX_BYTES + 1 - 3)
		self.assertEqual(len(big), booking.PHOTO_MAX_BYTES + 1)
		with self.assertRaises(frappe.ValidationError):
			self.attach(local_id, big)
		self.assertEqual(self.files(local_id), [])

	def test_only_a_jpeg_is_taken(self):
		import io

		from PIL import Image

		local_id = self.booked()
		buffer = io.BytesIO()
		Image.new("RGB", (20, 20), (1, 2, 3)).save(buffer, format="PNG")
		with self.assertRaises(frappe.ValidationError) as caught:
			self.attach(local_id, buffer.getvalue())
		self.assertIn("JPEG", str(caught.exception))
		with self.assertRaises(frappe.ValidationError):
			self.attach(local_id, b"not an image at all")

	def test_a_wrong_sha256_is_refused(self):
		local_id = self.booked()
		with self.assertRaises(frappe.ValidationError) as caught:
			self.attach(local_id, jpeg(), sha="0" * 64)
		self.assertIn("sha256", str(caught.exception))
		self.assertEqual(self.files(local_id), [])

	def test_an_expense_of_another_outlet_is_refused(self):
		local_id = self.booked()
		name = frappe.db.get_value(AGGREGATE, {"local_id": local_id}, "name")
		frappe.db.set_value(AGGREGATE, name, "outlet", "_Test Crenya Another Outlet")
		try:
			with self.assertRaises(frappe.PermissionError):
				self.attach(local_id, jpeg())
		finally:
			frappe.db.set_value(AGGREGATE, name, "outlet", self.profile.name)

	def test_an_unknown_expense_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.attach("EXP-NOPE", jpeg())

	def test_a_kitchen_display_may_not_attach_photos(self):
		local_id = self.booked()
		kds = new_id()
		device_api.register_device(
			device_id=kds,
			device_name="Kitchen display",
			pos_profile=self.profile.name,
			app_version="0.7.0",
			platform="linux",
			device_type="kds",
		)
		with self.assertRaises(frappe.PermissionError):
			self.attach(local_id, jpeg(), device_id=kds)
		self.assertEqual(self.files(local_id), [])


class TestRetailExpensesOff(SyncCase):
	"""A site without POS Expense (before this app's migrate): no capability, nothing to book."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		if booking.expenses_supported():
			# erpnext branch: this app ships POS Expense, so only a site before its migrate runs these
			raise unittest.SkipTest("POS Expense is installed (crenya_pos_api ships it)")
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
