"""Shops web admin: renaming a till and setting its update channel (0.17.0,
crenya_pos_api/api/web_admin/outlets.py). A web rename survives the till registering again; the
invoice prefix (`device_short`) never changes; the channel is a System Manager's.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_web_admin_devices
"""

import uuid

import frappe

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import web_admin
from crenya_pos_api.sync.context import DEVICE_DOCTYPE
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.web_admin_case import MANAGER, SHOP2, WebAdminCase


class TestWebAdminDevices(WebAdminCase):
	def register(self, name="Till 2", outlet=None, device_id=None):
		device_id = device_id or str(uuid.uuid4())
		device_api.register_device(
			device_id=device_id,
			device_name=name,
			pos_profile=outlet or self.profile.name,
			app_version="0.11.0",
		)
		frappe.db.commit()
		return device_id

	def row(self, device_id, outlet=None):
		answer = web_admin.devices(outlet or self.profile.name)
		return next(row for row in answer["rows"] if row["name"] == device_id)

	def stored(self, device_id):
		return frappe.db.get_value(
			DEVICE_DOCTYPE,
			device_id,
			["device_name", "name_set_on_web", "device_short", "update_channel"],
			as_dict=True,
		)

	def test_rename_sets_name_and_flag(self):
		device_id = self.register()
		self.as_user(MANAGER)
		answer = web_admin.rename_device(
			self.profile.name, device_id, "  Front   till ", self.row(device_id)["modified"]
		)
		row = next(row for row in answer["rows"] if row["name"] == device_id)
		self.assertEqual((row["device_name"], row["name_set_on_web"]), ("Front till", True))
		self.assertEqual(
			(self.stored(device_id).device_name, self.stored(device_id).name_set_on_web), ("Front till", 1)
		)
		self.assertTrue(
			frappe.get_all(
				"Comment",
				filters={"reference_name": device_id, "content": ["like", f"%app.crenya.com by {MANAGER}%"]},
			)
		)

	def test_rename_blank_or_65_chars_refused(self):
		device_id = self.register()
		self.as_user(MANAGER)
		modified = self.row(device_id)["modified"]
		for bad in ("", "   ", "x" * 65, "bad\x07name"):
			with self.assertRaises(frappe.ValidationError, msg=repr(bad)):
				web_admin.rename_device(self.profile.name, device_id, bad, modified)
		# 64 is fine
		web_admin.rename_device(self.profile.name, device_id, "y" * 64, modified)
		self.assertEqual(self.stored(device_id).device_name, "y" * 64)

	def test_rename_other_outlet_device_refused(self):
		device_id = self.register(outlet=SHOP2)
		modified = self.row(device_id, SHOP2)["modified"]
		self.as_user(MANAGER)
		with self.assertRaises(frappe.PermissionError):
			web_admin.rename_device(self.profile.name, device_id, "Mine now", modified)
		frappe.set_user("Administrator")
		self.assertEqual(self.stored(device_id).device_name, "Till 2")

	def test_rename_stale_modified_refused(self):
		device_id = self.register()
		self.as_user(MANAGER)
		modified = self.row(device_id)["modified"]
		web_admin.rename_device(self.profile.name, device_id, "First", modified)
		with self.assertRaises(frappe.TimestampMismatchError):
			web_admin.rename_device(self.profile.name, device_id, "Second", modified)
		self.assertEqual(self.stored(device_id).device_name, "First")

	def test_web_name_kept_on_reregister(self):
		device_id = self.register("Till 2")
		self.as_user(MANAGER)
		web_admin.rename_device(self.profile.name, device_id, "Front till", self.row(device_id)["modified"])
		frappe.set_user("Administrator")
		self.register("Till 2", device_id=device_id)
		self.assertEqual(self.stored(device_id).device_name, "Front till")
		# a till never renamed on the web still takes its own name
		other = self.register("Till 3")
		self.register("Till 3b", device_id=other)
		self.assertEqual(self.stored(other).device_name, "Till 3b")

	def test_device_short_unchanged(self):
		device_id = self.register()
		short = self.stored(device_id).device_short
		self.as_user(MANAGER)
		web_admin.rename_device(self.profile.name, device_id, "Front till", self.row(device_id)["modified"])
		frappe.set_user("Administrator")
		self.register("Till 2", device_id=device_id)
		self.assertEqual(self.stored(device_id).device_short, short)
		self.assertEqual(self.row(device_id)["device_short"], short)

	def test_channel_system_manager_only(self):
		device_id = self.register()
		self.as_user(MANAGER)
		answer = web_admin.devices(self.profile.name)
		self.assertIs(answer["can_set_channel"], False)
		with self.assertRaises(frappe.PermissionError):
			web_admin.set_device_channel(
				self.profile.name, device_id, "beta", self.row(device_id)["modified"]
			)
		frappe.set_user("Administrator")
		self.assertEqual(self.stored(device_id).update_channel, "stable")
		self.assertIs(web_admin.devices(self.profile.name)["can_set_channel"], True)
		answer = web_admin.set_device_channel(
			self.profile.name, device_id, "beta", self.row(device_id)["modified"]
		)
		self.assertEqual(next(r for r in answer["rows"] if r["name"] == device_id)["update_channel"], "beta")
		self.assertEqual([c["value"] for c in answer["choices"]["update_channels"]], ["stable", "beta"])

	def test_channel_value_must_be_stable_or_beta(self):
		device_id = self.register()
		for bad in ("alpha", "Beta", ""):
			with self.assertRaises(frappe.ValidationError, msg=repr(bad)):
				web_admin.set_device_channel(
					self.profile.name, device_id, bad, self.row(device_id)["modified"]
				)
		self.assertEqual(self.stored(device_id).update_channel, "stable")

	def test_bootstrap_carries_device_name(self):
		device_id = self.register("Till 2")
		self.assertEqual(device_api.get_bootstrap(device_id=device_id)["device"]["device_name"], "Till 2")
		web_admin.rename_device(self.profile.name, device_id, "Front till", self.row(device_id)["modified"])
		frappe.db.commit()
		self.assertEqual(device_api.get_bootstrap(device_id=device_id)["device"]["device_name"], "Front till")

	def test_new_methods_refuse_api_key_session(self):
		device_id = self.register()
		modified = self.row(device_id)["modified"]
		for user in (MANAGER, "Administrator"):
			frappe.set_user(user)
			with fixtures.signed_with_key():
				with self.assertRaises(frappe.PermissionError):
					web_admin.rename_device(self.profile.name, device_id, "Key till", modified)
				with self.assertRaises(frappe.PermissionError):
					web_admin.set_device_channel(self.profile.name, device_id, "beta", modified)
		frappe.set_user("Administrator")
		self.assertEqual(
			(self.stored(device_id).device_name, self.stored(device_id).update_channel), ("Till 2", "stable")
		)

	def test_flat_web_admin_paths_resolve(self):
		"""app.crenya.com calls `crenya_pos_api.api.web_admin.<method>`: the new methods are there, in
		`__all__` (the every-method API-key loop covers them) and whitelisted."""
		for name in ("rename_device", "set_device_channel", "post_cash_difference"):
			self.assertIn(name, web_admin.__all__)
			function = frappe.get_attr(f"crenya_pos_api.api.web_admin.{name}")
			self.assertIn(function, frappe.whitelisted, name)
		self.assertIn("can_set_channel", web_admin.devices(self.profile.name))
