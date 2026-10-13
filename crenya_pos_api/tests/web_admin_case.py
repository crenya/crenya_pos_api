"""The shared case of the Shops web admin integration tests (not a test module itself: the runner
collects test_*.py only): the test POS Profile (`fixtures`), a second shop listing its own staff,
and the users who manage them or only sell.

Users and the second shop persist on the test site (names start with _Test Crenya / crenya.shop).
"""

import frappe

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

from crenya_pos_api.sync.approvals import APPROVER_ROLE
from crenya_pos_api.sync.cashier import PIN_FIELD
from crenya_pos_api.sync.context import POS_USER_ROLE
from crenya_pos_api.tests import fixtures

# a manager of every shop that lists nobody (the test profile)
MANAGER = "crenya.shop.manager@example.com"
# a cashier: sells, manages nothing
CASHIER = "crenya.shop.cashier@example.com"
# the second shop lists these two alone: its own cashier and its own manager
SHOP2 = "_Test Crenya Shop 2"
SHOP2_CASHIER = "crenya.shop2.cashier@example.com"
SHOP2_MANAGER = "crenya.shop2.manager@example.com"


def ensure_user(email, roles, pin=None):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"roles": [{"role": role} for role in roles],
			}
		).insert(ignore_permissions=True)
	if pin:
		doc = frappe.get_doc("User", email)
		doc.set(PIN_FIELD, pin)
		doc.save(ignore_permissions=True)
	return email


def ensure_shop2(users):
	"""A second shop of the test company whose Applicable for Users lists `users`."""
	company = frappe.get_doc("Company", fixtures.COMPANY)
	customer = frappe.db.get_value("Customer", {"customer_name": fixtures.CUSTOMER}, "name")
	profile = fixtures.ensure_pos_profile(company, customer, name=SHOP2)
	if {row.user for row in profile.applicable_for_users} != set(users):
		profile.set("applicable_for_users", [{"user": user} for user in users])
		profile.save(ignore_permissions=True)
	return profile


class WebAdminCase(FrappeTestCase):
	"""Fixtures, the users and the second shop; each test runs as Administrator unless it signs
	in as another user with `as_user`."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.profile = fixtures.setup_fixtures()
		ensure_user(MANAGER, [POS_USER_ROLE, APPROVER_ROLE], pin="135790")
		ensure_user(CASHIER, [POS_USER_ROLE], pin="246802")
		ensure_user(SHOP2_CASHIER, [POS_USER_ROLE], pin="112233")
		ensure_user(SHOP2_MANAGER, [POS_USER_ROLE, APPROVER_ROLE])
		ensure_shop2([SHOP2_CASHIER, SHOP2_MANAGER])
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def as_user(self, user):
		frappe.set_user(user)

	def keep_profile(self, name, *fields):
		"""Put `fields` of POS Profile `name` back after the test."""
		saved = frappe.db.get_value("POS Profile", name, list(fields), as_dict=True)

		def restore():
			frappe.db.set_value("POS Profile", name, saved, update_modified=False)
			frappe.clear_document_cache("POS Profile", name)
			frappe.db.commit()

		self.addCleanup(restore)
