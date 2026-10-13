"""Store controls of a retail till (crenya_pos_api/sync/approvals.py): the approval actions, the
manager role and the bootstrap's `approvals`, and the shift's optional `store_controls`, on the
pure code (no site)."""

import unittest
from decimal import Decimal

import frappe

from crenya_pos_api.setup.install import CUSTOM_FIELDS
from crenya_pos_api.sync import approvals
from crenya_pos_api.sync.approvals import APPROVAL_ACTIONS, APPROVER_ROLE, RETAIL_PRIVILEGES
from crenya_pos_api.sync.errors import VALIDATION, SyncError
from crenya_pos_api.sync.validation import validate_store_controls


def profile(**values):
	return frappe._dict(values)


class TestApprovalActions(unittest.TestCase):
	def test_the_actions_are_one_table(self):
		self.assertEqual(APPROVER_ROLE, "Crenya POS Manager")
		self.assertEqual(
			[key for key, *_ in APPROVAL_ACTIONS],
			[
				"price_change",
				"discount_over_limit",
				"line_void",
				"bill_clear",
				"return",
				"open_return",
				"no_sale",
				"stock_mode",
				"settings",
			],
		)
		for key, field, label, label_ar in APPROVAL_ACTIONS:
			self.assertEqual(field, f"crenya_approve_{key}")
			self.assertTrue(label and label_ar, key)
		self.assertEqual(
			RETAIL_PRIVILEGES,
			{"cashier": ("Crenya POS User",), "manager": ("Crenya POS User", "Crenya POS Manager")},
		)

	def test_profile_fields_come_from_the_table(self):
		fields = {field["fieldname"]: field for field in CUSTOM_FIELDS["POS Profile"]}
		self.assertEqual(fields["crenya_max_discount_percent"]["fieldtype"], "Percent")
		for _key, fieldname, label, _label_ar in APPROVAL_ACTIONS:
			self.assertEqual(fields[fieldname]["fieldtype"], "Check")
			self.assertEqual(fields[fieldname]["default"], "1")
			self.assertEqual(fields[fieldname]["label"], label)
		self.assertEqual(fields["crenya_store_controls_section"]["fieldtype"], "Section Break")
		self.assertEqual(fields["crenya_store_controls_section"]["label"], "Store controls")

	def test_bootstrap_lists_required_actions_in_order(self):
		every = {field: 1 for _key, field, *_ in APPROVAL_ACTIONS}
		self.assertEqual(
			approvals.approvals_bootstrap(profile(**every))["required"], [key for key, *_ in APPROVAL_ACTIONS]
		)
		some = {**every, "crenya_approve_price_change": 0, "crenya_approve_no_sale": 0}
		answer = approvals.approvals_bootstrap(profile(**some))
		self.assertEqual(
			answer["required"],
			[
				"discount_over_limit",
				"line_void",
				"bill_clear",
				"return",
				"open_return",
				"stock_mode",
				"settings",
			],
		)
		self.assertEqual(answer["approver_roles"], ["Crenya POS Manager"])
		# a profile without the fields (not migrated yet) requires nothing: today's behaviour
		self.assertEqual(approvals.approvals_bootstrap(profile())["required"], [])

	def test_blank_max_discount_is_null(self):
		for blank in (None, 0, 0.0, ""):
			self.assertIsNone(
				approvals.approvals_bootstrap(profile(crenya_max_discount_percent=blank))[
					"max_discount_percent"
				]
			)
		self.assertEqual(approvals.max_discount_percent(profile(crenya_max_discount_percent=10.0)), "10")
		self.assertEqual(approvals.max_discount_percent(profile(crenya_max_discount_percent=12.5)), "12.5")


class TestStoreControlsPayload(unittest.TestCase):
	def test_absent_is_zeros(self):
		zeros = {
			"line_voids": {"count": 0, "amount": Decimal(0)},
			"bill_clears": {"count": 0, "amount": Decimal(0)},
			"no_sales": 0,
			"approvals": 0,
		}
		self.assertEqual(validate_store_controls(None), zeros)
		self.assertEqual(validate_store_controls({}), zeros)

	def test_values_are_normalized(self):
		data = validate_store_controls(
			{
				"line_voids": {"count": 3, "amount": "1.250"},
				"bill_clears": {"count": "1", "amount": "4.000"},
				"no_sales": 2,
				"approvals": 5,
			}
		)
		self.assertEqual(data["line_voids"], {"count": 3, "amount": Decimal("1.250")})
		self.assertEqual(data["bill_clears"], {"count": 1, "amount": Decimal("4.000")})
		self.assertEqual((data["no_sales"], data["approvals"]), (2, 5))

	def test_bad_values_are_validation_errors(self):
		for bad in (
			[],
			{"line_voids": []},
			{"line_voids": {"count": -1, "amount": "0"}},
			{"line_voids": {"count": 1, "amount": "-0.5"}},
			{"line_voids": {"count": 1, "amount": "abc"}},
			{"bill_clears": {"count": 1.5, "amount": "0"}},
			{"no_sales": -1},
			{"approvals": "many"},
		):
			with self.subTest(bad=bad), self.assertRaises(SyncError) as caught:
				validate_store_controls(bad)
			self.assertEqual(caught.exception.code, VALIDATION)


if __name__ == "__main__":
	unittest.main()
