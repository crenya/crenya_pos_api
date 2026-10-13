"""Pull traps (audit P0 #4): a till is never sent an item or a price it cannot sell. A variant
template or a serial-numbered item, and an Item Price for one customer, supplier or batch, are left
out of the pull; one a till already holds comes back as a tombstone, so the till drops it.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_pull
"""

import unittest
import uuid

import frappe

from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.sync.cursor import Cursor, decode_cursor, encode_cursor
from crenya_pos_api.sync.pull import ItemPriceSpec, ItemSpec
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import SyncCase


class TestPullTraps(SyncCase):
	def pull_all(self, entity, cursor=None):
		"""(records, tombstones, next cursor) of every page from `cursor`."""
		records, tombstones = [], []
		while True:
			page = sync_api.pull_changes(device_id=self.device_id, entity=entity, cursor=cursor, limit=500)
			records.extend(page["records"])
			tombstones.extend(page["tombstones"])
			cursor = page["next_cursor"]
			if not page["has_more"]:
				return records, tombstones, cursor

	def new_item(self, prefix):
		code = f"_TC-{prefix}-{uuid.uuid4().hex[:8]}"
		fixtures.ensure_item(code, 1.0, is_stock_item=0)
		frappe.db.commit()
		return code

	def assert_item_dropped(self, field):
		code = self.new_item(field.upper())
		records, _tombs, cursor = self.pull_all("item")
		self.assertIn(code, [r["item_code"] for r in records])

		# the till holds it; it becomes unsellable: a tombstone, never a record
		frappe.db.set_value("Item", code, field, 1)
		frappe.db.commit()
		records, tombstones, _cursor = self.pull_all("item", cursor)
		self.assertNotIn(code, [r["item_code"] for r in records])
		self.assertIn(code, tombstones)

		# a fresh till never gets it as a record
		records, _tombs, _cursor = self.pull_all("item")
		self.assertNotIn(code, [r["item_code"] for r in records])

	def test_template_item_not_pulled(self):
		self.assert_item_dropped("has_variants")

	def test_serial_item_not_pulled(self):
		self.assert_item_dropped("has_serial_no")

	def test_customer_price_not_pulled(self):
		code = self.new_item("CUSTPRICE")
		general = frappe.db.get_value("Item Price", {"item_code": code, "price_list": fixtures.PRICE_LIST})
		special = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": code,
				"price_list": fixtures.PRICE_LIST,
				"price_list_rate": 0.5,
				"currency": fixtures.CURRENCY,
				"customer": self.walk_in,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		records, _tombs, cursor = self.pull_all("item_price")
		names = [r["name"] for r in records]
		self.assertIn(general, names)
		self.assertNotIn(special.name, names)

		# a price the till holds that becomes a customer's: a tombstone
		frappe.db.set_value("Item Price", general, "customer", self.walk_in)
		frappe.db.commit()
		records, tombstones, _cursor = self.pull_all("item_price", cursor)
		self.assertNotIn(general, [r["name"] for r in records])
		self.assertIn(general, tombstones)

	def test_supplier_and_batch_prices_not_pulled(self):
		code = self.new_item("SUPPRICE")
		general = frappe.db.get_value("Item Price", {"item_code": code, "price_list": fixtures.PRICE_LIST})
		_records, _tombs, cursor = self.pull_all("item_price")
		for field, value in (("supplier", "_TC any supplier"), ("batch_no", "_TC any batch")):
			with self.subTest(field=field):
				# set straight in the database: the link is not checked
				frappe.db.set_value("Item Price", general, field, value)
				frappe.db.commit()
				records, tombstones, cursor = self.pull_all("item_price", cursor)
				self.assertNotIn(general, [r["name"] for r in records])
				self.assertIn(general, tombstones)
				frappe.db.set_value("Item Price", general, field, None, update_modified=False)
				frappe.db.commit()

	def test_a_cursor_before_the_traps_restarts_once(self):
		"""A till that pulled before this release holds items it cannot sell: its old (unscoped)
		cursor restarts the records once, so those come back as tombstones; the tombstone
		position is kept."""
		code = self.new_item("OLDTILL")
		frappe.db.set_value("Item", code, "has_variants", 1)
		frappe.db.commit()
		_records, _tombs, cursor = self.pull_all("item")
		current = decode_cursor(cursor, "item")
		old = encode_cursor(
			Cursor(
				entity="item",
				modified=current.modified,
				name=current.name,
				tomb_creation=current.tomb_creation,
				tomb_name=current.tomb_name,
			)
		)
		records, tombstones, again = self.pull_all("item", old)
		self.assertIn(fixtures.MILK, [r["item_code"] for r in records])
		self.assertIn(code, tombstones)
		self.assertEqual(decode_cursor(again, "item").tomb_creation, current.tomb_creation)


class TestSellableRules(unittest.TestCase):
	def test_items(self):
		spec = ItemSpec()
		self.assertFalse(spec.excluded(frappe._dict(has_variants=0, has_serial_no=0)))
		self.assertTrue(spec.excluded(frappe._dict(has_variants=1, has_serial_no=0)))
		self.assertTrue(spec.excluded(frappe._dict(has_variants=0, has_serial_no=1)))
		# a variant itself is sold like any item
		self.assertFalse(spec.excluded(frappe._dict(has_variants=0, has_serial_no=0, variant_of="T")))

	def test_prices(self):
		spec = ItemPriceSpec()
		self.assertFalse(spec.excluded(frappe._dict(customer=None, supplier="", batch_no=None)))
		for field in ("customer", "supplier", "batch_no"):
			self.assertTrue(spec.excluded(frappe._dict({field: "X"})), field)
