"""A customer's payment taken at the till on an ERPNext site (`Customer Payment`, retail P1 task 8):
one Payment Entry allocated oldest first with the rest as an advance, idempotent by event and by the
till's local id, refused (and booked once after a Retry) when ERPNext cannot book it.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_customer_payment
"""

import uuid
from decimal import Decimal

import frappe
from frappe.utils import flt, nowdate

from crenya_pos_api.api import web_admin
from crenya_pos_api.sync.approvals import APPROVER_ROLE
from crenya_pos_api.sync.context import POS_USER_ROLE
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import money, new_id
from crenya_pos_api.tests.test_integration_credit import CreditCase, make_customer
from crenya_pos_api.tests.web_admin_case import MANAGER, ensure_user

AGGREGATE = "Customer Payment"
PAYMENT_ENTRY = "Payment Entry"


def outstanding(invoice):
	return Decimal(money(frappe.db.get_value("Sales Invoice", invoice, "outstanding_amount")))


class PaymentCase(CreditCase):
	def payment(self, customer, amount, **over):
		payload = {
			"local_id": new_id(),
			"pos_profile": self.profile.name,
			"company": fixtures.COMPANY,
			"shift_local_id": new_id(),
			"business_date": nowdate(),
			"cashier": "Administrator",
			"customer_local_id": None,
			"customer": customer,
			"mode_of_payment": fixtures.CASH,
			"amount": amount,
			"currency": fixtures.CURRENCY,
			"reference": None,
			"note": None,
			"received_at": "2026-10-14T08:00:00Z",
		}
		payload.update(over)
		return payload

	def pay(self, payload, event_id=None):
		return self.push_one(self.event(payload, AGGREGATE, event_id=event_id))

	def sell(self, customer, total):
		"""A sale wholly on account: the invoice's name."""
		return self.post(self.on_account_sale(customer, total=total))["name"]

	def entries(self, local_id):
		return frappe.get_all(PAYMENT_ENTRY, filters={"crenya_local_id": local_id}, pluck="name")


class TestCustomerPayment(PaymentCase):
	def test_receipt_allocates_oldest_first(self):
		customer = make_customer("oldest", limit=None)
		first = self.sell(customer, "30.000")
		second = self.sell(customer, "50.000")
		third = self.sell(customer, "40.000")
		payload = self.payment(customer, "60.000")
		result = self.assertOk(self.pay(payload))
		self.assertEqual(result["doctype"], PAYMENT_ENTRY)
		self.assertEqual(
			result["payment"],
			{"allocated": "60.000", "unallocated": "0.000", "invoices": [first, second]},
		)
		self.assertEqual(
			(outstanding(first), outstanding(second), outstanding(third)),
			(Decimal("0.000"), Decimal("20.000"), Decimal("40.000")),
		)
		entry = frappe.get_doc(PAYMENT_ENTRY, result["name"])
		self.assertEqual((entry.docstatus, entry.payment_type, entry.party), (1, "Receive", customer))
		self.assertEqual(entry.crenya_local_id, payload["local_id"])
		self.assertEqual(Decimal(money(entry.paid_amount)), Decimal("60.000"))
		self.assertEqual(
			[(row.reference_name, Decimal(money(row.allocated_amount))) for row in entry.references],
			[(first, Decimal("30.000")), (second, Decimal("30.000"))],
		)

	def test_receipt_more_than_debt_leaves_advance(self):
		customer = make_customer("advance", limit=None)
		invoice = self.sell(customer, "25.000")
		result = self.assertOk(self.pay(self.payment(customer, "40.000")))
		self.assertEqual(
			result["payment"], {"allocated": "25.000", "unallocated": "15.000", "invoices": [invoice]}
		)
		self.assertEqual(outstanding(invoice), Decimal("0.000"))
		entry = frappe.get_doc(PAYMENT_ENTRY, result["name"])
		self.assertEqual(Decimal(money(entry.unallocated_amount)), Decimal("15.000"))

	def test_receipt_with_no_debt_is_all_advance(self):
		customer = make_customer("nodebt", limit=None)
		result = self.assertOk(self.pay(self.payment(customer, "12.500")))
		self.assertEqual(result["payment"], {"allocated": "0.000", "unallocated": "12.500", "invoices": []})

	def test_receipt_idempotent_by_local_id(self):
		customer = make_customer("idem", limit=None)
		invoice = self.sell(customer, "20.000")
		payload = self.payment(customer, "5.000")
		event_id = new_id()
		first = self.assertOk(self.pay(payload, event_id=event_id))
		again = self.pay(payload, event_id=event_id)
		self.assertEqual(again["status"], "duplicate")
		# the answer was lost and the till sent it under a new event id
		third = self.pay(payload)
		self.assertEqual(third["status"], "duplicate")
		self.assertEqual({first["name"], again["name"], third["name"]}, {first["name"]})
		self.assertEqual(len(self.entries(payload["local_id"])), 1)
		self.assertEqual(outstanding(invoice), Decimal("15.000"))
		self.assertEqual(third["payment"]["allocated"], "5.000")

	def test_the_local_id_is_null_never_empty_on_other_entries(self):
		customer = make_customer("nullid", limit=None)
		self.assertOk(self.pay(self.payment(customer, "1.000")))
		empty = frappe.db.sql("select count(*) from `tabPayment Entry` where crenya_local_id = ''")[0][0]
		self.assertEqual(empty, 0)

	def test_receipt_without_mode_account_is_refused_then_retried_once(self):
		customer = make_customer("noaccount", limit=None)
		invoice = self.sell(customer, "30.000")
		mode = f"_Test Crenya Receipt {uuid.uuid4().hex[:6]}"
		frappe.get_doc(
			{"doctype": "Mode of Payment", "mode_of_payment": mode, "type": "Cash", "enabled": 1}
		).insert(ignore_permissions=True)
		profile = frappe.get_doc("POS Profile", self.profile.name)
		profile.append("payments", {"mode_of_payment": mode})
		profile.flags.ignore_validate = True
		profile.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep
		try:
			payload = self.payment(customer, "30.000", mode_of_payment=mode)
			event_id = new_id()
			refused = self.pay(payload, event_id=event_id)
			self.assertError(refused, "validation", False)
			self.assertIn("account", refused["error"]["message"])
			self.assertEqual(self.entries(payload["local_id"]), [])
			self.assertEqual(outstanding(invoice), Decimal("30.000"))
			# the Refused page lists it, with the account hint
			ensure_user(MANAGER, [POS_USER_ROLE, APPROVER_ROLE])
			frappe.set_user(MANAGER)
			try:
				rows = web_admin.refused_sales(self.profile.name)["rows"]
			finally:
				frappe.set_user("Administrator")
			row = next(row for row in rows if row["name"] == event_id)
			self.assertEqual(row["aggregate_type"], AGGREGATE)
			self.assertEqual(row["local_id"], payload["local_id"])
			self.assertIn("account", row["hint"])
			# the owner adds the account; the till sends the same event again (Retry), twice
			mop = frappe.get_doc("Mode of Payment", mode)
			cash = frappe.db.get_value(
				"Mode of Payment Account",
				{"parent": fixtures.CASH, "company": fixtures.COMPANY},
				"default_account",
			)
			mop.append("accounts", {"company": fixtures.COMPANY, "default_account": cash})
			mop.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep
			booked = self.assertOk(self.pay(payload, event_id=event_id))
			again = self.pay(payload, event_id=event_id)
			self.assertEqual(again["status"], "duplicate")
			self.assertEqual(len(self.entries(payload["local_id"])), 1)
			self.assertEqual(booked["payment"]["invoices"], [invoice])
			self.assertEqual(outstanding(invoice), Decimal("0.000"))
		finally:
			profile = frappe.get_doc("POS Profile", self.profile.name)
			profile.payments = [row for row in profile.payments if row.mode_of_payment != mode]
			profile.flags.ignore_validate = True
			profile.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep

	def test_receipt_for_customer_outside_the_outlet_refused(self):
		outside = fixtures.make_customer(f"_TC receipt outside {new_id()[:6]}")
		saved_groups = [row.customer_group for row in self.profile.customer_groups]
		other_group = "_Test Crenya Other Receipt Customers"
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
			result = self.pay(self.payment(outside, "5.000"))
			self.assertError(result, "validation", False)
			self.assertIn("not a customer of outlet", result["error"]["message"])
		finally:
			set_groups(saved_groups)

	def test_receipt_amount_decimals_omr(self):
		customer = make_customer("omr", limit=None)
		invoice = self.sell(customer, "10.250")
		# four decimals on a 3-decimal currency: refused, nothing booked
		payload = self.payment(customer, "10.2501")
		self.assertError(self.pay(payload), "validation", False)
		self.assertEqual(self.entries(payload["local_id"]), [])
		result = self.assertOk(self.pay(self.payment(customer, "10.250")))
		self.assertEqual(result["payment"]["allocated"], "10.250")
		self.assertEqual(outstanding(invoice), Decimal("0.000"))
		self.assertEqual(flt(frappe.db.get_value("Payment Entry", result["name"], "paid_amount"), 3), 10.25)

	def test_receipt_other_currency_or_mode_not_on_the_outlet_refused(self):
		customer = make_customer("badmode", limit=None)
		self.assertError(self.pay(self.payment(customer, "1.000", currency="USD")), "validation", False)
		self.assertError(
			self.pay(self.payment(customer, "1.000", mode_of_payment="No such mode")), "validation", False
		)

	def test_receipt_for_new_customer_waits_for_customer_event(self):
		customer_event = self.customer_payload("Receipt Waiter")
		payload = self.payment(None, "7.000", customer_local_id=customer_event["local_id"])
		event_id = new_id()
		waiting = self.pay(payload, event_id=event_id)
		self.assertError(waiting, "dependency_missing", True)
		self.assertEqual(self.entries(payload["local_id"]), [])
		self.assertOk(self.push_one(self.event(customer_event, "Customer")))
		booked = self.assertOk(self.pay(payload, event_id=event_id))
		self.assertEqual(booked["payment"]["unallocated"], "7.000")
		customer = frappe.db.get_value("Customer", {"crenya_local_id": customer_event["local_id"]}, "name")
		self.assertEqual(frappe.db.get_value(PAYMENT_ENTRY, booked["name"], "party"), customer)

	def test_receipt_is_taken_with_the_credit_switch_off(self):
		"""Never refuse what the customer paid: the outlet's switch only governs new credit sales."""
		customer = make_customer("switchoff", limit=None)
		invoice = self.sell(customer, "10.000")
		self.set_switch(0)
		try:
			result = self.assertOk(self.pay(self.payment(customer, "10.000")))
		finally:
			self.set_switch(1)
		self.assertEqual(result["payment"]["invoices"], [invoice])

	def test_refused_list_includes_customer_payments(self):
		customer = make_customer("refusedlist", limit=None)
		payload = self.payment(customer, "5.000", currency="USD")
		event_id = new_id()
		self.assertError(self.pay(payload, event_id=event_id), "validation", False)
		ensure_user(MANAGER, [POS_USER_ROLE, APPROVER_ROLE])
		frappe.set_user(MANAGER)
		try:
			rows = web_admin.refused_sales(self.profile.name)["rows"]
		finally:
			frappe.set_user("Administrator")
		row = next(row for row in rows if row["name"] == event_id)
		self.assertEqual(row["aggregate_type"], AGGREGATE)
		self.assertTrue(row["hint"] and row["hint_ar"])
