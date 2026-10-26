"""A till's new customer (0.15.0, provider settlement spec NC2, NC4, R6, 7.2): `crenya_location`, the
`crenya_pos_customer_extenders` hook, and the server-only grant flag that lets an extender switch on
the House Account facility of the customer it is creating, past `stamp_credit_change`.

The pushes run as a cashier whose only role is Crenya POS User (no facility editor), so the grant
flag, not a role, is what lets the facility on.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_customer_extender
"""

import frappe

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.sync.credit import (
	CREDIT_CHANGED_AT_FIELD,
	CREDIT_ENABLED_FIELD,
	GRANT_FLAG,
	facility_editor_refusal,
)
from crenya_pos_api.tests import extension_dummy as dummy
from crenya_pos_api.tests.sync_case import SyncCase, new_id
from crenya_pos_api.tests.web_admin_case import ensure_user
from crenya_pos_api.utils.dates import format_db_datetime

LOCATION_FIELD = "crenya_location"
SALES_USER = "_test_crenya_sales_user@example.com"
GRANT = {"approved_by": "manager@example.com", "approved_at": "2026-10-26T08:15:30.123Z"}
EXTENDER_HOOKS = {"crenya_pos_customer_extenders": [dummy.CUSTOMER_EXTENDER]}
# no customer extender, whatever the site's apps hook (crenya_restaurant registers one, Task 9)
NO_EXTENDER = {"crenya_pos_customer_extenders": []}
XSS = '<img src=x onerror="alert(1)">'


def comments(customer):
	return frappe.get_all(
		"Comment",
		filters={"reference_doctype": "Customer", "reference_name": customer, "comment_type": "Info"},
		order_by="creation asc",
		pluck="content",
	)


def event_note(event_id):
	return frappe.db.get_value("Crenya Sync Event", {"event_id": event_id}, "note")


class TestCustomerExtender(SyncCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.cashier = cls._cashier(cls)
		cls.cashier_device = new_id()
		frappe.set_user(cls.cashier)
		try:
			device_api.register_device(
				device_id=cls.cashier_device, device_name="Extender till", pos_profile=cls.profile.name
			)
		finally:
			frappe.set_user("Administrator")
		ensure_user(SALES_USER, ["Sales User"])
		frappe.db.commit()  # nosemgrep

	def setUp(self):
		dummy.CUSTOMER_CALLS.clear()

	def tearDown(self):
		frappe.set_user("Administrator")

	def push_as_cashier(self, payload, hooks=None):
		event = self.event(payload, "Customer")
		frappe.set_user(self.cashier)
		try:
			if hooks is None:
				[result] = sync_api.push_batch(device_id=self.cashier_device, events=[event])
			else:
				with dummy.fake_hooks(hooks):
					[result] = sync_api.push_batch(device_id=self.cashier_device, events=[event])
		finally:
			frappe.set_user("Administrator")
		return result, event["event_id"]

	def test_retail_payload_unchanged(self):
		payload = self.customer_payload("Retail Shape Customer")
		result, event_id = self.push_as_cashier(payload, NO_EXTENDER)
		self.assertOk(result)
		doc = frappe.get_doc("Customer", result["name"])
		self.assertEqual(
			(doc.customer_name, doc.customer_type, doc.mobile_no, doc.crenya_local_id),
			("Retail Shape Customer", "Individual", "+96891234567", payload["local_id"]),
		)
		self.assertFalse(doc.email_id)
		self.assertFalse(doc.tax_id)
		self.assertFalse(doc.get(LOCATION_FIELD))
		self.assertEqual(doc.get(CREDIT_ENABLED_FIELD), 0)
		self.assertIsNone(doc.get(CREDIT_CHANGED_AT_FIELD))
		self.assertEqual(comments(doc.name), [f"Created at the till Extender till ({self.cashier_device})"])
		self.assertIsNone(event_note(event_id))

	def test_location_validated_and_stored(self):
		payload = self.customer_payload("Location Customer")
		payload["location"] = "  Al Khuwair, behind Lulu "
		result, _event_id = self.push_as_cashier(payload)
		self.assertOk(result)
		self.assertEqual(
			frappe.db.get_value("Customer", result["name"], LOCATION_FIELD), "Al Khuwair, behind Lulu"
		)

	def test_location_141_chars_refused(self):
		payload = self.customer_payload("Long Location Customer")
		payload["location"] = "x" * 141
		result, _event_id = self.push_as_cashier(payload)
		self.assertError(result, "validation", False)
		self.assertFalse(frappe.db.exists("Customer", {"crenya_local_id": payload["local_id"]}))

	def test_credit_facility_ignored_without_extender(self):
		payload = self.customer_payload("No Extender Customer")
		payload["credit_facility"] = dict(GRANT)
		result, event_id = self.push_as_cashier(payload, NO_EXTENDER)
		self.assertOk(result)
		self.assertEqual(frappe.db.get_value("Customer", result["name"], CREDIT_ENABLED_FIELD), 0)
		self.assertIn("credit_facility", event_note(event_id))

	def test_extender_called_and_comment_added(self):
		plain = self.customer_payload("Extender Plain Customer")
		result, event_id = self.push_as_cashier(plain, EXTENDER_HOOKS)
		self.assertOk(result)
		self.assertEqual(dummy.CUSTOMER_CALLS, [(self.cashier_device, "Extender Plain Customer", None)])
		# no comment returned: only core's
		self.assertEqual(len(comments(result["name"])), 1)
		self.assertIsNone(event_note(event_id))

		declined = self.customer_payload("Extender Declined Customer")
		declined["credit_facility"] = {**GRANT, "approved_by": "declined@example.com"}
		result, event_id = self.push_as_cashier(declined, EXTENDER_HOOKS)
		self.assertOk(result)
		self.assertEqual(frappe.db.get_value("Customer", result["name"], CREDIT_ENABLED_FIELD), 0)
		self.assertIn("not switched on", event_note(event_id))
		self.assertEqual(len(comments(result["name"])), 1)

	def test_grant_flag_lets_extender_enable(self):
		self.assertNotIn("Accounts Manager", frappe.get_roles(self.cashier))
		payload = self.customer_payload("Granted Customer")
		payload["credit_facility"] = dict(GRANT)
		result, event_id = self.push_as_cashier(payload, EXTENDER_HOOKS)
		self.assertOk(result)
		row = frappe.db.get_value(
			"Customer",
			result["name"],
			[CREDIT_ENABLED_FIELD, CREDIT_CHANGED_AT_FIELD, "modified"],
			as_dict=True,
		)
		self.assertEqual(row[CREDIT_ENABLED_FIELD], 1)
		# stamped by the insert; ERPNext's primary contact (a mobile) then db_sets the Customer, so
		# `modified` may be a little later
		self.assertIsNotNone(row[CREDIT_CHANGED_AT_FIELD])
		self.assertLessEqual(
			format_db_datetime(row[CREDIT_CHANGED_AT_FIELD]), format_db_datetime(row.modified)
		)
		texts = comments(result["name"])
		self.assertEqual(len(texts), 2)
		self.assertIn("approved by manager@example.com", texts[1])
		self.assertIn("switched on", event_note(event_id))

	def test_extender_without_the_flag_refused(self):
		payload = self.customer_payload("Flagless Customer")
		hooks = {
			"crenya_pos_customer_extenders": [
				"crenya_pos_api.tests.extension_dummy.extend_customer_without_flag"
			]
		}
		result, _event_id = self.push_as_cashier(payload, hooks)
		self.assertEqual(result["status"], "error", msg=result)
		self.assertFalse(frappe.db.exists("Customer", {"crenya_local_id": payload["local_id"]}))

	def test_extender_bad_return_refused(self):
		payload = self.customer_payload("Bad Return Customer")
		hooks = {
			"crenya_pos_customer_extenders": [
				"crenya_pos_api.tests.extension_dummy.extend_customer_bad_return"
			]
		}
		result, _event_id = self.push_as_cashier(payload, hooks)
		self.assertError(result, "internal", True)
		self.assertFalse(frappe.db.exists("Customer", {"crenya_local_id": payload["local_id"]}))

	def test_grant_flag_not_settable_from_client(self):
		from frappe import client

		group, territory = frappe.db.get_value("Customer", self.walk_in, ["customer_group", "territory"])

		def as_dict(label):
			return {
				"doctype": "Customer",
				"customer_name": f"_Test Crenya Forged Grant {label} {new_id()[:6]}",
				"customer_type": "Individual",
				"customer_group": group,
				"territory": territory,
				"flags": {GRANT_FLAG: "till"},
				GRANT_FLAG: "till",
				"__flags": {GRANT_FLAG: "till"},
				CREDIT_ENABLED_FIELD: 1,
			}

		frappe.set_user(SALES_USER)
		self.assertTrue(frappe.has_permission("Customer", "create"))
		with self.assertRaises(frappe.PermissionError) as caught:
			frappe.get_doc(as_dict("doc")).insert()
		self.assertIn(facility_editor_refusal(), str(caught.exception))
		with self.assertRaises(frappe.PermissionError) as caught:
			client.insert(as_dict("client"))
		self.assertIn(facility_editor_refusal(), str(caught.exception))
		with self.assertRaises(frappe.PermissionError) as caught:
			client.insert(frappe.as_json(as_dict("json")))
		self.assertIn(facility_editor_refusal(), str(caught.exception))

		# the same flag set by server code on an EXISTING customer is no grant (only a new one)
		frappe.set_user("Administrator")
		plain = as_dict("existing")
		plain[CREDIT_ENABLED_FIELD] = 0
		existing = frappe.get_doc(plain).insert(ignore_permissions=True)
		frappe.set_user(SALES_USER)
		doc = frappe.get_doc("Customer", existing.name)
		doc.set(CREDIT_ENABLED_FIELD, 1)
		doc.flags[GRANT_FLAG] = "till"
		with self.assertRaises(frappe.PermissionError):
			doc.save()
		# an unknown grant source is no grant either
		new = frappe.get_doc({**as_dict("other source")})
		new.flags[GRANT_FLAG] = "desk"
		with self.assertRaises(frappe.PermissionError):
			new.insert()
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Customer", existing.name, CREDIT_ENABLED_FIELD), 0)

	def test_push_payload_cannot_set_the_flag(self):
		payload = self.customer_payload("Forged Payload Customer")
		payload.update({"flags": {GRANT_FLAG: "till"}, GRANT_FLAG: "till", CREDIT_ENABLED_FIELD: 1})
		result, _event_id = self.push_as_cashier(payload, EXTENDER_HOOKS)
		self.assertOk(result)
		self.assertEqual(frappe.db.get_value("Customer", result["name"], CREDIT_ENABLED_FIELD), 0)

	def test_pull_has_location(self):
		located = self.customer_payload("Pulled Location Customer")
		located["location"] = "Ruwi, near the clock tower"
		plain = self.customer_payload("Pulled Plain Customer")
		names = []
		for payload in (located, plain):
			result, _event_id = self.push_as_cashier(payload)
			names.append(self.assertOk(result)["name"])
		records, cursor = {}, None
		while True:
			page = sync_api.pull_changes(
				device_id=self.device_id, entity="customer", cursor=cursor, limit=500
			)
			records.update({record["name"]: record for record in page["records"]})
			cursor = page["next_cursor"]
			if not page["has_more"]:
				break
		self.assertEqual(records[names[0]]["location"], "Ruwi, near the clock tower")
		self.assertIn("location", records[names[1]])
		self.assertIsNone(records[names[1]]["location"])

	def test_location_must_be_one_line(self):
		for bad in ("Ruwi\nnear the souq", "Ruwi\x00", "Ruwi\tsouq", "Ruwi\u2028souq"):
			payload = self.customer_payload("Two Line Customer")
			payload["location"] = bad
			result, _event_id = self.push_as_cashier(payload, NO_EXTENDER)
			self.assertError(result, "validation", False)
			self.assertIn("one line", result["error"]["message"])
			self.assertFalse(frappe.db.exists("Customer", {"crenya_local_id": payload["local_id"]}))
		payload = self.customer_payload("Spaced Location Customer")
		payload["location"] = "  Ruwi    near  the souq "
		result, _event_id = self.push_as_cashier(payload, NO_EXTENDER)
		self.assertOk(result)
		self.assertEqual(
			frappe.db.get_value("Customer", result["name"], LOCATION_FIELD), "Ruwi near the souq"
		)

	def test_comments_are_escaped(self):
		device_id = new_id()
		frappe.set_user(self.cashier)
		try:
			device_api.register_device(
				device_id=device_id, device_name=f"Till {XSS}", pos_profile=self.profile.name
			)
		finally:
			frappe.set_user("Administrator")
		payload = self.customer_payload("Escaped Comment Customer")
		payload["credit_facility"] = {**GRANT, "approved_by": XSS}
		event = self.event(payload, "Customer")
		frappe.set_user(self.cashier)
		try:
			with dummy.fake_hooks(EXTENDER_HOOKS):
				[result] = sync_api.push_batch(device_id=device_id, events=[event])
		finally:
			frappe.set_user("Administrator")
		self.assertOk(result)
		texts = comments(result["name"])
		self.assertEqual(len(texts), 2, texts)
		for text in texts:
			self.assertNotIn("<img", text)
			self.assertIn("&lt;img", text)

	def test_till_path_refuses_a_web_grant(self):
		payload = self.customer_payload("Web Grant On Till Customer")
		hooks = {
			"crenya_pos_customer_extenders": [
				"crenya_pos_api.tests.extension_dummy.extend_customer_web_grant"
			]
		}
		result, _event_id = self.push_as_cashier(payload, hooks)
		self.assertError(result, "internal", True)
		self.assertFalse(frappe.db.exists("Customer", {"crenya_local_id": payload["local_id"]}))
