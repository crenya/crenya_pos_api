"""The Customer's House Account facility (0.14.0, restaurant spec D6/RC14) on an ERPNext site: the
fields and their patch, `crenya_credit_changed_at` stamped by the Customer validate hook, the credit
status of a customer-flag device and `save_credit_facility` (P4). The pull is in
test_integration_pull (TestCustomerFlagPull).

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_credit_facility
"""

from unittest import mock

import frappe

from crenya_pos_api.api import credit as credit_api
from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import web_admin
from crenya_pos_api.sync.context import POS_USER_ROLE
from crenya_pos_api.sync.credit import CREDIT_CHANGED_AT_FIELD, CREDIT_ENABLED_FIELD
from crenya_pos_api.tests import extension_dummy as dummy
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import new_id
from crenya_pos_api.tests.test_integration_credit import make_customer
from crenya_pos_api.tests.test_integration_web_admin_credit import ACCOUNTS, WebCreditCase
from crenya_pos_api.tests.web_admin_case import MANAGER, ensure_user
from crenya_pos_api.utils.dates import format_db_datetime

FLAG_DEVICE = "restaurant_pos"
RESTAURANT_MANAGER = "_test_crenya_restaurant_manager@example.com"
OTHER_COMPANY = "_Test Crenya Other Company"
SALES_USER = "_test_crenya_sales_user@example.com"


def flag_hooks():
	"""What crenya_restaurant hooks (Task 5): its till takes credit and gets the customer's flag."""
	return dummy.fake_hooks(
		{
			"crenya_pos_credit_device_types": [FLAG_DEVICE],
			"crenya_pos_credit_customer_flag_device_types": [FLAG_DEVICE],
		}
	)


def facility(customer):
	return frappe.db.get_value(
		"Customer",
		customer,
		[CREDIT_ENABLED_FIELD, CREDIT_CHANGED_AT_FIELD, "disabled", "modified"],
		as_dict=True,
	)


def save_customer(customer, **values):
	doc = frappe.get_doc("Customer", customer)
	doc.update(values)
	doc.save(ignore_permissions=True)
	return doc


def register(profile, device_type):
	device_id = new_id()
	device_api.register_device(
		device_id=device_id, device_name=device_type, pos_profile=profile, device_type=device_type
	)
	return device_id


class TestFacilityFields(WebCreditCase):
	def test_fields_installed_default_off(self):
		meta = frappe.get_meta("Customer")
		flag = meta.get_field(CREDIT_ENABLED_FIELD)
		stamp = meta.get_field(CREDIT_CHANGED_AT_FIELD)
		self.assertEqual((flag.fieldtype, flag.label), ("Check", "House Account (credit facility)"))
		self.assertEqual((stamp.fieldtype, stamp.read_only), ("Datetime", 1))
		customer = make_customer("facility new")
		row = facility(customer)
		self.assertEqual((row[CREDIT_ENABLED_FIELD], row[CREDIT_CHANGED_AT_FIELD]), (0, None))
		# the column is NOT NULL with default 0: every existing customer reads 0, never null
		table = frappe.qb.DocType("Customer")
		nulls = frappe.qb.from_(table).select(table.name).where(table[CREDIT_ENABLED_FIELD].isnull()).run()
		self.assertFalse(nulls)

	def test_patch_idempotent_on_existing_site(self):
		from crenya_pos_api.patches.v0_14 import credit_customer_fields

		plain = make_customer("facility patch plain")
		enabled = make_customer("facility patch on")
		save_customer(enabled, **{CREDIT_ENABLED_FIELD: 1})
		frappe.db.commit()  # nosemgrep
		before = {name: facility(name) for name in (plain, enabled)}
		credit_customer_fields.execute()
		credit_customer_fields.execute()
		after = {name: facility(name) for name in (plain, enabled)}
		# no customer saved: nothing switched, no modified moved (no till pulls them again)
		self.assertEqual(after, before)
		self.assertEqual(after[plain][CREDIT_ENABLED_FIELD], 0)
		self.assertEqual(after[enabled][CREDIT_ENABLED_FIELD], 1)
		self.assertTrue(frappe.get_meta("Customer").has_field(CREDIT_CHANGED_AT_FIELD))


class TestChangedAt(WebCreditCase):
	def test_changed_at_stamped_on_flag_change(self):
		customer = make_customer("stamp flag")
		save_customer(customer, **{CREDIT_ENABLED_FIELD: 1})
		row = facility(customer)
		self.assertIsNotNone(row[CREDIT_CHANGED_AT_FIELD])
		# the save's own modified: what a till pulls with this record and echoes back (R5)
		self.assertEqual(format_db_datetime(row[CREDIT_CHANGED_AT_FIELD]), format_db_datetime(row.modified))
		save_customer(customer, **{CREDIT_ENABLED_FIELD: 0})
		again = facility(customer)
		self.assertEqual(
			format_db_datetime(again[CREDIT_CHANGED_AT_FIELD]), format_db_datetime(again.modified)
		)
		self.assertGreater(again[CREDIT_CHANGED_AT_FIELD], row[CREDIT_CHANGED_AT_FIELD])

	def test_changed_at_stamped_on_disable(self):
		customer = make_customer("stamp disable")
		save_customer(customer, **{CREDIT_ENABLED_FIELD: 1})
		first = facility(customer)[CREDIT_CHANGED_AT_FIELD]
		save_customer(customer, disabled=1)
		row = facility(customer)
		self.assertGreater(row[CREDIT_CHANGED_AT_FIELD], first)
		self.assertEqual(format_db_datetime(row[CREDIT_CHANGED_AT_FIELD]), format_db_datetime(row.modified))

	def test_changed_at_untouched_on_other_edits(self):
		customer = make_customer("stamp other")
		save_customer(customer, **{CREDIT_ENABLED_FIELD: 1})
		stamped = facility(customer)[CREDIT_CHANGED_AT_FIELD]
		save_customer(customer, mobile_no="+96890000000")
		row = facility(customer)
		self.assertEqual(row[CREDIT_CHANGED_AT_FIELD], stamped)
		self.assertGreater(row.modified, stamped)
		# never changed: never stamped
		plain = make_customer("stamp never")
		save_customer(plain, mobile_no="+96890000001")
		self.assertIsNone(facility(plain)[CREDIT_CHANGED_AT_FIELD])


class TestFacilityGuard(WebCreditCase):
	"""Review F1 (P4 on the server): Sales Users may write Customers, not switch a House Account."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(SALES_USER, ["Sales User"])
		frappe.db.commit()  # nosemgrep

	def test_flag_change_by_non_editor_refused(self):
		from frappe import client

		customer = make_customer("guard")
		frappe.set_user(SALES_USER)
		doc = frappe.get_doc("Customer", customer)
		doc.set(CREDIT_ENABLED_FIELD, 1)
		with self.assertRaises(frappe.PermissionError) as caught:
			doc.save()
		self.assertIn("System Manager or an Accounts Manager", str(caught.exception))
		with self.assertRaises(frappe.PermissionError) as caught:
			client.set_value("Customer", customer, CREDIT_ENABLED_FIELD, 1)
		self.assertIn("System Manager or an Accounts Manager", str(caught.exception))
		frappe.set_user("Administrator")
		self.assertEqual(facility(customer)[CREDIT_ENABLED_FIELD], 0)

	def test_non_editor_may_still_disable_and_edit(self):
		from frappe import client

		customer = make_customer("guard disable")
		save_customer(customer, **{CREDIT_ENABLED_FIELD: 1})
		frappe.set_user(SALES_USER)
		client.set_value("Customer", customer, "disabled", 1)
		frappe.set_user("Administrator")
		row = facility(customer)
		self.assertEqual((row[CREDIT_ENABLED_FIELD], row.disabled), (1, 1))
		self.assertEqual(format_db_datetime(row[CREDIT_CHANGED_AT_FIELD]), format_db_datetime(row.modified))

	def test_editor_and_migrate_may_switch(self):
		customer = make_customer("guard editor")
		frappe.set_user(ACCOUNTS)
		save_customer(customer, **{CREDIT_ENABLED_FIELD: 1})
		frappe.set_user(SALES_USER)
		frappe.flags.in_migrate = True
		try:
			save_customer(customer, **{CREDIT_ENABLED_FIELD: 0})
		finally:
			frappe.flags.in_migrate = False
		frappe.set_user("Administrator")
		self.assertEqual(facility(customer)[CREDIT_ENABLED_FIELD], 0)


class TestFacilityStatus(WebCreditCase):
	def test_get_status_carries_credit_enabled_for_flag_device(self):
		enabled = make_customer("status on", limit=150)
		save_customer(enabled, **{CREDIT_ENABLED_FIELD: 1})
		plain = make_customer("status off", limit=150)
		gone = make_customer("status disabled", limit=150)
		save_customer(gone, **{CREDIT_ENABLED_FIELD: 1})
		save_customer(gone, disabled=1)
		restaurant = register(self.profile.name, FLAG_DEVICE)
		with flag_hooks():
			answers = {
				name: credit_api.get_status(device_id=restaurant, customer=name)
				for name in (enabled, plain, gone)
			}
			till = credit_api.get_status(device_id=self.device_id, customer=enabled)
		self.assertEqual(
			[answers[name]["credit_enabled"] for name in (enabled, plain, gone)], [True, False, False]
		)
		# a retail till's answer is 0.13.2's
		# (a superset since 0.16.0: the overdue figures and credit days are new keys)
		for key in ("as_of", "available", "credit_limit", "customer", "outstanding", "over_limit"):
			self.assertIn(key, till)
		self.assertEqual(till["customer"], enabled)
		self.assertEqual(till["credit_limit"], "150.000")
		self.assertIs(till["over_limit"], False)
		self.assertEqual(
			set(till) - {"as_of"},
			{"available", "credit_limit", "customer", "outstanding", "over_limit"}
			| {"overdue", "oldest_unpaid", "credit_days"},
		)
		self.assertNotIn("credit_enabled", till)
		# a credit type that is not a flag type: no key either
		with dummy.fake_hooks({"crenya_pos_credit_device_types": [FLAG_DEVICE]}):
			self.assertNotIn("credit_enabled", credit_api.get_status(device_id=restaurant, customer=enabled))

	def test_switched_off_outlet_sends_no_flag(self):
		customer = make_customer("status switch", limit=None)
		save_customer(customer, **{CREDIT_ENABLED_FIELD: 1})
		restaurant = register(self.profile.name, FLAG_DEVICE)
		self.post(self.on_account_sale(customer, total="10.000"))
		self.set_switch(0)
		try:
			with flag_hooks():
				status = credit_api.get_status(device_id=restaurant, customer=customer)
		finally:
			self.set_switch(1)
		# a customer who owes is still answered, without the flag (the gate of credit_limit)
		self.assertEqual(status["outstanding"], "10.000")
		self.assertNotIn("credit_enabled", status)


class TestSaveCreditFacility(WebCreditCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Role", "Restaurant Manager"):
			frappe.get_doc({"doctype": "Role", "role_name": "Restaurant Manager"}).insert(
				ignore_permissions=True
			)
		ensure_user(RESTAURANT_MANAGER, [POS_USER_ROLE, "Restaurant Manager"])
		frappe.db.commit()  # nosemgrep

	def test_save_credit_facility_accountant_only(self):
		customer = make_customer("facility roles")
		for user in (MANAGER, RESTAURANT_MANAGER):
			frappe.set_user(user)
			with self.assertRaises(frappe.PermissionError, msg=user) as caught:
				web_admin.save_credit_facility(self.profile.name, customer, 1)
			self.assertIn("System Manager or an Accounts Manager", str(caught.exception))
		frappe.set_user("Administrator")
		self.assertEqual(facility(customer)[CREDIT_ENABLED_FIELD], 0)
		frappe.set_user(ACCOUNTS)
		answer = web_admin.save_credit_facility(self.profile.name, customer, 1)
		self.assertEqual((answer["customer"], answer["credit_enabled"]), (customer, True))
		self.assertTrue(answer["changed_at"].endswith("Z"))
		frappe.set_user("Administrator")
		self.assertEqual(facility(customer)[CREDIT_ENABLED_FIELD], 1)

	def test_save_credit_facility_writes_comment_and_version(self):
		customer = make_customer("facility audit")
		answer = web_admin.save_credit_facility(self.profile.name, customer, "1")
		self.assertIs(answer["credit_enabled"], True)
		comments = frappe.get_all(
			"Comment",
			filters={"reference_doctype": "Customer", "reference_name": customer},
			pluck="content",
		)
		self.assertIn(
			"House Account (credit facility) switched on from app.crenya.com by Administrator", comments
		)
		versions = frappe.get_all(
			"Version", filters={"ref_doctype": "Customer", "docname": customer}, pluck="data"
		)
		self.assertTrue(any(CREDIT_ENABLED_FIELD in (data or "") for data in versions))
		# the same state again changes nothing and comments nothing
		modified = facility(customer).modified
		again = web_admin.save_credit_facility(self.profile.name, customer, True)
		self.assertEqual(again, answer)
		self.assertEqual(facility(customer).modified, modified)
		off = web_admin.save_credit_facility(self.profile.name, customer, 0)
		self.assertIs(off["credit_enabled"], False)
		self.assertTrue(
			frappe.db.exists(
				"Comment",
				{
					"reference_name": customer,
					"content": "House Account (credit facility) switched off from app.crenya.com by Administrator",
				},
			)
		)
		with self.assertRaises(frappe.ValidationError):
			web_admin.save_credit_facility(self.profile.name, customer, "maybe")

	def test_save_credit_facility_needs_web_session(self):
		customer = make_customer("facility session")
		with mock.patch("crenya_pos_api.web.context.request_uses_api_key", return_value=True):
			with self.assertRaises(frappe.PermissionError):
				web_admin.save_credit_facility(self.profile.name, customer, 1)
		self.assertEqual(facility(customer)[CREDIT_ENABLED_FIELD], 0)

	def test_a_disabled_customer_is_switched_off_not_on(self):
		customer = make_customer("facility disabled")
		save_customer(customer, **{CREDIT_ENABLED_FIELD: 1})
		save_customer(customer, disabled=1)
		with self.assertRaises(frappe.ValidationError):
			web_admin.save_credit_facility(self.profile.name, customer, 1)
		self.assertIs(web_admin.save_credit_facility(self.profile.name, customer, 0)["credit_enabled"], False)
		with self.assertRaises(frappe.ValidationError):
			web_admin.save_credit_facility(self.profile.name, customer, 1)

	def test_save_refuses_customer_of_other_company(self):
		"""ERPNext's Customer has no company: the outlet's customers are those its tills may sell to
		(inside its customer groups), less its walk-in and another company's internal customer."""
		# `represents_company` is unique on the site: free it of any customer a test left holding it,
		# and again when this test is done
		def free():
			frappe.db.sql(
				"update `tabCustomer` set is_internal_customer = 0, represents_company = null "
				"where represents_company = %s",
				OTHER_COMPANY,
			)
			frappe.db.commit()  # nosemgrep

		free()
		self.addCleanup(free)
		# an internal customer that stands for another company
		internal = make_customer("facility internal")
		frappe.db.set_value(
			"Customer", internal, {"is_internal_customer": 1, "represents_company": OTHER_COMPANY}
		)
		with self.assertRaises(frappe.PermissionError) as caught:
			web_admin.save_credit_facility(self.profile.name, internal, 1)
		self.assertIn(OTHER_COMPANY, str(caught.exception))
		self.assertEqual(facility(internal)[CREDIT_ENABLED_FIELD], 0)
		# an ordinary customer again is the outlet's
		frappe.db.set_value("Customer", internal, {"is_internal_customer": 0, "represents_company": None})
		self.assertIs(web_admin.save_credit_facility(self.profile.name, internal, 1)["credit_enabled"], True)
		# the walk-in, and a customer that does not exist
		for customer in (self.walk_in, f"_TC missing {new_id()[:6]}"):
			with self.assertRaises(frappe.PermissionError, msg=customer):
				web_admin.save_credit_facility(self.profile.name, customer, 1)
		self.assertEqual(facility(self.walk_in)[CREDIT_ENABLED_FIELD], 0)
		# a customer outside the outlet's customer groups
		outside = make_customer("facility outside")
		other_group = "_Test Crenya Other Web Customers"
		# `leaf_customer_group()` may itself be that group on a site that kept it: put the customer
		# in a group the outlet will not list
		frappe.db.set_value("Customer", outside, "customer_group", "Individual")
		if not frappe.db.exists("Customer Group", other_group):
			from frappe.utils.nestedset import get_root_of

			fixtures._insert(
				{
					"doctype": "Customer Group",
					"customer_group_name": other_group,
					"parent_customer_group": get_root_of("Customer Group"),
				}
			)
		saved = [row.customer_group for row in self.profile.customer_groups]
		profile = frappe.get_doc("POS Profile", self.profile.name)
		profile.set("customer_groups", [{"customer_group": other_group}])
		profile.flags.ignore_validate = True
		profile.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep
		frappe.clear_document_cache("POS Profile", self.profile.name)
		try:
			with self.assertRaises(frappe.PermissionError) as caught:
				web_admin.save_credit_facility(self.profile.name, outside, 1)
			self.assertIn("is not a customer of", str(caught.exception))
		finally:
			profile = frappe.get_doc("POS Profile", self.profile.name)
			profile.set("customer_groups", [{"customer_group": g} for g in saved])
			profile.flags.ignore_validate = True
			profile.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep
			frappe.clear_document_cache("POS Profile", self.profile.name)
		self.assertEqual(facility(outside)[CREDIT_ENABLED_FIELD], 0)
