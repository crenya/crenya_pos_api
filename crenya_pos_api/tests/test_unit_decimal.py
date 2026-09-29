import unittest
from decimal import Decimal

from crenya_pos_api.utils.decimal import (
	DecimalParseError,
	as_decimal,
	format_money,
	format_number,
	parse_decimal,
	quantize,
	within_tolerance,
)


class TestParseDecimal(unittest.TestCase):
	def test_accepts_decimal_strings_and_ints(self):
		self.assertEqual(parse_decimal("12.345"), Decimal("12.345"))
		self.assertEqual(parse_decimal("-1.200"), Decimal("-1.200"))
		self.assertEqual(parse_decimal(" 3 "), Decimal("3"))
		self.assertEqual(parse_decimal(".5"), Decimal("0.5"))
		self.assertEqual(parse_decimal("+2."), Decimal("2"))
		self.assertEqual(parse_decimal(7), Decimal(7))

	def test_rejects_floats_bools_and_garbage(self):
		for value in (1.2, True, "abc", "1e5", "NaN", "Infinity", "1,000", "", "1.2.3", "--1", [], {}):
			with self.assertRaises(DecimalParseError, msg=repr(value)):
				parse_decimal(value, "qty")

	def test_none_handling(self):
		self.assertIsNone(parse_decimal(None, allow_none=True))
		self.assertIsNone(parse_decimal("  ", allow_none=True))
		with self.assertRaises(DecimalParseError):
			parse_decimal(None)

	def test_error_names_field(self):
		with self.assertRaises(DecimalParseError) as ctx:
			parse_decimal("x", "items[0].rate")
		self.assertIn("items[0].rate", str(ctx.exception))

	def test_rejects_overlong_values(self):
		with self.assertRaises(DecimalParseError):
			parse_decimal("1" * 40)


class TestFormatting(unittest.TestCase):
	def test_money_fixed_precision(self):
		self.assertEqual(format_money(1.2, 3), "1.200")
		self.assertEqual(format_money("5.25", 3), "5.250")
		self.assertEqual(format_money(0, 3), "0.000")
		self.assertEqual(format_money(None, 2), "0.00")

	def test_money_rounds_half_away_from_zero(self):
		self.assertEqual(format_money("1.0005", 3), "1.001")
		self.assertEqual(format_money("-1.0005", 3), "-1.001")
		self.assertEqual(format_money(2.675, 2), "2.68")

	def test_float_noise_is_ignored(self):
		self.assertEqual(format_money(0.1 + 0.2, 3), "0.300")
		self.assertEqual(format_money(1.143, 3), "1.143")

	def test_negative_zero_is_normalized(self):
		self.assertEqual(format_money(-0.0001, 3), "0.000")
		self.assertEqual(format_number(-0.0, 3), "0")

	def test_number_strips_trailing_zeros(self):
		self.assertEqual(format_number(3.0), "3")
		self.assertEqual(format_number("0.50"), "0.5")
		self.assertEqual(format_number(100), "100")
		self.assertEqual(format_number(-2.0), "-2")
		self.assertEqual(format_number(1.23456, 3), "1.235")
		self.assertEqual(format_number(5.0), "5")

	def test_as_decimal_and_quantize(self):
		self.assertEqual(as_decimal(1.2), Decimal("1.2"))
		self.assertEqual(as_decimal(None), Decimal(0))
		self.assertEqual(quantize("0.0574", 3), Decimal("0.057"))

	def test_within_tolerance_is_exact(self):
		self.assertTrue(within_tolerance("1.200", "1.210", "0.010"))
		self.assertFalse(within_tolerance("1.200", "1.211", "0.010"))
		self.assertTrue(within_tolerance(1.2, "1.19", 0.01))
		self.assertTrue(within_tolerance("-5.250", "-5.245", "0.010"))


if __name__ == "__main__":
	unittest.main()
