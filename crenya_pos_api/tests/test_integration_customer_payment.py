"""A customer's payment taken at the till on an ERPNext site (`Customer Payment`, retail P1 task 8):
one Payment Entry allocated oldest first with the rest as an advance, idempotent by event and by the
till's local id, refused (and booked once after a Retry) when ERPNext cannot book it.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_customer_payment
"""

import threading
import time
import uuid
from decimal import Decimal
from unittest import mock

import frappe
from frappe.utils import add_days, flt, nowdate

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.api import web_admin
from crenya_pos_api.api.auth import sign_in
from crenya_pos_api.sync import customer_payment as payments
from crenya_pos_api.sync import events
from crenya_pos_api.sync.approvals import APPROVER_ROLE
from crenya_pos_api.sync.context import DEVICE_ROLE, POS_USER_ROLE
from crenya_pos_api.sync.errors import PERMISSION
from crenya_pos_api.tests import extension_dummy as dummy
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import money, new_id
from crenya_pos_api.tests.test_integration_credit import CreditCase, make_customer
from crenya_pos_api.tests.test_integration_device_users import PASSWORD
from crenya_pos_api.tests.test_integration_device_users import ensure_user as ensure_person
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

	def test_the_typed_reference_is_the_payment_entrys_reference_no(self):
		customer = make_customer("refno", limit=None)
		payload = self.payment(customer, "7.000", reference="CARD-SLIP-77")
		result = self.assertOk(self.pay(payload))
		entry = frappe.get_doc(PAYMENT_ENTRY, result["name"])
		self.assertEqual(entry.reference_no, "CARD-SLIP-77")
		self.assertEqual(entry.crenya_local_id, payload["local_id"])
		self.assertIn("reference CARD-SLIP-77", entry.remarks)
		# a resend finds the entry by the till's id, not by the reference
		again = self.pay(payload)
		self.assertEqual(again["status"], "duplicate")
		self.assertEqual(again["name"], result["name"])
		self.assertEqual(len(self.entries(payload["local_id"])), 1)

	def test_without_a_typed_reference_the_till_id_stays_the_reference_no(self):
		customer = make_customer("norefno", limit=None)
		payload = self.payment(customer, "3.000")
		entry = frappe.get_doc(PAYMENT_ENTRY, self.assertOk(self.pay(payload))["name"])
		self.assertEqual(entry.reference_no, payload["local_id"])

	def test_a_blank_reference_is_none_and_the_till_id_stays_the_reference_no(self):
		customer = make_customer("blankref", limit=None)
		payload = self.payment(customer, "2.000", reference="   ")
		entry = frappe.get_doc(PAYMENT_ENTRY, self.assertOk(self.pay(payload))["name"])
		self.assertEqual(entry.reference_no, payload["local_id"])
		self.assertNotIn("reference", entry.remarks)

	def test_a_reference_is_stripped_and_one_over_140_characters_is_refused(self):
		customer = make_customer("longref", limit=None)
		exactly = "R" * 140
		payload = self.payment(customer, "2.000", reference=f"  {exactly}  ")
		entry = frappe.get_doc(PAYMENT_ENTRY, self.assertOk(self.pay(payload))["name"])
		self.assertEqual(entry.reference_no, exactly)
		too_long = self.payment(customer, "2.000", reference="R" * 141)
		self.assertError(self.pay(too_long), "validation", False)
		self.assertEqual(self.entries(too_long["local_id"]), [])

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

	def test_customer_payment_from_unlisted_type_refused(self):
		"""Waiters and kitchen displays never take a customer payment; a type an app lists does."""
		customer = make_customer("types", limit=None)
		self.sell(customer, "10.000")
		restaurant = new_id()
		device_api.register_device(
			device_id=restaurant,
			device_name="Dining",
			pos_profile=self.profile.name,
			device_type="restaurant_pos",
		)
		payload = self.payment(customer, "10.000")
		event = self.event(payload, AGGREGATE)
		result = sync_api.push_batch(device_id=restaurant, events=[event])[0]
		self.assertEqual(result["status"], "error")
		self.assertEqual(result["error"]["code"], PERMISSION)
		self.assertIn("not on a restaurant_pos device", result["error"]["message"])
		self.assertEqual(self.entries(payload["local_id"]), [])
		with dummy.fake_hooks({"crenya_pos_credit_device_types": ["restaurant_pos"]}):
			self.assertOk(sync_api.push_batch(device_id=restaurant, events=[event])[0])
		self.assertEqual(len(self.entries(payload["local_id"])), 1)

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


class TestShiftCashierDevice(PaymentCase):
	"""0.16.0: a till's Payment Entry records its shift, cashier and device (Task 8 reads them)."""

	def entry_of(self, payload):
		result = self.assertOk(self.pay(payload))
		return frappe.get_doc(PAYMENT_ENTRY, result["name"])

	def test_till_payment_has_shift_cashier_device(self):
		customer = make_customer("shiftcols", limit=None)
		cashier = ensure_person("shift.cashier@example.com", [POS_USER_ROLE, "Sales User"])
		entry = self.entry_of(self.payment(customer, "5.000", shift_local_id="S-1", cashier=cashier))
		self.assertEqual(entry.crenya_shift_id, "S-1")
		self.assertEqual(entry.crenya_cashier, cashier)
		self.assertEqual(entry.crenya_device, self.device_id)

	def test_payment_without_shift_leaves_field_empty(self):
		customer = make_customer("noshift", limit=None)
		entry = self.entry_of(self.payment(customer, "5.000", shift_local_id=None))
		self.assertFalse(entry.crenya_shift_id)
		self.assertEqual(entry.crenya_device, self.device_id)

	def test_unknown_cashier_falls_back_to_session_user(self):
		customer = make_customer("nocashier", limit=None)
		entry = self.entry_of(self.payment(customer, "5.000", cashier="nobody@nowhere.invalid"))
		self.assertEqual(entry.crenya_cashier, payments.enabled_user("Administrator"))

	def test_patch_idempotent(self):
		from crenya_pos_api.patches.v0_16 import payment_entry_shift_fields

		customer = make_customer("patchtwice", limit=None)
		entry = self.entry_of(self.payment(customer, "5.000", shift_local_id="S-9"))
		before = frappe.db.get_value(PAYMENT_ENTRY, entry.name, "modified")
		payment_entry_shift_fields.execute()
		payment_entry_shift_fields.execute()
		self.assertEqual(frappe.db.get_value(PAYMENT_ENTRY, entry.name, "modified"), before)
		self.assertEqual(frappe.db.get_value(PAYMENT_ENTRY, entry.name, "crenya_shift_id"), "S-9")
		for name in ("crenya_shift_id", "crenya_cashier", "crenya_device"):
			self.assertEqual(frappe.db.count("Custom Field", {"dt": PAYMENT_ENTRY, "fieldname": name}), 1)


class TestCustomerPaymentRules(PaymentCase):
	def test_oldest_means_posting_date_before_creation(self):
		customer = make_customer("postdate", limit=None)
		later_made = self.sell(customer, "30.000")
		earlier_dated = self.post(
			self.on_account_sale(customer, total="40.000", posting_date=add_days(nowdate(), -1))
		)["name"]
		# made second, dated first: it is the oldest debt
		result = self.assertOk(self.pay(self.payment(customer, "25.000")))
		self.assertEqual(result["payment"]["invoices"], [earlier_dated])
		self.assertEqual(
			(outstanding(earlier_dated), outstanding(later_made)), (Decimal("15.000"), Decimal("30.000"))
		)

	def test_an_invoice_of_another_company_is_not_allocated(self):
		customer = make_customer("othercompany", limit=None)
		invoice = self.sell(customer, "30.000")
		frappe.db.set_value("Sales Invoice", invoice, "company", "_Test Crenya Other Company")
		try:
			result = self.assertOk(self.pay(self.payment(customer, "10.000")))
		finally:
			frappe.db.set_value("Sales Invoice", invoice, "company", fixtures.COMPANY)
		self.assertEqual(result["payment"], {"allocated": "0.000", "unallocated": "10.000", "invoices": []})

	def test_the_default_customer_is_refused(self):
		result = self.pay(self.payment(self.walk_in, "5.000"))
		self.assertError(result, "validation", False)
		self.assertIn("named customer", result["error"]["message"])

	def test_a_unique_key_collision_answers_as_a_duplicate(self):
		"""Both lookups miss (a concurrent request committed in between): the insert meets the
		unique `crenya_local_id` and the event is answered with the entry that won."""
		customer = make_customer("collide", limit=None)
		payload = self.payment(customer, "3.000")
		first = self.assertOk(self.pay(payload))
		real = payments.payment_for_local_id
		calls = []

		def blind_twice(local_id, device_id=None):
			calls.append(local_id)
			return None if len(calls) <= 2 else real(local_id, device_id)

		with mock.patch.object(payments, "payment_for_local_id", blind_twice):
			again = self.pay(payload)
		self.assertEqual((again["status"], again["name"]), ("duplicate", first["name"]))
		self.assertEqual(len(self.entries(payload["local_id"])), 1)

	def test_another_devices_entry_with_the_same_local_id_is_not_ours(self):
		customer = make_customer("otherdevice", limit=None)
		payload = self.payment(customer, "3.000")
		self.assertOk(self.pay(payload))
		self.assertIsNotNone(payments.payment_for_local_id(payload["local_id"], self.device_id))
		self.assertIsNone(payments.payment_for_local_id(payload["local_id"], new_id()))
		self.assertIsNotNone(payments.payment_for_local_id(payload["local_id"]))


class TestConcurrentReceipts(PaymentCase):
	"""Two receipts of one customer never allocate the same invoice twice: the second waits for the
	first to commit and then reads the balances (MariaDB's REPEATABLE READ would otherwise show it
	the old ones)."""

	def test_two_receipts_for_one_invoice_allocate_it_once(self):
		customer = make_customer("race", limit=None)
		invoice = self.sell(customer, "30.000")
		first = self.payment(customer, "30.000")
		second = self.payment(customer, "30.000")
		lock = events._lock_name(self.event(first, AGGREGATE))
		self.assertTrue(lock)
		# the same customer, named by its till id, takes the same lock
		self.assertEqual(
			payments.CustomerPaymentHandler().lock_key({"customer": customer}), f"customer:{customer}"
		)
		self.assertTrue(events._acquire_lock(lock))
		site = frappe.local.site
		sites_path = frappe.local.sites_path
		device_id = self.device_id
		event = self.event(second, AGGREGATE)
		answers = []

		def worker():
			frappe.init(site=site, sites_path=sites_path)
			frappe.connect()
			try:
				frappe.set_user("Administrator")
				answers.extend(sync_api.push_batch(device_id=device_id, events=[event]))
			except Exception as exc:  # the main thread reports it
				answers.append(exc)
			finally:
				frappe.destroy()

		thread = threading.Thread(target=worker)
		thread.start()
		try:
			# the second receipt is waiting for the lock; the first books and commits meanwhile
			time.sleep(2)
			self.assertEqual(self.entries(second["local_id"]), [])
			won = self.assertOk(self.pay(first))
		finally:
			events._release_lock(lock)
		thread.join(60)
		self.assertFalse(thread.is_alive())
		self.assertEqual(len(answers), 1, msg=answers)
		lost = answers[0]
		self.assertNotIsInstance(lost, Exception, msg=repr(lost))
		self.assertEqual(lost["status"], "ok", msg=lost.get("error"))
		self.assertEqual(won["payment"]["invoices"], [invoice])
		# the second read the invoice paid: nothing allocated, all an advance
		self.assertEqual(lost["payment"], {"allocated": "0.000", "unallocated": "30.000", "invoices": []})
		frappe.db.rollback()
		self.assertEqual(outstanding(invoice), Decimal("0.000"))
		allocated = frappe.db.sql(
			"select coalesce(sum(allocated_amount), 0) from `tabPayment Entry Reference` "
			"where reference_doctype = 'Sales Invoice' and reference_name = %s and docstatus = 1",
			(invoice,),
		)[0][0]
		self.assertEqual(Decimal(money(allocated)), Decimal("30.000"))


class TestCustomerPaymentAsDeviceUser(PaymentCase):
	"""The receipt as the device user, who holds the device role only."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls._saved_throttle = frappe.conf.get("throttle_user_limit")
		frappe.conf.throttle_user_limit = 10000
		cls.person = ensure_person("_test_crenya_pay_cashier@example.com", [POS_USER_ROLE, "Sales User"])
		frappe.db.commit()  # nosemgrep

	@classmethod
	def tearDownClass(cls):
		if cls._saved_throttle is None:
			frappe.conf.pop("throttle_user_limit", None)
		else:
			frappe.conf.throttle_user_limit = cls._saved_throttle
		super().tearDownClass()

	def register(self):
		"""(device id, the device user, the sign-in answer): a till paired as the device user."""
		frappe.local.request_ip = "127.0.0.1"
		device_id = new_id()
		answer = sign_in(self.person, PASSWORD, device_id)
		user = answer["device_user"]
		self.assertEqual(set(frappe.get_roles(user)) - {"All", "Guest"}, {DEVICE_ROLE})
		frappe.set_user(user)
		try:
			device_api.register_device(
				device_id=device_id,
				device_name="Receipt till",
				pos_profile=self.profile.name,
				app_version="0.9.0",
				platform="linux",
			)
		finally:
			frappe.set_user("Administrator")
		frappe.db.commit()  # nosemgrep
		return device_id, user, answer

	def push_as(self, user, device_id, payload):
		"""The result of the push as `user`, and the session user right after it (not reset by the
		helper: the booking must hand the session back)."""
		frappe.set_user(user)
		try:
			result = sync_api.push_batch(device_id=device_id, events=[self.event(payload, AGGREGATE)])[0]
			after = frappe.session.user
		finally:
			frappe.set_user("Administrator")
		return result, after

	def test_the_device_user_posts_a_receipt_and_an_advance(self):
		device_id, user, _answer = self.register()
		customer = make_customer("devpay", limit=None)
		invoice = self.sell(customer, "25.000")
		result, after = self.push_as(user, device_id, self.payment(customer, "40.000"))
		booked = self.assertOk(result)
		# the session is the device user again when push_batch returns
		self.assertEqual(after, user)
		self.assertEqual(
			booked["payment"], {"allocated": "25.000", "unallocated": "15.000", "invoices": [invoice]}
		)
		# the trail shows who booked it; the remarks name the device
		owner, modified_by, remarks = frappe.db.get_value(
			PAYMENT_ENTRY, booked["name"], ["owner", "modified_by", "remarks"]
		)
		self.assertEqual((owner, modified_by), (user, user))
		self.assertTrue(remarks.startswith(f"Received at the till {device_id} "))
		# the device user was given no right on Payment Entry for it
		self.assertFalse(frappe.has_permission("Payment Entry", "read", user=user))

	def test_the_session_is_the_device_user_after_a_refused_receipt(self):
		device_id, user, _answer = self.register()
		customer = make_customer("devrefused", limit=None)
		result, after = self.push_as(user, device_id, self.payment(customer, "5.000", currency="USD"))
		self.assertError(result, "validation", False)
		self.assertEqual(after, user)

	def test_the_session_is_the_device_user_after_erpnext_refuses_the_booking(self):
		from erpnext.accounts.doctype.payment_entry.payment_entry import PaymentEntry

		device_id, user, _answer = self.register()
		customer = make_customer("devfail", limit=None)
		self.sell(customer, "10.000")
		payload = self.payment(customer, "10.000")
		with mock.patch.object(PaymentEntry, "submit", side_effect=frappe.ValidationError("refused")):
			result, after = self.push_as(user, device_id, payload)
		self.assertEqual(result["status"], "error")
		self.assertEqual(after, user)
		self.assertEqual(self.entries(payload["local_id"]), [])
		# and the same receipt books once ERPNext lets it
		retried, after = self.push_as(user, device_id, payload)
		self.assertOk(retried)
		self.assertEqual(after, user)

	def test_a_receipt_over_http_with_the_device_key(self):
		"""Through the real WSGI application with the device's API key: two receipts in one batch (the
		second runs after the first booked as Administrator), a good answer, and each Payment Entry
		is the device user's."""
		from werkzeug.test import Client

		device_id, user, answer = self.register()
		customer = make_customer("devhttp", limit=None)
		invoice = self.sell(customer, "25.000")
		first = self.payment(customer, "10.000")
		second = self.payment(customer, "20.000")
		site = frappe.local.site
		out = []

		def worker():
			try:
				from frappe.app import application

				response = Client(application).post(
					"/api/method/crenya_pos_api.api.sync.push_batch",
					json={
						"device_id": device_id,
						"events": [self.event(first, AGGREGATE), self.event(second, AGGREGATE)],
					},
					headers={
						"Authorization": f"token {answer['api_key']}:{answer['api_secret']}",
						"X-Frappe-Site-Name": site,
					},
					base_url=f"http://{site}",
				)
				out.append((response.status_code, response.get_json()))
			except Exception as exc:  # the main thread reports it
				out.append(exc)

		thread = threading.Thread(target=worker)
		thread.start()
		thread.join(120)
		self.assertFalse(thread.is_alive())
		self.assertNotIsInstance(out[0], Exception, msg=repr(out[0]))
		status, body = out[0]
		self.assertEqual(status, 200, msg=body)
		results = body["message"]
		self.assertEqual([r["status"] for r in results], ["ok", "ok"], msg=results)
		self.assertEqual(results[0]["payment"]["invoices"], [invoice])
		self.assertEqual(
			results[1]["payment"], {"allocated": "15.000", "unallocated": "5.000", "invoices": [invoice]}
		)
		frappe.db.rollback()
		for result in results:
			self.assertEqual(frappe.db.get_value(PAYMENT_ENTRY, result["name"], "owner"), user)

	def test_experiment_if_owner_docperm_instead_of_administrator(self):
		"""An experiment, never a failure: would a Custom DocPerm for the device role (if_owner) let
		the booking run as the device user? The outcome is printed for the CI log."""
		from contextlib import nullcontext

		device_id, user, _answer = self.register()
		customer = make_customer("devexperiment", limit=None)
		self.sell(customer, "10.000")
		perm = frappe.get_doc(
			{
				"doctype": "Custom DocPerm",
				"parent": PAYMENT_ENTRY,
				"parenttype": "DocType",
				"parentfield": "permissions",
				"role": DEVICE_ROLE,
				"permlevel": 0,
				"read": 1,
				"write": 1,
				"create": 1,
				"submit": 1,
				"if_owner": 1,
			}
		).insert(ignore_permissions=True)
		frappe.clear_cache(doctype=PAYMENT_ENTRY)
		try:
			with mock.patch.object(payments, "_as_administrator", nullcontext):
				result, _after = self.push_as(user, device_id, self.payment(customer, "10.000"))
			print(
				f"EXPERIMENT if_owner DocPerm without Administrator: {result['status']} {result.get('error')}"
			)
		finally:
			frappe.set_user("Administrator")
			frappe.delete_doc("Custom DocPerm", perm.name, ignore_permissions=True, force=True)
			frappe.clear_cache(doctype=PAYMENT_ENTRY)
			frappe.db.commit()  # nosemgrep


class TestReturnsShareTheReceiptLock(PaymentCase):
	def test_a_return_that_credits_the_account_takes_the_customers_receipt_lock(self):
		customer = make_customer("lockret", limit=500)
		sale = self.on_account_sale(customer, "100.000", cash="40.000")
		self.post(sale)
		ret = self.return_payload(sale, [(fixtures.MILK, "-1", "50.000", 1)], credit_offset="50.000")
		ret["payments"] = []
		receipt = self.event(self.payment(customer, "5.000"), AGGREGATE)
		lock = events._lock_name(receipt)
		self.assertTrue(lock)
		self.assertEqual(events._lock_name(self.event(ret)), lock)
		# a plain return, and a sale, take none
		plain = self.return_payload(sale, [(fixtures.MILK, "-1", "50.000", 1)])
		self.assertIsNone(events._lock_name(self.event(plain)))
		self.assertIsNone(events._lock_name(self.event(sale)))
		# and the credited return still posts
		self.assertOk(self.push_one(self.event(ret)))
