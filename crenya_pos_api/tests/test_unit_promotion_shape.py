"""Promotions and loyalty programs as app.crenya.com edits them (crenya_pos_api/web/
promotion_shape.py and loyalty_shape.py): the pure checks and the ERPNext records they become."""

import unittest

from crenya_pos_api.sync.promotions import rule_qualifies
from crenya_pos_api.web.loyalty_shape import loyalty_problem, to_loyalty_program
from crenya_pos_api.web.promotion_shape import from_pricing_rule, promotion_problem, to_pricing_rule

OMR = 3
TYPES = ["Single Tier Program", "Multiple Tier Program"]


def promotion(**over):
	row = {
		"title": "10% off snacks",
		"enabled": True,
		"apply_on": "item_group",
		"targets": ["Snacks"],
		"kind": "discount_percentage",
		"value": "10",
	}
	row.update(over)
	return row


def program(**over):
	row = {
		"loyalty_program_name": "Muscat Rewards",
		"loyalty_program_type": "Single Tier Program",
		"from_date": "2026-10-01",
		"auto_opt_in": True,
		"conversion_factor": "0.010",
		"expiry_duration": 365,
		"expense_account": "Loyalty - MS",
		"cost_center": "Main - MS",
		"collection_rules": [{"tier_name": "Standard", "min_spent": "0", "collection_factor": "1"}],
	}
	row.update(over)
	return row


class TestPromotionShape(unittest.TestCase):
	def test_buy_two_get_one_maps_to_product_discount(self):
		data = promotion(
			apply_on="item_code",
			targets=["CHIPS"],
			kind="free_item",
			value=None,
			same_item=True,
			free_qty="1",
			min_qty="2",
			recursive=True,
		)
		del data["value"]
		self.assertIsNone(promotion_problem(data, OMR))
		rule = to_pricing_rule(data)
		self.assertEqual(rule["apply_on"], "Item Code")
		self.assertEqual(rule["items"], [{"item_code": "CHIPS"}])
		self.assertEqual((rule["item_groups"], rule["brands"]), ([], []))
		self.assertEqual(rule["price_or_product_discount"], "Product")
		self.assertEqual((rule["same_item"], rule["free_item"], rule["free_qty"]), (1, None, 1.0))
		self.assertEqual((rule["is_recursive"], rule["recurse_for"], rule["min_qty"]), (1, 2.0, 2.0))
		self.assertEqual((rule["discount_percentage"], rule["max_qty"]), (0.0, 0.0))
		# the till takes it
		self.assertTrue(rule_qualifies({**rule, "company": "Shop"}, "Shop"))

	def test_other_free_item_needs_its_code(self):
		data = promotion(kind="free_item", free_qty="1", min_qty="3")
		del data["value"]
		self.assertIn("free item", promotion_problem(data, OMR))
		rule = to_pricing_rule({**data, "free_item": "JUICE"})
		self.assertEqual((rule["same_item"], rule["free_item"], rule["is_recursive"]), (0, "JUICE", 0))

	def test_coupon_refused(self):
		self.assertIn("Coupon", promotion_problem(promotion(coupon_code_based=1), OMR))

	def test_negative_percent_refused(self):
		for value in ("-5", "0", "100.5"):
			self.assertIsNotNone(promotion_problem(promotion(value=value), OMR), value)
		self.assertIsNone(promotion_problem(promotion(value="100"), OMR))

	def test_amount_uses_currency_precision(self):
		self.assertIsNone(promotion_problem(promotion(kind="discount_amount", value="0.250"), OMR))
		self.assertIsNotNone(promotion_problem(promotion(kind="discount_amount", value="0.2505"), OMR))
		self.assertIsNotNone(promotion_problem(promotion(kind="rate", value="1.005"), 2))
		self.assertIsNotNone(promotion_problem(promotion(kind="rate", value="-1"), OMR))

	def test_other_problems(self):
		for bad in (
			promotion(title=" "),
			promotion(apply_on="transaction"),
			promotion(targets=[]),
			promotion(targets=["A", "A"]),
			promotion(kind="margin"),
			promotion(min_qty="5", max_qty="2"),
			promotion(valid_from="2026-10-10", valid_upto="2026-10-01"),
			promotion(valid_from="10/10/2026"),
			promotion(priority=21),
			promotion(value=0.5),
			promotion(customer="x"),
			"not an object",
		):
			self.assertIsNotNone(promotion_problem(bad, OMR), bad)

	def test_percentage_round_trip(self):
		data = promotion(priority=3, valid_from="2026-10-01", min_qty="2")
		rule = to_pricing_rule(data)
		self.assertEqual(
			(rule["rate_or_discount"], rule["discount_percentage"]), ("Discount Percentage", 10.0)
		)
		self.assertEqual(rule["item_groups"], [{"item_group": "Snacks"}])
		self.assertEqual(rule["priority"], "3")
		back = from_pricing_rule(
			{**rule, "name": "PRLE-0001", "modified": "x"}, {"item_groups": ["Snacks"]}, OMR
		)
		for key in (
			"title",
			"enabled",
			"apply_on",
			"targets",
			"kind",
			"value",
			"priority",
			"valid_from",
			"min_qty",
		):
			self.assertEqual(back[key], data[key], key)

	def test_other_shapes_are_not_promotions(self):
		rule = to_pricing_rule(promotion())
		self.assertIsNone(from_pricing_rule({**rule, "apply_on": "Transaction"}, {}, OMR))
		self.assertIsNone(from_pricing_rule({**rule, "coupon_code_based": 1}, {}, OMR))
		self.assertIsNone(from_pricing_rule({**rule, "applicable_for": "Customer"}, {}, OMR))


class TestLoyaltyShape(unittest.TestCase):
	def test_valid_program(self):
		self.assertIsNone(loyalty_problem(program(), OMR, TYPES))
		record = to_loyalty_program(program())
		self.assertEqual(record["auto_opt_in"], 1)
		self.assertEqual(record["collection_rules"][0]["tier_name"], "Standard")
		self.assertEqual(record["conversion_factor"], 0.01)
		self.assertEqual(record["collection_rules"][0]["min_spent"], 0.0)

	def test_tiers(self):
		multi = program(
			loyalty_program_type="Multiple Tier Program",
			collection_rules=[
				{"tier_name": "Silver", "min_spent": "0", "collection_factor": "1"},
				{"tier_name": "Gold", "min_spent": "500.000", "collection_factor": "0.5"},
			],
		)
		self.assertIsNone(loyalty_problem(multi, OMR, TYPES))
		self.assertIn(
			"exactly one tier",
			loyalty_problem({**multi, "loyalty_program_type": "Single Tier Program"}, OMR, TYPES),
		)
		no_zero = program(collection_rules=[{"tier_name": "A", "min_spent": "10", "collection_factor": "1"}])
		self.assertIn("lowest tier", loyalty_problem(no_zero, OMR, TYPES))
		self.assertIsNotNone(loyalty_problem(program(collection_rules=[]), OMR, TYPES))
		fine = [{"tier_name": "A", "min_spent": "0.0001", "collection_factor": "1"}]
		self.assertIsNotNone(loyalty_problem(program(collection_rules=fine), OMR, TYPES))

	def test_problems(self):
		for bad in (
			program(loyalty_program_name=""),
			program(name="Old name"),
			program(loyalty_program_type="Points"),
			program(expense_account=""),
			program(cost_center=None),
			program(conversion_factor="0"),
			program(from_date=None),
			program(to_date="2026-09-01"),
			program(expiry_duration=-1),
			program(project="x"),
		):
			self.assertIsNotNone(loyalty_problem(bad, OMR, TYPES), bad)


if __name__ == "__main__":
	unittest.main()
