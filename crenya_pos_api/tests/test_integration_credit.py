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
from crenya_pos_api.sync.context import DEVICE_ROLE, POS_USER_ROLE, get_device_context
from crenya_pos_api.sync.credit import CREDIT_SALES_FIELD, credit_limit_waived
from crenya_pos_api.sync.errors import InvalidRequestError
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

	def test_status_other_company_refused(self):
		customer = make_customer("company", limit=150)
		ctx = get_device_context(self.device_id)
		with self.assertRaises(InvalidRequestError):
			credit_service.status_for(ctx, customer, company="_Test Crenya Other Company")
		self.assertEqual(
			credit_service.status_for(ctx, customer, company=fixtures.COMPANY)["credit_limit"], "150.000"
		)

	def test_status_is_for_tills(self):
		kds = new_id()
		device_api.register_device(
			device_id=kds, device_name="Kitchen", pos_profile=self.profile.name, device_type="kds"
		)
		customer = make_customer("kds", limit=150)
		with self.assertRaises(InvalidRequestError):
			credit_api.get_status(device_id=kds, customer=customer)

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

	def test_on_account_when_outlet_switch_off_refused(self):
		customer = make_customer("off", limit=500)
		self.set_switch(0)
		try:
			result = self.assertError(
				self.push_one(self.event(self.on_account_sale(customer, "100.000"))), "validation", False
			)
			self.assertIn("switched off", result["error"]["message"])
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
