"""The cash over/short of a shift (0.17.0), the pure part: which payment rows are posted and with
which sign (no site)."""

import unittest
from decimal import Decimal

from crenya_pos_api.cash_difference import (
	ERROR_CODES,
	OFF,
	cash_difference_block,
	cash_rows,
	error_code,
	stored_error,
)

CASH_MODES = {"Cash", "Cash USD"}


def shift(*payments):
	return {
		"payments": [
			{"mode_of_payment": mode, "expected": expected, "counted": counted, "difference": difference}
			for mode, expected, counted, difference in payments
		]
	}


class TestCashRows(unittest.TestCase):
	def test_cash_rows_only_cash_type_modes(self):
		doc = shift(
			("Cash", 67.9, 67.85, -0.05),
			("Card", 10.0, 9.0, -1.0),
			("Cash USD", 5.0, 5.2, 0.2),
		)
		self.assertEqual(
			cash_rows(doc, CASH_MODES, 3), [("Cash", Decimal("-0.050")), ("Cash USD", Decimal("0.200"))]
		)

	def test_cash_rows_zero_difference_none(self):
		self.assertEqual(cash_rows(shift(("Cash", 67.9, 67.9, 0.0)), CASH_MODES, 3), [])
		# below the currency's smallest unit is no difference
		self.assertEqual(cash_rows(shift(("Cash", 67.9, 67.9, 0.0004)), CASH_MODES, 3), [])
		self.assertEqual(cash_rows(shift(("Card", 1.0, 0.0, -1.0)), CASH_MODES, 3), [])
		self.assertEqual(cash_rows({"payments": []}, CASH_MODES, 3), [])

	def test_short_and_over_signs(self):
		# OMR, 3 decimals: expected 67.900, counted 67.850 is short by 0.050; counted 68.000 is over by 0.100
		self.assertEqual(
			cash_rows(shift(("Cash", 67.9, 67.85, -0.05)), CASH_MODES, 3), [("Cash", Decimal("-0.050"))]
		)
		self.assertEqual(
			cash_rows(shift(("Cash", 67.9, 68.0, 0.1)), CASH_MODES, 3), [("Cash", Decimal("0.100"))]
		)
		# a float stored by the database reads exactly
		self.assertEqual(
			cash_rows(shift(("Cash", 0, 0, 0.1 + 0.2)), CASH_MODES, 2), [("Cash", Decimal("0.30"))]
		)

	def test_block_of_a_shift_before_the_feature(self):
		self.assertEqual(
			cash_difference_block({}), {"state": OFF, "entry": None, "error": None, "can_retry": False}
		)
		self.assertTrue(
			cash_difference_block({"cash_difference_state": "Not posted", "cash_difference_error": "x: y"})[
				"can_retry"
			]
		)

	def test_error_codes_pinned(self):
		# the web Z page translates these (contract, Task 2); anything else shows one generic line
		for code in (
			"missing_account",
			"account_not_leaf",
			"account_wrong_type",
			"no_cash_account",
			"frozen_date",
		):
			self.assertIn(code, ERROR_CODES)
		for code in ERROR_CODES:
			self.assertRegex(code, r"^[a-z][a-z_]+$")

	def test_block_sends_the_code_never_the_words(self):
		stored = stored_error("frozen_date", "The accounts of <b>X</b> are frozen up to 2026-11-04")
		self.assertTrue(stored.startswith("frozen_date: "))
		block = cash_difference_block(
			{"cash_difference_state": "Not posted", "cash_difference_error": stored}
		)
		self.assertEqual(
			block, {"state": "Not posted", "entry": None, "error": "frozen_date", "can_retry": True}
		)
		# words from before the codes (or anything not code-shaped) are a generic code
		self.assertEqual(error_code("Account X is frozen"), "posting_failed")
		self.assertEqual(error_code("entry_refused: Debit and credit not equal"), "entry_refused")
		self.assertIsNone(error_code(None))
		# a posted shift carries no error
		self.assertIsNone(
			cash_difference_block({"cash_difference_state": "Posted", "cash_difference_error": "x"})["error"]
		)


if __name__ == "__main__":
	unittest.main()
