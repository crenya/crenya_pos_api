"""The expenses booking engine (moved down from crenya_restaurant, 0.13.0) on an ERPNext site: a POS
Expense becomes one paid Purchase Invoice; edit, delete, a failed booking and its retry, a duplicate
bill number, the photo, the Z rows and the setup checks.

The restaurant's booking cases moved here; the push handler stays in crenya_restaurant until the
retail slice's Task 6, so these cases create the POS Expense the way the handler does and call the
engine. erptrue_app is not a dependency: the cases need its POS Expense (CI's `expenses` job installs
it); without it they are skipped and `TestExpensesOff` runs.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_expenses_booking
"""

import unittest
import uuid
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.model.base_document import get_controller
from frappe.utils import flt, nowdate

from crenya_pos_api.expenses import booking, reports
from crenya_pos_api.expenses.product import RESTAURANT, RETAIL, product_of_expense
from crenya_pos_api.sync import device as device_sync
from crenya_pos_api.sync import entitlement as entitlement_module
from crenya_pos_api.tests import fixtures as core
from crenya_pos_api.tests.sync_case import SyncCase, money

ABBR = core.ABBR
EXPENSE_ACCOUNT = f"_Test Crenya Outlet Expenses - {ABBR}"
BANK_ACCOUNT = f"_Test Crenya Expense Bank - {ABBR}"
CGST = f"_Test Crenya CGST - {ABBR}"
SGST = f"_Test Crenya SGST - {ABBR}"
VAT_TEMPLATE_TITLE = "_Test Crenya Purchase VAT 5%"
VAT_TEMPLATE = f"{VAT_TEMPLATE_TITLE} - {ABBR}"
GST_TEMPLATE_TITLE = "_Test Crenya Purchase GST"
GST_TEMPLATE = f"{GST_TEMPLATE_TITLE} - {ABBR}"
CARD = "_Test Crenya Expense Card"
SUPPLIER_GROUP = "_Test Crenya Expense Suppliers"
VEG_SHOP = "_Test Crenya Veg Shop"
CASH_SUPPLIER = "_Test Crenya Cash Supplier"
GAS = "_Test Gas"
CLEANING = "_Test Cleaning"


def _insert(doc):
	return frappe.get_doc(doc).insert(ignore_permissions=True)


def ensure_account(name, account_name, parent, account_type):
	if not frappe.db.exists("Account", name):
		_insert(
			{
				"doctype": "Account",
				"account_name": account_name,
				"parent_account": parent,
				"company": core.COMPANY,
				"account_type": account_type,
				"account_currency": core.CURRENCY,
			}
		)
	return name


def ensure_purchase_template(title, rows):
	name = f"{title} - {ABBR}"
	if not frappe.db.exists("Purchase Taxes and Charges Template", name):
		_insert(
			{
				"doctype": "Purchase Taxes and Charges Template",
				"title": title,
				"company": core.COMPANY,
				"taxes": [
					{
						"charge_type": "On Net Total",
						"account_head": head,
						"description": head,
						"rate": rate,
						"category": "Total",
						"add_deduct_tax": "Add",
					}
					for head, rate in rows
				],
			}
		)
	return name


def ensure_supplier(name, group):
	if not frappe.db.exists("Supplier", name):
		_insert({"doctype": "Supplier", "supplier_name": name, "supplier_group": group})
	return name


def ensure_category(name, account=None):
	if not frappe.db.exists("POS Expense Category", name):
		_insert({"doctype": "POS Expense Category", "category_name": name})
	frappe.db.set_value("POS Expense Category", name, {"expense_account": account, "is_active": 1})
	return name


def cash_account():
	return frappe.db.get_value(
		"Mode of Payment Account", {"parent": core.CASH, "company": core.COMPANY}, "default_account"
	)


class ExpenseCase(SyncCase):
	"""A shop with cash and a card paid from a bank account, the expense settings on, suppliers,
	a purchase VAT template (5 %) and categories."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# erptrue_app's entitlement hook reads a plan the test site does not have: no plan, no caps
		for target in (entitlement_module, device_sync):
			patcher = patch.object(target, "get_entitlement", return_value=None)
			patcher.start()
			cls.addClassCleanup(patcher.stop)
		company = frappe.get_doc("Company", core.COMPANY)
		if not frappe.db.get_single_value("Stock Settings", "stock_uom"):
			frappe.db.set_single_value("Stock Settings", "stock_uom", "Nos")
		ensure_account(
			EXPENSE_ACCOUNT, "_Test Crenya Outlet Expenses", f"Indirect Expenses - {ABBR}", "Expense Account"
		)
		ensure_account(BANK_ACCOUNT, "_Test Crenya Expense Bank", f"Bank Accounts - {ABBR}", "Bank")
		ensure_account(CGST, "_Test Crenya CGST", f"Duties and Taxes - {ABBR}", "Tax")
		ensure_account(SGST, "_Test Crenya SGST", f"Duties and Taxes - {ABBR}", "Tax")
		ensure_purchase_template(VAT_TEMPLATE_TITLE, [(core.VAT_ACCOUNT, 5)])
		ensure_purchase_template(GST_TEMPLATE_TITLE, [(CGST, 2.5), (SGST, 2.5)])
		core.ensure_mode_of_payment(CARD, "Bank", company, BANK_ACCOUNT)
		from frappe.utils.nestedset import get_root_of

		if not frappe.db.exists("Supplier Group", SUPPLIER_GROUP):
			_insert(
				{
					"doctype": "Supplier Group",
					"supplier_group_name": SUPPLIER_GROUP,
					"parent_supplier_group": get_root_of("Supplier Group"),
				}
			)
		ensure_supplier(VEG_SHOP, SUPPLIER_GROUP)
		ensure_supplier(CASH_SUPPLIER, SUPPLIER_GROUP)
		if booking.expenses_supported():
			ensure_category(GAS, EXPENSE_ACCOUNT)
			ensure_category(CLEANING, None)
		if not any(row.mode_of_payment == CARD for row in cls.profile.payments):
			profile = frappe.get_doc("POS Profile", cls.profile.name)
			profile.append("payments", {"mode_of_payment": CARD})
			profile.flags.ignore_validate = True
			profile.save(ignore_permissions=True)
		frappe.db.set_value(
			"POS Profile",
			cls.profile.name,
			{
				"crenya_expenses_enabled": 1,
				"crenya_expense_cash_supplier": CASH_SUPPLIER,
				"crenya_expense_supplier_group": SUPPLIER_GROUP,
				"crenya_expense_tax_template": VAT_TEMPLATE,
				"crenya_expense_add_needs_pin": 0,
			},
		)
		frappe.clear_document_cache("POS Profile", cls.profile.name)
		frappe.db.commit()  # nosemgrep

	def outlet(self):
		return frappe.get_doc("POS Profile", self.profile.name)

	def make_expense(self, **over):
		"""A POS Expense as the push handler makes it, before its booking."""
		values = {
			"supplier": None,
			"payee_name": "Gas man",
			"bill_no": None,
			"bill_date": nowdate(),
			"mode_of_payment": core.CASH,
			"currency": core.CURRENCY,
			"category": GAS,
			"amount": 10.5,
			"net_amount": 10.0,
			"tax_amount": 0.5,
			"taxes_and_charges": VAT_TEMPLATE,
			"description": None,
			"local_id": f"EXP-{uuid.uuid4().hex[:10]}",
			"company": core.COMPANY,
			"outlet": self.profile.name,
			"device": self.device_id,
			"shift": f"SH-{uuid.uuid4().hex[:8]}",
			"expense_date": nowdate(),
			"user": "Administrator",
			"status": booking.NOT_BOOKED,
		}
		lines = over.pop("lines", None) or [{"category": GAS, "description": "Cylinder", "amount": 10.0}]
		taxes = over.pop("taxes", [{"account_head": core.VAT_ACCOUNT, "rate": "5", "amount": "0.500"}])
		values.update(over)
		doc = frappe.new_doc(booking.EXPENSE)
		doc.update(values)
		for line in lines:
			doc.append("lines", line)
		shot = {
			"supplier": values["supplier"],
			"payee_name": values["payee_name"],
			"bill_no": values["bill_no"],
			"bill_date": values["bill_date"],
			"mode_of_payment": values["mode_of_payment"],
			"lines": [
				{
					"category": line["category"],
					"description": line.get("description"),
					"amount": money(line["amount"]),
				}
				for line in lines
			],
			"tax_template": values["taxes_and_charges"],
			"taxes": taxes,
			"net": money(values["net_amount"]),
			"tax": money(values["tax_amount"]),
			"total": money(values["amount"]),
			"notes": None,
		}
		booking.add_log(doc, booking.ADD, user="Administrator", device=self.device_id, after=shot)
		doc.insert(ignore_permissions=True)
		return doc

	def gl(self, voucher, account):
		rows = frappe.get_all(
			"GL Entry",
			filters={"voucher_no": voucher, "account": account, "is_cancelled": 0},
			fields=["debit", "credit"],
		)
		return Decimal(money(sum(r.debit for r in rows))), Decimal(money(sum(r.credit for r in rows)))


class TestExpenseBooking(ExpenseCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		if not booking.expenses_supported():
			raise unittest.SkipTest("erptrue_app's POS Expense with the till fields is not installed")
		super().setUpClass()

	def test_the_precision_is_the_sites_currency_decimals(self):
		self.assertEqual(booking.currency_precision(core.CURRENCY), 3)

	def test_pi_total_equals_cash_paid_out(self):
		doc = self.make_expense()
		self.assertEqual(booking.book(doc), booking.BOOKED)
		pi = frappe.get_doc("Purchase Invoice", doc.purchase_invoice)
		cash = cash_account()
		self.assertEqual((pi.docstatus, pi.is_paid, pi.update_stock), (1, 1, 0))
		self.assertEqual(Decimal(money(pi.grand_total)), Decimal("10.500"))
		self.assertEqual(Decimal(money(pi.paid_amount)), Decimal("10.500"))
		self.assertEqual(pi.cash_bank_account, cash)
		self.assertEqual(pi.supplier, CASH_SUPPLIER)
		self.assertEqual(str(pi.posting_date), nowdate())
		self.assertEqual(pi.items[0].expense_account, EXPENSE_ACCOUNT)
		self.assertEqual(pi.items[0].item_name, GAS)
		self.assertEqual([t.charge_type for t in pi.taxes], ["Actual"])
		self.assertEqual(self.gl(pi.name, cash), (Decimal("0.000"), Decimal("10.500")))
		self.assertEqual(self.gl(pi.name, core.VAT_ACCOUNT), (Decimal("0.500"), Decimal("0.000")))
		self.assertEqual(self.gl(pi.name, EXPENSE_ACCOUNT), (Decimal("10.000"), Decimal("0.000")))
		self.assertEqual([row.action for row in doc.log], ["add", "booked"])

	def test_a_two_row_template_books_the_tills_split_omr(self):
		# OMR: three decimals, the last row taking the rounding
		doc = self.make_expense(
			amount=10.501,
			net_amount=10.001,
			tax_amount=0.5,
			taxes_and_charges=GST_TEMPLATE,
			lines=[{"category": GAS, "description": None, "amount": 10.001}],
			taxes=[
				{"account_head": CGST, "rate": "2.5", "amount": "0.250"},
				{"account_head": SGST, "rate": "2.5", "amount": "0.250"},
			],
		)
		self.assertEqual(booking.book(doc), booking.BOOKED, msg=doc.posting_error)
		pi = frappe.get_doc("Purchase Invoice", doc.purchase_invoice)
		self.assertEqual(Decimal(money(pi.grand_total)), Decimal("10.501"))
		self.assertEqual(
			[Decimal(money(t.tax_amount)) for t in pi.taxes], [Decimal("0.250"), Decimal("0.250")]
		)
		self.assertEqual(self.gl(pi.name, cash_account()), (Decimal("0.000"), Decimal("10.501")))

	def test_rebook_reposts_one_pi(self):
		doc = self.make_expense()
		booking.book(doc)
		old = doc.purchase_invoice
		doc.lines[0].amount = 20.0
		doc.amount, doc.net_amount, doc.tax_amount = 21.0, 20.0, 1.0
		booking.add_log(
			doc,
			booking.EDIT,
			user="Administrator",
			after={
				"taxes": [{"account_head": core.VAT_ACCOUNT, "rate": "5", "amount": "1.000"}],
			},
		)
		doc.save(ignore_permissions=True)
		self.assertEqual(booking.rebook(doc), booking.BOOKED, msg=doc.posting_error)
		self.assertNotEqual(doc.purchase_invoice, old)
		self.assertEqual(frappe.db.get_value("Purchase Invoice", old, "docstatus"), 2)
		new = frappe.get_doc("Purchase Invoice", doc.purchase_invoice)
		self.assertEqual((new.docstatus, new.amended_from), (1, old))
		self.assertEqual(Decimal(money(new.grand_total)), Decimal("21.000"))
		cash = new.cash_bank_account
		credit = sum(self.gl(name, cash)[1] - self.gl(name, cash)[0] for name in (old, new.name))
		self.assertEqual(credit, Decimal("21.000"))
		self.assertEqual([row.action for row in doc.log], ["add", "booked", "edit", "booked"])

	def test_unbook_cancels_pi_keeps_row_and_log(self):
		doc = self.make_expense()
		booking.book(doc)
		pi = doc.purchase_invoice
		booking.add_log(doc, booking.DELETE, user="Administrator", reason="Entered twice")
		doc.save(ignore_permissions=True)
		self.assertEqual(booking.unbook(doc), booking.CANCELLED)
		self.assertEqual(frappe.db.get_value("Purchase Invoice", pi, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value(booking.EXPENSE, doc.name, "status"), booking.CANCELLED)
		self.assertEqual(booking.deleted_at_till([doc.name]), {doc.name})

	def test_failed_booking_keeps_row_not_booked_and_retry_books(self):
		# a mode of the outlet without an account for the company: the PI cannot be made
		mode = f"_Test Crenya Petty {uuid.uuid4().hex[:6]}"
		_insert({"doctype": "Mode of Payment", "mode_of_payment": mode, "type": "Cash", "enabled": 1})
		doc = self.make_expense(mode_of_payment=mode)
		self.assertEqual(booking.book(doc), booking.NOT_BOOKED)
		doc.reload()
		self.assertEqual((doc.status, doc.purchase_invoice), (booking.NOT_BOOKED, None))
		self.assertIn(mode, doc.posting_error)
		self.assertEqual([row.action for row in doc.log], ["add", "booking failed"])
		self.assertEqual(
			frappe.db.count("Purchase Invoice", {"remarks": ["like", f"%POS Expense {doc.name}%"]}), 0
		)
		mop = frappe.get_doc("Mode of Payment", mode)
		mop.append("accounts", {"company": core.COMPANY, "default_account": cash_account()})
		mop.save(ignore_permissions=True)
		self.assertEqual(booking.retry(doc), booking.BOOKED)
		doc.reload()
		self.assertEqual(doc.status, booking.BOOKED)
		self.assertFalse(doc.posting_error)
		self.assertEqual(frappe.db.get_value("Purchase Invoice", doc.purchase_invoice, "docstatus"), 1)
		self.assertEqual([row.action for row in doc.log][-1], "booked")
		# booked already: a second retry books nothing new
		first = doc.purchase_invoice
		booking.book(doc)
		self.assertEqual(doc.purchase_invoice, first)

	def test_the_failure_names_the_products_settings_page(self):
		self.outlet().db_set("crenya_expense_cash_supplier", None)
		frappe.clear_document_cache("POS Profile", self.profile.name)
		try:
			doc = self.make_expense()
			with self.assertRaises(booking.BookingError) as retail:
				booking.build_purchase_invoice(doc, self.outlet())
			self.assertIn("Shops → Settings → Expenses", str(retail.exception))
			with self.assertRaises(booking.BookingError) as restaurant:
				booking.build_purchase_invoice(doc, self.outlet(), product=RESTAURANT)
			self.assertIn("Restaurants → Settings → Expenses", str(restaurant.exception))
			# the product of an expense is its device's: a till is retail
			self.assertIs(product_of_expense(doc), RETAIL)
		finally:
			frappe.db.set_value(
				"POS Profile", self.profile.name, "crenya_expense_cash_supplier", CASH_SUPPLIER
			)
			frappe.clear_document_cache("POS Profile", self.profile.name)

	def test_duplicate_bill_no_books_with_remark(self):
		field = "check_supplier_invoice_uniqueness"
		before = frappe.db.get_single_value("Accounts Settings", field)
		frappe.db.set_single_value("Accounts Settings", field, 1)
		try:
			bill = f"B-{uuid.uuid4().hex[:6]}"
			first = self.make_expense(supplier=VEG_SHOP, payee_name=None, bill_no=bill)
			booking.book(first)
			second = self.make_expense(supplier=VEG_SHOP, payee_name=None, bill_no=bill)
			notes = []
			self.assertEqual(booking.book(second, notes), booking.BOOKED)
			pi = frappe.get_doc("Purchase Invoice", second.purchase_invoice)
			self.assertFalse(pi.bill_no)
			self.assertIn(f"Bill no. {bill} (already used by {first.purchase_invoice})", pi.remarks)
			self.assertEqual(second.bill_no, bill)
			self.assertTrue(any("is already on" in note for note in notes))
		finally:
			frappe.db.set_single_value("Accounts Settings", field, before or 0)

	def test_total_changed_by_erpnext_on_insert_is_not_booked(self):
		# e.g. a supplier's tax withholding: ERPNext's validate changes the total after the build
		controller = get_controller("Purchase Invoice")
		validate = controller.validate

		def changed(doc, *args, **kwargs):
			validate(doc, *args, **kwargs)
			doc.grand_total = flt(doc.grand_total) - 1

		doc = self.make_expense()
		with patch.object(controller, "validate", changed):
			self.assertEqual(booking.book(doc), booking.NOT_BOOKED)
		self.assertIn("is not the expense total", doc.posting_error)
		self.assertIsNone(doc.purchase_invoice)
		# the savepoint took the invoice back
		self.assertEqual(
			frappe.db.count("Purchase Invoice", {"remarks": ["like", f"%POS Expense {doc.name}%"]}), 0
		)
		# nothing changes it now: the retry books it
		self.assertEqual(booking.retry(doc), booking.BOOKED)

	def test_a_deadlock_is_retried_not_kept_not_booked(self):
		doc = self.make_expense()
		with patch.object(booking, "_post", side_effect=frappe.QueryDeadlockError):
			with self.assertRaises(frappe.QueryDeadlockError):
				booking.book(doc)
		self.assertEqual(frappe.db.get_value(booking.EXPENSE, doc.name, "status"), booking.NOT_BOOKED)
		self.assertFalse(frappe.db.get_value(booking.EXPENSE, doc.name, "posting_error"))

	def test_card_expense_books_from_card_account_not_cash(self):
		doc = self.make_expense(mode_of_payment=CARD)
		booking.book(doc)
		pi = frappe.get_doc("Purchase Invoice", doc.purchase_invoice)
		self.assertEqual((pi.mode_of_payment, pi.cash_bank_account), (CARD, BANK_ACCOUNT))
		self.assertEqual(self.gl(pi.name, BANK_ACCOUNT), (Decimal("0.000"), Decimal("10.500")))
		self.assertEqual(self.gl(pi.name, cash_account()), (Decimal("0.000"), Decimal("0.000")))

	def test_photo_attached_to_pi_once(self):
		doc = self.make_expense()
		booking.book(doc)
		content = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"bill-{uuid.uuid4().hex[:6]}.jpg",
				"content": b"\xff\xd8\xff\xe0jpeg",
				"is_private": 1,
			}
		).insert(ignore_permissions=True)
		doc.photo = content.file_url
		doc.save(ignore_permissions=True)
		booking.attach_photo_to_invoice(doc, doc.purchase_invoice)
		booking.attach_photo_to_invoice(doc, doc.purchase_invoice)
		files = frappe.get_all(
			"File",
			filters={"file_url": content.file_url, "attached_to_doctype": "Purchase Invoice"},
			fields=["attached_to_name", "is_private"],
		)
		self.assertEqual([(f.attached_to_name, f.is_private) for f in files], [(doc.purchase_invoice, 1)])

	def test_z_rows_list_the_shifts_expenses_and_leave_out_a_delete(self):
		shift = frappe._dict(local_id=f"SH-{uuid.uuid4().hex[:8]}", pos_profile=self.profile.name)
		kept = self.make_expense(shift=shift.local_id, payee_name="Gas man", bill_no="B-17")
		card = self.make_expense(shift=shift.local_id, payee_name="Lulu", mode_of_payment=CARD)
		gone = self.make_expense(shift=shift.local_id, payee_name="Typed twice")
		for doc in (kept, card, gone):
			booking.book(doc)
		booking.add_log(gone, booking.DELETE, user="Administrator", reason="Typed twice")
		gone.save(ignore_permissions=True)
		booking.unbook(gone)
		rows = reports.shift_expenses(shift, {core.CASH})
		self.assertEqual([r["payee"] for r in rows], ["Gas man", "Lulu"])
		self.assertEqual([r["is_cash"] for r in rows], [True, False])
		self.assertEqual([reports.expense_label(r) for r in rows][1], f"Lulu · {GAS} ({CARD})")
		self.assertEqual(sum(r["amount"] for r in rows), 21.0)

	def test_setup_checks_report_the_outlet(self):
		checks = {row["key"]: row for row in booking.setup_checks(self.outlet())}
		for key in (
			"module",
			"cash_mode",
			"cash_account",
			"payable_account",
			"expense_account",
			"categories",
			"tax_template",
			"stock_uom",
			"cash_supplier",
		):
			self.assertIn(key, checks)
			self.assertIn(checks[key]["status"], ("ok", "warn", "missing"))
		self.assertEqual(checks["module"]["status"], "ok")
		self.assertEqual(checks["cash_account"]["status"], "ok")
		self.assertEqual(checks["cash_supplier"]["status"], "ok")
		self.assertEqual(checks["tax_template"]["status"], "ok")

	def test_ensure_cash_supplier_idempotent(self):
		outlet = frappe.get_doc("POS Profile", self.profile.name)
		outlet.db_set("crenya_expense_cash_supplier", None)
		frappe.clear_document_cache("POS Profile", outlet.name)
		try:
			outlet = self.outlet()
			name, _created = booking.ensure_cash_supplier(outlet)
			again, created_again = booking.ensure_cash_supplier(self.outlet())
			self.assertTrue(name)
			self.assertEqual((again, created_again), (name, False))
			self.assertEqual(frappe.db.count("Supplier", {"name": name}), 1)
			# a disabled Cash Supplier is enabled again, never left on the outlet
			frappe.db.set_value("Supplier", name, "disabled", 1)
			third, _created = booking.ensure_cash_supplier(self.outlet())
			self.assertEqual(third, name)
			self.assertEqual(frappe.db.get_value("Supplier", name, "disabled"), 0)
		finally:
			frappe.db.set_value(
				"POS Profile", self.profile.name, "crenya_expense_cash_supplier", CASH_SUPPLIER
			)
			frappe.clear_document_cache("POS Profile", self.profile.name)

	def test_supplier_allowed_is_the_cash_supplier_or_the_group(self):
		outlet = self.outlet()
		self.assertTrue(booking.supplier_allowed(outlet, CASH_SUPPLIER))
		self.assertTrue(booking.supplier_allowed(outlet, VEG_SHOP))
		self.assertFalse(booking.supplier_allowed(outlet, "No such supplier"))

	def test_template_rows_are_the_companys_enabled_template(self):
		rows = booking.template_rows(VAT_TEMPLATE, core.COMPANY)
		self.assertEqual([row.account_head for row in rows], [core.VAT_ACCOUNT])
		self.assertIsNone(booking.template_rows(VAT_TEMPLATE, "Another Company"))
		self.assertIsNone(booking.template_rows(None, core.COMPANY))


class TestExpensesOff(ExpenseCase):
	"""A site without erptrue_app's POS Expense (or before its migrate): no expenses at all."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		if booking.expenses_supported():
			raise unittest.SkipTest("erptrue_app's POS Expense is installed on this site")
		super().setUpClass()

	def test_expenses_off_without_erptrue_app(self):
		self.assertFalse(booking.expenses_supported())
		checks = {row["key"]: row for row in booking.setup_checks(self.outlet())}
		self.assertEqual(checks["module"]["status"], "missing")
		shift = frappe._dict(local_id="SH-1", pos_profile=self.profile.name)
		self.assertEqual(reports.shift_expenses(shift, {core.CASH}), [])
