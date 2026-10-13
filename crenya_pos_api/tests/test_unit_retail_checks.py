"""A shop's setup checks (crenya_pos_api/web/retail_checks.py): the pure checks of the facts read
from a POS Profile. The facts are an example shop in OMR."""

import unittest

from crenya_pos_api.web.retail_checks import CHECK_KEYS, retail_checks
from crenya_pos_api.web.setup_checks import MISSING, OK, WARN


def facts(**over):
	"""A shop set up right."""
	row = {
		"company": "Muscat Shop LLC",
		"currency": "OMR",
		"price_list": {"name": "Standard Selling", "selling": 1, "currency": "OMR"},
		"tax_template": {"name": "Oman VAT 5% - MS", "company": "Muscat Shop LLC"},
		"warehouse": {"name": "Stores - MS", "is_group": 0},
		"customer": "Walk-in",
		"payment_modes": [{"mode": "Cash", "account": "Cash - MS"}],
		"disable_rounded_total": 0,
		"write_off_account": "Write Off - MS",
		"cashiers": 2,
		"approvers": 1,
		"approvals_required": True,
	}
	row.update(over)
	return row


def by_key(rows):
	return {row["key"]: row for row in rows}


class TestRetailChecks(unittest.TestCase):
	def test_all_ok_in_order(self):
		rows = retail_checks(facts())
		self.assertEqual([row["key"] for row in rows], list(CHECK_KEYS))
		self.assertEqual({row["status"] for row in rows}, {OK})
		self.assertTrue(all(row["fix"] is None for row in rows))
		self.assertTrue(all(row["label"] for row in rows))

	def test_price_list_of_another_currency_or_buying(self):
		self.assertEqual(by_key(retail_checks(facts(price_list=None)))["price_list"]["status"], MISSING)
		other = facts(price_list={"name": "AED", "selling": 1, "currency": "AED"})
		self.assertEqual(by_key(retail_checks(other))["price_list"]["status"], MISSING)
		buying = facts(price_list={"name": "Buying", "selling": 0, "currency": "OMR"})
		row = by_key(retail_checks(buying))["price_list"]
		self.assertEqual(row["status"], MISSING)
		self.assertTrue(row["fix"])

	def test_no_tax_template_warns_other_company_missing(self):
		self.assertEqual(by_key(retail_checks(facts(tax_template=None)))["tax_template"]["status"], WARN)
		other = facts(tax_template={"name": "VAT - X", "company": "Other Co"})
		self.assertEqual(by_key(retail_checks(other))["tax_template"]["status"], MISSING)

	def test_group_warehouse_missing(self):
		group = facts(warehouse={"name": "All Warehouses - MS", "is_group": 1})
		self.assertEqual(by_key(retail_checks(group))["warehouse"]["status"], MISSING)
		self.assertEqual(by_key(retail_checks(facts(warehouse=None)))["warehouse"]["status"], MISSING)

	def test_customer(self):
		self.assertEqual(by_key(retail_checks(facts(customer=None)))["default_customer"]["status"], MISSING)

	def test_payment_mode_without_account(self):
		rows = by_key(retail_checks(facts(payment_modes=[{"mode": "Cash", "account": None}])))
		self.assertEqual(rows["payment_modes"]["status"], MISSING)
		self.assertIn("Cash", rows["payment_modes"]["detail"])
		self.assertEqual(by_key(retail_checks(facts(payment_modes=[])))["payment_modes"]["status"], MISSING)

	def test_write_off_needed_only_with_rounded_total(self):
		self.assertEqual(by_key(retail_checks(facts(write_off_account=None)))["write_off"]["status"], MISSING)
		off = facts(write_off_account=None, disable_rounded_total=1)
		self.assertEqual(by_key(retail_checks(off))["write_off"]["status"], OK)

	def test_no_cashier(self):
		self.assertEqual(by_key(retail_checks(facts(cashiers=0)))["cashiers"]["status"], MISSING)

	def test_no_approver_missing_only_when_approvals_required(self):
		self.assertEqual(by_key(retail_checks(facts(approvers=0)))["approver"]["status"], MISSING)
		none_required = facts(approvers=0, approvals_required=False)
		self.assertEqual(by_key(retail_checks(none_required))["approver"]["status"], WARN)


if __name__ == "__main__":
	unittest.main()
