"""The Customer Credit Facility event (0.16.0, House Account phase 2): a restaurant till enables an
EXISTING customer's House Account (limit and days) under a manager's approval and a checker app's yes.
Every refusal is final and leaves the customer as it was.

The restaurant till is a device of type `restaurant_pos`; the hooks of the other apps are faked (the
checker is a test function), so the suite is the same with crenya_restaurant installed or not.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_credit_facility_event
"""

from decimal import Decimal
from unittest import mock

import frappe

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.api.web_admin import credit as web_credit
from crenya_pos_api.sync.credit import (
	CREDIT_CHANGED_AT_FIELD,
	CREDIT_ENABLED_FIELD,
	CREDIT_SALES_FIELD,
	GRANT_FLAG,
	TILL_ENABLE_GRANT,
	facility_editor_refusal,
)
from crenya_pos_api.sync.credit_facility import AGGREGATE_CREDIT_FACILITY, facility_subject
from crenya_pos_api.tests import extension_dummy as dummy
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import new_id
from crenya_pos_api.tests.test_integration_credit import CreditCase, make_customer
from crenya_pos_api.tests.web_admin_case import ensure_user

FLAG_DEVICE = "restaurant_pos"
WAITER_DEVICE = "restaurant_waiter"
SALES_USER = "_test_crenya_sales_user@example.com"
MANAGER = "manager@example.com"
OTHER_COMPANY = "_Test Crenya Other Company"
CHECKER_HOOK = "crenya_pos_credit_facility_checkers"


def hooks(checkers=(dummy.FACILITY_ACCEPT,)):
	"""What crenya_restaurant hooks: its till takes credit and gets the customer's flag, and the
	checkers given (none: no app accepts a grant)."""
	return dummy.fake_hooks(
		{
			"crenya_pos_credit_device_types": [FLAG_DEVICE],
			"crenya_pos_credit_customer_flag_device_types": [FLAG_DEVICE],
			CHECKER_HOOK: list(checkers),
		}
	)


def state(customer):
	doc = frappe.get_doc("Customer", customer)
	rows = [row for row in doc.credit_limits if row.company == fixtures.COMPANY]
	return {
		"enabled": frappe.utils.cint(doc.get(CREDIT_ENABLED_FIELD)),
		"changed_at": doc.get(CREDIT_CHANGED_AT_FIELD),
		"limit": Decimal(str(rows[0].credit_limit)) if rows else None,
		"terms": doc.payment_terms,
		"modified": doc.modified,
	}


def comments(customer):
	return frappe.get_all(
		"Comment",
		filters={"reference_doctype": "Customer", "reference_name": customer, "comment_type": "Info"},
		order_by="creation asc",
		pluck="content",
	)


def versions(customer):
	return frappe.db.count("Version", {"ref_doctype": "Customer", "docname": customer})


class TestCreditFacilityEvent(CreditCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(SALES_USER, ["Sales User"])
		with hooks():
			cls.restaurant = cls.register(FLAG_DEVICE)
			cls.waiter = cls.register(WAITER_DEVICE)
		frappe.db.commit()  # nosemgrep

	@classmethod
	def register(cls, device_type):
		device_id = new_id()
		device_api.register_device(
			device_id=device_id,
			device_name=device_type,
			pos_profile=cls.profile.name,
			device_type=device_type,
		)
		return device_id

	def setUp(self):
		dummy.FACILITY_CALLS.clear()

	def tearDown(self):
		frappe.set_user("Administrator")

	def payload(self, customer, limit="50.000", days=30, subject=None, **approval):
		"""`limit` / `days` ... leaves the key out; the subject is the one the till derives."""
		body = {"local_id": new_id(), "customer": customer}
		if limit is not ...:
			body["credit_limit"] = limit
		if days is not ...:
			body["credit_days"] = days
		shown_limit = None if limit in (None, ...) else Decimal(limit)
		shown_days = None if days in (None, ...) else days
		body["approval"] = {
			"action": "credit_facility",
			"subject": subject or facility_subject(customer, shown_limit, shown_days),
			"approved_by": MANAGER,
			"approved_at": "2026-10-28T08:15:30Z",
			**approval,
		}
		return body

	def enable(self, customer, device=None, checkers=(dummy.FACILITY_ACCEPT,), **kwargs):
		"""Push the event; returns (result, event)."""
		event = self.event(self.payload(customer, **kwargs), AGGREGATE_CREDIT_FACILITY)
		with hooks(checkers):
			[result] = sync_api.push_batch(device_id=device or self.restaurant, events=[event])
		return result, event

	def refused(self, customer, fragment, code="permission", **kwargs):
		before = state(customer)
		count = versions(customer)
		result, _event = self.enable(customer, **kwargs)
		self.assertError(result, code, False)
		self.assertIn(fragment, result["error"]["message"])
		self.assertEqual(state(customer), before)
		self.assertEqual(versions(customer), count)
		return result

	# the grant

	def test_enable_existing_sets_flag_limit_days(self):
		customer = make_customer("enable")
		count = versions(customer)
		result, event = self.enable(customer)
		self.assertOk(result)
		self.assertEqual(result["name"], customer)
		self.assertEqual(
			result["facility"],
			{"customer": customer, "credit_enabled": True, "credit_limit": "50.000", "credit_days": 30},
		)
		now = state(customer)
		self.assertEqual((now["enabled"], now["limit"], now["terms"]), (1, Decimal("50"), "House Account 30 days"))
		self.assertIsNotNone(now["changed_at"])
		self.assertEqual(versions(customer), count + 1)
		[comment] = comments(customer)
		self.assertIn("Credit terms for", comment)
		self.assertIn(fixtures.COMPANY, comment)
		self.assertIn(MANAGER, comment)
		self.assertEqual(dummy.FACILITY_CALLS, [(self.restaurant, customer, "50.000", 30)])
		# the grant flag lives on the saved object only: a desk save is as refused as before
		self.assertEqual(frappe.db.get_value("Crenya Sync Event", event["event_id"], "status"), "ok")

	def test_checker_note_becomes_the_comment(self):
		customer = make_customer("note")
		result, _event = self.enable(customer, checkers=(dummy.FACILITY_ACCEPT_NOTE,))
		self.assertOk(result)
		# a Comment is shown as HTML: the checker's text is escaped
		self.assertEqual(comments(customer), [frappe.utils.escape_html(dummy.FACILITY_NOTE)])

	def test_days_only_leaves_the_limit(self):
		customer = make_customer("days only", limit=200)
		result, _event = self.enable(customer, limit=..., days=14)
		self.assertOk(result)
		now = state(customer)
		self.assertEqual((now["enabled"], now["limit"], now["terms"]), (1, Decimal("200"), "House Account 14 days"))
		self.assertEqual(result["facility"]["credit_limit"], "200.000")

	def test_null_limit_and_days_clear(self):
		customer = make_customer("clear", limit=200)
		result, _event = self.enable(customer, limit=None, days=None)
		self.assertOk(result)
		now = state(customer)
		self.assertEqual((now["enabled"], now["limit"], now["terms"]), (1, None, None))
		self.assertEqual(result["facility"]["credit_limit"], None)

	# refusals

	def test_refused_without_checker_hook(self):
		customer = make_customer("no checker")
		# a site where an app (crenya_restaurant) registers checkers: the hook is patched to none
		self.refused(customer, "No app accepts a House Account grant here", checkers=())
		self.assertEqual(dummy.FACILITY_CALLS, [])

	def test_checker_refusal_is_final_with_its_reason(self):
		customer = make_customer("checker no")
		self.refused(customer, dummy.FACILITY_REFUSAL, checkers=(dummy.FACILITY_REFUSE,))
		self.assertEqual(len(dummy.FACILITY_CALLS), 1)

	def test_every_checker_must_accept(self):
		customer = make_customer("second no")
		self.refused(
			customer, dummy.FACILITY_REFUSAL, checkers=(dummy.FACILITY_ACCEPT, dummy.FACILITY_REFUSE)
		)
		self.assertEqual(len(dummy.FACILITY_CALLS), 2)

	def test_a_checker_that_answers_neither_none_nor_a_reason_refuses(self):
		customer = make_customer("odd checker")
		self.refused(customer, "a checker must return None or a reason", checkers=(dummy.FACILITY_BAD_ANSWER,))

	def test_refused_walk_in(self):
		before = state(self.walk_in)
		result, _event = self.enable(self.walk_in)
		self.assertError(result, "validation", False)
		self.assertIn("walk-in", result["error"]["message"])
		self.assertEqual(state(self.walk_in), before)
		self.assertEqual(dummy.FACILITY_CALLS, [])

	def test_refused_disabled(self):
		customer = make_customer("disabled")
		frappe.db.set_value("Customer", customer, "disabled", 1)
		self.refused(customer, "disabled", code="validation")
		self.assertEqual(dummy.FACILITY_CALLS, [])

	def test_refused_service_provider(self):
		customer = make_customer("provider")
		previous = web_credit.credit_product("restaurant")
		product = web_credit.CreditProduct(
			key="restaurant",
			outlets=lambda outlet=None: [],
			manages=lambda roles, user, profile: False,
			excluded_customers=lambda ctx: {customer} if ctx.company == fixtures.COMPANY else set(),
			excluded_refusal="{name} is a service provider: record its payment on Service providers",
		)
		web_credit.register_credit_product(product)
		self.addCleanup(self.restore_product, previous)
		with mock.patch("crenya_pos_api.api.web_admin.scope.outlet_product", return_value="restaurant"):
			self.refused(customer, "is a service provider", code="validation")
		self.assertEqual(dummy.FACILITY_CALLS, [])

	@staticmethod
	def restore_product(previous):
		if previous is None:
			web_credit._PRODUCTS.pop("restaurant", None)
		else:
			web_credit.register_credit_product(previous)

	def test_refused_other_company_internal_customer(self):
		customer = make_customer("internal")
		# `represents_company` is unique on the site: free it again for the other tests
		self.addCleanup(self.free_company, customer)
		frappe.db.set_value(
			"Customer", customer, {"is_internal_customer": 1, "represents_company": OTHER_COMPANY}
		)
		self.refused(customer, "represents a company", code="validation")
		self.assertEqual(dummy.FACILITY_CALLS, [])

	@staticmethod
	def free_company(customer):
		frappe.db.set_value("Customer", customer, {"is_internal_customer": 0, "represents_company": None})
		frappe.db.commit()  # nosemgrep

	def test_refused_unknown_customer(self):
		result, _event = self.enable("_Test Crenya No Such Customer", limit=None, days=None)
		self.assertError(result, "validation", False)
		self.assertIn("Not found", result["error"]["message"])

	def test_refused_outlet_credit_off(self):
		customer = make_customer("credit off")
		self.set_switch(0)
		try:
			self.refused(customer, "switched off", code="validation")
		finally:
			self.set_switch(1)
		self.assertEqual(dummy.FACILITY_CALLS, [])

	def test_subject_mismatch_refused(self):
		customer = make_customer("subject")
		# the manager approved 50 and the payload says 80
		wrong = facility_subject(customer, Decimal("50"), 30)
		self.refused(customer, "The approval was for other details", limit="80.000", subject=wrong)
		self.assertEqual(dummy.FACILITY_CALLS, [])
		# the same for the days and for a subject of another customer
		self.refused(customer, "The approval was for other details", days=60, subject=wrong)
		other = facility_subject("CUST-OTHER", Decimal("50"), 30)
		self.refused(customer, "The approval was for other details", subject=other)

	def test_waiter_device_refused(self):
		customer = make_customer("waiter")
		self.refused(customer, "cannot enable", device=self.waiter)
		self.assertEqual(dummy.FACILITY_CALLS, [])

	def test_retail_till_refused(self):
		customer = make_customer("retail till")
		self.refused(customer, "cannot enable", device=self.device_id)
		self.assertEqual(dummy.FACILITY_CALLS, [])

	def test_limit_with_too_many_decimals_refused(self):
		customer = make_customer("decimals")
		# OMR has 3 decimals; the subject is for the same text
		self.refused(
			customer,
			"more than 3 decimals",
			code="validation",
			limit="50.0005",
		)

	def test_limit_below_outstanding_refused(self):
		# A10: ERPNext refuses a limit below what the customer owes; its words reach the till
		customer = make_customer("owes", limit=500)
		self.post(self.on_account_sale(customer, "100.000"))
		before = state(customer)
		count = versions(customer)
		result, _event = self.enable(customer, limit="50.000", days=30)
		self.assertError(result, "validation", False)
		self.assertIn("outstanding", result["error"]["message"].lower())
		after = state(customer)
		self.assertEqual((after["enabled"], after["limit"], after["terms"]), (0, Decimal("500"), None))
		self.assertEqual(after["modified"], before["modified"])
		self.assertEqual(versions(customer), count)
		self.assertEqual(comments(customer), [])

	# a resend

	def test_resend_same_values_no_change(self):
		customer = make_customer("resend")
		self.assertOk(self.enable(customer)[0])
		first = state(customer)
		count, notes = versions(customer), comments(customer)
		dummy.FACILITY_CALLS.clear()
		# a new event for the same values is answered, whatever the checkers (or a missing one) now say
		for checkers in ((dummy.FACILITY_REFUSE,), ()):
			result, event = self.enable(customer, checkers=checkers)
			self.assertOk(result)
			self.assertTrue(result["facility"]["credit_enabled"])
			self.assertEqual(result["facility"]["credit_limit"], "50.000")
			self.assertEqual(state(customer), first)
			self.assertEqual((versions(customer), comments(customer)), (count, notes))
			self.assertIn(
				"nothing changed", frappe.db.get_value("Crenya Sync Event", event["event_id"], "note")
			)
		self.assertEqual(dummy.FACILITY_CALLS, [])

	def test_resend_with_wrong_subject_refused(self):
		customer = make_customer("resend forged")
		self.assertOk(self.enable(customer)[0])
		wrong = facility_subject(customer, Decimal("80"), 30)
		# the values match what the customer has, the approval is for other details: refused
		self.refused(customer, "The approval was for other details", subject=wrong)

	def test_changed_values_are_not_a_resend(self):
		customer = make_customer("changed")
		self.assertOk(self.enable(customer)[0])
		result, _event = self.enable(customer, limit="80.000", days=45)
		self.assertOk(result)
		now = state(customer)
		self.assertEqual((now["limit"], now["terms"]), (Decimal("80"), "House Account 45 days"))
		self.assertEqual(len(comments(customer)), 2)
		# and a refusing checker now refuses the change
		self.refused(customer, dummy.FACILITY_REFUSAL, limit="90.000", checkers=(dummy.FACILITY_REFUSE,))

	# the desk stays closed

	def test_desk_user_still_cannot_switch_flag(self):
		customer = make_customer("desk")
		self.assertOk(self.enable(customer)[0])
		other = make_customer("desk other")
		frappe.set_user(SALES_USER)
		# a Sales User's save switching the flag on an existing customer is refused ...
		doc = frappe.get_doc("Customer", other)
		doc.set(CREDIT_ENABLED_FIELD, 1)
		with self.assertRaises(frappe.PermissionError) as caught:
			doc.save()
		self.assertIn(facility_editor_refusal(), str(caught.exception))
		# ... also with the flag a client tries to smuggle in a document dict (dropped) ...
		forged = frappe.get_doc("Customer", other)
		forged.update(
			{
				"flags": {GRANT_FLAG: TILL_ENABLE_GRANT},
				GRANT_FLAG: TILL_ENABLE_GRANT,
				"__flags": {GRANT_FLAG: TILL_ENABLE_GRANT},
				CREDIT_ENABLED_FIELD: 1,
			}
		)
		self.assertNotEqual(forged.flags.get(GRANT_FLAG), TILL_ENABLE_GRANT)
		with self.assertRaises(frappe.PermissionError):
			forged.save()
		# ... and the grant a till's event used is gone from the customer it enabled
		again = frappe.get_doc("Customer", customer)
		again.set(CREDIT_ENABLED_FIELD, 0)
		with self.assertRaises(frappe.PermissionError):
			again.save()
		frappe.set_user("Administrator")
		self.assertEqual(state(other)["enabled"], 0)
		self.assertEqual(state(customer)["enabled"], 1)

	def test_the_grant_is_no_grant_on_a_new_customer(self):
		group, territory = frappe.db.get_value("Customer", self.walk_in, ["customer_group", "territory"])
		frappe.set_user(SALES_USER)
		new = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": f"_Test Crenya Forged Enable {new_id()[:6]}",
				"customer_type": "Individual",
				"customer_group": group,
				"territory": territory,
				CREDIT_ENABLED_FIELD: 1,
			}
		)
		new.flags[GRANT_FLAG] = TILL_ENABLE_GRANT
		with self.assertRaises(frappe.PermissionError):
			new.insert()

	def test_capability(self):
		self.assertEqual(sync_api.get_sync_capabilities()["features"]["credit_facility_event"], 1)
