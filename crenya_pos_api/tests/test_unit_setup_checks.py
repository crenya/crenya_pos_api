"""Money and rounding setup checks (crenya_pos_api/web/setup_checks.py, moved from crenya_restaurant
0.14.2): the pure checks of a site's precision and rounding facts, for 3-decimal and 2-decimal
currencies, on Frappe 15 and 16.
The facts are example sites; the decimals come from each example Currency's Fraction Units and
Number Format, and the number formats from Frappe's own table."""

import unittest
from unittest.mock import MagicMock, patch

from crenya_pos_api.compat import number_format_precisions
from crenya_pos_api.sync import locale_data as locale
from crenya_pos_api.web import setup_checks
from crenya_pos_api.web.setup_checks import (
	MISSING,
	OK,
	WARN,
	check,
	fraction_decimals,
	money_checks,
)

FORMATS = number_format_precisions()


def facts(**over):
	"""binfagash today: an OMR site set up right (number format #,###.###, float precision 3,
	currency precision empty, number format not from the currency, Banker's Rounding)."""
	row = {
		"currency": "OMR",
		"frappe_major": 16,
		"rounding_method": "Banker's Rounding",
		"currency_precision": None,
		"float_precision": 3,
		"number_format": "#,###.###",
		"use_number_format_from_currency": False,
		"currency_number_format": "#,###.###",
		"fraction_units": 1000,
		"precisions": {
			"Sales Invoice grand total": 3,
			"Sales Invoice item rate": 3,
			"Purchase Invoice grand total": 3,
		},
	}
	row.update(over)
	return row


def two(**over):
	"""An INR site as ERPNext makes it: 2 decimals everywhere."""
	return facts(
		**{
			"currency": "INR",
			"number_format": "#,##,###.##",
			"currency_number_format": "#,##,###.##",
			"fraction_units": 100,
			"float_precision": None,
			"precisions": dict.fromkeys(facts()["precisions"], 2),
			**over,
		}
	)


def by_key(rows):
	return {row["key"]: row for row in rows}


@patch.object(setup_checks, "_", lambda text: text)
class TestMoneyChecks(unittest.TestCase):
	def test_frappe_number_formats_have_their_decimals(self):
		self.assertEqual(FORMATS["#,###.###"], 3)
		self.assertEqual(FORMATS["#,###.##"], 2)
		self.assertEqual(FORMATS["#,###"], 0)

	def test_fraction_units_give_decimals(self):
		self.assertEqual(
			[fraction_decimals(u) for u in (1000, 100, 10, 1, 0, None, 5, "1000")],
			[3, 2, 1, 0, None, None, None, 3],
		)

	def test_a_site_set_up_right_is_all_ok(self):
		for site in (facts(), facts(frappe_major=15), two(), two(frappe_major=15)):
			rows = money_checks(site, FORMATS)
			self.assertEqual(
				[r["key"] for r in rows],
				["rounding_method", "money_precision", "float_precision", "new_rows", "currency_format"],
			)
			self.assertEqual({r["status"] for r in rows}, {OK}, site)
			self.assertEqual({r["fix"] for r in rows}, {None})

	def test_rounding_other_than_bankers_warns(self):
		for method in ("Commercial Rounding", "Banker's Rounding (legacy)"):
			row = by_key(money_checks(facts(rounding_method=method), FORMATS))["rounding_method"]
			self.assertEqual((row["status"], row["detail"]), (WARN, method))
			self.assertEqual(
				row["fix"],
				"System Settings → Rounding Method → Banker's Rounding (affects every document from now on)",
			)

	def test_empty_system_format_is_frappes_default(self):
		# a v15 2-decimal site whose System Settings leave Number Format empty: Frappe's #,###.##
		for major in (15, 16):
			rows = by_key(
				money_checks(
					two(frappe_major=major, number_format=None, currency_number_format=None), FORMATS
				)
			)
			self.assertEqual({r["status"] for r in rows.values()}, {OK}, rows)
			self.assertIn("empty: #,###.##", rows["currency_format"]["detail"])
		# with OMR the empty format is still 2 decimals: the precision and the v15 trap are real
		site = facts(
			frappe_major=15,
			number_format=None,
			use_number_format_from_currency=True,
			precisions=dict.fromkeys(facts()["precisions"], 2),
		)
		rows = by_key(money_checks(site, FORMATS))
		self.assertEqual(rows["new_rows"]["status"], MISSING)
		self.assertIn("#,###.##", rows["new_rows"]["detail"])

	def test_two_decimal_system_format_misses_omr_decimals(self):
		site = facts(number_format="#,###.##", precisions=dict.fromkeys(facts()["precisions"], 2))
		row = by_key(money_checks(site, FORMATS))["money_precision"]
		self.assertEqual(row["status"], MISSING)
		self.assertEqual(
			row["fix"], "System Settings → Number Format → a format with the currency's decimals"
		)
		self.assertIn("OMR has 3 decimals", row["detail"])
		self.assertIn("#,###.###", row["detail"])

	def test_a_set_currency_precision_is_the_fix(self):
		site = facts(currency_precision=2, precisions=dict.fromkeys(facts()["precisions"], 2))
		row = by_key(money_checks(site, FORMATS))["money_precision"]
		self.assertEqual(row["status"], MISSING)
		self.assertIn("Currency Precision", row["fix"])
		# a set Currency Precision of 3 is every currency's: OMR and INR both fine
		self.assertEqual(
			by_key(money_checks(facts(currency_precision=3), FORMATS))["money_precision"]["status"], OK
		)

	def test_the_currency_format_is_the_fix_when_taken_from_the_currency(self):
		site = facts(
			use_number_format_from_currency=True,
			currency_number_format="#,###.##",
			precisions=dict.fromkeys(facts()["precisions"], 2),
		)
		rows = by_key(money_checks(site, FORMATS))
		self.assertEqual(
			rows["money_precision"]["fix"], "Currency → Number Format → a format with the currency's decimals"
		)
		self.assertEqual(rows["currency_format"]["status"], WARN)

	def test_one_field_short_is_missing(self):
		site = facts(precisions={**facts()["precisions"], "Sales Invoice item rate": 2})
		row = by_key(money_checks(site, FORMATS))["money_precision"]
		self.assertEqual(row["status"], MISSING)
		self.assertIn("Sales Invoice item rate 2", row["detail"])
		self.assertIn("Customize Form", row["fix"])

	def test_float_precision_below_the_decimals_warns(self):
		row = by_key(money_checks(facts(float_precision=2), FORMATS))["float_precision"]
		self.assertEqual(row["status"], WARN)
		# empty is Frappe's 3: OMR fine
		row = by_key(money_checks(facts(float_precision=None), FORMATS))["float_precision"]
		self.assertEqual((row["status"], row["detail"]), (OK, "empty: 3"))

	def test_v15_trap_flagged_on_frappe_15_only(self):
		trap = {
			"use_number_format_from_currency": True,
			"number_format": "#,###.##",
			"currency_number_format": "#,###.###",
		}
		row = by_key(money_checks(facts(frappe_major=15, **trap), FORMATS))["new_rows"]
		self.assertEqual(row["status"], MISSING)
		self.assertIn("Use Number Format from Currency → off", row["fix"])
		self.assertIn("#,###.##", row["detail"])
		self.assertEqual(
			by_key(money_checks(facts(frappe_major=16, **trap), FORMATS))["new_rows"]["status"], OK
		)
		# a set Currency Precision covering the decimals, or a 2-decimal currency: no trap
		self.assertEqual(
			by_key(money_checks(facts(frappe_major=15, currency_precision=3, **trap), FORMATS))["new_rows"][
				"status"
			],
			OK,
		)
		self.assertEqual(
			by_key(money_checks(two(frappe_major=15, use_number_format_from_currency=True), FORMATS))[
				"new_rows"
			]["status"],
			OK,
		)

	def test_currency_format_against_fraction_units(self):
		row = by_key(money_checks(facts(currency_number_format="#,###.##"), FORMATS))["currency_format"]
		self.assertEqual(row["status"], WARN)
		self.assertIn("Fraction Units 1000 (3 decimals)", row["detail"])
		# empty: the system format stands in, as on the tills
		row = by_key(money_checks(facts(currency_number_format=None), FORMATS))["currency_format"]
		self.assertEqual(row["status"], OK)
		self.assertIn("empty: #,###.###", row["detail"])
		# no Fraction Units: the decimals come from the Number Format, and the units are asked for
		rows = by_key(money_checks(facts(fraction_units=None), FORMATS))
		self.assertEqual(rows["currency_format"]["status"], WARN)
		self.assertEqual(rows["money_precision"]["status"], OK)

	def test_unknown_decimals_warn(self):
		site = facts(fraction_units=None, currency_number_format="#x", number_format="#y")
		rows = by_key(money_checks(site, FORMATS))
		self.assertEqual(rows["money_precision"]["status"], WARN)
		self.assertEqual(rows["float_precision"]["status"], OK)
		self.assertEqual(rows["new_rows"]["status"], OK)

	def test_no_currency_no_checks(self):
		self.assertEqual(setup_checks.money_setup_checks(None), [])

	def test_check_row_fix_only_when_not_ok(self):
		self.assertEqual(
			check("k", OK, "Label", "d", "Fix it"),
			{"key": "k", "status": OK, "label": "Label", "detail": "d", "fix": None},
		)
		self.assertEqual(check("k", WARN, "Label", fix="Fix it")["fix"], "Fix it")


class TestRoundingMethod(unittest.TestCase):
	def test_empty_rounding_method_is_frappes_legacy(self):
		db = MagicMock()
		with patch("frappe.db", db, create=True):
			db.get_single_value.return_value = None
			self.assertEqual(locale.rounding_method(), "Banker's Rounding (legacy)")
			db.get_single_value.return_value = "Commercial Rounding"
			self.assertEqual(locale.rounding_method(), "Commercial Rounding")
		self.assertEqual(locale.BANKERS, "Banker's Rounding")


if __name__ == "__main__":
	unittest.main()
