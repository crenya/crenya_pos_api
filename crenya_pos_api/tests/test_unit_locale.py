"""Locale data helpers: dialling codes, totals tolerance, currency view, cash denominations (no site needed)."""

import unittest
from decimal import Decimal

from crenya_pos_api.sync.locale_data import currency_view, format_denominations
from crenya_pos_api.utils.decimal import smallest_unit_tolerance
from crenya_pos_api.utils.phone import build_phone_country_codes, isd_code, phone_country_code

# a slice of frappe.geo.country_info's data, in its shape
COUNTRY_INFO = {
	"Oman": {"code": "om", "currency": "OMR", "isd": "+968"},
	"India": {"code": "in", "currency": "INR", "isd": "+91"},
	"Åland Islands": {"code": "ax", "currency": "EUR", "isd": "+358"},
	"Western Sahara": {"code": "eh", "currency": "MAD", "isd": None},
	"Curaçao": {"code": "cw", "currency": "ANG"},
	"Egypt": {"code": "eg", "currency": "EGP", "isd": "20"},
	"Bahrain": {"code": "bh", "currency": "BHD", "isd": " +973 "},
	"Broken": "not a dict",
}


class TestPhoneCountryCodes(unittest.TestCase):
	def test_isd_code_normalises(self):
		self.assertEqual(isd_code("+968"), "+968")
		self.assertEqual(isd_code("20"), "+20")
		self.assertEqual(isd_code(" +973 "), "+973")
		self.assertEqual(isd_code(971), "+971")
		for empty in (None, "", "  ", True):
			with self.subTest(value=empty):
				self.assertEqual(isd_code(empty), "")

	def test_company_country_first_then_by_name(self):
		rows = build_phone_country_codes(COUNTRY_INFO, "Oman")
		self.assertEqual(rows[0], {"iso": "OM", "name": "Oman", "code": "+968"})
		self.assertEqual([row["name"] for row in rows[1:]], ["Åland Islands", "Bahrain", "Egypt", "India"])

	def test_skips_countries_without_isd(self):
		names = {row["name"] for row in build_phone_country_codes(COUNTRY_INFO, "Oman")}
		self.assertNotIn("Western Sahara", names)
		self.assertNotIn("Curaçao", names)
		self.assertNotIn("Broken", names)

	def test_codes_have_plus_and_upper_case_iso(self):
		for row in build_phone_country_codes(COUNTRY_INFO):
			with self.subTest(country=row["name"]):
				self.assertTrue(row["code"].startswith("+"))
				self.assertEqual(row["iso"], row["iso"].upper())
				self.assertEqual(len(row["iso"]), 2)

	def test_unknown_or_empty_company_country_sorts_by_name(self):
		for country in (None, "", "Atlantis", 968):
			with self.subTest(country=country):
				rows = build_phone_country_codes(COUNTRY_INFO, country)
				self.assertEqual(
					[row["name"] for row in rows], ["Åland Islands", "Bahrain", "Egypt", "India", "Oman"]
				)

	def test_company_phone_country_code_has_no_fallback(self):
		self.assertEqual(phone_country_code("Oman", COUNTRY_INFO), "+968")
		self.assertEqual(phone_country_code(" India ", COUNTRY_INFO), "+91")
		self.assertEqual(phone_country_code("Egypt", COUNTRY_INFO), "+20")
		for country in (None, "", "Atlantis", "Western Sahara", "Broken", 968):
			with self.subTest(country=country):
				self.assertEqual(phone_country_code(country, COUNTRY_INFO), "")


class TestTotalTolerance(unittest.TestCase):
	def test_ten_smallest_units(self):
		cases = {0: "10", 1: "1.0", 2: "0.10", 3: "0.010", 4: "0.0010"}
		for precision, expected in cases.items():
			with self.subTest(precision=precision):
				tolerance = smallest_unit_tolerance(precision, 10)
				self.assertEqual(f"{tolerance:f}", expected)
				self.assertEqual(tolerance, Decimal(expected))

	def test_precision_as_string_or_empty(self):
		self.assertEqual(smallest_unit_tolerance("3", 10), Decimal("0.010"))
		self.assertEqual(smallest_unit_tolerance(None, 10), Decimal("10"))


class TestCurrencyView(unittest.TestCase):
	def test_values_from_currency_record(self):
		row = {
			"symbol": "ر.ع.",
			"symbol_on_right": 1,
			"fraction": "Baisa",
			"fraction_units": 1000,
			"number_format": "#,###.###",
			"smallest_currency_fraction_value": 0.005,
		}
		self.assertEqual(
			currency_view("OMR", row, 3, "#,###.##"),
			{
				"code": "OMR",
				"symbol": "ر.ع.",
				"symbol_on_right": True,
				"fraction": "Baisa",
				"fraction_units": 1000,
				"number_format": "#,###.###",
				"precision": 3,
				"smallest_currency_fraction_value": "0.005",
			},
		)

	def test_missing_values_fall_back_or_are_null(self):
		view = currency_view("XYZ", {"symbol": " ", "number_format": None}, 2, "#.###,##")
		self.assertIsNone(view["symbol"])
		self.assertIs(view["symbol_on_right"], False)
		self.assertIsNone(view["fraction"])
		self.assertIsNone(view["fraction_units"])
		self.assertEqual(view["number_format"], "#.###,##")
		self.assertIsNone(view["smallest_currency_fraction_value"])
		self.assertEqual(currency_view("XYZ", None, 2)["number_format"], "")


class TestCashDenominations(unittest.TestCase):
	def test_highest_first_as_decimal_strings(self):
		rows = [
			{"value": 0.1, "label": "100 Baisa", "kind": "coin"},
			{"value": 20.0, "label": "20 Rial", "kind": "note"},
			{"value": 0.5, "label": "Half Rial", "kind": "note"},
			{"value": 1.0, "label": "1 Rial", "kind": "note"},
			{"value": 0.025, "label": "25 Baisa", "kind": "coin"},
			{"value": 5.0, "label": "5 Rial", "kind": "note"},
		]
		self.assertEqual(
			format_denominations(rows),
			[
				{"value": "20", "label": "20 Rial", "kind": "note"},
				{"value": "5", "label": "5 Rial", "kind": "note"},
				{"value": "1", "label": "1 Rial", "kind": "note"},
				{"value": "0.5", "label": "Half Rial", "kind": "note"},
				{"value": "0.1", "label": "100 Baisa", "kind": "coin"},
				{"value": "0.025", "label": "25 Baisa", "kind": "coin"},
			],
		)

	def test_skips_non_positive_and_defaults_label_and_kind(self):
		rows = [
			{"value": 0, "label": "zero", "kind": "coin"},
			{"value": -1, "label": "negative", "kind": "coin"},
			{"value": "2.50", "label": " ", "kind": "banknote"},
		]
		self.assertEqual(format_denominations(rows), [{"value": "2.5", "label": "2.5", "kind": "note"}])

	def test_values_keep_their_decimals(self):
		rows = [
			{"value": 0.005, "label": "5 fils", "kind": "coin"},
			{"value": 0.25, "label": "quarter", "kind": "coin"},
		]
		self.assertEqual([row["value"] for row in format_denominations(rows)], ["0.25", "0.005"])

	def test_empty(self):
		self.assertEqual(format_denominations([]), [])


if __name__ == "__main__":
	unittest.main()
