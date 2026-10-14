"""The expense push handler (moved down from crenya_restaurant, 0.13.0) and the registry's
tolerance of an older crenya_restaurant. Pure: no site (hooks are faked).

crenya_restaurant 0.15.6 (live) still hooks `POS Expense`, `expense_category` and `expense_supplier`
from its own hooks.py; this app now has them built in. Deploying this app first must not break it.
"""

import unittest
from decimal import Decimal
from unittest import mock

from crenya_pos_api.expenses import sync as expense_sync
from crenya_pos_api.expenses.sync import (
	OPERATIONS,
	ExpenseAggregate,
	approver_allowed,
	validate_expense_payload,
)
from crenya_pos_api.sync import registry
from crenya_pos_api.sync.errors import PERMISSION, VALIDATION, SyncError
from crenya_pos_api.sync.pull import ENTITIES, ExpenseCategorySpec, ExpenseSupplierSpec
from crenya_pos_api.sync.validation import AGGREGATE_TYPES
from crenya_pos_api.tests import extension_dummy as dummy

VAT = "VAT 5% - _TCP"


def payload(**over):
	p = {
		"local_id": "EXP-D01-1",
		"pos_profile": "Outlet",
		"company": "Company",
		"shift_local_id": "SH-1",
		"business_date": "2026-10-12",
		"user": "cashier@example.com",
		"approved_by": None,
		"edited_at": "2026-10-12T09:15:00Z",
		"supplier": None,
		"payee_name": "Gas man",
		"bill_no": "B-17",
		"bill_date": "2026-10-12",
		"mode_of_payment": "Cash",
		"currency": "OMR",
		"lines": [{"category": "Gas", "description": None, "amount": "10.000"}],
		"tax_template": "Purchase VAT 5% - _TCP",
		"taxes": [{"account_head": VAT, "rate": "5", "amount": "0.500"}],
		"net": "10.000",
		"tax": "0.500",
		"total": "10.500",
		"notes": None,
		"reason": None,
	}
	p.update(over)
	return p


def refused(test, operation, raw, code=VALIDATION):
	with test.assertRaises(SyncError) as caught:
		validate_expense_payload(operation, raw)
	test.assertEqual(caught.exception.code, code)
	return caught.exception.message


class TestPayload(unittest.TestCase):
	def test_a_submit_is_read_as_decimals(self):
		data = validate_expense_payload("submit", payload())
		self.assertEqual(data["local_id"], "EXP-D01-1")
		self.assertEqual(
			(data["net"], data["tax"], data["total"]),
			(Decimal("10.000"), Decimal("0.500"), Decimal("10.500")),
		)
		self.assertEqual(
			data["lines"][0], {"category": "Gas", "description": None, "amount": Decimal("10.000")}
		)
		self.assertEqual(data["taxes"][0]["account_head"], VAT)
		self.assertEqual(str(data["business_date"]), "2026-10-12")
		self.assertEqual(data["edited_at"].isoformat(), "2026-10-12T09:15:00+00:00")
		self.assertEqual(OPERATIONS, ("submit", "amend", "cancel"))

	def test_what_is_refused(self):
		self.assertIn("operation", refused(self, "delete", payload()))
		self.assertIn("lines", refused(self, "submit", payload(lines=[])))
		# money is a decimal string, never a float
		self.assertIn("total", refused(self, "submit", payload(total=10.5)))
		self.assertIn("amount", refused(self, "submit", payload(lines=[{"category": "Gas", "amount": "0"}])))
		self.assertIn("business_date", refused(self, "submit", payload(business_date="12/10/2026")))
		self.assertIn("edited_at", refused(self, "submit", payload(edited_at="2026-10-12 09:15")))
		self.assertIn("mode_of_payment", refused(self, "submit", payload(mode_of_payment=None)))
		self.assertIn("category", refused(self, "submit", payload(lines=[{"amount": "1.000"}])))

	def test_an_edit_or_a_delete_needs_an_approver_and_a_delete_its_reason(self):
		self.assertIn("approved_by", refused(self, "amend", payload()))
		self.assertIn("approved_by", refused(self, "cancel", payload(reason="Wrong amount")))
		self.assertIn("reason", refused(self, "cancel", payload(approved_by="m@example.com")))
		self.assertIn("reason", refused(self, "cancel", payload(approved_by="m@example.com", reason="no")))
		self.assertIn(
			"reason", refused(self, "cancel", payload(approved_by="m@example.com", reason="x" * 141))
		)
		data = validate_expense_payload("cancel", payload(approved_by="m@example.com", reason="Typed twice"))
		self.assertEqual(data["reason"], "Typed twice")
		self.assertEqual(
			validate_expense_payload("amend", payload(approved_by="m@example.com"))["approved_by"],
			"m@example.com",
		)

	def test_find_by_local_id_never_matches(self):
		# amend and cancel share the submit's local_id: only event_id idempotency applies there, and
		# submit itself answers the expense it already has
		self.assertIsNone(ExpenseAggregate().find_by_local_id("EXP-D01-1"))
		self.assertEqual(ExpenseAggregate.aggregate_type, "POS Expense")
		self.assertEqual(ExpenseAggregate.operations, OPERATIONS)


class TestApprover(unittest.TestCase):
	def test_a_holder_of_a_product_role_who_works_at_the_outlet(self):
		retail = ("Crenya POS Manager",)
		self.assertTrue(approver_allowed(["Crenya POS Manager"], "m@x", set(), retail))
		self.assertTrue(approver_allowed(["Crenya POS Manager"], "m@x", {"m@x"}, retail))
		self.assertFalse(approver_allowed(["Crenya POS Manager"], "m@x", {"o@x"}, retail))
		self.assertFalse(approver_allowed(["Crenya POS User"], "m@x", set(), retail))
		# the roles are the product's: a restaurant manager is no retail approver
		self.assertFalse(approver_allowed(["Restaurant Manager"], "m@x", set(), retail))
		self.assertTrue(approver_allowed(["Restaurant Manager"], "m@x", set(), ("Restaurant Manager",)))

	def test_an_unknown_device_type_is_no_product(self):
		ctx = mock.Mock(device_type="kds")
		with self.assertRaises(SyncError) as caught:
			expense_sync.product_of_device(ctx)
		self.assertEqual(caught.exception.code, PERMISSION)
		self.assertIn("kds", caught.exception.message)


# crenya_restaurant 0.15.6's hooks.py, the three entries that moved (the paths are NOT importable
# here: a registry that tried to load them would raise)
RESTAURANT_0_15_6 = {
	"crenya_pos_pull_entities": {
		"expense_category": "crenya_restaurant.sync.expenses.ExpenseCategorySpec",
		"expense_supplier": "crenya_restaurant.sync.expenses.ExpenseSupplierSpec",
	},
	"crenya_pos_aggregates": {
		"POS Expense": "crenya_restaurant.sync.expenses.ExpenseAggregate",
	},
}


class TestExpensePullsForAnOffOutlet(unittest.TestCase):
	"""A retail outlet with Expenses off is told of no record and no deleted record either."""

	def ctx(self, device_type, enabled):
		profile = {"crenya_expenses_enabled": enabled}
		return mock.Mock(
			device_type=device_type,
			profile=mock.Mock(get=lambda key, default=None: profile.get(key, default)),
		)

	def test_no_tombstones_while_a_retail_outlet_has_expenses_off(self):
		for spec in (ExpenseCategorySpec(), ExpenseSupplierSpec()):
			self.assertFalse(spec.sends_tombstones(self.ctx("till", 0)))
			self.assertTrue(spec.sends_tombstones(self.ctx("till", 1)))
			# a restaurant outlet's pulls are what they were
			self.assertTrue(spec.sends_tombstones(self.ctx("restaurant_pos", 0)))

	def test_other_entities_keep_theirs(self):
		self.assertTrue(ENTITIES["item"].sends_tombstones(self.ctx("till", 0)))


class TestOlderRestaurant(unittest.TestCase):
	def setUp(self):
		registry._logged_duplicates.clear()

	def test_hook_entry_duplicating_a_builtin_is_ignored(self):
		with dummy.fake_hooks(RESTAURANT_0_15_6, site_ready=True):
			aggregates = registry.aggregates()
			entities = registry.entities()
			# nothing raised, nothing doubled: the built-ins answer
			self.assertEqual(tuple(aggregates), AGGREGATE_TYPES)
			self.assertIsInstance(aggregates["POS Expense"], ExpenseAggregate)
			self.assertEqual(list(entities), list(ENTITIES))
			self.assertIsInstance(entities["expense_category"], ExpenseCategorySpec)
			self.assertIsInstance(entities["expense_supplier"], ExpenseSupplierSpec)
			self.assertIs(registry.handler("POS Expense"), aggregates["POS Expense"])
			self.assertIs(registry.handler_for_doctype("POS Expense"), aggregates["POS Expense"])

	def test_the_other_entries_of_the_same_app_still_load(self):
		hooks = {
			"crenya_pos_pull_entities": {
				**RESTAURANT_0_15_6["crenya_pos_pull_entities"],
				dummy.ENTITY: "crenya_pos_api.tests.extension_dummy.BrandSpec",
			},
			"crenya_pos_aggregates": {
				**RESTAURANT_0_15_6["crenya_pos_aggregates"],
				dummy.TASK_AGGREGATE: "crenya_pos_api.tests.extension_dummy.TaskAggregate",
			},
		}
		with dummy.fake_hooks(hooks, site_ready=True):
			self.assertEqual(list(registry.entities()), [*ENTITIES, dummy.ENTITY])
			self.assertEqual(list(registry.aggregates()), [*AGGREGATE_TYPES, dummy.TASK_AGGREGATE])

	def test_an_ignored_entry_is_logged_once(self):
		logger = mock.Mock()
		with mock.patch("frappe.logger", return_value=logger):
			for _ in range(3):
				with dummy.fake_hooks(RESTAURANT_0_15_6, site_ready=True):
					registry.aggregates()
					registry.entities()
		# three requests, three entries, three lines
		self.assertEqual(logger.warning.call_count, 3)

	def test_another_builtin_still_cannot_be_replaced(self):
		hooks = {
			"crenya_pos_aggregates": {"Sales Invoice": "crenya_restaurant.sync.expenses.ExpenseAggregate"}
		}
		with dummy.fake_hooks(hooks, site_ready=True):
			with self.assertRaises(registry.ExtensionHookError):
				registry.aggregates()
		hooks = {"crenya_pos_pull_entities": {"item": "crenya_restaurant.sync.expenses.ExpenseCategorySpec"}}
		with dummy.fake_hooks(hooks, site_ready=True):
			with self.assertRaises(registry.ExtensionHookError):
				registry.entities()


if __name__ == "__main__":
	unittest.main()
