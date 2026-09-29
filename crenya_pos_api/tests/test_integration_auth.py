"""Till sign-in: username + password → API key pair.

Run with: bench --site <site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_auth
"""

import frappe

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

from crenya_pos_api.api.auth import sign_in
from crenya_pos_api.setup.install import POS_USER_ROLE, after_install

PASSWORD = "Till-Pass-2026!"


def ensure_user(email, roles):
	if not frappe.db.exists("User", email):
		user = frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": "Till", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	else:
		user = frappe.get_doc("User", email)
	user.set("roles", [])
	user.add_roles(*roles)
	user.new_password = PASSWORD
	user.save(ignore_permissions=True)
	return user.name


class TestCrenyaSignIn(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		after_install()
		cls.cashier = ensure_user("_test_crenya_cashier@example.com", [POS_USER_ROLE, "Sales User"])
		cls.clerk = ensure_user("_test_crenya_clerk@example.com", ["Sales User"])
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_password_sign_in_returns_working_keys(self):
		result = sign_in(self.cashier, PASSWORD)
		self.assertEqual(result["user"], self.cashier)
		self.assertTrue(result["api_key"])
		self.assertTrue(result["api_secret"])

		from frappe.utils.password import get_decrypted_password

		self.assertEqual(frappe.db.get_value("User", self.cashier, "api_key"), result["api_key"])
		self.assertEqual(get_decrypted_password("User", self.cashier, "api_secret"), result["api_secret"])

	def test_second_till_reuses_existing_keys(self):
		first = sign_in(self.cashier, PASSWORD)
		second = sign_in(self.cashier, PASSWORD)
		self.assertEqual(first["api_key"], second["api_key"])
		self.assertEqual(first["api_secret"], second["api_secret"])

	def test_wrong_password_is_rejected(self):
		with self.assertRaises(frappe.AuthenticationError):
			sign_in(self.cashier, "not-the-password")

	def test_missing_credentials_are_rejected(self):
		with self.assertRaises(frappe.AuthenticationError):
			sign_in(self.cashier, "")

	def test_user_without_pos_role_is_rejected(self):
		with self.assertRaises(frappe.PermissionError):
			sign_in(self.clerk, PASSWORD)
