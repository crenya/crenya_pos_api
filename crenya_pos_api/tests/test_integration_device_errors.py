"""A retail till's errors into the Error Log (`crenya_pos_api.api.device_errors.report`, 0.17.0) on
an ERPNext site: each row refers to its till, its title groups one bug of every till (no till name,
no version), at most 5 a call, the same error once per 10 minutes, 30 reports an hour per till, and
only an enabled retail till reports here.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_device_errors
"""

import json
import uuid

import frappe

from crenya_pos_api import device_errors
from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import device_errors as device_errors_api
from crenya_pos_api.sync.context import DEVICE_DOCTYPE
from crenya_pos_api.sync.errors import DeviceNotRegisteredError
from crenya_pos_api.tests.sync_case import SyncCase, new_id


class TestDeviceErrors(SyncCase):
	def till(self, name="Front till", device_type=None):
		device_id = new_id()
		device_api.register_device(
			device_id=device_id,
			device_name=name,
			pos_profile=self.profile.name,
			app_version="0.11.0",
			device_type=device_type,
		)
		frappe.db.commit()
		return device_id

	def logged(self, device, marker):
		return frappe.get_all(
			"Error Log",
			filters={
				"reference_doctype": DEVICE_DOCTYPE,
				"reference_name": device,
				"error": ["like", f"%{marker}%"],
			},
			fields=["method", "error"],
		)

	def report(self, device_id, errors):
		return device_errors_api.report(device_id=device_id, errors=json.dumps(errors))

	def test_till_error_logged_with_device_reference(self):
		device_id = self.till()
		marker = uuid.uuid4().hex
		answer = self.report(
			device_id,
			[
				{
					"message": f"panicked {marker}",
					"detail": f"stack {marker}",
					"kind": "panic",
					"app": "crenya pos",
					"version": "0.11.0",
				}
			],
		)
		self.assertEqual(answer, {"logged": 1, "skipped": 0})
		rows = self.logged(device_id, marker)
		self.assertEqual(len(rows), 1)
		self.assertIn(f"stack {marker}", rows[0].error)

	def test_title_names_app_version_device(self):
		# ledger amendment to Task 4 (Task 0 step 1): the title is "crenya pos till: <first line>",
		# the app, version, till name and id are in the error body, so Sentry groups one bug as one issue
		device_id = self.till("Front till")
		marker = uuid.uuid4().hex
		self.report(device_id, [{"message": f"boom {marker}", "app": "crenya pos", "version": "0.11.0"}])
		(row,) = self.logged(device_id, marker)
		self.assertEqual(row.method, f"crenya pos till: boom {marker}")
		for words in ("crenya pos 0.11.0", "Front till", device_id):
			self.assertIn(words, row.error)
		# another till's same bug has the same title
		other = self.till("Back till")
		self.report(other, [{"message": f"boom {marker}", "app": "crenya pos", "version": "0.11.0"}])
		self.assertEqual(self.logged(other, marker)[0].method, row.method)

	def test_more_than_5_skipped(self):
		device_id = self.till()
		marker = uuid.uuid4().hex
		answer = self.report(device_id, [{"message": f"error {i} {marker}"} for i in range(8)])
		self.assertEqual(answer, {"logged": 5, "skipped": 3})
		self.assertEqual(len(self.logged(device_id, marker)), 5)

	def test_dedupe_600s(self):
		device_id = self.till()
		marker = uuid.uuid4().hex
		error = {"message": f"same {marker}", "kind": "ui"}
		self.assertEqual(self.report(device_id, [error, error]), {"logged": 1, "skipped": 1})
		self.assertEqual(self.report(device_id, [error]), {"logged": 0, "skipped": 1})
		self.assertEqual(len(self.logged(device_id, marker)), 1)
		self.assertEqual(device_errors.DEDUPE_SECONDS, 600)

	def test_rate_limit_429_after_30(self):
		device_id = self.till()
		marker = uuid.uuid4().hex
		for index in range(device_errors.RETAIL_PER_HOUR):
			self.report(device_id, [{"message": f"storm {index} {marker}"}])
		with self.assertRaises(frappe.TooManyRequestsError) as caught:
			self.report(device_id, [{"message": f"one too many {marker}"}])
		self.assertEqual(caught.exception.http_status_code, 429)
		self.assertEqual(len(self.logged(device_id, marker)), device_errors.RETAIL_PER_HOUR)
		# another till is not held back
		self.assertEqual(self.report(self.till(), [{"message": f"fine {marker}"}])["logged"], 1)

	def test_relayed_by_a_master_stored_once_under_the_child(self):
		"""A paired till's reports reach the server through its master, which calls under the child's
		device id (as it forwards the child's control log): logged under the child, once per report
		id, and counted against the child's own hourly cap."""
		master = self.till("Master till")
		child = self.till("Paired till")
		marker = uuid.uuid4().hex
		report = {"id": f"err-{marker}", "message": f"relayed {marker}", "kind": "panic", "version": "0.11.0"}
		self.assertEqual(self.report(child, [report]), {"logged": 1, "skipped": 0})
		# the master forwards it again after a timeout: not logged twice
		self.assertEqual(
			self.report(child, [dict(report, message=f"relayed again {marker}")]), {"logged": 0, "skipped": 1}
		)
		rows = self.logged(child, marker)
		self.assertEqual(len(rows), 1)
		self.assertIn("Paired till", rows[0].error)
		self.assertEqual(self.logged(master, marker), [])
		# the child's cap is its own: 2 of its 30 used, the master's untouched
		for index in range(device_errors.RETAIL_PER_HOUR - 2):
			self.report(child, [{"message": f"more {index} {marker}"}])
		with self.assertRaises(frappe.TooManyRequestsError):
			self.report(child, [{"message": f"over {marker}"}])
		self.assertEqual(self.report(master, [{"message": f"master {marker}"}])["logged"], 1)

	def test_restaurant_device_type_refused(self):
		device_id = self.till("Counter", device_type="restaurant_pos")
		with self.assertRaises(frappe.PermissionError):
			self.report(device_id, [{"message": "x"}])

	def test_disabled_device_refused(self):
		device_id = self.till()
		frappe.db.set_value(DEVICE_DOCTYPE, device_id, "enabled", 0)
		frappe.db.commit()
		with self.assertRaises(DeviceNotRegisteredError):
			self.report(device_id, [{"message": "x"}])

	def test_payload_over_64kb_refused_before_parse(self):
		device_id = self.till()
		body = "[" + " " * device_errors.MAX_PAYLOAD_BYTES + "]"
		with self.assertRaises(frappe.ValidationError):
			device_errors_api.report(device_id=device_id, errors=body)
		# the request's own length is read first
		saved = getattr(frappe.local, "request", None)
		frappe.local.request = type("Request", (), {"content_length": device_errors.MAX_PAYLOAD_BYTES + 1})()
		try:
			with self.assertRaises(frappe.ValidationError):
				device_errors_api.report(device_id=device_id, errors=[{"message": "x"}])
		finally:
			frappe.local.request = saved

	def test_bootstrap_announces_device_errors_for_till_only(self):
		features = device_api.get_bootstrap(device_id=self.device_id)["features"]
		self.assertEqual(features.get("device_errors"), 1)
		kds = self.till("Kitchen", device_type="kds")
		self.assertNotIn("device_errors", device_api.get_bootstrap(device_id=kds).get("features") or {})

	def test_restaurant_endpoint_unchanged(self):
		if "crenya_restaurant" not in frappe.get_installed_apps():
			self.skipTest("crenya_restaurant is not installed")
		from crenya_restaurant.api import device_errors as restaurant_api

		device_id = self.till("Counter", device_type="restaurant_pos")
		marker = uuid.uuid4().hex
		answer = restaurant_api.report(
			device_id=device_id, errors=json.dumps([{"message": f"hub {marker}", "app": "crenya restaurant"}])
		)
		self.assertEqual(answer, {"logged": 1, "skipped": 0})
		self.assertTrue(self.logged(device_id, marker)[0].method.startswith("crenya restaurant on Counter"))
