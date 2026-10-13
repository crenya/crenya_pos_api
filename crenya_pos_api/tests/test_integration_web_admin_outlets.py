"""Shops web admin: outlets, devices and downloads (crenya_pos_api/api/web_admin/outlets.py).

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_web_admin_outlets
"""

import uuid
from unittest.mock import patch

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import web_admin
from crenya_pos_api.api.web_admin import scope
from crenya_pos_api.sync import central, entitlement
from crenya_pos_api.sync.context import DEVICE_DOCTYPE
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.web_admin_case import CASHIER, MANAGER, SHOP2, SHOP2_MANAGER, WebAdminCase

RESTAURANT = "_Test Crenya Restaurant Outlet"


def names(answer):
	return [row["name"] for row in answer["outlets"]]


class TestWebAdminOutlets(WebAdminCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# crenya_restaurant's field, as a site with the restaurant app has it, and one restaurant
		create_custom_fields(
			{
				"POS Profile": [
					{
						"fieldname": scope.RESTAURANT_MODE_FIELD,
						"fieldtype": "Check",
						"label": "Restaurant mode",
						"insert_after": "company",
					}
				]
			},
			ignore_validate=True,
		)
		company = frappe.get_doc("Company", fixtures.COMPANY)
		profile = fixtures.ensure_pos_profile(company, cls.profile.customer, name=RESTAURANT)
		frappe.db.set_value("POS Profile", profile.name, scope.RESTAURANT_MODE_FIELD, 1)
		frappe.db.commit()

	def test_outlets_excludes_restaurant_profiles(self):
		listed = names(web_admin.outlets())
		self.assertIn(self.profile.name, listed)
		self.assertIn(SHOP2, listed)
		self.assertNotIn(RESTAURANT, listed)
		row = next(row for row in web_admin.outlets()["outlets"] if row["name"] == self.profile.name)
		self.assertEqual(row["company"], fixtures.COMPANY)
		self.assertEqual(row["currency"], fixtures.CURRENCY)
		self.assertTrue(row["can_manage"])
		with self.assertRaises(frappe.PermissionError):
			web_admin.devices(RESTAURANT)

	def test_cashier_sees_no_outlets(self):
		self.as_user(CASHIER)
		self.assertEqual(web_admin.outlets(), {"outlets": []})
		with self.assertRaises(frappe.PermissionError):
			web_admin.devices(self.profile.name)
		with self.assertRaises(frappe.PermissionError):
			web_admin.downloads()

	def test_manager_sees_the_shops_listing_them(self):
		# the test profile lists nobody (every user works there); shop 2 lists its own manager
		self.as_user(MANAGER)
		listed = names(web_admin.outlets())
		self.assertIn(self.profile.name, listed)
		self.assertNotIn(SHOP2, listed)
		with self.assertRaises(frappe.PermissionError):
			web_admin.devices(SHOP2)
		self.as_user(SHOP2_MANAGER)
		self.assertIn(SHOP2, names(web_admin.outlets()))

	def test_downloads_reads_retail_manifest(self):
		answer = {"version": "0.9.0", "installers": []}
		with patch.object(central, "downloads", return_value=answer) as downloads:
			self.as_user(MANAGER)
			self.assertEqual(web_admin.downloads(), answer)
		downloads.assert_called_once_with(central.RETAIL)

	def test_devices_lists_the_outlets_tills(self):
		device_id = str(uuid.uuid4())
		device_api.register_device(
			device_id=device_id, device_name="Shop till", pos_profile=self.profile.name
		)
		frappe.db.commit()
		self.as_user(MANAGER)
		answer = web_admin.devices(self.profile.name)
		self.assertEqual(answer["outlet"], self.profile.name)
		self.assertTrue(answer["can_manage"])
		row = next(row for row in answer["rows"] if row["name"] == device_id)
		self.assertEqual(row["device_type"], "till")
		self.assertTrue(row["counts_for_plan"])
		self.assertTrue(row["modified"])
		self.assertEqual([c["value"] for c in answer["choices"]["device_types"]], ["till"])

	def test_set_device_enabled_respects_plan_cap(self):
		device_id = str(uuid.uuid4())
		device_api.register_device(
			device_id=device_id, device_name="Spare till", pos_profile=self.profile.name
		)
		# not seen for a long time and no open shift: it may be switched off
		frappe.db.set_value(
			DEVICE_DOCTYPE, device_id, "last_seen", "2020-01-01 00:00:00", update_modified=False
		)
		frappe.db.commit()

		def modified():
			rows = web_admin.devices(self.profile.name)["rows"]
			return next(row for row in rows if row["name"] == device_id)["modified"]

		self.as_user(MANAGER)
		answer = web_admin.set_device_enabled(self.profile.name, device_id, False, modified())
		self.assertEqual(next(r for r in answer["rows"] if r["name"] == device_id)["enabled"], 0)
		self.assertTrue(
			frappe.get_all(
				"Comment", filters={"reference_name": device_id, "content": ["like", "%app.crenya.com%"]}
			)
		)

		# every terminal slot is taken: reactivating is refused in the cap's words
		used = sum(
			1
			for row in entitlement.device_rows()
			if row.enabled and (row.get("device_type") or "till") == "till"
		)
		full = {
			"status": "Active",
			"paid_until": None,
			"max_outlets": 0,
			"max_terminals": used,
			"addons": {"pos": 1, "restaurant": 1},
		}
		with patch.object(entitlement, "get_entitlement", return_value=full):
			with self.assertRaises(frappe.PermissionError) as refused:
				web_admin.set_device_enabled(self.profile.name, device_id, True, modified())
			self.assertIn(str(used), str(refused.exception))
		self.assertEqual(frappe.db.get_value(DEVICE_DOCTYPE, device_id, "enabled"), 0)

		# within the cap it comes back
		answer = web_admin.set_device_enabled(self.profile.name, device_id, True, modified())
		self.assertEqual(next(r for r in answer["rows"] if r["name"] == device_id)["enabled"], 1)

	def test_device_in_use_is_not_deactivated(self):
		device_id = str(uuid.uuid4())
		device_api.register_device(
			device_id=device_id, device_name="Busy till", pos_profile=self.profile.name
		)
		frappe.db.set_value(DEVICE_DOCTYPE, device_id, "last_seen", frappe.utils.now(), update_modified=False)
		frappe.db.commit()
		row = next(r for r in web_admin.devices(self.profile.name)["rows"] if r["name"] == device_id)
		self.assertTrue(row["in_use"])
		with self.assertRaises(frappe.ValidationError):
			web_admin.set_device_enabled(self.profile.name, device_id, False, row["modified"])

	def test_device_of_another_outlet_refused(self):
		device_id = str(uuid.uuid4())
		device_api.register_device(device_id=device_id, device_name="Other till", pos_profile=SHOP2)
		frappe.db.commit()
		modified = str(frappe.db.get_value(DEVICE_DOCTYPE, device_id, "modified"))
		with self.assertRaises(frappe.PermissionError):
			web_admin.set_device_enabled(self.profile.name, device_id, False, modified)
