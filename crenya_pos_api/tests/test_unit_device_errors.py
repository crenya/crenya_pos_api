"""Device errors into the Error Log (0.17.0, moved down from crenya_restaurant 0.15.3), on the pure
code with Frappe mocked: a device logs its own errors, a hub of the caller's hub types those of the
devices paired with it on its POS Profile and of no other, the fields are cut to their lengths, the
same error is logged once per window, a device type the caller does not accept is refused, and a
retail till's title names neither the till nor the version."""

import unittest
from unittest import mock

import frappe

from crenya_pos_api import device_errors
from crenya_pos_api.sync.context import DeviceContext

HUB = "hub-1"
PHONE = "phone-1"
PROFILE = "Main Outlet"
RESTAURANT_TYPES = {"restaurant_pos", "waiter", "kds"}
HUB_TYPES = {"restaurant_pos"}


class Refused(Exception):
	pass


def context(device_type="restaurant_pos", name=HUB, device_name="Counter 1"):
	device = {
		"name": name,
		"device_id": name,
		"device_name": device_name,
		"pos_profile": PROFILE,
		"enabled": 1,
		"device_type": device_type,
	}
	return DeviceContext(device=device, profile=None)


class FakeCache:
	def __init__(self):
		self.values = {}

	def get_value(self, key, expires=False):
		return self.values.get(key)

	def set_value(self, key, value, expires_in_sec=None):
		self.values[key] = value


def device_row(name, profile=PROFILE, enabled=1, device_type="waiter"):
	return frappe._dict(
		name=name, device_name="Waiter phone 2", pos_profile=profile, enabled=enabled, device_type=device_type
	)


class DeviceErrorsCase(unittest.TestCase):
	def setUp(self):
		self.rows = {PHONE: device_row(PHONE)}
		self.logged = []
		self.hits = []
		patches = (
			mock.patch.object(device_errors.frappe, "cache", FakeCache(), create=True),
			mock.patch.object(device_errors, "_device_row", side_effect=lambda name: self.rows.get(name)),
			mock.patch.object(device_errors.frappe, "log_error", create=True, side_effect=self._log),
			mock.patch.object(device_errors, "_", side_effect=lambda text: text),
		)
		for p in patches:
			p.start()
			self.addCleanup(p.stop)

	def _log(self, **kwargs):
		self.logged.append(kwargs)

	def restaurant(self, ctx, errors):
		return device_errors.record(ctx, errors, RESTAURANT_TYPES, self.hits.append, hub_types=HUB_TYPES)

	def retail(self, ctx, errors):
		return device_errors.record(ctx, errors, {"till"}, self.hits.append, title=device_errors.retail_title)

	def test_a_device_logs_its_own_error(self):
		answer = self.restaurant(
			context(),
			[
				{
					"message": "panicked at src/lib.rs:12",
					"detail": "stack",
					"app": "crenya restaurant",
					"version": "0.7.3",
				}
			],
		)
		self.assertEqual(answer, {"logged": 1, "skipped": 0})
		self.assertEqual(
			self.logged[0]["title"], "crenya restaurant 0.7.3 on Counter 1: panicked at src/lib.rs:12"
		)
		self.assertEqual(self.logged[0]["reference_name"], HUB)
		self.assertIn("stack", self.logged[0]["message"])
		self.assertEqual(self.hits, [HUB])

	def test_a_hub_relays_its_paired_phones_errors_only(self):
		self.rows["other-outlet"] = device_row("other-outlet", profile="Other Outlet")
		self.rows["off"] = device_row("off", enabled=0)
		self.rows["till"] = device_row("till", device_type="till")
		errors = [
			{
				"device_id": PHONE,
				"message": "TypeError: x is undefined",
				"app": "crenya waiter",
				"version": "0.5.0",
			},
			{"device_id": "other-outlet", "message": "a"},
			{"device_id": "off", "message": "b"},
			{"device_id": "till", "message": "c"},
			{"device_id": "unknown", "message": "d"},
		]
		self.assertEqual(self.restaurant(context(), errors), {"logged": 1, "skipped": 4})
		self.assertEqual(self.logged[0]["reference_name"], PHONE)
		self.assertTrue(
			self.logged[0]["title"].startswith("crenya waiter 0.5.0 on Waiter phone 2: TypeError")
		)

	def test_only_a_hub_reports_for_another_device(self):
		kds = context(device_type="kds", name="kds-1")
		self.assertEqual(
			self.restaurant(kds, [{"device_id": PHONE, "message": "x"}]), {"logged": 0, "skipped": 1}
		)
		# a retail till relays for nobody
		till = context(device_type="till", name="till-1")
		self.assertEqual(
			self.retail(till, [{"device_id": PHONE, "message": "x"}]), {"logged": 0, "skipped": 1}
		)

	def test_the_same_error_is_logged_once_per_window(self):
		error = {"message": "panicked", "kind": "panic"}
		self.assertEqual(self.restaurant(context(), [error, error]), {"logged": 1, "skipped": 1})
		self.assertEqual(self.restaurant(context(), [error]), {"logged": 0, "skipped": 1})
		# another device's same error is its own
		self.assertEqual(self.restaurant(context(name="hub-2"), [error]), {"logged": 1, "skipped": 0})

	def test_a_report_id_is_logged_once(self):
		till = context(device_type="till", name="till-1")
		first = {"id": "r-1", "message": "boom", "kind": "panic"}
		self.assertEqual(self.retail(till, [first]), {"logged": 1, "skipped": 0})
		# the same report again (resent, or relayed by the hub) after the content window: still once
		self.assertEqual(self.retail(till, [dict(first, message="boom again")]), {"logged": 0, "skipped": 1})
		# the same id of another till is that till's own
		self.assertEqual(
			self.retail(context(device_type="till", name="till-2"), [first]), {"logged": 1, "skipped": 0}
		)

	def test_at_most_five_per_call_and_the_fields_cut(self):
		errors = [{"message": f"error {i}", "detail": "x" * 9000} for i in range(8)]
		self.assertEqual(self.restaurant(context(), errors), {"logged": 5, "skipped": 3})
		cleaned = device_errors.clean({"message": "m" * 1000, "app": "a" * 100})
		self.assertEqual(len(cleaned["message"]), device_errors.LENGTHS["message"])
		self.assertEqual(len(cleaned["app"]), device_errors.LENGTHS["app"])
		self.assertIsNone(device_errors.clean({"message": "  "}))
		self.assertIsNone(device_errors.clean("not a dict"))

	def test_the_title_is_cut_for_the_error_log(self):
		error = device_errors.clean({"message": "m" * 300, "app": "a" * 40, "version": "v" * 20})
		self.assertEqual(len(device_errors.title_of(error, "d" * 140)), device_errors.TITLE_MAX)
		self.assertLessEqual(len(device_errors.retail_title(error)), device_errors.TITLE_MAX)

	def test_title_default_app(self):
		error = device_errors.clean({"message": "boom"})
		self.assertEqual(device_errors.title_of(error, "Till"), "crenya pos on Till: boom")
		self.assertEqual(
			device_errors.title_of(error, "Hub", default_app="restaurant device"),
			"restaurant device on Hub: boom",
		)

	def test_retail_title_names_no_till_and_no_version(self):
		till = context(device_type="till", name="till-1", device_name="Front till")
		error = {"message": "boom\nsecond line", "app": "crenya pos", "version": "0.11.0", "kind": "panic"}
		self.assertEqual(self.retail(till, [error]), {"logged": 1, "skipped": 0})
		self.assertEqual(self.logged[0]["title"], "crenya pos till: boom")
		# the till, its id and the version are in the message
		for words in ("Front till", "till-1", "crenya pos 0.11.0", "kind: panic"):
			self.assertIn(words, self.logged[0]["message"])

	def test_a_report_body_too_large_is_refused_before_parsing(self):
		with mock.patch.object(device_errors, "invalid", side_effect=Refused), self.assertRaises(Refused):
			device_errors.check_payload_size(device_errors.MAX_PAYLOAD_BYTES + 1)
		device_errors.check_payload_size(device_errors.MAX_PAYLOAD_BYTES)
		device_errors.check_payload_size(None)

	def test_a_type_not_accepted_is_refused(self):
		with mock.patch.object(device_errors, "refuse", side_effect=Refused), self.assertRaises(Refused):
			self.restaurant(context(device_type="till"), [{"message": "x"}])
		with mock.patch.object(device_errors, "refuse", side_effect=Refused), self.assertRaises(Refused):
			self.retail(context(device_type="restaurant_pos"), [{"message": "x"}])
		self.assertEqual(self.logged, [])
		self.assertEqual(self.hits, [])


if __name__ == "__main__":
	unittest.main()
