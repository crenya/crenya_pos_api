"""Shops web admin, the pure parts of credit and expenses (retail P1 task 12): the credit limit a
manager types, the credit setup check, and the Z report's ON ACCOUNT and EXPENSES sections at OMR's
3 decimals. No site."""

import unittest
from decimal import Decimal

import frappe

from crenya_pos_api.api.web_admin.credit import _row, parse_limit
from crenya_pos_api.api.web_admin.day_close import Z_LABELS, expenses_section, on_account_section, z_sections
from crenya_pos_api.web.retail_checks import credit_checks


class TestParseLimit(unittest.TestCase):
	def test_a_limit_is_a_decimal_at_the_currencys_decimals(self):
		self.assertEqual(parse_limit("250.500", 3), Decimal("250.500"))
		self.assertEqual(parse_limit("100", 3), Decimal("100"))
		self.assertEqual(parse_limit("12.34", 2), Decimal("12.34"))

	def test_zero_or_blank_is_no_limit(self):
		for value in (None, "", "  ", "0", "0.000"):
			self.assertIsNone(parse_limit(value, 3), msg=repr(value))

	def test_what_is_refused(self):
		for value, fragment in (
			("10.0001", "more than 3 decimals"),
			("12.345", "more than 2 decimals"),
			("-5", "negative"),
			("abc", "credit_limit"),
			(12.5, "credit_limit"),
		):
			precision = 2 if value == "12.345" else 3
			with self.assertRaises(ValueError, msg=repr(value)) as caught:
				parse_limit(value, precision)
			self.assertIn(fragment, str(caught.exception))


class TestCreditRow(unittest.TestCase):
	def customer(self):
		return frappe._dict(name="CUST-1", customer_name="Ahmed", modified="2026-10-14 09:00:00.000000")

	def test_over_the_limit_is_flagged_and_nothing_is_available(self):
		row = _row(self.customer(), Decimal("100.000"), Decimal("150.000"), 2, 3)
		self.assertEqual(
			(row["credit_limit"], row["outstanding"], row["available"], row["over_limit"]),
			("100.000", "150.000", "0.000", True),
		)
		self.assertEqual((row["customer"], row["name"], row["open_invoices"]), ("CUST-1", "Ahmed", 2))

	def test_under_the_limit(self):
		row = _row(self.customer(), Decimal("500.000"), Decimal("420.000"), 1, 3)
		self.assertEqual((row["available"], row["over_limit"]), ("80.000", False))

	def test_exactly_at_the_limit_is_not_over(self):
		row = _row(self.customer(), Decimal("100.000"), Decimal("100.000"), 1, 3)
		self.assertEqual((row["available"], row["over_limit"]), ("0.000", False))

	def test_no_limit_has_no_limit_and_never_over(self):
		row = _row(self.customer(), None, Decimal("80.000"), 1, 3)
		self.assertEqual(
			(row["credit_limit"], row["available"], row["over_limit"], row["outstanding"]),
			(None, None, False, "80.000"),
		)

	def test_a_customer_in_credit_shows_a_negative_balance(self):
		row = _row(self.customer(), None, Decimal("-30.000"), 0, 3)
		self.assertEqual(row["outstanding"], "-30.000")


class TestCreditChecks(unittest.TestCase):
	def test_none_while_the_switch_is_off(self):
		self.assertEqual(credit_checks({"enabled": False, "company": "Co", "receivable_account": None}), [])

	def test_credit_setup_check_names_missing_receivable_account(self):
		(row,) = credit_checks({"enabled": True, "company": "Co", "receivable_account": None})
		self.assertEqual((row["key"], row["status"]), ("credit_receivable", "missing"))
		self.assertIn("receivable account", row["label"])
		self.assertIn("Company Co", row["fix"])

	def test_ok_with_the_account(self):
		(row,) = credit_checks({"enabled": True, "company": "Co", "receivable_account": "Debtors - C"})
		self.assertEqual((row["status"], row["detail"], row["fix"]), ("ok", "Debtors - C", None))


# float 10.000, cash sales 50.000, cash received 15.250, cash paid out 7.350, card paid out 4.000:
# expected cash 67.900; the card expects its sales (20.000), counted as expected
SHIFT = {
	"sales_count": 7,
	"sales_total": 70.0,
	"tax_total": 3.333,
	"net_total": 66.667,
	"returns_count": 0,
	"returns_total": 0,
	"opening_float": 10.0,
	"expense_count": 2,
	"on_account_sales_count": 2,
	"on_account_sales_amount": 90.0,
	"on_account_returned_amount": 0,
	"on_account_received_count": 1,
	"on_account_received_amount": 15.25,
	"payments": [
		{
			"mode_of_payment": "Cash",
			"expected": 67.9,
			"counted": 67.9,
			"difference": 0,
			"received": 15.25,
			"paid_out": 7.35,
		},
		{
			"mode_of_payment": "Card",
			"expected": 20.0,
			"counted": 20.0,
			"difference": 0,
			"received": 0,
			"paid_out": 4.0,
		},
	],
}
EXPENSES = [
	{
		"payee": "Gas man",
		"bill_no": "B-17",
		"categories": "Gas",
		"mode_of_payment": "Cash",
		"is_cash": True,
		"amount": 7.35,
	},
	{
		"payee": "Cleaner",
		"bill_no": None,
		"categories": "Cleaning",
		"mode_of_payment": "Card",
		"is_cash": False,
		"amount": 4.0,
	},
]


def rows_of(sections, key):
	return next(s for s in sections if s["key"] == key)["rows"]


class TestZOnAccountAndExpenses(unittest.TestCase):
	def test_z_sections_on_account_and_expenses(self):
		sections = z_sections(SHIFT, 3, "VAT", None, expenses=EXPENSES)
		self.assertEqual(
			[s["key"] for s in sections],
			["sales", "payments", "returns", "store_controls", "paid_out", "on_account", "expenses"],
		)
		payments = {row["label_en"]: row["value"] for row in rows_of(sections, "payments")}
		# the drawer counts what the till expected: 67.900, no difference
		self.assertEqual(payments["Cash expected"], "67.900")
		self.assertEqual(payments["Cash counted"], "67.900")
		self.assertEqual(payments["Cash difference"], "0.000")
		self.assertEqual(payments["Card expected"], "20.000")
		on_account = [(r["label_en"], r["value"], r["kind"]) for r in rows_of(sections, "on_account")]
		self.assertEqual(
			on_account,
			[
				("Sales on account", "2", "count"),
				("Sold on account", "90.000", "money"),
				("Returned to the account", "0.000", "money"),
				("Payments received", "1", "count"),
				("Received on account", "15.250", "money"),
				("Cash received", "15.250", "money"),
			],
		)
		paid_out = [(r["label_en"], r["value"]) for r in rows_of(sections, "paid_out")]
		self.assertEqual(
			paid_out, [("Expenses", "2"), ("Cash paid out", "7.350"), ("Card paid out", "4.000")]
		)
		expenses = [(r["label_en"], r["value"]) for r in rows_of(sections, "expenses")]
		self.assertEqual(
			expenses,
			[
				("Gas man · B-17 · Gas", "7.350"),
				("Cleaner · Cleaning (Card)", "4.000"),
				("Total", "11.350"),
			],
		)

	def test_every_row_has_arabic_and_a_kind(self):
		for section in z_sections(SHIFT, 3, "VAT", None, expenses=EXPENSES):
			self.assertTrue(section["title_ar"])
			for row in section["rows"]:
				self.assertTrue(row["label_ar"])
				self.assertIn(row["kind"], ("money", "count"))

	def test_a_shift_without_credit_or_expenses_has_neither_section(self):
		plain = {key: value for key, value in SHIFT.items() if not key.startswith("on_account")}
		plain["payments"] = [dict(p, received=0) for p in SHIFT["payments"]]
		self.assertIsNone(on_account_section(plain, 3))
		self.assertIsNone(expenses_section([], 3))
		self.assertIsNone(expenses_section(None, 3))
		self.assertEqual(
			[s["key"] for s in z_sections(plain, 3, "VAT")],
			["sales", "payments", "returns", "store_controls", "paid_out"],
		)

	def test_received_only_shows_the_on_account_section(self):
		shift = dict(SHIFT, on_account_sales_count=0, on_account_sales_amount=0)
		shift = {
			k: v
			for k, v in shift.items()
			if k not in ("on_account_received_count", "on_account_received_amount")
		}
		self.assertEqual(rows_of([on_account_section(shift, 3)], "on_account")[-1]["value"], "15.250")

	def test_two_decimals(self):
		rows = rows_of(z_sections(SHIFT, 2, "GST", None, expenses=EXPENSES), "expenses")
		self.assertEqual(rows[-1]["value"], "11.35")

	def test_labels_exist(self):
		for key in ("on_account", "expenses", "expenses_total", "received_mode"):
			self.assertTrue(all(Z_LABELS[key]))


if __name__ == "__main__":
	unittest.main()
