"""Credit sales (0.13.0) on an ERPNext site: an invoice partly or wholly on the customer's account,
the credit status, ERPNext's limit check waived for the till's invoices (and only for them), the
credit return, the shift's credit fields, the customer's limit on the pull and the bootstrap.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_credit
"""

import uuid

import frappe
from frappe.utils import flt, nowdate

from crenya_pos_api.api import credit as credit_api
from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.api.auth import sign_in
from crenya_pos_api.sync import credit as credit_service
from crenya_pos_api.sync.approvals import APPROVAL_ACTIONS
from crenya_pos_api.sync.context import DEVICE_ROLE, POS_USER_ROLE, get_device_context
from crenya_pos_api.sync.credit import CREDIT_SALES_FIELD, credit_limit_waived
from crenya_pos_api.sync.errors import InvalidRequestError
from crenya_pos_api.sync.pull import CREDIT_SCOPE, CustomerSpec
from crenya_pos_api.tests import extension_dummy as dummy
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import SyncCase, money, new_id
from crenya_pos_api.tests.test_integration_device_users import PASSWORD, ensure_user

SHIFT = "Crenya POS Shift"
EVENT = "Crenya Sync Event"
CASHIER = "_test_crenya_credit_cashier@example.com"


def make_customer(label, limit=None, loyalty=None):
	"""A named customer with a credit limit row for the test company (none: no limit)."""
	name = f"_Test Crenya Credit {label} {uuid.uuid4().hex[:6]}"
	customer = fixtures.make_customer(name, loyalty)
	if limit is not None:
		doc = frappe.get_doc("Customer", customer)
		doc.append("credit_limits", {"company": fixtures.COMPANY, "credit_limit": limit})
		doc.save(ignore_permissions=True)
	return customer


class CreditCase(SyncCase):
	"""An outlet that sells on account."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.saved_switch = frappe.db.get_value("POS Profile", cls.profile.name, CREDIT_SALES_FIELD)
		cls.set_switch(1)

	@classmethod
	def tearDownClass(cls):
		cls.set_switch(cls.saved_switch or 0)
		super().tearDownClass()

	@classmethod
	def set_switch(cls, value):
		frappe.db.set_value("POS Profile", cls.profile.name, CREDIT_SALES_FIELD, value)
		frappe.db.commit()  # nosemgrep
		frappe.clear_document_cache("POS Profile", cls.profile.name)

	def on_account_sale(self, customer, total="100.000", cash="0.000", **over):
		"""A sale of `total` (two lines of half of it) with `cash` paid and the rest on account."""
		half = f"{float(total) / 2:.3f}"
		on_account = f"{float(total) - float(cash):.3f}"
		payments = [{"mode_of_payment": fixtures.CASH, "amount": cash}] if float(cash) else []
		payload = self.sale_payload(
			lines=[(fixtures.MILK, "2", half)],
			payments=payments,
			customer=customer,
			on_account=on_account,
			**over,
		)
		return payload

	def post(self, payload, **kwargs):
		return self.assertOk(self.push_one(self.event(payload, **kwargs)))

	def note_of(self, event):
		return frappe.db.get_value(EVENT, event["event_id"], "note")

	def desk_invoice(self, customer):
		"""A Sales Invoice made as a desk user would (on account, Administrator, no waiver)."""
		doc = frappe.new_doc("Sales Invoice")
		doc.update(
			{
				"customer": customer,
				"company": fixtures.COMPANY,
				"currency": fixtures.CURRENCY,
				"posting_date": nowdate(),
				"set_posting_time": 1,
				"posting_time": "12:00:00",
				"due_date": nowdate(),
				"selling_price_list": fixtures.PRICE_LIST,
				"ignore_pricing_rule": 1,
				"update_stock": 1,
				"cost_center": fixtures.COST_CENTER,
				"taxes_and_charges": fixtures.TAX_TEMPLATE,
			}
		)
		doc.append(
			"items", {"item_code": fixtures.MILK, "qty": 1, "rate": 10, "warehouse": fixtures.WAREHOUSE}
		)
		template = frappe.get_doc("Sales Taxes and Charges Template", fixtures.TAX_TEMPLATE)
		for row in template.taxes:
			doc.append(
				"taxes",
				{
					key: row.get(key)
					for key in (
						"charge_type",
						"account_head",
						"description",
						"rate",
						"included_in_print_rate",
						"cost_center",
					)
				},
			)
		return doc


class TestCreditStatus(CreditCase):
	def test_credit_status_matches_erpnext(self):
		from erpnext.selling.doctype.customer.customer import get_credit_limit, get_customer_outstanding

		customer = make_customer("status", limit=500)
		self.post(self.on_account_sale(customer, "120.000", cash="20.000"))
		status = credit_service.credit_status(customer, fixtures.COMPANY)
		self.assertEqual(status["customer"], customer)
		self.assertEqual(status["credit_limit"], "500.000")
		self.assertEqual(
			status["outstanding"], f"{flt(get_customer_outstanding(customer, fixtures.COMPANY)):.3f}"
		)
		self.assertEqual(status["outstanding"], "100.000")
		self.assertEqual(status["available"], "400.000")
		self.assertIs(status["over_limit"], False)
		self.assertEqual(flt(status["credit_limit"]), get_credit_limit(customer, fixtures.COMPANY))
		self.assertTrue(status["as_of"].endswith("Z"))

	def test_status_follows_the_limit_order_customer_group_company(self):
		group = fixtures.leaf_customer_group()
		customer = fixtures.make_customer(f"_TC Credit order {new_id()[:6]}")
		self.assertEqual(frappe.db.get_value("Customer", customer, "customer_group"), group)
		saved_company = frappe.db.get_value("Company", fixtures.COMPANY, "credit_limit")
		group_doc = frappe.get_doc("Customer Group", group)
		saved_rows = [row.as_dict() for row in group_doc.credit_limits]
		try:
			group_doc.set("credit_limits", [])
			group_doc.save(ignore_permissions=True)
			frappe.db.set_value("Company", fixtures.COMPANY, "credit_limit", 0)
			frappe.clear_document_cache("Company", fixtures.COMPANY)
			self.assertIsNone(credit_service.credit_status(customer, fixtures.COMPANY)["credit_limit"])
			# the company's
			frappe.db.set_value("Company", fixtures.COMPANY, "credit_limit", 700)
			frappe.clear_document_cache("Company", fixtures.COMPANY)
			self.assertEqual(
				credit_service.credit_status(customer, fixtures.COMPANY)["credit_limit"], "700.000"
			)
			# the group's beats the company's
			group_doc.append("credit_limits", {"company": fixtures.COMPANY, "credit_limit": 300})
			group_doc.save(ignore_permissions=True)
			self.assertEqual(
				credit_service.credit_status(customer, fixtures.COMPANY)["credit_limit"], "300.000"
			)
			# the customer's own beats both
			doc = frappe.get_doc("Customer", customer)
			doc.append("credit_limits", {"company": fixtures.COMPANY, "credit_limit": 100})
			doc.save(ignore_permissions=True)
			self.assertEqual(
				credit_service.credit_status(customer, fixtures.COMPANY)["credit_limit"], "100.000"
			)
		finally:
			group_doc.reload()
			group_doc.set("credit_limits", [])
			for row in saved_rows:
				group_doc.append(
					"credit_limits", {"company": row["company"], "credit_limit": row["credit_limit"]}
				)
			group_doc.save(ignore_permissions=True)
			frappe.db.set_value("Company", fixtures.COMPANY, "credit_limit", saved_company or 0)
			frappe.clear_document_cache("Company", fixtures.COMPANY)

	def test_no_limit_is_unlimited(self):
		customer = make_customer("nolimit", limit=None)
		self.post(self.on_account_sale(customer, "80.000"))
		saved = frappe.db.get_value("Company", fixtures.COMPANY, "credit_limit")
		frappe.db.set_value("Company", fixtures.COMPANY, "credit_limit", 0)
		frappe.clear_document_cache("Company", fixtures.COMPANY)
		try:
			status = credit_service.credit_status(customer, fixtures.COMPANY)
		finally:
			frappe.db.set_value("Company", fixtures.COMPANY, "credit_limit", saved or 0)
			frappe.clear_document_cache("Company", fixtures.COMPANY)
		self.assertEqual(
			(status["credit_limit"], status["available"], status["over_limit"]), (None, None, False)
		)
		self.assertEqual(status["outstanding"], "80.000")
		# a limit of 0 on the customer is no limit either
		zero = make_customer("zero", limit=0)
		self.assertIsNone(credit_service.credit_status(zero, fixtures.COMPANY)["credit_limit"])

	def test_status_api_and_unknown_customer_is_error(self):
		customer = make_customer("api", limit=150)
		status = credit_api.get_status(device_id=self.device_id, customer=customer)
		self.assertEqual(status["credit_limit"], "150.000")
		self.assertEqual(status["available"], "150.000")
		with self.assertRaises(InvalidRequestError):
			credit_api.get_status(device_id=self.device_id, customer="No such customer")
		with self.assertRaises(InvalidRequestError):
			credit_api.get_status(device_id=self.device_id, customer=None)

	def test_status_is_one_answer_for_nothing_to_see(self):
		customer = make_customer("uniform", limit=150)
		messages = set()
		for ask in (
			lambda: credit_api.get_status(device_id=self.device_id, customer="No such customer"),
			lambda: credit_api.get_invoice_outstanding(device_id=self.device_id, invoice="ACC-SINV-NOPE"),
		):
			with self.assertRaises(InvalidRequestError) as caught:
				ask()
			messages.add(str(caught.exception))
		# a customer outside the outlet's groups, and another outlet's invoice, answer the same
		outside = fixtures.make_customer(f"_TC outside {new_id()[:6]}")
		saved_groups = [row.customer_group for row in self.profile.customer_groups]
		other_group = "_Test Crenya Other Customers"
		if not frappe.db.exists("Customer Group", other_group):
			from frappe.utils.nestedset import get_root_of

			fixtures._insert(
				{
					"doctype": "Customer Group",
					"customer_group_name": other_group,
					"parent_customer_group": get_root_of("Customer Group"),
				}
			)

		def set_groups(groups):
			profile = frappe.get_doc("POS Profile", self.profile.name)
			profile.set("customer_groups", [{"customer_group": g} for g in groups])
			profile.flags.ignore_validate = True
			profile.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep
			frappe.clear_document_cache("POS Profile", self.profile.name)

		set_groups([other_group])
		try:
			with self.assertRaises(InvalidRequestError) as caught:
				credit_api.get_status(device_id=self.device_id, customer=outside)
			messages.add(str(caught.exception))
		finally:
			set_groups(saved_groups)
		sold = self.post(self.sale_payload(lines=[(fixtures.MILK, "2", "0.600")]))
		frappe.db.set_value("Sales Invoice", sold["name"], "pos_profile", "_Test Crenya Another Outlet")
		try:
			with self.assertRaises(InvalidRequestError) as caught:
				credit_api.get_invoice_outstanding(device_id=self.device_id, invoice=sold["name"])
			messages.add(str(caught.exception))
		finally:
			frappe.db.set_value("Sales Invoice", sold["name"], "pos_profile", self.profile.name)
		self.assertEqual(len(messages), 1)
		# an outlet that does not sell on account answers no credit question
		self.set_switch(0)
		try:
			with self.assertRaises(InvalidRequestError):
				credit_api.get_status(device_id=self.device_id, customer=customer)
		finally:
			self.set_switch(1)

	def test_status_is_for_tills(self):
		kds = new_id()
		device_api.register_device(
			device_id=kds, device_name="Kitchen", pos_profile=self.profile.name, device_type="kds"
		)
		customer = make_customer("kds", limit=150)
		with self.assertRaises(InvalidRequestError):
			credit_api.get_status(device_id=kds, customer=customer)

	def test_get_status_from_hooked_type_allowed(self):
		restaurant = new_id()
		device_api.register_device(
			device_id=restaurant,
			device_name="Dining",
			pos_profile=self.profile.name,
			device_type="restaurant_pos",
		)
		customer = make_customer("hooked", limit=150)
		with self.assertRaises(InvalidRequestError):
			credit_api.get_status(device_id=restaurant, customer=customer)
		with dummy.fake_hooks({"crenya_pos_credit_device_types": ["restaurant_pos"]}):
			status = credit_api.get_status(device_id=restaurant, customer=customer)
		self.assertEqual(status["customer"], customer)
		self.assertEqual(status["credit_limit"], "150.000")

	def test_invoice_outstanding_api(self):
		customer = make_customer("out", limit=500)
		sale = self.on_account_sale(customer, "100.000", cash="40.000")
		sold = self.post(sale)
		answer = credit_api.get_invoice_outstanding(device_id=self.device_id, invoice=sold["name"])
		self.assertEqual(
			(answer["name"], answer["grand_total"], answer["outstanding"]),
			(sold["name"], "100.000", "60.000"),
		)
		by_local_id = credit_api.get_invoice_outstanding(device_id=self.device_id, invoice=sale["local_id"])
		self.assertEqual(by_local_id["name"], sold["name"])
		with self.assertRaises(InvalidRequestError):
			credit_api.get_invoice_outstanding(device_id=self.device_id, invoice="ACC-SINV-NOPE")

	def test_with_the_switch_off_an_old_invoice_is_still_answered(self):
		customer = make_customer("offinvoice", limit=500)
		sale = self.on_account_sale(customer, "100.000", cash="40.000")
		sold = self.post(sale)
		self.set_switch(0)
		try:
			answer = credit_api.get_invoice_outstanding(device_id=self.device_id, invoice=sold["name"])
			self.assertEqual((answer["grand_total"], answer["outstanding"]), ("100.000", "60.000"))
			# the uniform not-found answer is unchanged
			with self.assertRaises(InvalidRequestError) as caught:
				credit_api.get_invoice_outstanding(device_id=self.device_id, invoice="ACC-SINV-NOPE")
			self.assertEqual(str(caught.exception), "Not found")
		finally:
			self.set_switch(1)

	def test_with_the_switch_off_a_customer_who_owes_is_answered_and_others_are_not(self):
		owes = make_customer("offowes", limit=500)
		clear = make_customer("offclear", limit=500)
		self.post(self.on_account_sale(owes, "100.000", cash="40.000"))
		self.set_switch(0)
		try:
			status = credit_api.get_status(device_id=self.device_id, customer=owes)
			self.assertEqual((status["outstanding"], status["available"]), ("60.000", "440.000"))
			with self.assertRaises(InvalidRequestError) as caught:
				credit_api.get_status(device_id=self.device_id, customer=clear)
			self.assertIn("switched off", str(caught.exception))
			# a customer who is not there, or not the outlet's, is still the same not-found answer
			with self.assertRaises(InvalidRequestError) as missing:
				credit_api.get_status(device_id=self.device_id, customer="No such customer")
			self.assertEqual(str(missing.exception), "Not found")
		finally:
			self.set_switch(1)
		# switched on again, the customer with nothing owing is answered
		self.assertEqual(
			credit_api.get_status(device_id=self.device_id, customer=clear)["outstanding"], "0.000"
		)


class TestOnAccountInvoice(CreditCase):
	def test_on_account_exact_omr(self):
		customer = make_customer("omr", limit=500)
		sale = self.on_account_sale(customer, "100.000", cash="40.000")
		result = self.post(sale)
		doc = frappe.get_doc("Sales Invoice", result["name"])
		self.assertEqual(doc.is_pos, 1)
		self.assertEqual(
			(flt(doc.grand_total, 3), flt(doc.paid_amount, 3), flt(doc.outstanding_amount, 3)),
			(100.0, 40.0, 60.0),
		)
		self.assertEqual(result["totals"]["outstanding_amount"], "60.000")
		self.assertEqual(
			[(row.mode_of_payment, flt(row.amount, 3)) for row in doc.payments], [("Cash", 40.0)]
		)
		# one minor unit more on account than the bill leaves: refused, exact, no tolerance
		off = self.on_account_sale(customer, "100.000", cash="40.000")
		off["on_account"] = "60.001"
		self.assertError(self.push_one(self.event(off)), "total_mismatch", False)
		self.assertFalse(self.invoices_with_local_id(off["local_id"]))
		# and one less
		less = self.on_account_sale(customer, "100.000", cash="40.000")
		less["on_account"] = "59.999"
		self.assertError(self.push_one(self.event(less)), "total_mismatch", False)

	def test_fully_on_account_posts_without_pos(self):
		customer = make_customer("full", limit=500)
		result = self.post(self.on_account_sale(customer, "100.000"))
		doc = frappe.get_doc("Sales Invoice", result["name"])
		self.assertEqual(doc.is_pos, 0)
		self.assertEqual(len(doc.payments), 0)
		self.assertEqual((flt(doc.grand_total, 3), flt(doc.outstanding_amount, 3)), (100.0, 100.0))
		self.assertEqual(doc.status, "Unpaid")
		# still the outlet's sale: reports and returns find it by its profile and shift
		self.assertEqual(doc.pos_profile, self.profile.name)
		self.assertEqual(doc.update_stock, 1)
		self.assertTrue(frappe.db.exists("Stock Ledger Entry", {"voucher_no": doc.name, "is_cancelled": 0}))
		# stock and taxes as a cash sale of the same lines
		cash = self.post(self.sale_payload(lines=[(fixtures.MILK, "2", "50.000")]))
		cash_doc = frappe.get_doc("Sales Invoice", cash["name"])
		self.assertEqual(flt(doc.total_taxes_and_charges, 3), flt(cash_doc.total_taxes_and_charges, 3))
		self.assertEqual(flt(doc.net_total, 3), flt(cash_doc.net_total, 3))
		self.assertEqual(
			sorted(flt(row.actual_qty) for row in self.sle(doc.name)),
			sorted(flt(row.actual_qty) for row in self.sle(cash_doc.name)),
		)

	def sle(self, name):
		return frappe.get_all(
			"Stock Ledger Entry", filters={"voucher_no": name, "is_cancelled": 0}, fields=["actual_qty"]
		)

	def test_over_limit_sale_posts_with_note(self):
		customer = make_customer("over", limit=150)
		self.post(self.on_account_sale(customer, "100.000"))
		event = self.event(self.on_account_sale(customer, "100.000"))
		result = self.assertOk(self.push_one(event))
		doc = frappe.get_doc("Sales Invoice", result["name"])
		self.assertEqual(doc.docstatus, 1)
		self.assertIn(f"Customer {customer} is over the credit limit by 50.000", self.note_of(event))

	def test_a_sale_inside_the_limit_has_no_note(self):
		customer = make_customer("inside", limit=500)
		event = self.event(self.on_account_sale(customer, "100.000"))
		self.assertOk(self.push_one(event))
		self.assertFalse(self.note_of(event))

	def test_cash_sale_to_over_limit_customer_posts(self):
		customer = make_customer("cashover", limit=150)
		self.post(self.on_account_sale(customer, "100.000"))
		self.post(self.on_account_sale(customer, "100.000"))
		cash = self.sale_payload(lines=[(fixtures.MILK, "2", "0.600")], customer=customer)
		result = self.assertOk(self.push_one(self.event(cash)))
		self.assertEqual(frappe.db.get_value("Sales Invoice", result["name"], "docstatus"), 1)

	def test_waiver_does_not_leak_to_other_requests(self):
		customer = make_customer("leak", limit=150)
		self.post(self.on_account_sale(customer, "100.000"))
		self.post(self.on_account_sale(customer, "100.000"))
		# the flag is gone when the push ends, and a desk user submitting the same still gets ERPNext's
		# refusal
		self.assertFalse(getattr(frappe.local, credit_service.WAIVER_FLAG, False))
		with self.assertRaises(frappe.ValidationError) as caught:
			doc = self.desk_invoice(customer)
			doc.insert(ignore_permissions=True)
			doc.submit()
		self.assertIn("Credit limit", str(caught.exception))
		frappe.db.rollback()
		# inside the waiver the same invoice posts; the waiver ends with the block, also on an error
		with credit_limit_waived():
			self.assertTrue(frappe.local.crenya_credit_waived)
		self.assertFalse(frappe.local.crenya_credit_waived)
		with self.assertRaises(ValueError), credit_limit_waived():
			raise ValueError("boom")
		self.assertFalse(frappe.local.crenya_credit_waived)

	def test_the_waiver_flag_is_not_seen_by_another_thread(self):
		import threading

		seen = []
		with credit_limit_waived():
			worker = threading.Thread(
				target=lambda: seen.append(getattr(frappe.local, credit_service.WAIVER_FLAG, False))
			)
			worker.start()
			worker.join()
		self.assertEqual(seen, [False])

	def test_the_waiver_is_installed_once(self):
		import erpnext.selling.doctype.customer.customer as customer_module

		credit_service._install_waiver()
		wrapped = customer_module.check_credit_limit
		credit_service._install_waiver()
		self.assertIs(customer_module.check_credit_limit, wrapped)

	def test_on_account_to_default_customer_refused(self):
		sale = self.on_account_sale(self.walk_in, "100.000")
		result = self.assertError(self.push_one(self.event(sale)), "validation", False)
		self.assertIn("named customer", result["error"]["message"])
		# the default customer left out of the payload is the default customer too
		sale = self.on_account_sale(self.walk_in, "100.000")
		sale["customer"] = None
		self.assertError(self.push_one(self.event(sale)), "validation", False)

	def test_a_credit_sale_made_while_the_switch_was_on_posts_when_it_is_off(self):
		"""Never refuse what the customer took: the owner switched it off before the sale synced."""
		customer = make_customer("off", limit=500)
		self.set_switch(0)
		try:
			event = self.event(self.on_account_sale(customer, "100.000"))
			result = self.assertOk(self.push_one(event))
			self.assertEqual(
				flt(frappe.db.get_value("Sales Invoice", result["name"], "outstanding_amount"), 3), 100.0
			)
			self.assertIn("switched off", self.note_of(event))
		finally:
			self.set_switch(1)

	def test_on_account_decimals_beyond_the_currency_refused(self):
		customer = make_customer("dec", limit=500)
		sale = self.on_account_sale(customer, "100.000", cash="40.000")
		sale["on_account"] = "60.0001"
		self.assertError(self.push_one(self.event(sale)), "validation", False)

	def test_old_till_invoice_without_on_account(self):
		customer = make_customer("old", limit=500)
		sale = self.sale_payload(lines=[(fixtures.MILK, "2", "0.600")], customer=customer)
		doc = frappe.get_doc("Sales Invoice", self.post(sale)["name"])
		self.assertEqual((doc.is_pos, flt(doc.outstanding_amount, 3)), (1, 0.0))
		# and with the outlet's switch off, as before
		self.set_switch(0)
		try:
			plain = self.sale_payload(lines=[(fixtures.MILK, "2", "0.600")], customer=customer)
			self.assertOk(self.push_one(self.event(plain)))
		finally:
			self.set_switch(1)

	def test_loyalty_points_follow_erpnext_on_the_full_total(self):
		customer = make_customer("loyal", limit=500, loyalty=fixtures.LOYALTY_PROGRAM)
		result = self.post(self.on_account_sale(customer, "100.000", cash="40.000"))
		points = frappe.get_all(
			"Loyalty Point Entry",
			filters={"invoice_type": "Sales Invoice", "invoice": result["name"]},
			fields=["loyalty_points", "purchase_amount"],
		)
		# Task 0: ERPNext earns on the whole bill, paid or not; no cap here
		self.assertEqual(
			[(row.loyalty_points, flt(row.purchase_amount, 3)) for row in points], [(100, 100.0)]
		)


class TestOnAccountPosting(CreditCase):
	def test_credit_only_posts_to_the_same_accounts_as_a_cash_sale(self):
		"""A profile with its own income and expense accounts: the plain (not POS) invoice of a sale
		wholly on account books revenue, cost of goods and tax to the same accounts and cost centres
		as the same sale for cash; only the customer's line differs."""
		income = "_Test Crenya Profile Income - _TCP"
		expense = "_Test Crenya Profile COGS - _TCP"
		for name, parent, kind in (
			(income, "Direct Income - _TCP", "Income Account"),
			(expense, "Direct Expenses - _TCP", "Cost of Goods Sold"),
		):
			if not frappe.db.exists("Account", name):
				fixtures._insert(
					{
						"doctype": "Account",
						"account_name": name.rsplit(" - ", 1)[0],
						"parent_account": parent,
						"company": fixtures.COMPANY,
						"account_type": kind,
						"account_currency": fixtures.CURRENCY,
					}
				)
		# a branch cost centre of its own, so a missing copy shows in the GL
		branch = "_Test Crenya Branch CC - _TCP"
		if not frappe.db.exists("Cost Center", branch):
			fixtures._insert(
				{
					"doctype": "Cost Center",
					"cost_center_name": "_Test Crenya Branch CC",
					"company": fixtures.COMPANY,
					"parent_cost_center": frappe.db.get_value(
						"Cost Center", {"company": fixtures.COMPANY, "is_group": 1}, "name"
					),
				}
			)
		fields = ("income_account", "expense_account", "cost_center")
		saved = frappe.db.get_value("POS Profile", self.profile.name, list(fields), as_dict=True)
		frappe.db.set_value(
			"POS Profile",
			self.profile.name,
			{"income_account": income, "expense_account": expense, "cost_center": branch},
		)
		frappe.clear_document_cache("POS Profile", self.profile.name)
		self.addCleanup(frappe.clear_document_cache, "POS Profile", self.profile.name)
		self.addCleanup(frappe.db.set_value, "POS Profile", self.profile.name, saved)
		customer = make_customer("gl", limit=500)
		credit = frappe.get_doc("Sales Invoice", self.post(self.on_account_sale(customer, "100.000"))["name"])
		cash = frappe.get_doc(
			"Sales Invoice",
			self.post(self.sale_payload(lines=[(fixtures.MILK, "2", "50.000")], customer=customer))["name"],
		)

		def lines(invoice):
			skip = {credit.debit_to, cash_account_of(cash)}
			return sorted(
				(row.account, flt(row.debit, 3), flt(row.credit, 3), row.cost_center)
				for row in frappe.get_all(
					"GL Entry",
					filters={"voucher_no": invoice.name, "is_cancelled": 0},
					fields=["account", "debit", "credit", "cost_center"],
				)
				if row.account not in skip
			)

		def cash_account_of(invoice):
			return invoice.payments[0].account

		self.assertEqual(lines(credit), lines(cash))
		self.assertTrue(any(row[0] == income for row in lines(cash)), "the profile's income account is used")
		self.assertTrue(any(row[3] == branch for row in lines(cash)), "the profile's cost centre is used")
		self.assertTrue(any(row[3] == branch for row in lines(credit)))

	def test_credit_with_a_loyalty_redemption_keeps_the_pos_shape(self):
		"""credit_only with a loyalty redemption keeps the POS shape: points pay part, the rest is
		on account."""
		customer = make_customer("loyalredeem", limit=500, loyalty=fixtures.LOYALTY_PROGRAM)
		self.post(self.on_account_sale(customer, "100.000", cash="100.000"))
		sale = self.on_account_sale(customer, "100.000")
		sale["on_account"] = "99.000"
		sale["loyalty"] = {"points": 100, "amount": "1.000"}
		result = self.post(sale)
		doc = frappe.get_doc("Sales Invoice", result["name"])
		self.assertEqual(doc.is_pos, 1)
		self.assertEqual(flt(doc.loyalty_amount, 3), 1.0)
		self.assertEqual(flt(doc.outstanding_amount, 3), 99.0)


class TestCreditReturn(CreditCase):
	def sold(self, customer):
		sale = self.on_account_sale(customer, "100.000", cash="40.000")
		return sale, self.post(sale)

	def credit_return(self, sale, offset, payments=None, **over):
		ret = self.return_payload(sale, [(fixtures.MILK, "-1", "50.000", 1)], credit_offset=offset, **over)
		ret["payments"] = payments if payments is not None else []
		return ret

	def outstanding(self, name):
		return flt(frappe.db.get_value("Sales Invoice", name, "outstanding_amount"), 3)

	def test_a_return_credited_whole_to_the_account(self):
		customer = make_customer("ret", limit=500)
		sale, sold = self.sold(customer)
		ret = self.credit_return(sale, "50.000")
		result = self.assertOk(self.push_one(self.event(ret)))
		doc = frappe.get_doc("Sales Invoice", result["name"])
		self.assertEqual((doc.is_return, doc.update_outstanding_for_self, len(doc.payments)), (1, 0, 0))
		self.assertEqual(self.outstanding(sold["name"]), 10.0)

	def test_a_return_partly_refunded(self):
		customer = make_customer("retpart", limit=500)
		sale, sold = self.sold(customer)
		ret = self.credit_return(
			sale, "30.000", payments=[{"mode_of_payment": fixtures.CASH, "amount": "-20.000"}]
		)
		result = self.assertOk(self.push_one(self.event(ret)))
		doc = frappe.get_doc("Sales Invoice", result["name"])
		self.assertEqual([flt(row.amount, 3) for row in doc.payments], [-20.0])
		self.assertEqual(self.outstanding(sold["name"]), 30.0)

	def test_the_return_split_is_exact(self):
		customer = make_customer("retexact", limit=500)
		sale, sold = self.sold(customer)
		short = self.credit_return(
			sale, "30.000", payments=[{"mode_of_payment": fixtures.CASH, "amount": "-19.999"}]
		)
		self.assertError(self.push_one(self.event(short)), "total_mismatch", False)
		none = self.credit_return(sale, "49.999")
		self.assertError(self.push_one(self.event(none)), "total_mismatch", False)
		self.assertEqual(self.outstanding(sold["name"]), 60.0)

	def test_a_credit_above_the_return_is_refused(self):
		customer = make_customer("retmore", limit=500)
		sale, _sold = self.sold(customer)
		more = self.credit_return(sale, "50.001")
		self.assertError(self.push_one(self.event(more)), "validation", False)

	def test_a_return_without_credit_offset_refunds_as_before(self):
		customer = make_customer("retplain", limit=500)
		sale, _sold = self.sold(customer)
		ret = self.return_payload(sale, [(fixtures.MILK, "-1", "50.000", 1)])
		result = self.assertOk(self.push_one(self.event(ret)))
		doc = frappe.get_doc("Sales Invoice", result["name"])
		self.assertEqual([flt(row.amount, 3) for row in doc.payments], [-50.0])
		self.assertEqual(doc.update_outstanding_for_self, 1)


class TestCreditShift(CreditCase):
	def test_old_till_shift_without_on_account(self):
		result = self.assertOk(self.push_one(self.event(self.shift_payload(), SHIFT)))
		doc = frappe.get_doc(SHIFT, result["name"])
		self.assertEqual(
			(doc.on_account_sales_count, doc.on_account_sales_amount, doc.on_account_received_count),
			(0, 0, 0),
		)
		self.assertEqual(doc.payments[0].received, 0)

	def test_shift_on_account_saved(self):
		shift = self.shift_payload(
			on_account={
				"sales_count": 2,
				"sales_amount": "90.000",
				"returned_amount": "5.000",
				"received_count": 1,
				"received_amount": "40.000",
			}
		)
		shift["payments"][0]["received"] = "40.000"
		result = self.assertOk(self.push_one(self.event(shift, SHIFT)))
		doc = frappe.get_doc(SHIFT, result["name"])
		self.assertEqual((doc.on_account_sales_count, doc.on_account_received_count), (2, 1))
		self.assertEqual(
			[
				flt(doc.get(f"on_account_{key}"), 3)
				for key in ("sales_amount", "returned_amount", "received_amount")
			],
			[90.0, 5.0, 40.0],
		)
		self.assertEqual(flt(doc.payments[0].received, 3), 40.0)
		# the till's expected is stored as sent: the server does not recompute the drawer
		self.assertEqual(flt(doc.payments[0].expected, 3), 22.4)

	def test_shift_amounts_beyond_the_currency_refused(self):
		shift = self.shift_payload(on_account={"sales_count": 1, "sales_amount": "90.0001"})
		self.assertError(self.push_one(self.event(shift, SHIFT)), "validation", False)
		self.assertFalse(frappe.db.exists(SHIFT, shift["local_id"]))
		bad = self.shift_payload()
		bad["payments"][0]["received"] = "-1.000"
		self.assertError(self.push_one(self.event(bad, SHIFT)), "validation", False)
		fine = self.shift_payload()
		fine["payments"][0]["received"] = "40.0001"
		self.assertError(self.push_one(self.event(fine, SHIFT)), "validation", False)
		self.assertFalse(frappe.db.exists(SHIFT, fine["local_id"]))


class TestCreditSync(CreditCase):
	def pull(self, device_id=None):
		records, _cursor, _pages = self._pull_all("customer", limit=500, device_id=device_id)
		return {row["name"]: row for row in records}

	def test_customer_pull_carries_credit_limit(self):
		limited = make_customer("pull", limit=500)
		unlimited = make_customer("pullnone", limit=None)
		records = self.pull()
		self.assertEqual(records[limited]["credit_limit"], "500.000")
		self.assertIn("credit_limit", records[unlimited])
		self.assertIsNone(records[unlimited]["credit_limit"])

	def test_switching_credit_on_sends_the_customers_again_with_their_limit(self):
		limited = make_customer("rescope", limit=500)
		self.set_switch(0)
		try:
			first = sync_api.pull_changes(device_id=self.device_id, entity="customer", limit=500)
			self.assertNotIn("credit_limit", {k for r in first["records"] for k in r})
			cursor = first["next_cursor"]
			while first["has_more"]:
				first = sync_api.pull_changes(
					device_id=self.device_id, entity="customer", cursor=cursor, limit=500
				)
				cursor = first["next_cursor"]
		finally:
			self.set_switch(1)
		# the owner turned credit on: the till's old cursor restarts the customers once
		again = sync_api.pull_changes(device_id=self.device_id, entity="customer", cursor=cursor, limit=500)
		seen = list(again["records"])
		while again["has_more"]:
			again = sync_api.pull_changes(
				device_id=self.device_id, entity="customer", cursor=again["next_cursor"], limit=500
			)
			seen += again["records"]
		self.assertEqual({r["name"]: r for r in seen}[limited]["credit_limit"], "500.000")
		# and it settles: nothing is sent again on the next pull
		after = sync_api.pull_changes(
			device_id=self.device_id, entity="customer", cursor=again["next_cursor"], limit=500
		)
		self.assertEqual(after["records"], [])

	def test_the_pulled_limits_are_erpnexts_for_a_whole_page(self):
		from erpnext.selling.doctype.customer.customer import get_credit_limit

		group = fixtures.leaf_customer_group()
		customers = [make_customer("page own", limit=250), make_customer("page none")]
		rows = frappe.get_all(
			"Customer", filters={"name": ["in", customers]}, fields=["name", "customer_group"]
		)
		saved = frappe.db.get_value("Company", fixtures.COMPANY, "credit_limit")
		group_doc = frappe.get_doc("Customer Group", group)
		saved_rows = [(r.company, r.credit_limit) for r in group_doc.credit_limits]
		try:
			for company_limit, group_limit, bypass in (
				(0, None, 0),
				(700, None, 0),
				(700, 300, 0),
				(700, 300, 1),
				(0, 300, 1),
			):
				frappe.db.set_value("Company", fixtures.COMPANY, "credit_limit", company_limit)
				frappe.clear_document_cache("Company", fixtures.COMPANY)
				group_doc.reload()
				group_doc.set("credit_limits", [])
				if group_limit:
					group_doc.append(
						"credit_limits",
						{
							"company": fixtures.COMPANY,
							"credit_limit": group_limit,
							"bypass_credit_limit_check": bypass,
						},
					)
				group_doc.save(ignore_permissions=True)
				batch = credit_service.customer_credit_limits(rows, fixtures.COMPANY)
				for name in customers:
					erpnext = flt(get_credit_limit(name, fixtures.COMPANY))
					self.assertEqual(
						flt(batch[name] or 0), erpnext, (name, company_limit, group_limit, bypass)
					)
		finally:
			group_doc.reload()
			group_doc.set("credit_limits", [])
			for company, limit in saved_rows:
				group_doc.append("credit_limits", {"company": company, "credit_limit": limit})
			group_doc.save(ignore_permissions=True)
			frappe.db.set_value("Company", fixtures.COMPANY, "credit_limit", saved or 0)
			frappe.clear_document_cache("Company", fixtures.COMPANY)

	def test_patch_v0_13_twice_changes_no_approval(self):
		from crenya_pos_api.patches.v0_13 import credit_fields

		fields = [field for _key, field, *_ in APPROVAL_ACTIONS]
		saved = frappe.db.get_value("POS Profile", self.profile.name, fields, as_dict=True)
		self.addCleanup(frappe.clear_document_cache, "POS Profile", self.profile.name)
		self.addCleanup(frappe.db.set_value, "POS Profile", self.profile.name, saved)
		frappe.db.set_value("POS Profile", self.profile.name, dict(zip(fields, [1, 0] * 8, strict=False)))
		frappe.db.commit()  # nosemgrep
		before = frappe.db.get_value("POS Profile", self.profile.name, fields, as_dict=True)
		credit_fields.execute()
		credit_fields.execute()
		self.assertEqual(frappe.db.get_value("POS Profile", self.profile.name, fields, as_dict=True), before)
		self.assertEqual(frappe.db.get_value("POS Profile", self.profile.name, CREDIT_SALES_FIELD), 1)
		self.assertTrue(frappe.get_meta("Payment Entry").has_field("crenya_local_id"))

	def test_pull_credit_limit_only_for_credit_device(self):
		limited = make_customer("pulltype", limit=500)
		other = new_id()
		device_api.register_device(
			device_id=other, device_name="Waiter", pos_profile=self.profile.name, device_type="waiter"
		)
		till_records = self.pull()
		self.assertEqual(till_records[limited]["credit_limit"], "500.000")
		first = sync_api.pull_changes(device_id=other, entity="customer", limit=500)
		self.assertNotIn(CREDIT_SCOPE, first["next_cursor"] or "")
		records = {row["name"]: row for row in first["records"]}
		self.assertIn(limited, records)
		self.assertNotIn("credit_limit", records[limited])
		# a type the hook lists gets the limit and the credit scope, as a till does
		with dummy.fake_hooks({"crenya_pos_credit_device_types": ["waiter"]}):
			listed = {row["name"]: row for row in self.pull(device_id=other)}
		self.assertEqual(listed[limited]["credit_limit"], "500.000")

	def test_retail_till_pull_unchanged(self):
		"""A till's record shape is 0.13.2's: the limit key on every customer, the credit scope."""
		limited = make_customer("pulltill", limit=500)
		unlimited = make_customer("pulltillnone", limit=None)
		with dummy.fake_hooks({"crenya_pos_credit_device_types": ["restaurant_pos"]}):
			records = self.pull()
		self.assertEqual(records[limited]["credit_limit"], "500.000")
		self.assertIsNone(records[unlimited]["credit_limit"])
		self.assertEqual(
			sorted(records[unlimited]),
			sorted(
				[
					"name",
					"customer_name",
					"customer_group",
					"mobile_no",
					"email_id",
					"tax_id",
					"disabled",
					"crenya_local_id",
					"modified",
					"credit_limit",
				]
			),
		)
		self.assertEqual(CustomerSpec().cursor_scope(get_device_context(self.device_id)), CREDIT_SCOPE)

	def test_an_outlet_without_credit_sends_no_limit(self):
		limited = make_customer("pulloff", limit=500)
		self.set_switch(0)
		try:
			self.assertNotIn("credit_limit", self.pull()[limited])
		finally:
			self.set_switch(1)

	def test_bootstrap_credit_block_till_only(self):
		# the shared profile's approvals depend on the tests that ran before (a patch test switches
		# them off): set the two credit ones as a new profile has them
		fields = ("crenya_approve_credit_over_limit", "crenya_approve_credit_sale")
		saved = frappe.db.get_value("POS Profile", self.profile.name, list(fields), as_dict=True)
		frappe.db.set_value("POS Profile", self.profile.name, dict(zip(fields, (1, 0), strict=True)))
		frappe.clear_document_cache("POS Profile", self.profile.name)
		self.addCleanup(frappe.clear_document_cache, "POS Profile", self.profile.name)
		self.addCleanup(frappe.db.set_value, "POS Profile", self.profile.name, saved)
		boot = device_api.get_bootstrap(device_id=self.device_id)
		self.assertEqual(boot["credit"], {"enabled": True})
		self.assertEqual(boot["features"]["credit_sales"], 1)
		self.assertIn("credit_over_limit", boot["approvals"]["required"])
		self.assertNotIn("credit_sale", boot["approvals"]["required"])
		self.set_switch(0)
		try:
			self.assertEqual(device_api.get_bootstrap(device_id=self.device_id)["credit"], {"enabled": False})
		finally:
			self.set_switch(1)
		kds = new_id()
		device_api.register_device(
			device_id=kds, device_name="Kitchen", pos_profile=self.profile.name, device_type="kds"
		)
		other = device_api.get_bootstrap(device_id=kds)
		self.assertNotIn("credit", other)
		self.assertNotIn("credit_sales", other.get("features", {}))


class TestCreditAsDeviceUser(CreditCase):
	"""The push as the device user, who holds the device role only: the over-limit sale posts, a
	cash sale to the over-limit customer posts, and the limit stays enforced for everyone else."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._saved_throttle = frappe.conf.get("throttle_user_limit")
		frappe.conf.throttle_user_limit = 10000
		cls.person = ensure_user(CASHIER, [POS_USER_ROLE, "Sales User"])
		frappe.db.commit()  # nosemgrep

	@classmethod
	def tearDownClass(cls):
		if cls._saved_throttle is None:
			frappe.conf.pop("throttle_user_limit", None)
		else:
			frappe.conf.throttle_user_limit = cls._saved_throttle
		super().tearDownClass()

	def test_the_device_user_posts_over_the_limit(self):
		frappe.local.request_ip = "127.0.0.1"
		device_id = new_id()
		answer = sign_in(self.person, PASSWORD, device_id)
		user = answer["device_user"]
		self.assertEqual(set(frappe.get_roles(user)) - {"All", "Guest"}, {DEVICE_ROLE})
		frappe.set_user(user)
		try:
			device = device_api.register_device(
				device_id=device_id,
				device_name="Credit till",
				pos_profile=self.profile.name,
				app_version="0.9.0",
				platform="linux",
			)
		finally:
			frappe.set_user("Administrator")
		frappe.db.commit()  # nosemgrep
		customer = make_customer("devuser", limit=150)

		def push(payload):
			payload["offline_number"] = f"OFF-{device['device_short']}-{uuid.uuid4().hex[:6]}"
			frappe.set_user(user)
			try:
				return sync_api.push_batch(device_id=device_id, events=[self.event(payload)])[0]
			finally:
				frappe.set_user("Administrator")

		self.assertOk(push(self.on_account_sale(customer, "100.000")))
		over = self.assertOk(push(self.on_account_sale(customer, "100.000")))
		self.assertEqual(frappe.db.get_value("Sales Invoice", over["name"], "owner"), user)
		self.assertOk(push(self.sale_payload(lines=[(fixtures.MILK, "2", "0.600")], customer=customer)))
		# a part-paid sale (POS shape) and a return credited to the account, as the device user
		part = self.on_account_sale(customer, "100.000", cash="40.000")
		sold = self.assertOk(push(part))
		self.assertEqual(
			flt(frappe.db.get_value("Sales Invoice", sold["name"], "outstanding_amount"), 3), 60.0
		)
		ret = self.return_payload(part, [(fixtures.MILK, "-1", "50.000", 1)], credit_offset="50.000")
		ret["payments"] = []
		credited = self.assertOk(push(ret))
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", credited["name"], "update_outstanding_for_self"), 0
		)
		self.assertEqual(
			flt(frappe.db.get_value("Sales Invoice", sold["name"], "outstanding_amount"), 3), 10.0
		)


class TestApprovalsContract(CreditCase):
	"""With credit sales on, the till's bootstrap always carries the approvals block with the credit
	actions under exactly these keys and the site's approver roles (golden)."""

	def test_credit_approvals_block_golden(self):
		fields = ("crenya_approve_credit_over_limit", "crenya_approve_credit_sale")
		saved = frappe.db.get_value("POS Profile", self.profile.name, list(fields), as_dict=True)
		self.addCleanup(frappe.clear_document_cache, "POS Profile", self.profile.name)
		self.addCleanup(frappe.db.set_value, "POS Profile", self.profile.name, saved)
		for over, sale in ((1, 0), (1, 1), (0, 0)):
			frappe.db.set_value(
				"POS Profile", self.profile.name, dict(zip(fields, (over, sale), strict=True))
			)
			frappe.clear_document_cache("POS Profile", self.profile.name)
			block = device_api.get_bootstrap(device_id=self.device_id)["approvals"]
			self.assertEqual(block["approver_roles"], ["Crenya POS Manager"])
			self.assertEqual(
				[(row["action"], row["label"], row["label_ar"]) for row in block["labels"]],
				[
					("credit_over_limit", "Sell on account over the limit", "بيع آجل فوق الحد"),
					("credit_sale", "Sell on account", "بيع بالآجل"),
				],
			)
			self.assertEqual("credit_over_limit" in block["required"], bool(over))
			self.assertEqual("credit_sale" in block["required"], bool(sale))
		self.set_switch(0)
		try:
			self.assertNotIn("labels", device_api.get_bootstrap(device_id=self.device_id)["approvals"])
		finally:
			self.set_switch(1)
