"""Cashier PINs and the `cashier` pull entity against a real ERPNext site.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_cashier
Users are named _test_crenya_pin_* and persist on the test site.
"""

import uuid

import frappe
from frappe.utils.password import get_decrypted_password, set_encrypted_password

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

from crenya_pos_api.api import cashier as cashier_api
from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.setup.install import POS_USER_ROLE, after_install
from crenya_pos_api.sync.cashier import PIN_FIELD, PIN_HASH_FIELD, clear_user_pin, verify_pin
from crenya_pos_api.tests import fixtures

CASHIER = "_test_crenya_pin_cashier@example.com"
SECOND = "_test_crenya_pin_second@example.com"
CLERK = "_test_crenya_pin_clerk@example.com"


def ensure_user(email, roles, first_name):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": first_name, "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	user.enabled = 1
	user.set("roles", [])
	user.set(PIN_HASH_FIELD, None)
	user.save(ignore_permissions=True)
	if roles:
		user.add_roles(*roles)
	return user.name


def set_pin(user, pin):
	doc = frappe.get_doc("User", user)
	doc.set(PIN_FIELD, pin)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return doc


def stored_pin_hash(user):
	return frappe.db.get_value("User", user, PIN_HASH_FIELD)


def auth_rows(user):
	return frappe.db.sql(
		"select count(*) from `__Auth` where doctype='User' and name=%s and fieldname=%s",
		(user, PIN_FIELD),
	)[0][0]


class TestCrenyaCashiers(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		after_install()

		cls._saved_lag = frappe.conf.get("crenya_pos_pull_lag_seconds")
		frappe.conf.crenya_pos_pull_lag_seconds = 0

		cls.profile = fixtures.setup_fixtures()
		cls.device_id = str(uuid.uuid4())
		device_api.register_device(
			device_id=cls.device_id, device_name="PIN Till", pos_profile=cls.profile.name
		)
		ensure_user(CASHIER, [POS_USER_ROLE, "Sales User"], "PIN Cashier")
		ensure_user(SECOND, [POS_USER_ROLE], "Second Cashier")
		ensure_user(CLERK, ["Sales User"], "PIN Clerk")
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		if cls._saved_lag is None:
			frappe.conf.pop("crenya_pos_pull_lag_seconds", None)
		else:
			frappe.conf.crenya_pos_pull_lag_seconds = cls._saved_lag
		super().tearDownClass()

	def tearDown(self):
		frappe.set_user("Administrator")

	def _pull_all(self, cursor=None):
		records, pages = [], 0
		while True:
			page = sync_api.pull_changes(device_id=self.device_id, entity="cashier", cursor=cursor, limit=50)
			pages += 1
			records.extend(page["records"])
			cursor = page["next_cursor"]
			if not page["has_more"]:
				return {record["name"]: record for record in records}, cursor
			self.assertLess(pages, 500, "pull did not terminate")

	# PINs

	def test_setting_pin_stores_only_the_hash(self):
		doc = set_pin(CASHIER, "4826")
		self.assertFalse(doc.get(PIN_FIELD))

		stored = frappe.db.get_value("User", CASHIER, [PIN_FIELD, PIN_HASH_FIELD], as_dict=True)
		self.assertFalse(stored[PIN_FIELD], "the PIN must not be kept in the User table")
		self.assertTrue(stored[PIN_HASH_FIELD].startswith("pbkdf2_sha256$120000$"))
		self.assertTrue(verify_pin("4826", stored[PIN_HASH_FIELD]))
		self.assertFalse(verify_pin("4827", stored[PIN_HASH_FIELD]))

		self.assertEqual(auth_rows(CASHIER), 0, "the PIN must not be kept in __Auth")
		self.assertIsNone(get_decrypted_password("User", CASHIER, PIN_FIELD, raise_exception=False))

		# saving the user again without a new PIN keeps the hash
		doc = frappe.get_doc("User", CASHIER)
		doc.save(ignore_permissions=True)
		self.assertEqual(stored_pin_hash(CASHIER), stored[PIN_HASH_FIELD])

		# a new PIN gets a new hash (fresh salt)
		set_pin(CASHIER, "123456")
		self.assertTrue(verify_pin("123456", stored_pin_hash(CASHIER)))
		self.assertFalse(verify_pin("4826", stored_pin_hash(CASHIER)))

	def test_invalid_pin_is_rejected(self):
		set_pin(CASHIER, "2468")
		before = stored_pin_hash(CASHIER)
		for pin in ("123", "1234567", "12a4", "12 4", "١٢٣٤"):
			doc = frappe.get_doc("User", CASHIER)
			doc.set(PIN_FIELD, pin)
			with self.subTest(pin=pin), self.assertRaises(frappe.ValidationError):
				doc.save(ignore_permissions=True)
			frappe.db.rollback()
			self.assertEqual(stored_pin_hash(CASHIER), before)
			self.assertEqual(auth_rows(CASHIER), 0)

	def test_pin_left_in_auth_is_hashed_and_removed(self):
		# as if an earlier save had skipped the hook and let Frappe store the PIN
		set_encrypted_password("User", CASHIER, "1357", PIN_FIELD)
		frappe.db.set_value("User", CASHIER, PIN_FIELD, "****", update_modified=False)
		frappe.db.commit()

		doc = frappe.get_doc("User", CASHIER)
		self.assertEqual(doc.get(PIN_FIELD), "****")
		doc.save(ignore_permissions=True)
		frappe.db.commit()

		self.assertTrue(verify_pin("1357", stored_pin_hash(CASHIER)))
		self.assertFalse(frappe.db.get_value("User", CASHIER, PIN_FIELD))
		self.assertEqual(auth_rows(CASHIER), 0)

	def test_clear_pin(self):
		set_pin(CASHIER, "9753")
		frappe.set_user(CLERK)
		with self.assertRaises(frappe.PermissionError):
			cashier_api.clear_pin(CASHIER)
		frappe.set_user("Administrator")
		self.assertTrue(stored_pin_hash(CASHIER))

		result = cashier_api.clear_pin(CASHIER)
		frappe.db.commit()
		self.assertEqual(result, {"user": CASHIER, "pin_set": False})
		self.assertFalse(stored_pin_hash(CASHIER))

		with self.assertRaises(frappe.DoesNotExistError):
			cashier_api.clear_pin("_test_crenya_nobody@example.com")

	# pull

	def test_pull_returns_role_holders_with_pin_hash(self):
		set_pin(CASHIER, "4826")
		records, _cursor = self._pull_all()

		self.assertIn(CASHIER, records)
		self.assertIn(SECOND, records)
		self.assertNotIn(CLERK, records, "users without the role (and without a PIN) are not sent")
		self.assertNotIn("Guest", records)

		cashier = records[CASHIER]
		self.assertEqual(set(cashier), {"name", "full_name", "enabled", "pin_hash", "roles", "modified"})
		self.assertEqual(cashier["enabled"], 1)
		# only device roles, never the user's other roles (CASHIER also has Sales User)
		self.assertEqual(cashier["roles"], [POS_USER_ROLE])
		self.assertEqual(cashier["full_name"], frappe.db.get_value("User", CASHIER, "full_name"))
		self.assertTrue(verify_pin("4826", cashier["pin_hash"]))
		self.assertIsNone(records[SECOND]["pin_hash"], "no PIN set → null")

	def test_pull_respects_applicable_for_users(self):
		profile = frappe.get_doc("POS Profile", self.profile.name)
		profile.append("applicable_for_users", {"user": CASHIER})
		profile.save(ignore_permissions=True)
		frappe.db.commit()
		try:
			records, _cursor = self._pull_all()
			self.assertIn(CASHIER, records)
			self.assertNotIn(SECOND, records, "cashier not listed on the POS Profile")
		finally:
			profile.reload()
			profile.set("applicable_for_users", [])
			profile.save(ignore_permissions=True)
			frappe.db.commit()

		records, _cursor = self._pull_all()
		self.assertIn(SECOND, records, "an empty table allows every cashier")

	def test_profile_access_change_reaches_tills(self):
		set_pin(CASHIER, "4826")
		set_pin(SECOND, "2222")
		records, cursor = self._pull_all()
		self.assertEqual(records[SECOND]["enabled"], 1)

		profile = frappe.get_doc("POS Profile", self.profile.name)
		try:
			# restricting the profile to CASHIER must disable SECOND on the till
			profile.append("applicable_for_users", {"user": CASHIER})
			profile.save(ignore_permissions=True)
			frappe.db.commit()
			records, cursor = self._pull_all(cursor)
			self.assertIn(SECOND, records, "a PIN holder removed from the profile is sent again")
			self.assertEqual(records[SECOND]["enabled"], 0)
			self.assertIsNone(records[SECOND]["pin_hash"])
		finally:
			profile.reload()
			profile.set("applicable_for_users", [])
			profile.save(ignore_permissions=True)
			frappe.db.commit()

		try:
			records, cursor = self._pull_all(cursor)
			self.assertEqual(records[SECOND]["enabled"], 1, "access restored when the table is cleared")
			self.assertTrue(verify_pin("2222", records[SECOND]["pin_hash"]))
		finally:
			# other tests expect SECOND without a PIN
			clear_user_pin(SECOND)
			frappe.db.commit()

	def test_role_removal_and_disabling_come_back_disabled(self):
		set_pin(SECOND, "1111")
		records, cursor = self._pull_all()
		self.assertEqual(records[SECOND]["enabled"], 1)

		try:
			frappe.get_doc("User", SECOND).remove_roles(POS_USER_ROLE)
			frappe.db.commit()
			records, cursor = self._pull_all(cursor)
			self.assertIn(SECOND, records, "a user who lost the role must be sent again")
			self.assertEqual(records[SECOND]["enabled"], 0)
			self.assertIsNone(records[SECOND]["pin_hash"])
			self.assertEqual(records[SECOND]["roles"], [])

			frappe.get_doc("User", SECOND).add_roles(POS_USER_ROLE)
			frappe.db.commit()
			records, cursor = self._pull_all(cursor)
			self.assertEqual(records[SECOND]["enabled"], 1)
			self.assertTrue(verify_pin("1111", records[SECOND]["pin_hash"]))
			self.assertEqual(records[SECOND]["roles"], [POS_USER_ROLE])

			user = frappe.get_doc("User", SECOND)
			user.enabled = 0
			user.save(ignore_permissions=True)
			frappe.db.commit()
			records, cursor = self._pull_all(cursor)
			self.assertEqual(records[SECOND]["enabled"], 0)
			self.assertIsNone(records[SECOND]["pin_hash"])
		finally:
			ensure_user(SECOND, [POS_USER_ROLE], "Second Cashier")
			frappe.db.commit()

	def test_deleted_user_is_a_tombstone(self):
		email = f"_test_crenya_pin_{uuid.uuid4().hex[:8]}@example.com"
		ensure_user(email, [POS_USER_ROLE], "Temp Cashier")
		frappe.db.commit()
		records, cursor = self._pull_all()
		self.assertIn(email, records)

		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		frappe.db.commit()
		page = sync_api.pull_changes(device_id=self.device_id, entity="cashier", cursor=cursor, limit=50)
		self.assertIn(email, page["tombstones"])
