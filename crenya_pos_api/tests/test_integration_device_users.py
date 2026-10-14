"""Per-device users: a device signs in with the key of its own user, never a person's (0.12.0).

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_device_users
"""

import base64
import hashlib
import hmac
import json
import types

import frappe

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.api.auth import sign_in
from crenya_pos_api.setup.install import after_install
from crenya_pos_api.sync import device_user
from crenya_pos_api.sync.cashier import PIN_FIELD
from crenya_pos_api.sync.context import DEVICE_DOCTYPE, DEVICE_ROLE, POS_USER_ROLE, all_device_roles
from crenya_pos_api.sync.errors import DeviceNotRegisteredError, DevicePermissionError, RekeyRequiredError
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import SyncCase, new_id

PASSWORD = "Till-Pass-2026!"
CASHIER = "_test_crenya_dev_cashier@example.com"
OTHER = "_test_crenya_dev_other@example.com"
STAFF = "_test_crenya_dev_staff@example.com"
BOSS = "_test_crenya_dev_boss@example.com"
PIN = "4826"


def ensure_user(email, roles, pin=None):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": "Dev", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	user.set("roles", [])
	user.add_roles(*roles)
	user.new_password = PASSWORD
	if pin:
		user.set(PIN_FIELD, pin)
	user.save(ignore_permissions=True)
	return user.name


class TestDeviceUsers(SyncCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Frappe refuses more than 60 new users an hour (`throttle_user_limit`); each test pairs a
		# device of its own, and a site pairing more than that in an hour is not a case to test
		cls._saved_throttle = frappe.conf.get("throttle_user_limit")
		frappe.conf.throttle_user_limit = 10000
		after_install()
		cls.cashier = ensure_user(CASHIER, [POS_USER_ROLE, "Sales User"])
		cls.other = ensure_user(OTHER, [POS_USER_ROLE])
		cls.staff = ensure_user(STAFF, [POS_USER_ROLE], pin=PIN)
		cls.boss = ensure_user(BOSS, ["System Manager"])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		if cls._saved_throttle is None:
			frappe.conf.pop("throttle_user_limit", None)
		else:
			frappe.conf.throttle_user_limit = cls._saved_throttle
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		self._ip = getattr(frappe.local, "request_ip", None)
		frappe.local.request_ip = "127.0.0.1"
		self._saved_conf = frappe.conf.get(device_user.REQUIRE_DEVICE_KEYS)

	def tearDown(self):
		frappe.local.request_ip = self._ip
		frappe.set_user("Administrator")
		if self._saved_conf is None:
			frappe.conf.pop(device_user.REQUIRE_DEVICE_KEYS, None)
		else:
			frappe.conf[device_user.REQUIRE_DEVICE_KEYS] = self._saved_conf

	# helpers

	def pair(self, person=None, device_id=None):
		"""A device signed in by `person`: (device id, the sign-in answer)."""
		device_id = device_id or new_id()
		return device_id, sign_in(person or self.cashier, PASSWORD, device_id)

	def register(self, user, device_id, device_type=None):
		frappe.set_user(user)
		try:
			return device_api.register_device(
				device_id=device_id,
				device_name="Till",
				pos_profile=self.profile.name,
				app_version="0.7.4",
				platform="linux",
				device_type=device_type,
			)
		finally:
			frappe.set_user("Administrator")

	def as_user(self, user, fn, *args, key=True, **kwargs):
		"""Call `fn` as `user`, signed with an API key (an Authorization header) unless key=False."""
		frappe.set_user(user)
		try:
			if key:
				with fixtures.signed_with_key():
					return fn(*args, **kwargs)
			return fn(*args, **kwargs)
		finally:
			frappe.set_user("Administrator")

	def pull(self, user, device_id, entity="cashier", **kwargs):
		return self.as_user(
			user, sync_api.pull_changes, device_id=device_id, entity=entity, limit=500, **kwargs
		)

	def user_key_warnings(self):
		return frappe.db.count("Error Log", {"method": ["like", "Crenya POS: a device is signed in%"]})

	def forget_warning(self, device_id):
		frappe.cache.delete_value(f"crenya_pos_user_key:{device_id}:{frappe.utils.nowdate()}")

	# pairing

	def test_sign_in_returns_a_device_users_key_never_the_persons(self):
		device_id, answer = self.pair()
		name = device_user.device_user_name(device_id)
		self.assertEqual(answer["user"], self.cashier)
		self.assertEqual(answer["key_kind"], "device")
		self.assertEqual(answer["device_user"], name)
		self.assertEqual(frappe.db.get_value("User", name, "api_key"), answer["api_key"])
		self.assertNotEqual(frappe.db.get_value("User", self.cashier, "api_key"), answer["api_key"])
		from frappe.utils.password import get_decrypted_password

		self.assertEqual(get_decrypted_password("User", name, "api_secret"), answer["api_secret"])

	def test_the_device_user_is_a_system_user_with_the_device_role_only(self):
		device_id, _answer = self.pair()
		name = device_user.device_user_name(device_id)
		user = frappe.get_doc("User", name)
		self.assertEqual(user.user_type, "System User")
		self.assertTrue(user.enabled)
		self.assertEqual([row.role for row in user.roles], [DEVICE_ROLE])
		self.assertEqual(user.get(device_user.DEVICE_ID_FIELD), device_id)
		self.assertEqual(user.get(device_user.PAIRED_BY_FIELD), self.cashier)
		# no role it holds opens the desk
		for row in user.roles:
			self.assertFalse(frappe.db.get_value("Role", row.role, "desk_access"), row.role)
		self.assertFalse(user.has_desk_access())
		# the name is device-<id>@<site host>
		self.assertEqual(name, f"device-{device_id}@{device_user.site_host()}")

	def test_a_second_sign_in_reuses_the_device_user_and_keeps_the_key(self):
		device_id, first = self.pair()
		_id, second = self.pair(self.other, device_id)
		self.assertEqual(first["api_key"], second["api_key"])
		self.assertEqual(first["api_secret"], second["api_secret"])
		# the last person to authorise it is `paired_by`
		self.assertEqual(frappe.db.get_value("User", first["device_user"], "crenya_paired_by"), self.other)

	def test_a_system_manager_pairs_a_device_and_the_device_is_no_system_manager(self):
		device_id, answer = self.pair(self.boss)
		self.assertEqual(answer["user"], self.boss)
		self.assertNotIn("System Manager", frappe.get_roles(answer["device_user"]))
		self.assertNotEqual(frappe.db.get_value("User", self.boss, "api_key"), answer["api_key"])

	def test_a_disabled_device_cannot_be_signed_in_again(self):
		device_id, _answer = self.pair()
		self.register(device_user.device_user_name(device_id), device_id)
		frappe.db.set_value(DEVICE_DOCTYPE, device_id, "enabled", 0)
		with self.assertRaises(DeviceNotRegisteredError):
			sign_in(self.cashier, PASSWORD, device_id)

	def test_a_bad_device_id_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			sign_in(self.cashier, PASSWORD, "x")

	def test_the_device_user_registers_and_is_the_devices_user(self):
		device_id, answer = self.pair()
		name = answer["device_user"]
		registered = self.register(name, device_id)
		self.assertEqual(registered["device_id"], device_id)
		row = frappe.db.get_value(DEVICE_DOCTYPE, device_id, ["user", "paired_by"], as_dict=True)
		self.assertEqual(row.user, name)
		self.assertEqual(row.paired_by, self.cashier)
		# the profiles offered are the person's
		profiles = self.as_user(name, device_api.list_pos_profiles)
		self.assertIn(self.profile.name, [p["name"] for p in profiles])
		# the sign-in name and the key kind
		caps = self.as_user(name, sync_api.get_sync_capabilities)
		self.assertEqual(caps["user"], self.cashier)
		self.assertEqual(caps["key_kind"], "device")
		self.assertTrue(caps["features"]["pin_seal"])
		boot = self.as_user(name, device_api.get_bootstrap, device_id=device_id)
		self.assertEqual(boot["key_kind"], "device")

	def test_the_devices_list_names_the_person_who_paired_the_device(self):
		from crenya_pos_api.web.devices import devices_answer

		device_id, answer = self.pair()
		self.register(answer["device_user"], device_id)
		rows = {row["name"]: row for row in devices_answer(self.profile.name, True)["rows"]}
		self.assertEqual(rows[device_id]["user"], self.cashier)
		self.assertEqual(rows[device_id]["user_name"], frappe.db.get_value("User", self.cashier, "full_name"))
		# a device not yet re-keyed still shows the person it is registered to
		old_id = new_id()
		self.register(self.other, old_id)
		rows = {row["name"]: row for row in devices_answer(self.profile.name, True)["rows"]}
		self.assertEqual(rows[old_id]["user"], self.other)

	def test_a_device_user_registers_other_device_types(self):
		for device_type in ("restaurant_pos", "kds", "waiter"):
			device_id, answer = self.pair()
			registered = self.register(answer["device_user"], device_id, device_type)
			self.assertEqual(registered["device_type"], device_type)

	def test_a_device_user_sells(self):
		device_id, answer = self.pair()
		name = answer["device_user"]
		device = self.register(name, device_id)
		frappe.db.commit()
		payload = self.sale_payload(cashier=self.cashier)
		payload["offline_number"] = f"OFF-{device['device_short']}-dev001"
		frappe.set_user(name)
		try:
			results = sync_api.push_batch(device_id=device_id, events=[self.event(payload)])
		finally:
			frappe.set_user("Administrator")
		self.assertOk(results[0])
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", results[0]["name"], "crenya_cashier"), self.cashier
		)
		self.assertEqual(frappe.db.get_value("Sales Invoice", results[0]["name"], "owner"), name)

	def test_a_device_user_sells_to_a_loyalty_customer(self):
		"""A sale of a customer in a Loyalty Program submitted by the device user alone (0.12.1):
		ERPNext reads the Customer under the pushing user's rights to book the points."""
		device_id, answer = self.pair()
		name = answer["device_user"]
		device = self.register(name, device_id)
		self.assertEqual(set(frappe.get_roles(name)) - {"All", "Guest"}, {DEVICE_ROLE})
		customer = fixtures.make_customer("_Test Crenya Loyal Buyer", fixtures.LOYALTY_PROGRAM)
		frappe.db.commit()
		payload = self.sale_payload(customer=customer, cashier=self.cashier)
		payload["offline_number"] = f"OFF-{device['device_short']}-loy001"
		frappe.set_user(name)
		try:
			results = sync_api.push_batch(device_id=device_id, events=[self.event(payload)])
		finally:
			frappe.set_user("Administrator")
		self.assertOk(results[0])
		self.assertEqual(frappe.db.get_value("Sales Invoice", results[0]["name"], "owner"), name)
		self.assertTrue(
			frappe.db.exists("Loyalty Point Entry", {"invoice": results[0]["name"], "customer": customer})
		)

	def test_another_devices_user_cannot_use_a_device(self):
		first_id, first = self.pair()
		second_id, second = self.pair(self.other)
		self.register(first["device_user"], first_id)
		self.register(second["device_user"], second_id)
		with self.assertRaises(DevicePermissionError):
			self.pull(second["device_user"], first_id)

	# a person's key on a device

	def test_a_persons_key_is_allowed_with_a_warning_while_the_setting_is_off(self):
		device_id = new_id()
		self.register(self.cashier, device_id)
		self.forget_warning(device_id)
		before = self.user_key_warnings()
		page = self.pull(self.cashier, device_id)
		self.assertEqual(page["entity"], "cashier")
		self.assertEqual(self.user_key_warnings(), before + 1)
		# once a day per device
		self.pull(self.cashier, device_id)
		self.assertEqual(self.user_key_warnings(), before + 1)

	def test_a_persons_key_is_refused_with_rekey_required_when_the_setting_is_on(self):
		device_id = new_id()
		self.register(self.cashier, device_id)
		frappe.conf[device_user.REQUIRE_DEVICE_KEYS] = 1
		for call in (
			lambda: self.pull(self.cashier, device_id),
			lambda: self.as_user(self.cashier, device_api.get_bootstrap, device_id=device_id),
			lambda: self.as_user(self.cashier, sync_api.push_batch, device_id=device_id, events=[]),
		):
			with self.assertRaises(RekeyRequiredError):
				call()
			error = frappe.local.response.get("error")
			self.assertEqual(error["code"], "rekey_required")
			self.assertFalse(error["retryable"])
		# a browser session is no key
		self.assertEqual(self.pull_as_session(self.cashier, device_id)["entity"], "cashier")

	def pull_as_session(self, user, device_id):
		return self.as_user(user, sync_api.pull_changes, device_id=device_id, entity="cashier", key=False)

	def test_a_device_users_key_is_never_refused(self):
		frappe.conf[device_user.REQUIRE_DEVICE_KEYS] = 1
		device_id, answer = self.pair()
		self.register(answer["device_user"], device_id)
		self.assertEqual(self.pull(answer["device_user"], device_id)["entity"], "cashier")

	def test_sign_in_without_a_device_returns_the_persons_key_only_while_the_setting_is_off(self):
		answer = sign_in(self.cashier, PASSWORD)
		self.assertEqual(answer["key_kind"], "user")
		self.assertEqual(frappe.db.get_value("User", self.cashier, "api_key"), answer["api_key"])
		frappe.conf[device_user.REQUIRE_DEVICE_KEYS] = 1
		with self.assertRaises(frappe.ValidationError):
			sign_in(self.cashier, PASSWORD)

	# re-key

	def test_rekey_exchanges_a_persons_key_for_the_devices_own(self):
		device_id = new_id()
		self.register(self.cashier, device_id)
		person_key = device_user.ensure_api_keys(self.cashier)
		frappe.conf[device_user.REQUIRE_DEVICE_KEYS] = 1
		with self.assertRaises(RekeyRequiredError):
			self.pull(self.cashier, device_id)

		answer = self.as_user(self.cashier, device_api.rekey_device, device_id=device_id)
		self.assertEqual(answer["key_kind"], "device")
		self.assertEqual(answer["user"], self.cashier)
		self.assertEqual(answer["device_user"], device_user.device_user_name(device_id))
		self.assertNotEqual(answer["api_key"], person_key[0])
		self.assertTrue(answer["pin_seal_key"])
		row = frappe.db.get_value(DEVICE_DOCTYPE, device_id, ["user", "paired_by"], as_dict=True)
		self.assertEqual((row.user, row.paired_by), (answer["device_user"], self.cashier))
		# the person's key was not rotated
		self.assertEqual(device_user.ensure_api_keys(self.cashier), person_key)

		# the device goes on with its own key, setting on
		self.assertEqual(self.pull(answer["device_user"], device_id)["entity"], "cashier")
		# asking again with the device key returns the same key
		again = self.as_user(answer["device_user"], device_api.rekey_device, device_id=device_id)
		self.assertEqual((again["api_key"], again["api_secret"]), (answer["api_key"], answer["api_secret"]))

	def test_rekey_is_for_the_owner_of_the_device_only(self):
		device_id = new_id()
		self.register(self.cashier, device_id)
		with self.assertRaises(DevicePermissionError):
			self.as_user(self.other, device_api.rekey_device, device_id=device_id)
		# not even a System Manager takes over a device's key
		with self.assertRaises(DevicePermissionError):
			self.as_user(self.boss, device_api.rekey_device, device_id=device_id)
		with self.assertRaises(DeviceNotRegisteredError):
			self.as_user(self.cashier, device_api.rekey_device, device_id=new_id())

	def test_a_device_user_cannot_rekey_another_device(self):
		first_id, first = self.pair()
		second_id, second = self.pair(self.other)
		self.register(first["device_user"], first_id)
		self.register(second["device_user"], second_id)
		with self.assertRaises(DevicePermissionError):
			self.as_user(first["device_user"], device_api.rekey_device, device_id=second_id)

	def test_a_device_user_acts_for_its_own_device_only(self):
		# not for another device registered to the person who paired it, even while the rollout runs
		till = new_id()
		self.register(self.cashier, till)
		hub = new_id()
		self.register(self.cashier, hub)
		answer = self.as_user(self.cashier, device_api.rekey_device, device_id=hub)
		self.assertEqual(self.pull(answer["device_user"], hub)["entity"], "cashier")
		with self.assertRaises(DevicePermissionError):
			self.pull(answer["device_user"], till)

	# deactivating a device

	def test_deactivating_a_device_disables_its_user_and_reactivating_enables_it(self):
		device_id, answer = self.pair()
		name = answer["device_user"]
		self.register(name, device_id)
		device = frappe.get_doc(DEVICE_DOCTYPE, device_id)
		device.enabled = 0
		device.save(ignore_permissions=True)
		self.assertFalse(frappe.db.get_value("User", name, "enabled"))
		device.reload()
		device.enabled = 1
		device.save(ignore_permissions=True)
		self.assertTrue(frappe.db.get_value("User", name, "enabled"))

	def test_deleting_a_device_disables_its_user(self):
		device_id, answer = self.pair()
		self.register(answer["device_user"], device_id)
		frappe.delete_doc(DEVICE_DOCTYPE, device_id, ignore_permissions=True, force=True)
		self.assertFalse(frappe.db.get_value("User", answer["device_user"], "enabled"))

	# what a device user cannot do

	def test_device_roles_hold_no_right_on_users_or_roles(self):
		roles = {POS_USER_ROLE, DEVICE_ROLE, *all_device_roles()}
		for doctype in ("User", "Has Role", "Role", "DocPerm", "Custom DocPerm", "User Permission"):
			for table in ("DocPerm", "Custom DocPerm"):
				rows = frappe.get_all(
					table,
					filters={"parent": doctype, "role": ["in", sorted(roles)]},
					fields=["role", "write", "create", "delete"],
				)
				for row in rows:
					self.assertEqual((row.write, row.create, row.delete), (0, 0, 0), f"{doctype}: {row}")

	def test_a_device_user_cannot_change_users_or_roles_through_frappes_api(self):
		device_id, answer = self.pair()
		name = answer["device_user"]
		self.register(name, device_id)
		frappe.set_user(name)
		try:
			# Frappe lets every user edit their own profile, so `write` on User has no answer without
			# a document; on anyone else's, and create and delete, a device user holds nothing
			for ptype in ("create", "delete"):
				self.assertFalse(frappe.has_permission("User", ptype, user=name), ptype)
			for ptype in ("write", "delete"):
				self.assertFalse(frappe.has_permission("User", ptype, doc=self.other, user=name), ptype)
			with self.assertRaises(frappe.PermissionError):
				frappe.client.set_value("User", self.other, "enabled", 0)
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(
					{
						"doctype": "Has Role",
						"parent": self.other,
						"parenttype": "User",
						"parentfield": "roles",
						"role": "System Manager",
					}
				).insert()
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(
					{"doctype": "User", "email": "_test_crenya_dev_new@example.com", "first_name": "New"}
				).insert()
			# nor can it give itself a role or another user type
			# (saving its own profile with a role added changes no role: roles are a higher level)
			own = frappe.get_doc("User", name)
			own.append("roles", {"role": "System Manager"})
			own.save()
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(
					{
						"doctype": "Has Role",
						"parent": name,
						"parenttype": "User",
						"parentfield": "roles",
						"role": "System Manager",
					}
				).insert()
		finally:
			frappe.set_user("Administrator")
		self.assertNotIn("System Manager", frappe.get_roles(name))
		self.assertEqual(frappe.db.get_value("User", self.other, "enabled"), 1)

	# PIN hashes

	def hash_of(self, page, user):
		return next(r["pin_hash"] for r in page["records"] if r["name"] == user)

	def sealed_with(self, raw, key, pin=PIN):
		"""The seal a device computes for `pin` against the server hash `raw` (pos-core's check)."""
		_scheme, rounds, salt, _derived = raw.split("$")
		derived = hashlib.pbkdf2_hmac("sha256", pin.encode(), base64.b64decode(salt), int(rounds), dklen=32)
		seal = hmac.new(key, b"crenya pin seal v1\0" + derived, hashlib.sha256).digest()
		return seal

	def test_a_device_that_seals_receives_no_raw_pin_hash(self):
		device_id, answer = self.pair()
		self.register(answer["device_user"], device_id)
		raw = frappe.db.get_value("User", self.staff, "crenya_pos_pin_hash")
		self.assertTrue(raw.startswith("pbkdf2_sha256$"))

		page = self.pull(answer["device_user"], device_id, pin_seal=1)
		sealed = self.hash_of(page, self.staff)
		self.assertTrue(sealed.startswith("crenya_pin1$"))
		self.assertNotIn("pbkdf2_sha256", json.dumps(page))
		derived_text = raw.split("$")[3]
		self.assertNotIn(derived_text, json.dumps(page))
		# it is the seal of that hash under the key shared at pairing, which checks the PIN
		key = bytes.fromhex(answer["pin_seal_key"])
		_scheme, key_id, rounds, salt, seal = sealed.split("$")
		self.assertEqual(rounds, raw.split("$")[1])
		self.assertEqual(salt, raw.split("$")[2])
		self.assertEqual(base64.b64decode(seal), self.sealed_with(raw, key))
		self.assertNotEqual(base64.b64decode(seal), self.sealed_with(raw, key, pin="4827"))
		self.assertEqual(key_id, hmac.new(key, b"crenya pin key id", hashlib.sha256).hexdigest()[:8])
		# another device's key does not open it
		other_key = device_user.pin_seal_key(new_id())
		self.assertNotEqual(base64.b64decode(seal), self.sealed_with(raw, other_key))

	def test_device_users_are_not_cashiers(self):
		device_id, answer = self.pair()
		self.register(answer["device_user"], device_id)
		names = [r["name"] for r in self.pull(answer["device_user"], device_id)["records"]]
		self.assertIn(self.staff, names)
		self.assertNotIn(answer["device_user"], names)

	def test_only_a_persons_key_gets_the_raw_hash_and_only_while_the_setting_is_off(self):
		device_id = new_id()
		self.register(self.cashier, device_id)
		raw = frappe.db.get_value("User", self.staff, "crenya_pos_pin_hash")
		self.assertEqual(self.hash_of(self.pull(self.cashier, device_id), self.staff), raw)
		# a device user's key never gets the raw hash: the seal if it asks, else none, setting off too
		device_id2, answer = self.pair()
		self.register(answer["device_user"], device_id2)
		self.assertIsNone(self.hash_of(self.pull(answer["device_user"], device_id2), self.staff))
		sealed = self.hash_of(self.pull(answer["device_user"], device_id2, pin_seal=1), self.staff)
		self.assertTrue(sealed.startswith("crenya_pin1$"))
		# the person's key with the setting on has none
		frappe.conf[device_user.REQUIRE_DEVICE_KEYS] = 1
		with self.assertRaises(RekeyRequiredError):
			self.pull(self.cashier, device_id)

	def test_a_device_that_starts_asking_for_seals_is_sent_every_cashier_again(self):
		device_id = new_id()
		self.register(self.cashier, device_id)
		first = self.pull(self.cashier, device_id)
		self.assertTrue(self.hash_of(first, self.staff).startswith("pbkdf2_sha256$"))
		second = self.pull(self.cashier, device_id, cursor=first["next_cursor"], pin_seal=1)
		self.assertTrue(self.hash_of(second, self.staff).startswith("crenya_pin1$"))

	# who a device user is, and what it cannot be

	def test_a_device_user_cannot_rewrite_who_it_is(self):
		device_id, answer = self.pair(self.boss)
		other_id, other = self.pair(self.other)
		name = answer["device_user"]
		for field, value in (
			(device_user.PAIRED_BY_FIELD, self.other),
			(device_user.DEVICE_ID_FIELD, other_id),
		):
			frappe.set_user(name)
			try:
				own = frappe.get_doc("User", name)
				own.set(field, value)
				# permlevel 1: Frappe drops the change of a user who may not make it ...
				own.save()
				self.assertNotEqual(frappe.db.get_value("User", name, field), value)
				# ... and the User hook refuses it where permissions are bypassed
				own = frappe.get_doc("User", name)
				own.set(field, value)
				with self.assertRaises(frappe.PermissionError):
					own.save(ignore_permissions=True)
			finally:
				frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("User", name, device_user.PAIRED_BY_FIELD), self.boss)
		self.assertEqual(device_user.acting_user(name), self.boss)
		# a new user cannot be made one either, through the API
		frappe.set_user(name)
		try:
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc(
					{
						"doctype": "User",
						"email": "_test_crenya_dev_fake@example.com",
						"first_name": "Fake",
						device_user.DEVICE_ID_FIELD: new_id(),
					}
				).insert()
		finally:
			frappe.set_user("Administrator")
		# a System Manager in a browser session still may
		frappe.set_user(self.boss)
		try:
			doc = frappe.get_doc("User", other["device_user"])
			doc.set(device_user.PAIRED_BY_FIELD, self.boss)
			doc.save(ignore_permissions=True)
		finally:
			frappe.set_user("Administrator")
		with fixtures.signed_with_key():
			frappe.set_user(self.boss)
			try:
				doc = frappe.get_doc("User", other["device_user"])
				doc.set(device_user.PAIRED_BY_FIELD, self.other)
				with self.assertRaises(frappe.PermissionError):
					doc.save(ignore_permissions=True)
			finally:
				frappe.set_user("Administrator")

	def test_a_device_user_keeps_exactly_its_device_role(self):
		from unittest import mock

		device_id = new_id()
		# another app's rule grants a role after the user is made: simulated in _create
		original = device_user._create

		def widened(*args, **kwargs):
			name = original(*args, **kwargs)
			frappe.get_doc("User", name).add_roles("Sales User")
			return name

		with mock.patch.object(device_user, "_create", widened), self.assertRaises(DevicePermissionError):
			sign_in(self.cashier, PASSWORD, device_id)
		self.assertFalse(frappe.db.exists("User", device_user.device_user_name(device_id)))
		# an existing device user an administrator widened is refused too
		device_id2, answer = self.pair()
		frappe.get_doc("User", answer["device_user"]).add_roles("Sales User")
		with self.assertRaises(DevicePermissionError):
			sign_in(self.cashier, PASSWORD, device_id2)

	def test_a_device_user_has_no_password_and_no_reset(self):
		from crenya_pos_api.api import auth

		device_id, answer = self.pair()
		name = answer["device_user"]
		doc = frappe.get_doc("User", name)
		doc.new_password = "Some-Pass-2026!"
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			auth.reset_password(name)
		# even a password set around the document (Frappe's update_password) opens no session
		from frappe.utils.password import update_password

		update_password(name, "Some-Pass-2026!")
		with self.assertRaises(frappe.AuthenticationError):
			sign_in(name, "Some-Pass-2026!")
		with self.assertRaises(frappe.AuthenticationError):
			device_user.refuse_login(types.SimpleNamespace(user=name))
		device_user.refuse_login(types.SimpleNamespace(user=self.cashier))

	def test_a_disabled_device_users_key_is_refused(self):
		from frappe.auth import validate_api_key_secret

		device_id, answer = self.pair()
		self.register(answer["device_user"], device_id)
		login_manager = getattr(frappe.local, "login_manager", None)
		frappe.local.login_manager = types.SimpleNamespace(user="")
		try:
			validate_api_key_secret(answer["api_key"], answer["api_secret"])
			self.assertEqual(frappe.session.user, answer["device_user"])
			frappe.set_user("Administrator")
			device = frappe.get_doc(DEVICE_DOCTYPE, device_id)
			device.enabled = 0
			device.save(ignore_permissions=True)
			frappe.local.login_manager = types.SimpleNamespace(user="")
			with self.assertRaises(frappe.AuthenticationError):
				validate_api_key_secret(answer["api_key"], answer["api_secret"])
		finally:
			frappe.local.login_manager = login_manager
			frappe.set_user("Administrator")
