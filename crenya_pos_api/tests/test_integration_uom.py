"""Pull entity `uom` against a real ERPNext site: whole-number flags, paging and tombstones.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_uom
"""

import uuid

import frappe

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.tests import fixtures

WHOLE = "_Test Crenya Whole Unit"
FRACTION = "_Test Crenya Fraction Unit"


def ensure_uom(name, whole):
	if frappe.db.exists("UOM", name):
		frappe.db.set_value("UOM", name, "must_be_whole_number", whole)
	else:
		frappe.get_doc({"doctype": "UOM", "uom_name": name, "must_be_whole_number": whole}).insert(
			ignore_permissions=True
		)


class TestCrenyaUom(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls._saved_lag = frappe.conf.get("crenya_pos_pull_lag_seconds")
		frappe.conf.crenya_pos_pull_lag_seconds = 0
		cls.profile = fixtures.setup_fixtures()
		ensure_uom(WHOLE, 1)
		ensure_uom(FRACTION, 0)
		cls.device_id = str(uuid.uuid4())
		device_api.register_device(
			device_id=cls.device_id, device_name="UOM Till", pos_profile=cls.profile.name
		)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		if cls._saved_lag is None:
			frappe.conf.pop("crenya_pos_pull_lag_seconds", None)
		else:
			frappe.conf.crenya_pos_pull_lag_seconds = cls._saved_lag
		super().tearDownClass()

	def pull(self, cursor=None, limit=1000):
		return sync_api.pull_changes(device_id=self.device_id, entity="uom", cursor=cursor, limit=limit)

	def pull_all(self, cursor=None, limit=1000):
		records, tombstones, pages = {}, [], 0
		while True:
			page = self.pull(cursor, limit)
			pages += 1
			self.assertEqual(page["entity"], "uom")
			for record in page["records"]:
				self.assertNotIn(record["name"], records, "a UOM was delivered twice")
				records[record["name"]] = record
			tombstones.extend(page["tombstones"])
			cursor = page["next_cursor"]
			if not page["has_more"]:
				return records, tombstones, cursor, pages
			self.assertLess(pages, 1000, "pull did not terminate")

	def test_capability(self):
		self.assertTrue(sync_api.get_sync_capabilities()["features"]["uom_entity"])

	def test_all_uoms_with_whole_number_flag(self):
		records, _tombstones, _cursor, _pages = self.pull_all()
		self.assertEqual(set(records), set(frappe.get_all("UOM", pluck="name")))
		self.assertIs(records[WHOLE]["must_be_whole_number"], True)
		self.assertIs(records[FRACTION]["must_be_whole_number"], False)
		self.assertEqual(set(records[WHOLE]), {"name", "must_be_whole_number", "modified"})
		for name, record in records.items():
			expected = bool(frappe.db.get_value("UOM", name, "must_be_whole_number"))
			self.assertIs(record["must_be_whole_number"], expected, name)

	def test_keyset_paging(self):
		paged, _tombstones, _cursor, pages = self.pull_all(limit=3)
		whole, _tombstones, _cursor, _pages = self.pull_all()
		self.assertEqual(paged, whole)
		self.assertGreater(pages, 1)

	def test_changes_and_tombstones(self):
		_records, _tombstones, cursor, _pages = self.pull_all()

		temp = f"_Test Crenya Temp Unit {uuid.uuid4().hex[:6]}"
		ensure_uom(temp, 1)
		frappe.db.commit()
		records, _tombstones, cursor, _pages = self.pull_all(cursor)
		self.assertIn(temp, records)
		self.assertTrue(records[temp]["must_be_whole_number"])

		uom = frappe.get_doc("UOM", temp)
		uom.must_be_whole_number = 0
		uom.save(ignore_permissions=True)
		frappe.db.commit()
		records, _tombstones, cursor, _pages = self.pull_all(cursor)
		self.assertEqual(list(records), [temp])
		self.assertFalse(records[temp]["must_be_whole_number"])

		frappe.delete_doc("UOM", temp, force=True, ignore_permissions=True)
		frappe.db.commit()
		records, tombstones, _cursor, _pages = self.pull_all(cursor)
		self.assertIn(temp, tombstones)
		self.assertNotIn(temp, records)
