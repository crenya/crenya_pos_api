"""TEMPORARY diagnostic (removed before review): every permission check a device-role-only
user's sale push fails, printed to the CI log."""

import uuid

import frappe
import frappe.permissions

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase
from unittest.mock import patch

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.tests.test_integration_sync import TestCrenyaSync, new_id


class TestZZDiag(FrappeTestCase):
	def test_denials(self):
		helper = TestCrenyaSync("test_capabilities")
		TestCrenyaSync.setUpClass()
		helper.profile, helper.walk_in = TestCrenyaSync.profile, TestCrenyaSync.walk_in
		cashier = helper._cashier()
		denied = []
		real = frappe.permissions.has_permission

		def recording(doctype, ptype="read", doc=None, user=None, *args, **kwargs):
			kwargs.pop("throw", None)
			ok = real(doctype, ptype, doc, user, *args, **kwargs)
			if not ok and (user or frappe.session.user) == cashier:
				denied.append((getattr(doctype, "doctype", doctype), ptype))
			return True

		device_id = new_id()
		frappe.set_user(cashier)
		try:
			helper.device = device_api.register_device(
				device_id=device_id, device_name="Diag", pos_profile=helper.profile.name
			)
			payload = helper.sale_payload(offline_number=f"OFF-{helper.device['device_short']}-{uuid.uuid4().hex[:6]}")
			with patch.object(frappe.permissions, "has_permission", recording):
				[result] = sync_api.push_batch(device_id=device_id, events=[helper.event(payload)])
		finally:
			frappe.set_user("Administrator")
		print("DIAG-DENIED", sorted(set(denied)), "RESULT", result.get("status"), result.get("error"))
