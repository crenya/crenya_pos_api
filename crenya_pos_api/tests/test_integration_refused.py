"""Shops web admin: refused sales and Retry (crenya_pos_api/api/web_admin/refused.py) and the
`sync_retry` pull entity (sync/retry.py).

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_refused
push_batch commits per event, so the events created here persist on the test site.
"""

import frappe

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.api import web_admin
from crenya_pos_api.sync.approvals import APPROVER_ROLE
from crenya_pos_api.sync.context import EVENT_DOCTYPE, POS_USER_ROLE
from crenya_pos_api.tests.sync_case import SyncCase, new_id
from crenya_pos_api.tests.web_admin_case import (
	CASHIER,
	MANAGER,
	SHOP2,
	SHOP2_CASHIER,
	SHOP2_MANAGER,
	ensure_shop2,
	ensure_user,
)


class TestRefusedSales(SyncCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(MANAGER, [POS_USER_ROLE, APPROVER_ROLE])
		ensure_user(CASHIER, [POS_USER_ROLE])
		ensure_user(SHOP2_CASHIER, [POS_USER_ROLE])
		ensure_user(SHOP2_MANAGER, [POS_USER_ROLE, APPROVER_ROLE])
		ensure_shop2([SHOP2_CASHIER, SHOP2_MANAGER])
		cls.other_device = new_id()
		device_api.register_device(device_id=cls.other_device, device_name="Shop 2 till", pos_profile=SHOP2)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def refused_event(self, device_id=None):
		"""A sale ERPNext refuses (its total is not the lines'): the event's id."""
		frappe.set_user("Administrator")
		event = self.event(self.sale_payload(grand_total="9.999"))
		result = sync_api.push_batch(device_id=device_id or self.device_id, events=[event])[0]
		self.assertEqual(result["status"], "error", msg=result)
		return event

	def test_refused_lists_only_outlet_devices(self):
		ours = self.refused_event()["event_id"]
		theirs = self.refused_event(self.other_device)["event_id"]
		frappe.set_user(MANAGER)
		answer = web_admin.refused_sales(self.profile.name)
		self.assertEqual(answer["outlet"], self.profile.name)
		names = [row["name"] for row in answer["rows"]]
		self.assertIn(ours, names)
		self.assertNotIn(theirs, names)
		row = next(row for row in answer["rows"] if row["name"] == ours)
		self.assertEqual(row["device_name"], "Till 1")
		self.assertEqual(row["aggregate_type"], "Sales Invoice")
		self.assertEqual(row["error_code"], "total_mismatch")
		self.assertTrue(row["hint"] and row["hint_ar"])
		self.assertEqual(row["attempts"], 1)
		self.assertIsNone(row["retry_requested_at"])
		# a cashier reads none; a manager of shop 1 neither retries nor reads shop 2's
		frappe.set_user(CASHIER)
		with self.assertRaises(frappe.PermissionError):
			web_admin.refused_sales(self.profile.name)
		frappe.set_user(MANAGER)
		with self.assertRaises(frappe.PermissionError):
			web_admin.refused_sales(SHOP2)
		with self.assertRaises(frappe.PermissionError):
			web_admin.retry_sale(theirs)

	def test_retry_sets_flag_and_entity_returns_it(self):
		event = self.refused_event()
		_records, cursor, _pages = self._pull_all("sync_retry", limit=500)
		frappe.set_user(MANAGER)
		answer = web_admin.retry_sale(event["event_id"])
		row = next(row for row in answer["rows"] if row["name"] == event["event_id"])
		self.assertTrue(row["retry_requested_at"])

		frappe.set_user("Administrator")
		page = sync_api.pull_changes(device_id=self.device_id, entity="sync_retry", cursor=cursor, limit=500)
		self.assertIn(event["event_id"], [r["event_id"] for r in page["records"]])
		# another device never gets it
		other = sync_api.pull_changes(device_id=self.other_device, entity="sync_retry", limit=500)
		self.assertNotIn(event["event_id"], [r["event_id"] for r in other["records"]])

		# the till sends it again: processed (refused again), then dropped from the entity
		result = sync_api.push_batch(device_id=self.device_id, events=[event])[0]
		self.assertEqual(result["status"], "error")
		self.assertEqual(frappe.db.get_value(EVENT_DOCTYPE, event["event_id"], "attempts"), 2)
		again = sync_api.pull_changes(
			device_id=self.device_id, entity="sync_retry", cursor=page["next_cursor"], limit=500
		)
		self.assertIn(event["event_id"], again["tombstones"])
		self.assertNotIn(event["event_id"], [r["event_id"] for r in again["records"]])

	def test_retry_by_a_manager_of_another_shop_refused(self):
		theirs = self.refused_event(self.other_device)["event_id"]
		frappe.set_user(MANAGER)
		with self.assertRaises(frappe.PermissionError):
			web_admin.retry_sale(theirs)
		self.assertIsNone(frappe.db.get_value(EVENT_DOCTYPE, theirs, "retry_requested_at"))
		# its own manager may
		frappe.set_user(SHOP2_MANAGER)
		web_admin.retry_sale(theirs)
		self.assertTrue(frappe.db.get_value(EVENT_DOCTYPE, theirs, "retry_requested_at"))

	def test_retry_pending_until_the_till_pushes_again(self):
		event = self.refused_event()

		def row():
			answer = web_admin.refused_sales(self.profile.name)
			return next(r for r in answer["rows"] if r["name"] == event["event_id"])

		frappe.set_user(MANAGER)
		self.assertFalse(row()["retry_pending"])
		web_admin.retry_sale(event["event_id"])
		self.assertTrue(row()["retry_pending"])
		# the till sends it again and it is refused again: Retry is offered again
		frappe.set_user("Administrator")
		result = sync_api.push_batch(device_id=self.device_id, events=[event])[0]
		self.assertEqual(result["status"], "error")
		frappe.set_user(MANAGER)
		again = row()
		self.assertFalse(again["retry_pending"])
		self.assertEqual(again["attempts"], 2)
		web_admin.retry_sale(event["event_id"])
		self.assertTrue(row()["retry_pending"])

	def test_retry_entity_not_required(self):
		# Review Focus 1: a till that never pulls sync_retry pushes as before, retry asked or not
		event = self.refused_event()
		frappe.set_user(MANAGER)
		web_admin.retry_sale(event["event_id"])
		frappe.set_user("Administrator")
		self.assertOk(self.push_one(self.event(self.sale_payload())))
		result = sync_api.push_batch(device_id=self.device_id, events=[event])[0]
		self.assertEqual(result["status"], "error")
		self.assertEqual(result["error"]["code"], "total_mismatch")

	def test_retry_of_an_ok_event_refused(self):
		frappe.set_user("Administrator")
		event = self.event(self.sale_payload())
		self.assertOk(self.push_one(event))
		frappe.set_user(MANAGER)
		with self.assertRaises(frappe.ValidationError):
			web_admin.retry_sale(event["event_id"])
		with self.assertRaises(frappe.PermissionError):
			web_admin.retry_sale("no such event")
