"""Shops web admin: promotions (Pricing Rules the till evaluates) and loyalty programs
(crenya_pos_api/api/web_admin/promotions.py and loyalty.py).

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_web_admin_promotions
Rules are titled _Test Crenya Web … and removed after each test; programs persist.
"""

import uuid

import frappe

from crenya_pos_api.api import web_admin
from crenya_pos_api.sync.approvals import APPROVER_ROLE
from crenya_pos_api.sync.context import POS_USER_ROLE
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import SyncCase
from crenya_pos_api.tests.web_admin_case import CASHIER, MANAGER, ensure_user

TITLE = "_Test Crenya Web"
ACCOUNTS = "crenya.shop.accounts@example.com"


def promotion(**over):
	row = {
		"title": f"{TITLE} 10% chips",
		"enabled": True,
		"apply_on": "item_code",
		"targets": [fixtures.CHIPS],
		"kind": "discount_percentage",
		"value": "10",
	}
	row.update(over)
	return row


def row_titled(answer, title):
	return next(row for row in answer["rows"] if row["title"] == title)


class TestWebAdminPromotions(SyncCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		fixtures.setup_promotion_fixtures()
		ensure_user(MANAGER, [POS_USER_ROLE, APPROVER_ROLE])
		ensure_user(CASHIER, [POS_USER_ROLE])
		frappe.db.commit()

	def setUp(self):
		self.addCleanup(self._delete_rules)
		frappe.set_user(MANAGER)

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def _delete_rules(self):
		frappe.set_user("Administrator")
		for name in frappe.get_all("Pricing Rule", filters={"title": ["like", f"{TITLE}%"]}, pluck="name"):
			frappe.delete_doc("Pricing Rule", name, ignore_permissions=True, force=True)
		frappe.db.commit()

	def test_rule_round_trips_to_till_pull(self):
		answer = web_admin.save_promotion(self.profile.name, promotion(priority=2, min_qty="2"))
		row = row_titled(answer, f"{TITLE} 10% chips")
		self.assertEqual(row["value"], "10")
		self.assertEqual(row["targets"], [fixtures.CHIPS])
		self.assertTrue(row["can_edit"])
		self.assertEqual(answer["currency"]["code"], fixtures.CURRENCY)
		self.assertIn(fixtures.ITEM_GROUP_PROMO, answer["choices"]["item_groups"])
		self.assertIn(fixtures.BRAND, answer["choices"]["brands"])

		frappe.set_user("Administrator")
		records, _cursor, _pages = self._pull_all("pricing_rule", limit=500)
		pulled = next(r for r in records if r["name"] == row["name"])
		self.assertEqual(pulled["disabled"], 0)
		self.assertEqual(pulled["apply_on"], "Item Code")
		self.assertEqual(pulled["items"], [{"item_code": fixtures.CHIPS, "uom": None}])
		self.assertEqual(pulled["price_or_product_discount"], "Price")
		self.assertEqual(pulled["rate_or_discount"], "Discount Percentage")
		self.assertEqual(pulled["discount_percentage"], "10")
		self.assertEqual(pulled["min_qty"], "2")
		self.assertEqual(pulled["priority"], 2)
		self.assertEqual(pulled["for_price_list"], fixtures.PRICE_LIST)

	def test_buy_two_get_one_saved_and_pulled(self):
		data = promotion(title=f"{TITLE} 2+1 juice", targets=[fixtures.JUICE], kind="free_item")
		del data["value"]
		data.update(same_item=True, free_qty="1", min_qty="2", recursive=True)
		row = row_titled(web_admin.save_promotion(self.profile.name, data), f"{TITLE} 2+1 juice")
		self.assertEqual(
			(row["kind"], row["free_qty"], row["recursive"], row["same_item"]), ("free_item", "1", True, True)
		)
		frappe.set_user("Administrator")
		records, _cursor, _pages = self._pull_all("pricing_rule", limit=500)
		pulled = next(r for r in records if r["name"] == row["name"])
		self.assertEqual((pulled["disabled"], pulled["price_or_product_discount"]), (0, "Product"))
		self.assertEqual((pulled["same_item"], pulled["is_recursive"], pulled["recurse_for"]), (1, 1, "2"))

	def test_change_disable_and_stale_refused(self):
		row = row_titled(web_admin.save_promotion(self.profile.name, promotion()), f"{TITLE} 10% chips")
		changed = web_admin.save_promotion(
			self.profile.name,
			promotion(name=row["name"], modified=row["modified"], kind="discount_amount", value="0.250"),
		)
		row = row_titled(changed, f"{TITLE} 10% chips")
		self.assertEqual((row["kind"], row["value"]), ("discount_amount", "0.250"))
		off = web_admin.set_promotion_enabled(self.profile.name, row["name"], False, row["modified"])
		self.assertFalse(row_titled(off, f"{TITLE} 10% chips")["enabled"])
		with self.assertRaises(frappe.TimestampMismatchError):
			web_admin.set_promotion_enabled(self.profile.name, row["name"], True, row["modified"])

	def test_shop_without_price_list_refused_for_a_manager(self):
		frappe.set_user("Administrator")
		saved = frappe.db.get_value("POS Profile", self.profile.name, "selling_price_list")
		frappe.db.set_value(
			"POS Profile", self.profile.name, "selling_price_list", None, update_modified=False
		)
		frappe.clear_document_cache("POS Profile", self.profile.name)
		frappe.db.commit()

		def restore():
			frappe.db.set_value(
				"POS Profile", self.profile.name, "selling_price_list", saved, update_modified=False
			)
			frappe.clear_document_cache("POS Profile", self.profile.name)
			frappe.db.commit()

		self.addCleanup(restore)
		frappe.set_user(MANAGER)
		with self.assertRaises(frappe.PermissionError):
			web_admin.save_promotion(self.profile.name, promotion())
		self.assertFalse(frappe.get_all("Pricing Rule", filters={"title": ["like", f"{TITLE}%"]}))

	def test_every_price_list_promotion_refused_for_a_manager(self):
		frappe.set_user("Administrator")
		rule = frappe.get_doc(
			{
				"doctype": "Pricing Rule",
				"title": f"{TITLE} every list",
				"apply_on": "Item Code",
				"items": [{"item_code": fixtures.CHIPS}],
				"selling": 1,
				"company": fixtures.COMPANY,
				"currency": fixtures.CURRENCY,
				"price_or_product_discount": "Price",
				"rate_or_discount": "Discount Percentage",
				"discount_percentage": 5,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		frappe.set_user(MANAGER)
		row = row_titled(web_admin.promotions(self.profile.name), f"{TITLE} every list")
		self.assertFalse(row["can_edit"])
		with self.assertRaises(frappe.PermissionError):
			web_admin.set_promotion_enabled(self.profile.name, rule.name, False, row["modified"])
		with self.assertRaises(frappe.PermissionError):
			web_admin.save_promotion(
				self.profile.name,
				promotion(title=f"{TITLE} every list", name=rule.name, modified=row["modified"]),
			)
		frappe.set_user("Administrator")
		self.assertTrue(
			row_titled(web_admin.promotions(self.profile.name), f"{TITLE} every list")["can_edit"]
		)

	def test_coupon_refused(self):
		with self.assertRaises(frappe.ValidationError):
			web_admin.save_promotion(self.profile.name, promotion(coupon_code_based=1))
		with self.assertRaises(frappe.ValidationError):
			web_admin.save_promotion(self.profile.name, promotion(value="-5"))

	def test_other_company_rule_invisible(self):
		other = frappe.db.get_value(
			"Company", {"name": ["!=", fixtures.COMPANY]}, ["name", "default_currency"], as_dict=True
		)
		if not other:
			self.skipTest("the test site has one company")
		frappe.set_user("Administrator")
		rule = frappe.get_doc(
			{
				"doctype": "Pricing Rule",
				"title": f"{TITLE} other company",
				"apply_on": "Item Code",
				"items": [{"item_code": fixtures.CHIPS}],
				"selling": 1,
				"company": other.name,
				"currency": other.default_currency,
				"price_or_product_discount": "Price",
				"rate_or_discount": "Discount Percentage",
				"discount_percentage": 5,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		frappe.set_user(MANAGER)
		answer = web_admin.promotions(self.profile.name)
		self.assertNotIn(rule.name, [row["name"] for row in answer["rows"]])
		with self.assertRaises(frappe.PermissionError):
			web_admin.set_promotion_enabled(self.profile.name, rule.name, False, str(rule.modified))

	def test_cashier_refused(self):
		frappe.set_user(CASHIER)
		with self.assertRaises(frappe.PermissionError):
			web_admin.promotions(self.profile.name)
		with self.assertRaises(frappe.PermissionError):
			web_admin.loyalty_programs(self.profile.name)


class TestWebAdminLoyalty(SyncCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(MANAGER, [POS_USER_ROLE, APPROVER_ROLE])
		ensure_user(ACCOUNTS, ["Accounts Manager"])
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def program(self, **over):
		row = {
			"loyalty_program_name": f"_Test Crenya Web Rewards {uuid.uuid4().hex[:6]}",
			"loyalty_program_type": "Multiple Tier Program",
			"from_date": "2026-01-01",
			"auto_opt_in": False,
			"conversion_factor": "0.010",
			"expiry_duration": 365,
			"expense_account": fixtures.LOYALTY_ACCOUNT,
			"cost_center": fixtures.COST_CENTER,
			"collection_rules": [
				{"tier_name": "Silver", "min_spent": "0", "collection_factor": "1"},
				{"tier_name": "Gold", "min_spent": "100.000", "collection_factor": "0.5"},
			],
		}
		row.update(over)
		return row

	def test_manager_reads_programs_but_never_writes(self):
		frappe.set_user(MANAGER)
		answer = web_admin.loyalty_programs(self.profile.name)
		self.assertFalse(answer["can_edit"])
		existing = next(row for row in answer["rows"] if row["name"] == fixtures.LOYALTY_PROGRAM)
		self.assertTrue(existing["active"])
		self.assertEqual(existing["collection_rules"][0]["min_spent"], "0.000")
		self.assertIn("Multiple Tier Program", answer["choices"]["program_types"])
		self.assertIn(fixtures.LOYALTY_ACCOUNT, answer["choices"]["expense_accounts"])
		data = self.program()
		with self.assertRaises(frappe.PermissionError):
			web_admin.save_loyalty_program(self.profile.name, data)
		self.assertFalse(frappe.db.exists("Loyalty Program", data["loyalty_program_name"]))

	def test_accounts_manager_creates_and_changes_programs(self):
		frappe.set_user(ACCOUNTS)
		answer = web_admin.loyalty_programs(self.profile.name)
		self.assertTrue(answer["can_edit"])
		data = self.program()
		answer = web_admin.save_loyalty_program(self.profile.name, data)
		saved = next(row for row in answer["rows"] if row["name"] == data["loyalty_program_name"])
		self.assertEqual([t["tier_name"] for t in saved["collection_rules"]], ["Silver", "Gold"])
		self.assertEqual(saved["collection_rules"][1]["min_spent"], "100.000")
		self.assertEqual(frappe.db.get_value("Loyalty Program", saved["name"], "company"), fixtures.COMPANY)

		changed = web_admin.save_loyalty_program(
			self.profile.name,
			{**data, "name": saved["name"], "modified": saved["modified"], "to_date": "2026-12-31"},
		)
		self.assertEqual(
			next(row for row in changed["rows"] if row["name"] == saved["name"])["to_date"], "2026-12-31"
		)
		with self.assertRaises(frappe.TimestampMismatchError):
			web_admin.save_loyalty_program(
				self.profile.name, {**data, "name": saved["name"], "modified": saved["modified"]}
			)
		# an Accounts Manager reads no outlet that is not a shop
		with self.assertRaises(frappe.PermissionError):
			web_admin.loyalty_programs("_Test Crenya No Such Outlet")

	def test_links_of_another_company_or_groups_refused(self):
		frappe.set_user(ACCOUNTS)
		group = f"Indirect Expenses - {fixtures.ABBR}"
		with self.assertRaises(frappe.ValidationError):
			web_admin.save_loyalty_program(self.profile.name, self.program(expense_account=group))
		with self.assertRaises(frappe.ValidationError):
			web_admin.save_loyalty_program(
				self.profile.name, self.program(cost_center="_Test No Such Cost Center")
			)
		with self.assertRaises(frappe.ValidationError):
			web_admin.save_loyalty_program(
				self.profile.name, self.program(loyalty_program_name=fixtures.LOYALTY_PROGRAM)
			)
