"""A customer captured at the till (audit P0 #10): the device authorises it, so a cashier whose
only role is Crenya POS User creates it, and the customer names the till it came from.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_customer
"""

import frappe

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.tests.sync_case import SyncCase, new_id


class TestTillCustomer(SyncCase):
	def test_cashier_with_only_pos_user_creates_customer(self):
		cashier = self._cashier()
		self.assertIn("Crenya POS User", frappe.get_roles(cashier))
		self.assertFalse(frappe.has_permission("Customer", "create", user=cashier))
		device_id = new_id()
		payload = self.customer_payload("Till Customer Only POS User")
		frappe.set_user(cashier)
		try:
			device_api.register_device(
				device_id=device_id, device_name="Cashier till", pos_profile=self.profile.name
			)
			[result] = sync_api.push_batch(device_id=device_id, events=[self.event(payload, "Customer")])
		finally:
			frappe.set_user("Administrator")
		self.assertOk(result)
		customer = frappe.get_doc("Customer", result["name"])
		self.assertEqual(customer.crenya_local_id, payload["local_id"])
		self.assertEqual(customer.owner, cashier)
		comments = frappe.get_all(
			"Comment",
			filters={
				"reference_doctype": "Customer",
				"reference_name": customer.name,
				"comment_type": "Info",
			},
			pluck="content",
		)
		self.assertTrue(any(device_id in text for text in comments), comments)
		# the push created it; the cashier still cannot create one outside a push
		self.assertFalse(frappe.has_permission("Customer", "create", user=cashier))
