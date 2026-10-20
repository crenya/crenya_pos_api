"""Pull traps (audit P0 #4): a till is never sent an item or a price it cannot sell. A variant
template or a serial-numbered item, and an Item Price for one customer, supplier or batch, are left
out of the pull; one a till already holds comes back as a tombstone, so the till drops it.

The customer's House Account facility on the pull (0.14.0, restaurant D6, R23): `credit_enabled`
for a customer-flag device only, under its own cursor scope.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_pull
"""

import unittest
import uuid

import frappe

from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.sync.credit import CREDIT_CHANGED_AT_FIELD, CREDIT_ENABLED_FIELD
from crenya_pos_api.sync.cursor import Cursor, decode_cursor, encode_cursor
from crenya_pos_api.sync.pull import CREDIT_FLAG_SCOPE, CREDIT_SCOPE, ItemPriceSpec, ItemSpec
from crenya_pos_api.tests import extension_dummy as dummy
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import SyncCase
from crenya_pos_api.tests.test_integration_credit import CreditCase, make_customer
from crenya_pos_api.tests.test_integration_credit_facility import (
	FLAG_DEVICE,
	flag_hooks,
	register,
	save_customer,
)
from crenya_pos_api.utils.dates import format_db_datetime

# a retail till's customer record in 0.13.2 (credit on)
TILL_CUSTOMER_KEYS = [
	"credit_limit",
	"crenya_local_id",
	"customer_group",
	"customer_name",
	"disabled",
	"email_id",
	"mobile_no",
	"modified",
	"name",
	"tax_id",
]


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

	def test_batches_of_serial_items_not_pulled(self):
		from frappe.utils import add_days, nowdate

		code = f"_TC-SERBATCH-{uuid.uuid4().hex[:8]}"
		fixtures.ensure_item(code, 1.0, has_batch_no=1, create_new_batch=0)
		batch = f"{code}-B1"
		fixtures.ensure_batch(batch, code, add_days(nowdate(), 365))
		frappe.db.commit()
		records, _tombs, _cursor = self.pull_all("batch")
		self.assertIn(batch, [r["name"] for r in records])

		frappe.db.set_value("Item", code, "has_serial_no", 1)
		frappe.db.commit()
		records, _tombs, _cursor = self.pull_all("batch")
		self.assertNotIn(batch, [r["name"] for r in records])

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


class TestCustomerFlagPull(CreditCase):
	def pull(self, device_id, cursor=None):
		records, cursor, _pages = self._pull_all("customer", limit=500, cursor=cursor, device_id=device_id)
		return {row["name"]: row for row in records}, cursor

	def customers(self):
		enabled = make_customer("flag pull on")
		save_customer(enabled, **{CREDIT_ENABLED_FIELD: 1})
		plain = make_customer("flag pull off")
		gone = make_customer("flag pull disabled")
		save_customer(gone, **{CREDIT_ENABLED_FIELD: 1})
		save_customer(gone, disabled=1)
		return enabled, plain, gone

	def test_pull_carries_credit_enabled_for_flag_device_only(self):
		enabled, plain, gone = self.customers()
		restaurant = register(self.profile.name, FLAG_DEVICE)
		kds = register(self.profile.name, "kds")
		hooks = {
			"crenya_pos_credit_device_types": [FLAG_DEVICE],
			# kds is listed as a flag type but takes no credit: it gets nothing
			"crenya_pos_credit_customer_flag_device_types": [FLAG_DEVICE, "kds"],
		}
		with dummy.fake_hooks(hooks):
			flagged, _cursor = self.pull(restaurant)
			kitchen, _cursor = self.pull(kds)
			till, _cursor = self.pull(self.device_id)
		self.assertEqual(
			[flagged[name]["credit_enabled"] for name in (enabled, plain, gone)], [True, False, False]
		)
		self.assertEqual(flagged[gone]["disabled"], 1)
		self.assertIn("credit_limit", flagged[enabled])
		self.assertNotIn("credit_enabled", kitchen[enabled])
		self.assertNotIn("credit_limit", kitchen[enabled])
		self.assertNotIn("credit_enabled", till[enabled])

	def test_disabled_customer_reads_not_enabled(self):
		_enabled, _plain, gone = self.customers()
		restaurant = register(self.profile.name, FLAG_DEVICE)
		with flag_hooks():
			records, _cursor = self.pull(restaurant)
		self.assertEqual(frappe.db.get_value("Customer", gone, CREDIT_ENABLED_FIELD), 1)
		self.assertIs(records[gone]["credit_enabled"], False)

	def test_retail_till_pull_has_no_credit_enabled(self):
		enabled, _plain, _gone = self.customers()
		# even a hook that lists "till" never gives a retail till the flag
		hooks = {
			"crenya_pos_credit_device_types": [FLAG_DEVICE],
			"crenya_pos_credit_customer_flag_device_types": [FLAG_DEVICE, "till"],
		}
		with dummy.fake_hooks(hooks):
			records, cursor = self.pull(self.device_id)
		self.assertEqual(sorted(records[enabled]), TILL_CUSTOMER_KEYS)
		self.assertEqual(decode_cursor(cursor, "customer").scope, CREDIT_SCOPE)

	def test_flag_change_resends_customer_on_next_pull(self):
		customer = make_customer("flag pull change")
		restaurant = register(self.profile.name, FLAG_DEVICE)
		with flag_hooks():
			first, cursor = self.pull(restaurant)
			self.assertIs(first[customer]["credit_enabled"], False)
			save_customer(customer, **{CREDIT_ENABLED_FIELD: 1})
			changed, cursor = self.pull(restaurant, cursor)
			self.assertIs(changed[customer]["credit_enabled"], True)
			# the record's modified is the facility's changed_at: what the till echoes back (R5)
			self.assertEqual(
				changed[customer]["modified"],
				format_db_datetime(frappe.db.get_value("Customer", customer, CREDIT_CHANGED_AT_FIELD)),
			)
			save_customer(customer, disabled=1)
			disabled, cursor = self.pull(restaurant, cursor)
			self.assertIs(disabled[customer]["credit_enabled"], False)
			after, _cursor = self.pull(restaurant, cursor)
		self.assertNotIn(customer, after)

	def test_flag_device_cursor_scope_resends_once(self):
		"""R23: a restaurant till whose cursor sat at the credit scope (crenya_restaurant before it
		hooked the flag) is sent every customer once with `credit_enabled`; a retail till is not."""
		enabled = make_customer("flag pull scope")
		save_customer(enabled, **{CREDIT_ENABLED_FIELD: 1})
		restaurant = register(self.profile.name, FLAG_DEVICE)
		with dummy.fake_hooks({"crenya_pos_credit_device_types": [FLAG_DEVICE]}):
			before, cursor = self.pull(restaurant)
			_till, till_cursor = self.pull(self.device_id)
		self.assertNotIn("credit_enabled", before[enabled])
		self.assertEqual(decode_cursor(cursor, "customer").scope, CREDIT_SCOPE)
		with flag_hooks():
			again, cursor = self.pull(restaurant, cursor)
			self.assertEqual(decode_cursor(cursor, "customer").scope, CREDIT_FLAG_SCOPE)
			self.assertEqual(set(again), set(before))
			self.assertTrue(all("credit_enabled" in record for record in again.values()))
			self.assertIs(again[enabled]["credit_enabled"], True)
			# once: the next pull has nothing new
			settled, _cursor = self.pull(restaurant, cursor)
			self.assertEqual(settled, {})
			# a retail till's cursor keeps its scope and is not restarted
			till_again, _cursor = self.pull(self.device_id, till_cursor)
		self.assertEqual(till_again, {})
