"""The pure parts of the Shops Z reports and refused sales: Z sections at the currency's
decimals, the refused-sale hints and the retry rule."""

import unittest
from datetime import datetime

from crenya_pos_api.api.web_admin.day_close import Z_LABELS, z_sections
from crenya_pos_api.api.web_admin.refused import HINTS, hint_key
from crenya_pos_api.sync.retry import retry_pending

SHIFT = {
	"sales_count": 12,
	"sales_total": 30.5,
	"tax_total": 1.452,
	"net_total": 29.1,
	"returns_count": 1,
	"returns_total": 1.4,
	"opening_float": 20,
	"line_voids_count": 3,
	"line_voids_amount": 1.25,
	"bill_clears_count": 0,
	"bill_clears_amount": 0,
	"no_sales_count": 2,
	"approvals_count": 4,
	"expense_count": 1,
	"payments": [
		{"mode_of_payment": "Cash", "expected": 40.5, "counted": 40.4, "difference": -0.1, "paid_out": 2},
		{"mode_of_payment": "Card", "expected": 10, "counted": 10, "difference": 0, "paid_out": 0},
	],
}


def rows(sections, key):
	return next(s for s in sections if s["key"] == key)["rows"]


class TestZSections(unittest.TestCase):
	def test_three_decimals_and_kinds(self):
		sections = z_sections(SHIFT, 3, "VAT", "ضريبة القيمة المضافة")
		self.assertEqual(
			[s["key"] for s in sections], ["sales", "payments", "returns", "store_controls", "paid_out"]
		)
		sales = rows(sections, "sales")
		self.assertEqual(
			[(r["value"], r["kind"]) for r in sales],
			[("12", "count"), ("30.500", "money"), ("1.452", "money"), ("29.100", "money")],
		)
		self.assertEqual((sales[2]["label_en"], sales[2]["label_ar"]), ("VAT", "ضريبة القيمة المضافة"))
		payments = rows(sections, "payments")
		self.assertEqual(payments[0]["value"], "20.000")
		self.assertEqual(
			[r["label_en"] for r in payments[1:4]], ["Cash expected", "Cash counted", "Cash difference"]
		)
		self.assertEqual(payments[3]["value"], "-0.100")
		# only modes with money paid out, after the expense count
		self.assertEqual(
			[(r["label_en"], r["value"]) for r in rows(sections, "paid_out")],
			[("Expenses", "1"), ("Cash paid out", "2.000")],
		)

	def test_two_decimals(self):
		sales = rows(z_sections(SHIFT, 2, "GST"), "sales")
		self.assertEqual(sales[1]["value"], "30.50")
		self.assertEqual(sales[2]["label_ar"], Z_LABELS["tax_total"][1])

	def test_every_row_has_arabic(self):
		for section in z_sections(SHIFT, 3, "VAT"):
			self.assertTrue(section["title_ar"])
			for row in section["rows"]:
				self.assertTrue(row["label_ar"])
				self.assertIn(row["kind"], ("money", "count"))


class TestHints(unittest.TestCase):
	def test_validation_messages(self):
		self.assertEqual(hint_key("validation", "1.0 units of Item X needed in Warehouse Stores"), "stock")
		self.assertEqual(hint_key("validation", "Price List Standard Selling is disabled"), "price_list")
		self.assertEqual(
			hint_key("validation", "Please set default Cash or Bank account in Mode of Payment Card"),
			"account",
		)
		self.assertEqual(hint_key("validation", "Something else"), "generic")

	def test_codes(self):
		self.assertEqual(hint_key("permission", "Not permitted"), "permission")
		self.assertEqual(hint_key("total_mismatch", ""), "total_mismatch")
		self.assertEqual(hint_key("internal", ""), "generic")
		self.assertEqual(hint_key(None, None), "generic")
		self.assertTrue(all(en and ar for en, ar in HINTS.values()))


class TestRetryPending(unittest.TestCase):
	def test_rule(self):
		asked = datetime(2026, 10, 13, 10, 0, 0)
		self.assertFalse(retry_pending(None, asked))
		self.assertTrue(retry_pending(asked, None))
		self.assertTrue(retry_pending(asked, datetime(2026, 10, 13, 9, 0, 0)))
		self.assertFalse(retry_pending(asked, datetime(2026, 10, 13, 10, 0, 1)))


if __name__ == "__main__":
	unittest.main()
