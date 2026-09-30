"""Loyalty payload and tax template selection helpers (no site needed)."""

import copy
import unittest
from decimal import Decimal

from crenya_pos_api.sync.errors import SyncError
from crenya_pos_api.sync.invoice_builder import LOYALTY_RETURN_MESSAGE, assert_returnable_at_till
from crenya_pos_api.sync.loyalty import check_redemption, max_redeemable_amount
from crenya_pos_api.sync.taxes import is_offline_template, resolve_invoice_template, select_tax_templates
from crenya_pos_api.sync.validation import validate_invoice_payload
from crenya_pos_api.tests.test_unit_validation import SALE, make_return


def sale(**overrides):
	payload = copy.deepcopy(SALE)
	payload.update(overrides)
	return payload


class TestLoyaltyPayload(unittest.TestCase):
	def assertInvalid(self, payload, fragment):
		with self.assertRaises(SyncError) as ctx:
			validate_invoice_payload(payload)
		self.assertEqual(ctx.exception.code, "validation")
		self.assertIn(fragment, ctx.exception.message)

	def test_absent_or_null_is_none(self):
		self.assertNotIn("loyalty", SALE)
		self.assertIsNone(validate_invoice_payload(sale())["loyalty"])
		self.assertIsNone(validate_invoice_payload(sale(loyalty=None))["loyalty"])

	def test_valid_is_normalized(self):
		data = validate_invoice_payload(sale(loyalty={"points": 120, "amount": "1.200"}))
		self.assertEqual(data["loyalty"], {"points": 120, "amount": Decimal("1.200")})
		self.assertIsInstance(data["loyalty"]["amount"], Decimal)

	def test_rejections(self):
		cases = [
			("not-an-object", "loyalty must be an object"),
			([120, "1.200"], "loyalty must be an object"),
			({"amount": "1.200"}, "loyalty.points"),
			({"points": 0, "amount": "1.200"}, "loyalty.points"),
			({"points": -5, "amount": "1.200"}, "loyalty.points"),
			({"points": 1.5, "amount": "1.200"}, "loyalty.points"),
			({"points": True, "amount": "1.200"}, "loyalty.points"),
			({"points": 10**10, "amount": "1.200"}, "loyalty.points"),
			({"points": 120}, "loyalty.amount"),
			({"points": 120, "amount": "0"}, "loyalty.amount must be positive"),
			({"points": 120, "amount": "-1.000"}, "loyalty.amount must be positive"),
			({"points": 120, "amount": 1.2}, "loyalty.amount"),
			({"points": 120, "amount": "abc"}, "loyalty.amount"),
		]
		for value, fragment in cases:
			with self.subTest(value=value):
				self.assertInvalid(sale(loyalty=value), fragment)

	def test_not_allowed_on_return(self):
		payload = make_return()
		payload["loyalty"] = {"points": 10, "amount": "0.100"}
		self.assertInvalid(payload, "cannot be redeemed on a return")


class TestReturnOfLoyaltyInvoice(unittest.TestCase):
	def test_invoice_without_points_is_returnable(self):
		for amount in (None, 0, 0.0, "0", 0.0004):
			with self.subTest(amount=amount):
				assert_returnable_at_till(amount, 3)

	def test_invoice_partly_paid_with_points_is_refused(self):
		for amount in (0.2, "1.200", 0.0005):
			with self.subTest(amount=amount), self.assertRaises(SyncError) as ctx:
				assert_returnable_at_till(amount, 3)
			self.assertEqual(ctx.exception.code, "validation")
			self.assertEqual(
				ctx.exception.message,
				"Return this invoice from ERPNext: it was partly paid with loyalty points",
			)
		self.assertEqual(LOYALTY_RETURN_MESSAGE, ctx.exception.message)


class TestRedemptionChecks(unittest.TestCase):
	def check(self, points, amount, factor="0.01", payable="5.000", tolerance="0.010"):
		check_redemption(
			{"points": points, "amount": Decimal(amount)}, float(factor), Decimal(payable), tolerance, 3
		)

	def test_exact_and_rounded_down_amounts_pass(self):
		self.check(120, "1.200")
		self.check(7, "0.035", factor="0.005")
		# 333 x 0.0033 = 1.0989: a till floors to 1.098
		self.check(333, "1.098", factor="0.0033")

	def test_amount_must_match_points(self):
		for amount in ("1.201", "1.500", "1.100"):
			with self.subTest(amount=amount), self.assertRaises(SyncError) as ctx:
				self.check(120, amount)
			self.assertEqual(ctx.exception.code, "validation")

	def test_amount_over_payable(self):
		with self.assertRaises(SyncError) as ctx:
			self.check(1000, "10.000", payable="5.000")
		self.assertIn("more than the invoice total", ctx.exception.message)

	def test_no_conversion_factor(self):
		with self.assertRaises(SyncError):
			self.check(10, "0.100", factor="0")

	def test_max_redeemable_amount_floors(self):
		self.assertEqual(max_redeemable_amount(120, 0.01, 3), Decimal("1.200"))
		self.assertEqual(max_redeemable_amount(333, 0.0033, 3), Decimal("1.098"))
		self.assertEqual(max_redeemable_amount(0, 0.01, 3), Decimal("0.000"))
		self.assertEqual(max_redeemable_amount(-5, 0.01, 3), Decimal("0.000"))
		self.assertEqual(max_redeemable_amount(10, None, 3), Decimal("0.000"))


def tax_row(charge_type="On Net Total", rate=5, included=1, account="VAT 5% - CR"):
	return {
		"charge_type": charge_type,
		"account_head": account,
		"description": "VAT",
		"rate": rate,
		"included_in_print_rate": included,
	}


TEMPLATES = [
	{"name": "Oman VAT 5% - CR", "title": "Oman VAT 5%"},
	{"name": "VAT 5% exclusive - CR", "title": "VAT 5% exclusive"},
	{"name": "Zero Rated - CR", "title": "Zero Rated"},
	{"name": "Delivery Charge - CR", "title": "Delivery Charge"},
]
ROWS = {
	"Oman VAT 5% - CR": [tax_row()],
	"VAT 5% exclusive - CR": [tax_row(included=0)],
	"Zero Rated - CR": [tax_row(rate=0)],
	"Delivery Charge - CR": [tax_row(), tax_row("Actual", rate=0, account="Freight - CR")],
}


class TestTaxTemplates(unittest.TestCase):
	def test_selection(self):
		result = select_tax_templates(TEMPLATES, ROWS, "Oman VAT 5% - CR")
		names = [row["name"] for row in result]
		self.assertEqual(names, ["Oman VAT 5% - CR", "VAT 5% exclusive - CR", "Zero Rated - CR"])
		self.assertEqual([row["is_default"] for row in result], [True, False, False])
		exclusive = result[1]
		self.assertEqual(exclusive["title"], "VAT 5% exclusive")
		self.assertEqual(
			exclusive["taxes"],
			[
				{
					"account_head": "VAT 5% - CR",
					"description": "VAT",
					"rate": "5",
					"included_in_print_rate": False,
				}
			],
		)
		self.assertEqual(result[2]["taxes"][0]["rate"], "0")

	def test_default_is_kept_even_when_unsupported_or_missing(self):
		result = select_tax_templates(TEMPLATES, ROWS, "Delivery Charge - CR")
		self.assertEqual(result[0]["name"], "Delivery Charge - CR")
		self.assertTrue(result[0]["is_default"])

		# the profile default was disabled after it was set: still offered, titled by name
		result = select_tax_templates(TEMPLATES[1:], ROWS, "Oman VAT 5% - CR")
		self.assertEqual(result[0]["name"], "Oman VAT 5% - CR")
		self.assertEqual(result[0]["title"], "Oman VAT 5% - CR")

	def test_no_default(self):
		result = select_tax_templates(TEMPLATES, ROWS, None)
		self.assertFalse(any(row["is_default"] for row in result))
		self.assertEqual([row["title"] for row in result], ["Oman VAT 5%", "VAT 5% exclusive", "Zero Rated"])

	def test_empty_template_is_offline(self):
		self.assertTrue(is_offline_template([]))
		self.assertFalse(is_offline_template([tax_row("On Previous Row Total")]))

	def test_resolve_invoice_template(self):
		allowed = {"Oman VAT 5% - CR", "Zero Rated - CR"}
		self.assertEqual(resolve_invoice_template(None, allowed, "Oman VAT 5% - CR"), "Oman VAT 5% - CR")
		self.assertEqual(resolve_invoice_template("", allowed, "Oman VAT 5% - CR"), "Oman VAT 5% - CR")
		self.assertEqual(
			resolve_invoice_template("Zero Rated - CR", allowed, "Oman VAT 5% - CR"), "Zero Rated - CR"
		)
		self.assertIsNone(resolve_invoice_template(None, set(), None))
		with self.assertRaises(SyncError) as ctx:
			resolve_invoice_template("Delivery Charge - CR", allowed, "Oman VAT 5% - CR")
		self.assertEqual(ctx.exception.code, "validation")
		self.assertIn("Delivery Charge - CR", ctx.exception.message)


if __name__ == "__main__":
	unittest.main()
