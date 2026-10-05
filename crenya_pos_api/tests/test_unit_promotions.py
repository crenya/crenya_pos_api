"""Promotion payload keys and Pricing Rule qualification (no site needed)."""

import copy
import unittest
from datetime import date, datetime

import frappe

from crenya_pos_api.sync.errors import SyncError
from crenya_pos_api.sync.promotions import rule_is_active, rule_is_expired, rule_qualifies
from crenya_pos_api.sync.pull import ItemGroupSpec
from crenya_pos_api.sync.validation import MAX_PRICING_RULES_PER_LINE, validate_invoice_payload
from crenya_pos_api.tests.test_unit_validation import SALE, make_return

COMPANY = "Crenya LLC"
TODAY = date(2026, 10, 1)


def sale_with_item(**item_overrides):
	payload = copy.deepcopy(SALE)
	payload["items"][0].update(item_overrides)
	return payload


def rule(**overrides):
	values = {
		"name": "PRLE-0001",
		"disable": 0,
		"selling": 1,
		"buying": 0,
		"company": COMPANY,
		"currency": "OMR",
		"condition": None,
		"coupon_code_based": 0,
		"applicable_for": "",
		"apply_rule_on_other": "",
		"margin_type": "Percentage",
		"margin_rate_or_amount": 0,
		"apply_on": "Item Code",
		"price_or_product_discount": "Price",
		"validate_applied_rule": 0,
		"valid_from": "2026-09-01",
		"valid_upto": None,
	}
	values.update(overrides)
	return values


class TestPromotionPayload(unittest.TestCase):
	def assertInvalid(self, payload, fragment):
		with self.assertRaises(SyncError) as ctx:
			validate_invoice_payload(payload)
		self.assertEqual(ctx.exception.code, "validation")
		self.assertIn(fragment, ctx.exception.message)

	def test_older_payloads_default_to_no_promotions(self):
		self.assertNotIn("pricing_rules", SALE["items"][0])
		line = validate_invoice_payload(copy.deepcopy(SALE))["items"][0]
		self.assertEqual(line["pricing_rules"], [])
		self.assertEqual(line["is_free_item"], 0)

		line = validate_invoice_payload(sale_with_item(pricing_rules=None, is_free_item=None))["items"][0]
		self.assertEqual((line["pricing_rules"], line["is_free_item"]), ([], 0))

	def test_valid_keys_are_normalized(self):
		line = validate_invoice_payload(
			sale_with_item(pricing_rules=[" PRLE-0001 ", "PRLE-0002", "PRLE-0001"], is_free_item=1)
		)["items"][0]
		self.assertEqual(line["pricing_rules"], ["PRLE-0001", "PRLE-0002"])
		self.assertEqual(line["is_free_item"], 1)

		for flag, expected in ((0, 0), ("1", 1), ("0", 0), (True, 1), (False, 0)):
			with self.subTest(flag=flag):
				line = validate_invoice_payload(sale_with_item(is_free_item=flag))["items"][0]
				self.assertEqual(line["is_free_item"], expected)

	def test_free_line_at_zero_rate(self):
		payload = sale_with_item(
			rate="0", price_list_rate="0.600", amount="0.000", pricing_rules=["PRLE-0003"], is_free_item=1
		)
		line = validate_invoice_payload(payload)["items"][0]
		self.assertEqual(line["rate"], 0)
		self.assertEqual(line["pricing_rules"], ["PRLE-0003"])

	def test_allowed_on_returns(self):
		payload = make_return()
		payload["items"][0].update(pricing_rules=["PRLE-0001"], is_free_item=1)
		line = validate_invoice_payload(payload)["items"][0]
		self.assertEqual((line["pricing_rules"], line["is_free_item"]), (["PRLE-0001"], 1))

	def test_rejections(self):
		cases = [
			({"pricing_rules": "PRLE-0001"}, "items[0].pricing_rules must be a list"),
			({"pricing_rules": {"name": "PRLE-0001"}}, "items[0].pricing_rules must be a list"),
			({"pricing_rules": [""]}, "items[0].pricing_rules[0] must be a non-empty string"),
			({"pricing_rules": ["  "]}, "items[0].pricing_rules[0] must be a non-empty string"),
			({"pricing_rules": ["PRLE-0001", 7]}, "items[0].pricing_rules[1] must be a non-empty string"),
			({"pricing_rules": [None]}, "items[0].pricing_rules[0] must be a non-empty string"),
			({"pricing_rules": ["x" * 141]}, "items[0].pricing_rules[0] is longer than 140 characters"),
			(
				{"pricing_rules": [f"PRLE-{i}" for i in range(MAX_PRICING_RULES_PER_LINE + 1)]},
				f"at most {MAX_PRICING_RULES_PER_LINE} entries",
			),
			({"is_free_item": 2}, "items[0].is_free_item must be 0 or 1"),
			({"is_free_item": "yes"}, "items[0].is_free_item must be 0 or 1"),
		]
		for overrides, fragment in cases:
			with self.subTest(overrides=overrides):
				self.assertInvalid(sale_with_item(**overrides), fragment)

	def test_limits_are_inclusive(self):
		names = [f"PRLE-{i:04d}" for i in range(MAX_PRICING_RULES_PER_LINE)]
		names[0] = "x" * 140
		line = validate_invoice_payload(sale_with_item(pricing_rules=names))["items"][0]
		self.assertEqual(len(line["pricing_rules"]), MAX_PRICING_RULES_PER_LINE)


class TestRuleQualification(unittest.TestCase):
	def test_supported_rules_qualify(self):
		cases = [
			rule(),
			rule(company=None),
			rule(company=""),
			rule(apply_on="Item Group"),
			rule(apply_on="Brand"),
			rule(apply_on="Transaction"),
			rule(price_or_product_discount="Product"),
			rule(applicable_for=None),
			rule(applicable_for="Customer"),
			rule(applicable_for="Customer Group"),
			rule(buying=1),
			rule(condition="   "),
			rule(margin_type="Amount", margin_rate_or_amount=0),
			rule(margin_type="", margin_rate_or_amount=5),
			rule(currency=None),
			rule(mixed_conditions=1, is_cumulative=1),
		]
		for case in cases:
			with self.subTest(case=case):
				self.assertTrue(rule_qualifies(case, COMPANY, "OMR"))

	def test_unsupported_rules_do_not_qualify(self):
		cases = {
			"buying only": rule(selling=0, buying=1),
			"another company": rule(company="Other LLC"),
			"condition": rule(condition="doc.grand_total > 10"),
			"coupon": rule(coupon_code_based=1),
			"territory": rule(applicable_for="Territory"),
			"sales partner": rule(applicable_for="Sales Partner"),
			"campaign": rule(applicable_for="Campaign"),
			"supplier": rule(applicable_for="Supplier"),
			"apply rule on other": rule(apply_rule_on_other="Item Group"),
			"margin percentage": rule(margin_type="Percentage", margin_rate_or_amount=10),
			"margin amount": rule(margin_type="Amount", margin_rate_or_amount="0.5"),
			"unknown apply_on": rule(apply_on="Item Variant"),
			"no discount kind": rule(price_or_product_discount=None),
			"validate applied rule": rule(validate_applied_rule=1),
			"other currency": rule(currency="USD"),
		}
		for label, case in cases.items():
			with self.subTest(label):
				self.assertFalse(rule_qualifies(case, COMPANY, "OMR"))

	def test_currency_is_only_checked_when_known(self):
		self.assertTrue(rule_qualifies(rule(currency="USD"), COMPANY))

	def test_expiry(self):
		self.assertFalse(rule_is_expired(rule(valid_upto=None), TODAY))
		self.assertFalse(rule_is_expired(rule(valid_upto="2026-10-01"), TODAY))
		self.assertFalse(rule_is_expired(rule(valid_upto=date(2026, 10, 1)), "2026-10-01"))
		self.assertTrue(rule_is_expired(rule(valid_upto="2026-09-30"), TODAY))
		self.assertTrue(rule_is_expired(rule(valid_upto=date(2026, 9, 30)), TODAY))

	def test_active(self):
		self.assertTrue(rule_is_active(rule(), COMPANY, TODAY, "OMR"))
		# a rule that starts later is sent active: the till checks the dates at sale time
		self.assertTrue(rule_is_active(rule(valid_from="2026-12-01"), COMPANY, TODAY, "OMR"))
		self.assertFalse(rule_is_active(rule(disable=1), COMPANY, TODAY, "OMR"))
		self.assertFalse(rule_is_active(rule(valid_upto="2026-09-30"), COMPANY, TODAY, "OMR"))
		self.assertFalse(rule_is_active(rule(coupon_code_based=1), COMPANY, TODAY, "OMR"))


class TestItemGroupRecords(unittest.TestCase):
	def group(self, name, arabic):
		return frappe._dict(
			name=name,
			parent_item_group="All Item Groups",
			lft=2,
			rgt=3,
			crenya_item_group_name_ar=arabic,
			modified=datetime(2026, 10, 5, 9, 0, 0),
		)

	def test_records_carry_the_arabic_name(self):
		record = ItemGroupSpec().build([self.group("Drinks", "مشروبات")], None)[0]
		self.assertEqual(
			record,
			{
				"name": "Drinks",
				"item_group_name_ar": "مشروبات",
				"parent_item_group": "All Item Groups",
				"lft": 2,
				"rgt": 3,
				"modified": "2026-10-05 09:00:00.000000",
			},
		)

	def test_empty_arabic_name_is_null(self):
		rows = [self.group("A", ""), self.group("B", None)]
		rows.append(
			frappe._dict({k: v for k, v in self.group("C", None).items() if k != "crenya_item_group_name_ar"})
		)
		for record in ItemGroupSpec().build(rows, None):
			self.assertIsNone(record["item_group_name_ar"], record["name"])


if __name__ == "__main__":
	unittest.main()
