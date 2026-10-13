"""Shop staff from app.crenya.com (crenya_pos_api/api/web_admin/staff.py): a System Manager sets a
user's retail till privilege, 6-digit PIN and shops.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_web_staff
"""

import frappe

from crenya_pos_api.api import web_admin
from crenya_pos_api.sync.approvals import APPROVER_ROLE
from crenya_pos_api.sync.cashier import PIN_HASH_FIELD, verify_pin
from crenya_pos_api.sync.context import POS_USER_ROLE
from crenya_pos_api.tests.web_admin_case import MANAGER, SHOP2, SHOP2_CASHIER, WebAdminCase, ensure_user

NEW_STAFF = "crenya.shop.newstaff@example.com"
OWNER = "crenya.shop.owner@example.com"
PIN = "246813"


def roles_of(user):
	return set(frappe.get_roles(user)) & {POS_USER_ROLE, APPROVER_ROLE, "System Manager"}


class TestWebStaff(WebAdminCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(OWNER, ["System Manager"])
		frappe.db.commit()

	def setUp(self):
		# a user with no till role at all, each test
		frappe.set_user("Administrator")
		ensure_user(NEW_STAFF, ["Accounts User"])
		doc = frappe.get_doc("User", NEW_STAFF)
		doc.set("roles", [{"role": "Accounts User"}])
		doc.set(PIN_HASH_FIELD, None)
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		self.as_user(OWNER)

	def tearDown(self):
		frappe.set_user("Administrator")
		profile = frappe.get_doc("POS Profile", SHOP2)
		profile.set(
			"applicable_for_users", [row for row in profile.applicable_for_users if row.user != NEW_STAFF]
		)
		profile.save(ignore_permissions=True)
		frappe.db.commit()
		super().tearDown()

	def test_cashier_privilege_gives_only_pos_user(self):
		answer = web_admin.staff_save(NEW_STAFF, "cashier", PIN)
		self.assertEqual(answer["privilege"], "cashier")
		self.assertTrue(answer["has_pin"])
		self.assertEqual(roles_of(NEW_STAFF), {POS_USER_ROLE})
		# the other roles are untouched; the PIN is stored only as its hash
		self.assertIn("Accounts User", frappe.get_roles(NEW_STAFF))
		self.assertTrue(verify_pin(PIN, frappe.db.get_value("User", NEW_STAFF, PIN_HASH_FIELD)))
		self.assertNotIn(PIN, str(answer))
		self.assertEqual([p["value"] for p in answer["privileges"]], ["cashier", "manager"])
		self.assertTrue(all(p["label_ar"] for p in answer["privileges"]))

	def test_manager_privilege_adds_approver_role(self):
		web_admin.staff_save(NEW_STAFF, "cashier")
		answer = web_admin.staff_save(NEW_STAFF, "manager")
		self.assertEqual(answer["privilege"], "manager")
		self.assertEqual(roles_of(NEW_STAFF), {POS_USER_ROLE, APPROVER_ROLE})
		answer = web_admin.staff_save(NEW_STAFF, None)
		self.assertIsNone(answer["privilege"])
		self.assertEqual(roles_of(NEW_STAFF), set())
		self.assertIn("Accounts User", frappe.get_roles(NEW_STAFF))

	def test_pin_must_be_six_digits(self):
		for bad in ("1234", "12345a", "1234567"):
			with self.assertRaises(frappe.ValidationError, msg=bad):
				web_admin.staff_save(NEW_STAFF, "cashier", bad)
		self.assertFalse(frappe.db.get_value("User", NEW_STAFF, PIN_HASH_FIELD))

	def test_non_system_manager_refused(self):
		self.as_user(MANAGER)
		for call in (
			lambda: web_admin.staff_get(NEW_STAFF),
			lambda: web_admin.staff_save(NEW_STAFF, "manager", PIN),
			lambda: web_admin.staff_clear_pin(NEW_STAFF),
		):
			with self.assertRaises(frappe.PermissionError):
				call()

	def test_cannot_edit_administrator(self):
		for user in ("Administrator", "administrator", "Guest"):
			with self.assertRaises(frappe.PermissionError, msg=user):
				web_admin.staff_save(user, "cashier", PIN)

	def test_owner_keeps_system_manager(self):
		answer = web_admin.staff_save(OWNER, "manager", PIN)
		self.assertEqual(answer["privilege"], "manager")
		self.assertIn("System Manager", frappe.get_roles(OWNER))
		web_admin.staff_save(OWNER, None)
		self.assertIn("System Manager", frappe.get_roles(OWNER))

	def test_outlet_listing_added_and_removed(self):
		answer = web_admin.staff_get(NEW_STAFF)
		shop2 = next(row for row in answer["all_outlets"] if row["name"] == SHOP2)
		self.assertFalse(shop2["everyone"])
		self.assertNotIn(SHOP2, answer["outlets"])
		# the test profile lists nobody: everyone works there and it is never changed
		self.assertIn(self.profile.name, answer["outlets"])
		answer = web_admin.staff_save(NEW_STAFF, "cashier", None, [SHOP2, self.profile.name])
		self.assertIn(SHOP2, answer["outlets"])
		self.assertIn(
			NEW_STAFF, {row.user for row in frappe.get_doc("POS Profile", SHOP2).applicable_for_users}
		)
		self.assertFalse(frappe.get_doc("POS Profile", self.profile.name).applicable_for_users)
		answer = web_admin.staff_save(NEW_STAFF, "cashier", None, [self.profile.name])
		self.assertNotIn(SHOP2, answer["outlets"])
		self.assertIn(
			SHOP2_CASHIER, {row.user for row in frappe.get_doc("POS Profile", SHOP2).applicable_for_users}
		)
		with self.assertRaises(frappe.ValidationError):
			web_admin.staff_save(NEW_STAFF, "cashier", None, ["_Test Crenya Not A Shop"])

	def test_clear_pin(self):
		web_admin.staff_save(NEW_STAFF, "cashier", PIN)
		answer = web_admin.staff_clear_pin(NEW_STAFF)
		self.assertFalse(answer["has_pin"])
		self.assertFalse(frappe.db.get_value("User", NEW_STAFF, PIN_HASH_FIELD))
		# again: nothing to clear, no error
		self.assertFalse(web_admin.staff_clear_pin(NEW_STAFF)["has_pin"])
