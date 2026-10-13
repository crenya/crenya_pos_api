"""A closed shift's store controls (audit P0 #5): the till's line voids, bill clears, no-sale
openings and approvals, counted for the Z report. A 0.8.0 till sends none and its shift is
accepted with zeros.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_shift
"""

import frappe

from crenya_pos_api.tests.sync_case import SyncCase

SHIFT = "Crenya POS Shift"
STORE_CONTROL_FIELDS = (
	"line_voids_count",
	"line_voids_amount",
	"bill_clears_count",
	"bill_clears_amount",
	"no_sales_count",
	"approvals_count",
)


class TestShiftStoreControls(SyncCase):
	def push_shift(self, **overrides):
		return self.push_one(self.event(self.shift_payload(**overrides), "Crenya POS Shift"))

	def stored(self, name):
		return frappe.db.get_value(SHIFT, name, list(STORE_CONTROL_FIELDS), as_dict=True)

	def test_shift_store_controls_saved(self):
		result = self.assertOk(
			self.push_shift(
				store_controls={
					"line_voids": {"count": 3, "amount": "1.250"},
					"bill_clears": {"count": 1, "amount": "4.000"},
					"no_sales": 2,
					"approvals": 5,
				}
			)
		)
		row = self.stored(result["name"])
		self.assertEqual((row.line_voids_count, row.bill_clears_count), (3, 1))
		self.assertAlmostEqual(row.line_voids_amount, 1.25)
		self.assertAlmostEqual(row.bill_clears_amount, 4.0)
		self.assertEqual((row.no_sales_count, row.approvals_count), (2, 5))

	def test_old_till_shift_without_store_controls(self):
		# Review Focus 1: a 0.8.0 till sends no `store_controls`; its shift is accepted, all zeros
		payload = self.shift_payload()
		self.assertNotIn("store_controls", payload)
		result = self.assertOk(self.push_one(self.event(payload, "Crenya POS Shift")))
		row = self.stored(result["name"])
		self.assertEqual([row[field] for field in STORE_CONTROL_FIELDS], [0] * len(STORE_CONTROL_FIELDS))

	def test_store_controls_bad_amount_refused(self):
		# OMR has 3 decimals: a fourth is refused, nothing is stored
		payload = self.shift_payload(
			store_controls={"line_voids": {"count": 1, "amount": "1.2345"}, "no_sales": 0, "approvals": 0}
		)
		result = self.assertError(self.push_one(self.event(payload, "Crenya POS Shift")), "validation", False)
		self.assertIn("line_voids.amount", result["error"]["message"])
		self.assertFalse(frappe.db.exists(SHIFT, payload["local_id"]))
