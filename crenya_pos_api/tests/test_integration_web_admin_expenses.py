"""Shops web admin, the till expenses (crenya_pos_api/api/web_admin/expenses.py, day_close.py), retail
P1 task 12: the Not booked list scoped to the shops a user manages, Retry that books once and always
tells the engine the product, one expense for a manager with no POS Expense right, the Cash Supplier
button, and the Z report's ON ACCOUNT and EXPENSES sections at OMR's 3 decimals.

The cases need erptrue_app's POS Expense (CI's `expenses` job installs it); without it they skip.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_web_admin_expenses
"""

import unittest
import uuid
from decimal import Decimal
from unittest import mock

import frappe
from frappe.utils import now_datetime

from crenya_pos_api.api import web_admin
from crenya_pos_api.expenses import booking
from crenya_pos_api.expenses.product import RETAIL
from crenya_pos_api.sync.approvals import APPROVER_ROLE
from crenya_pos_api.sync.context import POS_USER_ROLE
from crenya_pos_api.tests import fixtures as core
from crenya_pos_api.tests.sync_case import money, new_id
from crenya_pos_api.tests.test_integration_expenses_booking import CARD, GAS, cash_account
from crenya_pos_api.tests.test_integration_expenses_retail import RetailExpenseCase, utc
from crenya_pos_api.tests.web_admin_case import SHOP2_CASHIER, SHOP2_MANAGER, ensure_shop2, ensure_user


class TestWebAdminExpenses(RetailExpenseCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		if not booking.expenses_supported():
			raise unittest.SkipTest("erptrue_app's POS Expense with the till fields is not installed")
		super().setUpClass()
		ensure_user(SHOP2_CASHIER, [POS_USER_ROLE])
		ensure_user(SHOP2_MANAGER, [POS_USER_ROLE, APPROVER_ROLE])
		ensure_shop2([SHOP2_CASHIER, SHOP2_MANAGER])
		frappe.db.commit()  # nosemgrep

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def not_booked(self):
		"""An expense its booking left Not booked (a mode of payment with no account for the company):
		(the expense, the mode)."""
		mode = f"_Test Crenya Petty {uuid.uuid4().hex[:6]}"
		frappe.get_doc(
			{"doctype": "Mode of Payment", "mode_of_payment": mode, "type": "Cash", "enabled": 1}
		).insert(ignore_permissions=True)
		profile = frappe.get_doc("POS Profile", self.profile.name)
		profile.append("payments", {"mode_of_payment": mode})
		profile.flags.ignore_validate = True
		profile.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep

		def restore():
			profile = frappe.get_doc("POS Profile", self.profile.name)
			profile.payments = [row for row in profile.payments if row.mode_of_payment != mode]
			profile.flags.ignore_validate = True
			profile.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep

		self.addCleanup(restore)
		payload = self.payload(mode_of_payment=mode)
		result = self.send(payload)
		self.assertEqual(result["expense"]["status"], "Not booked")
		return self.expense(payload["local_id"]), mode

	def add_account(self, mode):
		mop = frappe.get_doc("Mode of Payment", mode)
		mop.append("accounts", {"company": core.COMPANY, "default_account": cash_account()})
		mop.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep

	def test_not_booked_scoped_to_managed_outlets(self):
		doc, mode = self.not_booked()
		frappe.set_user(self.manager)
		answer = web_admin.expenses_not_booked()
		row = next(row for row in answer["rows"] if row["name"] == doc.name)
		self.assertEqual(
			(row["outlet"], row["amount"], row["can_retry"], row["attempts"]),
			(self.profile.name, "7.350", True, 1),
		)
		self.assertEqual(row["currency"]["precision"], 3)
		self.assertIn(mode, row["posting_error"])
		self.assertEqual(row["category"], GAS)
		self.assertEqual(
			[
				r["name"]
				for r in web_admin.expenses_not_booked(self.profile.name)["rows"]
				if r["name"] == doc.name
			],
			[doc.name],
		)
		# another shop's manager sees none of it, and may not ask for this shop
		frappe.set_user(SHOP2_MANAGER)
		self.assertNotIn(doc.name, [r["name"] for r in web_admin.expenses_not_booked()["rows"]])
		with self.assertRaises(frappe.PermissionError):
			web_admin.expenses_not_booked(self.profile.name)
		# a cashier manages nothing
		frappe.set_user(self.cashier)
		self.assertEqual(web_admin.expenses_not_booked()["rows"], [])
		with self.assertRaises(frappe.PermissionError):
			web_admin.expenses_not_booked(self.profile.name)

	def test_retry_booking_books_once(self):
		doc, mode = self.not_booked()
		self.add_account(mode)
		frappe.set_user(self.manager)
		with mock.patch.object(booking, "retry", wraps=booking.retry) as retry:
			answer = web_admin.retry_expense_booking(doc.name)
		# the engine is told the shop's product, never left to guess
		self.assertIs(retry.call_args.kwargs["product"], RETAIL)
		self.assertEqual((answer["name"], answer["status"]), (doc.name, "Booked"))
		self.assertTrue(answer["purchase_invoice"])
		self.assertIsNone(answer["posting_error"])
		self.assertEqual(frappe.db.get_value("Purchase Invoice", answer["purchase_invoice"], "docstatus"), 1)
		# booked already: nothing to retry
		with self.assertRaises(frappe.ValidationError):
			web_admin.retry_expense_booking(doc.name)
		# another shop's manager may not retry this one
		frappe.set_user(SHOP2_MANAGER)
		with self.assertRaises(frappe.PermissionError):
			web_admin.retry_expense_booking(doc.name)

	def test_shop_manager_without_pos_expense_read_sees_till_expense(self):
		doc, _mode = self.not_booked()
		self.assertFalse(frappe.has_permission("POS Expense", "read", user=self.manager))
		frappe.set_user(self.manager)
		answer = web_admin.till_expense(doc.name)
		self.assertEqual(answer["expense"]["name"], doc.name)
		self.assertEqual([line["category"] for line in answer["expense"]["lines"]], [GAS])
		self.assertEqual([row["action"] for row in answer["expense"]["log"]], ["add", "booking failed"])
		self.assertEqual(answer["currency"]["precision"], 3)
		self.assertIs(answer["can_retry"], True)
		for user, name in ((SHOP2_MANAGER, doc.name), (self.manager, "EXP-NOPE"), (self.cashier, doc.name)):
			frappe.set_user(user)
			with self.assertRaises(frappe.PermissionError, msg=user):
				web_admin.till_expense(name)

	def test_ensure_cash_supplier_for_a_manager(self):
		saved = frappe.db.get_value("POS Profile", self.profile.name, booking.CASH_SUPPLIER_FIELD)

		def restore():
			frappe.db.set_value("POS Profile", self.profile.name, booking.CASH_SUPPLIER_FIELD, saved)
			frappe.clear_document_cache("POS Profile", self.profile.name)
			frappe.db.commit()  # nosemgrep

		self.addCleanup(restore)
		frappe.db.set_value("POS Profile", self.profile.name, booking.CASH_SUPPLIER_FIELD, None)
		frappe.clear_document_cache("POS Profile", self.profile.name)
		frappe.set_user(self.manager)
		made = web_admin.ensure_cash_supplier(self.profile.name)
		self.assertTrue(made["cash_supplier"])
		self.assertEqual(
			frappe.db.get_value("POS Profile", self.profile.name, booking.CASH_SUPPLIER_FIELD),
			made["cash_supplier"],
		)
		again = web_admin.ensure_cash_supplier(self.profile.name)
		self.assertEqual((again["cash_supplier"], again["created"]), (made["cash_supplier"], False))
		frappe.set_user(SHOP2_MANAGER)
		with self.assertRaises(frappe.PermissionError):
			web_admin.ensure_cash_supplier(self.profile.name)

	def test_z_sections_on_account_and_expenses(self):
		"""Float 10.000, cash sales 50.000, cash received 15.250, cash paid out 7.350, card paid out
		4.000: the drawer expects 67.900, the card its sales (20.000), counted as expected."""
		shift_id = new_id()
		cash = self.payload(shift_local_id=shift_id)
		card = self.payload(
			shift_local_id=shift_id,
			mode_of_payment=CARD,
			lines=[{"category": GAS, "description": "Gas", "amount": "3.810"}],
			taxes=[{"account_head": core.VAT_ACCOUNT, "rate": "5", "amount": "0.190"}],
			net="3.810",
			tax="0.190",
			total="4.000",
			payee_name="Cleaner",
		)
		self.send(cash)
		self.send(card)
		closed = now_datetime().replace(microsecond=0)
		payload = self.shift_payload(
			local_id=shift_id,
			opening_float="10.000",
			opened_at=utc(closed.replace(hour=0, minute=0, second=0)),
			closed_at=utc(closed),
			expense_count=2,
			on_account={
				"sales_count": 2,
				"sales_amount": "90.000",
				"returned_amount": "0.000",
				"received_count": 1,
				"received_amount": "15.250",
			},
			payments=[
				{
					"mode_of_payment": core.CASH,
					"expected": "67.900",
					"counted": "67.900",
					"difference": "0.000",
					"received": "15.250",
					"paid_out": "7.350",
				},
				{
					"mode_of_payment": CARD,
					"expected": "20.000",
					"counted": "20.000",
					"difference": "0.000",
					"paid_out": "4.000",
				},
			],
		)
		self.assertOk(self.push_one(self.event(payload, "Crenya POS Shift")))
		frappe.set_user(self.manager)
		answer = web_admin.day_close(shift_id)
		sections = {section["key"]: section for section in answer["sections"]}
		self.assertEqual(
			list(sections),
			["sales", "payments", "returns", "store_controls", "paid_out", "on_account", "expenses"],
		)
		payments = {row["label_en"]: row["value"] for row in sections["payments"]["rows"]}
		self.assertEqual(
			(payments["Cash expected"], payments["Cash counted"], payments["Cash difference"]),
			("67.900", "67.900", "0.000"),
		)
		self.assertEqual(payments[f"{CARD} expected"], "20.000")
		on_account = {row["label_en"]: row["value"] for row in sections["on_account"]["rows"]}
		self.assertEqual(
			(on_account["Sold on account"], on_account["Received on account"], on_account["Cash received"]),
			("90.000", "15.250", "15.250"),
		)
		paid_out = {row["label_en"]: row["value"] for row in sections["paid_out"]["rows"]}
		self.assertEqual((paid_out["Cash paid out"], paid_out[f"{CARD} paid out"]), ("7.350", "4.000"))
		spent = sections["expenses"]["rows"]
		self.assertEqual([row["value"] for row in spent], ["7.350", "4.000", "11.350"])
		self.assertIn("Gas man", spent[0]["label_en"])
		self.assertTrue(spent[1]["label_en"].endswith(f"({CARD})"))
		self.assertEqual(Decimal(spent[-1]["value"]), Decimal(money(7.35 + 4.0)))
