"""Shops web admin: Z reports (crenya_pos_api/api/web_admin/day_close.py) of shifts a till pushed.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_web_admin_day_close
"""

import frappe

from crenya_pos_api.api import web_admin
from crenya_pos_api.sync.approvals import APPROVER_ROLE
from crenya_pos_api.sync.context import POS_USER_ROLE
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import SyncCase
from crenya_pos_api.tests.web_admin_case import (
	CASHIER,
	MANAGER,
	SHOP2,
	SHOP2_CASHIER,
	SHOP2_MANAGER,
	ensure_shop2,
	ensure_user,
)

# the shifts' dates (opened 2026-09-29 04:00 UTC), with a day either side for the site's time zone
FROM, TO = "2026-09-28", "2026-09-30"


def section(answer, key):
	return next(s for s in answer["sections"] if s["key"] == key)


class TestWebAdminDayClose(SyncCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(MANAGER, [POS_USER_ROLE, APPROVER_ROLE])
		ensure_user(CASHIER, [POS_USER_ROLE])
		ensure_user(SHOP2_CASHIER, [POS_USER_ROLE])
		ensure_user(SHOP2_MANAGER, [POS_USER_ROLE, APPROVER_ROLE])
		ensure_shop2([SHOP2_CASHIER, SHOP2_MANAGER])
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def push_shift(self, **overrides):
		frappe.set_user("Administrator")
		return self.assertOk(self.push_one(self.event(self.shift_payload(**overrides), "Crenya POS Shift")))[
			"name"
		]

	def test_day_close_has_store_controls_section(self):
		shift = self.push_shift(
			store_controls={
				"line_voids": {"count": 3, "amount": "1.250"},
				"bill_clears": {"count": 1, "amount": "4.000"},
				"no_sales": 2,
				"approvals": 5,
			}
		)
		frappe.set_user(MANAGER)
		answer = web_admin.day_close(shift)
		self.assertEqual(
			[s["key"] for s in answer["sections"]],
			["sales", "payments", "returns", "store_controls", "paid_out"],
		)
		controls = section(answer, "store_controls")["rows"]
		self.assertEqual(
			[(r["value"], r["kind"]) for r in controls],
			[
				("3", "count"),
				("1.250", "money"),
				("1", "count"),
				("4.000", "money"),
				("2", "count"),
				("5", "count"),
			],
		)
		self.assertTrue(all(r["label_ar"] for s in answer["sections"] for r in s["rows"]))
		self.assertEqual(answer["outlet"], self.profile.name)
		self.assertEqual(answer["company"], fixtures.COMPANY)
		self.assertEqual(answer["currency"]["code"], fixtures.CURRENCY)
		self.assertEqual(answer["currency"]["precision"], 3)
		# the till counted 2 bills, none reached ERPNext
		self.assertEqual(answer["missing_bills"], 2)

	def test_day_close_three_decimal_strings(self):
		shift = self.push_shift()
		frappe.set_user(MANAGER)
		answer = web_admin.day_close(shift)
		sales = {r["label_en"]: r["value"] for r in section(answer, "sales")["rows"]}
		self.assertEqual(sales["Sales"], "2.400")
		self.assertEqual(sales["Bills"], "2")
		payments = {r["label_en"]: r["value"] for r in section(answer, "payments")["rows"]}
		self.assertEqual(payments["Opening float"], "20.000")
		self.assertEqual(payments[f"{fixtures.CASH} difference"], "-0.100")

		listed = web_admin.day_closes(FROM, TO, self.profile.name)
		row = next(r for r in listed["rows"] if r["shift"] == shift)
		self.assertEqual(row["sales"], "2.400")
		self.assertEqual(row["cash_expected"], "22.400")
		self.assertEqual(row["cash_counted"], "22.300")
		self.assertEqual(row["cash_difference"], "-0.100")
		self.assertEqual(row["provider_sales"], "0.000")
		self.assertEqual(row["device_name"], "Till 1")
		self.assertEqual(listed["currency"]["code"], fixtures.CURRENCY)
		for key in ("company", "tax_label", "time_zone", "generated_at"):
			self.assertIn(key, listed)

	def test_reader_without_outlet_refused(self):
		shift = self.push_shift()
		frappe.set_user(CASHIER)
		with self.assertRaises(frappe.PermissionError):
			web_admin.day_close(shift)
		with self.assertRaises(frappe.PermissionError):
			web_admin.day_closes(FROM, TO, self.profile.name)
		frappe.set_user(MANAGER)
		# shop 2 lists its own staff only
		with self.assertRaises(frappe.PermissionError):
			web_admin.day_closes(FROM, TO, SHOP2)
		with self.assertRaises(frappe.PermissionError):
			web_admin.day_close("no such shift")

	def test_bad_range_refused(self):
		frappe.set_user(MANAGER)
		for first, last in (("2026-09-30", "2026-09-28"), ("29-09-2026", TO), ("2025-01-01", "2026-09-30")):
			with self.assertRaises(frappe.ValidationError):
				web_admin.day_closes(first, last, self.profile.name)
