"""Promotions against a real ERPNext site: the `pricing_rule` and `item_group` feeds and
till invoices carrying promotion names and free lines.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_promotions
Rules and schemes are titled _Test Crenya … and removed again after the class.
"""

import json
import uuid
from decimal import Decimal

import frappe
from frappe.utils import add_days, nowdate, nowtime

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.sync.hashing import payload_hash
from crenya_pos_api.tests import fixtures


def new_id():
	return str(uuid.uuid4())


def money(value):
	return f"{Decimal(value).quantize(Decimal('0.001')):f}"


class TestCrenyaPromotions(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

		cls._saved_precision = frappe.db.get_single_value("System Settings", "currency_precision")
		frappe.db.set_single_value("System Settings", "currency_precision", "3")
		frappe.db.set_default("currency_precision", "3")
		frappe.clear_cache()
		cls._saved_lag = frappe.conf.get("crenya_pos_pull_lag_seconds")
		frappe.conf.crenya_pos_pull_lag_seconds = 0

		cls.profile = fixtures.setup_fixtures()
		fixtures.setup_promotion_fixtures()
		cls.device_id = new_id()
		cls.device = device_api.register_device(
			device_id=cls.device_id, device_name="Promo Till", pos_profile=cls.profile.name
		)
		cls.rules = []
		cls.schemes = []
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for scheme in cls.schemes:
			frappe.delete_doc("Promotional Scheme", scheme, force=True, ignore_missing=True)
		for rule in cls.rules:
			frappe.delete_doc("Pricing Rule", rule, force=True, ignore_missing=True)
		frappe.db.set_single_value("System Settings", "currency_precision", cls._saved_precision or "")
		frappe.db.set_default("currency_precision", cls._saved_precision or "")
		if cls._saved_lag is None:
			frappe.conf.pop("crenya_pos_pull_lag_seconds", None)
		else:
			frappe.conf.crenya_pos_pull_lag_seconds = cls._saved_lag
		frappe.db.commit()
		frappe.clear_cache()
		super().tearDownClass()

	# helpers

	def make_rule(self, **values):
		doc = {
			"doctype": "Pricing Rule",
			"title": f"_Test Crenya {uuid.uuid4().hex[:8]}",
			"apply_on": "Item Code",
			"items": [{"item_code": fixtures.CHIPS}],
			"selling": 1,
			"company": fixtures.COMPANY,
			"currency": fixtures.CURRENCY,
			"price_or_product_discount": "Price",
			"rate_or_discount": "Discount Percentage",
			"discount_percentage": 10,
			"valid_from": add_days(nowdate(), -1),
		}
		doc.update(values)
		rule = frappe.get_doc(doc).insert(ignore_permissions=True)
		self.rules.append(rule.name)
		frappe.db.commit()
		return rule

	def pull_all(self, entity, cursor=None):
		records, pages = {}, 0
		while True:
			page = sync_api.pull_changes(device_id=self.device_id, entity=entity, cursor=cursor, limit=100)
			pages += 1
			for record in page["records"]:
				records[record["name"]] = record
			cursor = page["next_cursor"]
			if not page["has_more"]:
				return records, cursor, page
			self.assertLess(pages, 500, "pull did not terminate")

	def line(self, line_no, item_code, qty, rate, price_list_rate=None, discount="0", **extra):
		values = {
			"line_no": line_no,
			"item_code": item_code,
			"item_name": item_code,
			"qty": qty,
			"uom": "Nos",
			"conversion_factor": "1",
			"price_list_rate": price_list_rate or rate,
			"discount_percentage": discount,
			"rate": rate,
			"amount": money(Decimal(qty) * Decimal(rate)),
			"item_tax_template": None,
			"against_line_no": None,
			"against_row_name": None,
		}
		values.update(extra)
		return values

	def push_sale(self, items):
		total = money(sum(Decimal(item["amount"]) for item in items))
		payload = {
			"local_id": new_id(),
			"offline_number": f"OFF-{self.device['device_short']}-{uuid.uuid4().hex[:6]}",
			"pos_profile": self.profile.name,
			"company": fixtures.COMPANY,
			"customer": self.profile.customer,
			"customer_local_id": None,
			"posting_date": nowdate(),
			"posting_time": nowtime()[:8],
			"currency": fixtures.CURRENCY,
			"is_return": 0,
			"return_against": None,
			"return_against_local_id": None,
			"taxes_and_charges": fixtures.TAX_TEMPLATE,
			"cashier": frappe.session.user,
			"items": items,
			"payments": [{"mode_of_payment": fixtures.CASH, "amount": total}],
			"client_totals": {"grand_total": total, "rounded_total": total},
		}
		event = {
			"event_id": new_id(),
			"aggregate_type": "Sales Invoice",
			"operation": "submit",
			"local_id": payload["local_id"],
			"sequence_no": 1,
			"payload_hash": payload_hash(payload),
			"payload": payload,
		}
		result = sync_api.push_batch(device_id=self.device_id, events=[event])[0]
		self.assertEqual(result["status"], "ok", msg=result.get("error"))
		return result, total

	# feeds

	def test_capabilities_announce_promotions(self):
		self.assertTrue(sync_api.get_sync_capabilities()["features"]["promotions"])

	def test_item_group_feed_and_item_brand(self):
		groups, _cursor, _page = self.pull_all("item_group")
		promo = groups[fixtures.ITEM_GROUP_PROMO]
		root = groups[fixtures.ITEM_GROUP_ROOT]
		self.assertEqual(promo["parent_item_group"], fixtures.ITEM_GROUP_ROOT)
		self.assertIsInstance(promo["lft"], int)
		self.assertTrue(root["lft"] < promo["lft"] < promo["rgt"] < root["rgt"])
		self.assertEqual(set(promo), {"name", "parent_item_group", "lft", "rgt", "modified"})
		# every group is sent, also those outside the profile's item groups
		self.assertIn(fixtures.ITEM_GROUP_OUTSIDE, groups)
		tree_root = [g for g in groups.values() if not g["parent_item_group"]]
		self.assertTrue(tree_root)

		items, _cursor, _page = self.pull_all("item")
		items = {record["item_code"]: record for record in items.values()}
		self.assertEqual(items[fixtures.CHIPS]["brand"], fixtures.BRAND)
		self.assertIsNone(items[fixtures.JUICE]["brand"])

	def test_pricing_rule_feed(self):
		group_pct = self.make_rule(
			apply_on="Item Group",
			items=[],
			item_groups=[{"item_group": fixtures.ITEM_GROUP_PROMO}],
			discount_percentage=10,
		)
		rate = self.make_rule(rate_or_discount="Rate", rate=0.5, discount_percentage=0, priority="2")
		buy2get1 = self.make_rule(
			price_or_product_discount="Product",
			rate_or_discount=None,
			discount_percentage=0,
			items=[{"item_code": fixtures.JUICE}],
			min_qty=2,
			same_item=1,
			free_qty=1,
			is_recursive=1,
			recurse_for=2,
			round_free_qty=1,
		)
		transaction = self.make_rule(
			apply_on="Transaction", items=[], discount_percentage=5, min_amt=10, max_amt=0
		)
		conditional = self.make_rule(condition="doc.grand_total > 10")
		coupon = self.make_rule(coupon_code_based=1)
		territory = self.make_rule(applicable_for="Territory", territory=_root_territory())

		records, _cursor, page = self.pull_all("pricing_rule")
		self.assertEqual(page["entity"], "pricing_rule")

		record = records[group_pct.name]
		self.assertEqual(record["disabled"], 0)
		self.assertEqual(record["apply_on"], "Item Group")
		self.assertEqual(record["item_groups"], [fixtures.ITEM_GROUP_PROMO])
		self.assertEqual((record["items"], record["brands"]), ([], []))
		self.assertEqual(record["rate_or_discount"], "Discount Percentage")
		self.assertEqual(record["discount_percentage"], "10")
		self.assertEqual(record["price_or_product_discount"], "Price")
		self.assertEqual(record["applicable_for"], "")
		self.assertIsNone(record["customer"])
		self.assertEqual(record["valid_from"], add_days(nowdate(), -1))
		self.assertIsNone(record["valid_upto"])
		self.assertEqual(record["min_qty"], "0")
		self.assertEqual(record["min_amt"], "0.000")
		self.assertEqual(record["title"], group_pct.title)
		self.assertTrue(record["modified"])

		record = records[rate.name]
		self.assertEqual((record["disabled"], record["rate_or_discount"]), (0, "Rate"))
		self.assertEqual(record["rate"], "0.500")
		self.assertEqual(record["priority"], 2)
		self.assertEqual(record["items"], [{"item_code": fixtures.CHIPS, "uom": None}])

		record = records[buy2get1.name]
		self.assertEqual((record["disabled"], record["price_or_product_discount"]), (0, "Product"))
		self.assertEqual((record["same_item"], record["is_recursive"], record["round_free_qty"]), (1, 1, 1))
		self.assertEqual((record["min_qty"], record["free_qty"], record["recurse_for"]), ("2", "1", "2"))
		self.assertEqual(record["free_item_rate"], "0.000")
		self.assertIsNone(record["free_item"])

		record = records[transaction.name]
		self.assertEqual((record["disabled"], record["apply_on"]), (0, "Transaction"))
		self.assertEqual((record["min_amt"], record["discount_percentage"]), ("10.000", "5"))

		# rules the till cannot evaluate are sent disabled
		for excluded in (conditional, coupon, territory):
			self.assertEqual(records[excluded.name]["disabled"], 1, excluded.name)
		self.assertNotIn("condition", records[conditional.name])

	def test_rule_that_stops_qualifying_comes_back_disabled(self):
		rule = self.make_rule()
		expiring = self.make_rule()
		changing = self.make_rule()
		records, cursor, _page = self.pull_all("pricing_rule")
		self.assertEqual([records[r.name]["disabled"] for r in (rule, expiring, changing)], [0, 0, 0])

		nothing, cursor, _page = self.pull_all("pricing_rule", cursor)
		self.assertNotIn(rule.name, nothing)

		rule.disable = 1
		rule.save(ignore_permissions=True)
		expiring.valid_from = add_days(nowdate(), -10)
		expiring.valid_upto = add_days(nowdate(), -1)
		expiring.save(ignore_permissions=True)
		changing.coupon_code_based = 1
		changing.save(ignore_permissions=True)
		frappe.db.commit()

		changed, cursor, _page = self.pull_all("pricing_rule", cursor)
		self.assertEqual(changed[rule.name]["disabled"], 1)
		self.assertEqual(changed[expiring.name]["disabled"], 1)
		self.assertEqual(changed[changing.name]["disabled"], 1)

		rule.disable = 0
		rule.save(ignore_permissions=True)
		frappe.db.commit()
		again, cursor, _page = self.pull_all("pricing_rule", cursor)
		self.assertEqual(again[rule.name]["disabled"], 0)

		frappe.delete_doc("Pricing Rule", rule.name, force=True)
		frappe.db.commit()
		page = sync_api.pull_changes(device_id=self.device_id, entity="pricing_rule", cursor=cursor)
		self.assertIn(rule.name, page["tombstones"])

	def test_promotional_scheme_rules_are_sent(self):
		name = f"_Test Crenya Scheme {uuid.uuid4().hex[:8]}"
		scheme = frappe.get_doc(
			{
				"doctype": "Promotional Scheme",
				"apply_on": "Item Code",
				"items": [{"item_code": fixtures.CHIPS}],
				"selling": 1,
				"company": fixtures.COMPANY,
				"currency": fixtures.CURRENCY,
				"valid_from": add_days(nowdate(), -1),
				"price_discount_slabs": [
					{
						"rule_description": "5% from 3",
						"min_qty": 3,
						"rate_or_discount": "Discount Percentage",
						"discount_percentage": 5,
					},
					{
						"rule_description": "10% from 6",
						"min_qty": 6,
						"rate_or_discount": "Discount Percentage",
						"discount_percentage": 10,
					},
				],
			}
		)
		scheme.insert(ignore_permissions=True, set_name=name)
		self.schemes.append(scheme.name)
		frappe.db.commit()

		records, _cursor, _page = self.pull_all("pricing_rule")
		generated = sorted(
			(r for r in records.values() if r["promotional_scheme"] == scheme.name),
			key=lambda r: Decimal(r["min_qty"]),
		)
		self.assertEqual(len(generated), 2)
		self.assertEqual([r["min_qty"] for r in generated], ["3", "6"])
		self.assertEqual([r["discount_percentage"] for r in generated], ["5", "10"])
		self.assertEqual([r["disabled"] for r in generated], [0, 0])
		self.assertEqual(generated[0]["items"], [{"item_code": fixtures.CHIPS, "uom": None}])

	# invoices

	def test_invoice_with_promotions_and_free_line(self):
		pct = self.make_rule(discount_percentage=10)
		buy2get1 = self.make_rule(
			price_or_product_discount="Product",
			rate_or_discount=None,
			discount_percentage=0,
			items=[{"item_code": fixtures.JUICE}],
			min_qty=2,
			same_item=1,
			free_qty=1,
		)
		items = [
			# 10 % off the price list rate of 1.000
			self.line(1, fixtures.CHIPS, "2", "0.900", "1.000", "10", pricing_rules=[pct.name]),
			self.line(2, fixtures.JUICE, "2", "0.600", pricing_rules=[buy2get1.name]),
			self.line(
				3, fixtures.JUICE, "1", "0", "0.600", "0", pricing_rules=[buy2get1.name], is_free_item=1
			),
			# a line without promotion keys (older tills)
			self.line(4, fixtures.JUICE, "1", "0.600"),
		]
		result, total = self.push_sale(items)
		self.assertEqual(result["totals"]["grand_total"], total)
		self.assertEqual(total, "3.600")

		doc = frappe.get_doc("Sales Invoice", result["name"])
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(doc.ignore_pricing_rule, 1)
		self.assertEqual(len(doc.items), 4, "ERPNext must not drop the free line")
		chips, juice, free, plain = doc.items

		self.assertAlmostEqual(chips.rate, 0.9, places=3)
		self.assertAlmostEqual(chips.price_list_rate, 1.0, places=3)
		self.assertAlmostEqual(chips.discount_percentage, 10, places=3)
		self.assertEqual(json.loads(chips.pricing_rules), [pct.name])
		self.assertEqual(chips.is_free_item, 0)

		self.assertEqual(json.loads(juice.pricing_rules), [buy2get1.name])
		self.assertAlmostEqual(juice.rate, 0.6, places=3)

		self.assertEqual(free.is_free_item, 1)
		self.assertEqual(free.rate, 0)
		self.assertEqual(free.amount, 0)
		self.assertEqual(free.qty, 1)
		self.assertEqual(json.loads(free.pricing_rules), [buy2get1.name])

		self.assertFalse(plain.pricing_rules)
		self.assertEqual(plain.is_free_item, 0)
		self.assertAlmostEqual(doc.grand_total, 3.6, places=3)
		self.assertAlmostEqual(doc.outstanding_amount, 0, places=3)


def _root_territory():
	from frappe.utils.nestedset import get_root_of

	return get_root_of("Territory")
