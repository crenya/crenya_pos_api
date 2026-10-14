"""The expenses engine (moved down from crenya_restaurant, 0.13.0): the VAT split check, the
outlet fields, the product seam and the Z rows. Pure: no site.

The restaurant's tests of these parts moved here with the code; the wording tests run over both
products.
"""

import unittest
from decimal import Decimal

from crenya_pos_api.expenses import booking
from crenya_pos_api.expenses.fields import EXPENSE_PROFILE_FIELDS
from crenya_pos_api.expenses.product import (
	RESTAURANT,
	RETAIL,
	ExpenseProduct,
	product_for,
	register_product,
)
from crenya_pos_api.expenses.reports import expense_label
from crenya_pos_api.sync.approvals import APPROVAL_ACTIONS

VAT = "VAT 5% - _TCP"
CGST = "CGST - _TCP"
SGST = "SGST - _TCP"


def data(**over):
	"""What the till said of an expense (the payload as `validate_expense_payload` reads it): OMR."""
	d = {
		"lines": [{"category": "Gas", "description": None, "amount": Decimal("10.000")}],
		"taxes": [{"account_head": VAT, "rate": Decimal("5"), "amount": Decimal("0.500")}],
		"net": Decimal("10.000"),
		"tax": Decimal("0.500"),
		"total": Decimal("10.500"),
	}
	d.update(over)
	return d


def omr(value):
	return Decimal(value)


class TestSplitCheck(unittest.TestCase):
	def test_split_check_omr(self):
		self.assertIsNone(booking.split_check(data(), [VAT], 3))

	def test_the_sums_must_match_to_the_minor_unit(self):
		self.assertIn("total", booking.split_check(data(total=omr("10.501")), [VAT], 3))
		self.assertIn("net", booking.split_check(data(net=omr("9.999"), total=omr("10.499")), [VAT], 3))
		taxes = [{"account_head": VAT, "rate": omr("5"), "amount": omr("0.499")}]
		self.assertIn("tax", booking.split_check(data(taxes=taxes), [VAT], 3))
		# more decimals than the currency has
		problem = booking.split_check(
			data(
				net=omr("10.0001"),
				total=omr("10.5001"),
				lines=[{"category": "Gas", "amount": omr("10.0001")}],
			),
			[VAT],
			3,
		)
		self.assertIn("decimals", problem)

	def test_tax_rows_are_the_templates_heads(self):
		self.assertIn("account", booking.split_check(data(), [CGST], 3))
		self.assertIn("account", booking.split_check(data(), [VAT, CGST], 3))

	def test_vat_off_has_no_tax(self):
		off = data(taxes=[], tax=omr("0.000"), total=omr("10.000"))
		self.assertIsNone(booking.split_check(off, None, 3))
		self.assertIn("tax", booking.split_check(data(), None, 3))

	def test_a_two_row_template(self):
		taxes = [
			{"account_head": CGST, "rate": omr("2.5"), "amount": omr("0.250")},
			{"account_head": SGST, "rate": omr("2.5"), "amount": omr("0.250")},
		]
		three = data(
			taxes=taxes,
			total=omr("10.501"),
			net=omr("10.001"),
			lines=[{"category": "Gas", "amount": omr("10.001")}],
		)
		self.assertIsNone(booking.split_check(three, [CGST, SGST], 3))
		# two decimals (INR)
		two = data(
			taxes=[
				{"account_head": CGST, "rate": omr("9"), "amount": omr("7.63")},
				{"account_head": SGST, "rate": omr("9"), "amount": omr("7.62")},
			],
			total=omr("100.00"),
			net=omr("84.75"),
			tax=omr("15.25"),
			lines=[{"category": "Gas", "amount": omr("84.75")}],
		)
		self.assertIsNone(booking.split_check(two, [CGST, SGST], 2))


class TestFields(unittest.TestCase):
	def test_the_outlet_fields(self):
		fields = {f["fieldname"]: f for f in EXPENSE_PROFILE_FIELDS}
		self.assertEqual(EXPENSE_PROFILE_FIELDS[0]["fieldtype"], "Section Break")
		self.assertEqual(EXPENSE_PROFILE_FIELDS[0]["label"], "Expenses")
		# after the last Store controls field
		self.assertEqual(EXPENSE_PROFILE_FIELDS[0]["insert_after"], APPROVAL_ACTIONS[-1][1])
		self.assertEqual(
			(fields["crenya_expenses_enabled"]["fieldtype"], fields["crenya_expenses_enabled"]["default"]),
			("Check", "0"),
		)
		self.assertEqual(fields["crenya_expense_cash_supplier"]["options"], "Supplier")
		self.assertEqual(fields["crenya_expense_supplier_group"]["options"], "Supplier Group")
		self.assertEqual(
			fields["crenya_expense_tax_template"]["options"], "Purchase Taxes and Charges Template"
		)
		self.assertEqual(fields["crenya_expense_add_needs_pin"]["default"], "0")
		self.assertTrue(all("depends_on" not in f for f in EXPENSE_PROFILE_FIELDS))

	def test_expense_fields_defined_once(self):
		names = [f["fieldname"] for f in EXPENSE_PROFILE_FIELDS]
		self.assertEqual(len(names), len(set(names)))

	def test_the_settings_constants_name_defined_fields(self):
		names = {f["fieldname"] for f in EXPENSE_PROFILE_FIELDS}
		for constant in (
			booking.ENABLED_FIELD,
			booking.CASH_SUPPLIER_FIELD,
			booking.SUPPLIER_GROUP_FIELD,
			booking.TAX_TEMPLATE_FIELD,
			booking.ADD_PIN_FIELD,
		):
			self.assertIn(constant, names)


class TestProducts(unittest.TestCase):
	def test_messages_name_the_products_settings_page(self):
		self.assertIn("Shops → Settings → Expenses", booking.off_reason(RETAIL))
		self.assertIn("Restaurants → Settings → Expenses", booking.off_reason(RESTAURANT))
		self.assertIn("Shops → Settings → Expenses", booking.no_cash_supplier_reason(RETAIL))
		# the restaurant's English text is what it was before the move
		self.assertEqual(
			booking.off_reason(RESTAURANT),
			"Expenses are switched off for this outlet (Restaurants → Settings → Expenses)",
		)
		self.assertEqual(
			booking.no_cash_supplier_reason(RESTAURANT),
			"Set this outlet's Cash Supplier (Restaurants → Settings → Expenses)",
		)

	def test_the_engine_names_no_product(self):
		source = open(booking.__file__, encoding="utf-8").read()
		self.assertNotIn("Restaurants →", source)
		self.assertNotIn("from crenya_restaurant", source)
		self.assertNotIn("import crenya_restaurant", source)

	def test_the_product_of_a_device_type(self):
		self.assertEqual(product_for(None), RETAIL)
		self.assertEqual(product_for("till"), RETAIL)
		self.assertEqual(product_for("restaurant_pos"), RESTAURANT)
		self.assertIsNone(product_for("kds"))
		self.assertEqual(
			(RETAIL.capability, RESTAURANT.capability), ("retail_expenses", "restaurant_expenses")
		)
		self.assertEqual(
			(RETAIL.bootstrap_key, RESTAURANT.bootstrap_key), ("expenses", "restaurant.expenses")
		)

	def test_a_registered_product_replaces_the_one_with_its_key(self):
		own = ExpenseProduct(
			key="restaurant",
			device_types=("restaurant_pos",),
			bootstrap_key="restaurant.expenses",
			capability="restaurant_expenses",
			settings_hint="Restaurants → Settings → Expenses",
			approver_roles=lambda ctx: ("Restaurant Manager",),
			outlet_users=lambda profile: set(),
		)
		register_product(own)
		try:
			self.assertEqual(product_for("restaurant_pos").approver_roles(None), ("Restaurant Manager",))
		finally:
			register_product(RESTAURANT)
		self.assertEqual(product_for("restaurant_pos"), RESTAURANT)


class TestZRows(unittest.TestCase):
	def test_expense_label(self):
		row = {"payee": "Gas man", "bill_no": "B-17", "categories": "Gas", "is_cash": True}
		self.assertEqual(expense_label(row), "Gas man · B-17 · Gas")
		card = {"payee": "Lulu", "bill_no": None, "categories": "Cleaning", "is_cash": False}
		self.assertEqual(expense_label({**card, "mode_of_payment": "Card"}), "Lulu · Cleaning (Card)")


if __name__ == "__main__":
	unittest.main()
