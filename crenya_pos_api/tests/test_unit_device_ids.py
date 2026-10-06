"""Device ids compared ignoring case (no site needed)."""

import unittest

from crenya_pos_api.sync.context import AmbiguousDeviceError, pick_device_name
from crenya_pos_api.sync.device import case_variant_refusal

DEVICE = "d88c9df7-a2ea-4117-9ac5-6bc0d8197408"
STORED = {
	"name": DEVICE,
	"user": "outlet@example.com",
	"pos_profile": "Outlet One",
	"device_type": "restaurant_pos",
	"enabled": 1,
}


class TestPickDeviceName(unittest.TestCase):
	def test_exact_spelling(self):
		self.assertEqual(pick_device_name(DEVICE, [DEVICE]), DEVICE)

	def test_other_case_finds_the_stored_spelling(self):
		self.assertEqual(pick_device_name(DEVICE.upper(), [DEVICE]), DEVICE)
		self.assertEqual(pick_device_name(DEVICE, [DEVICE.upper()]), DEVICE.upper())

	def test_none_when_no_device(self):
		self.assertIsNone(pick_device_name(DEVICE, []))

	def test_rows_that_are_not_case_variants_are_ignored(self):
		self.assertIsNone(pick_device_name(DEVICE, ["another-device-id", None]))

	def test_exact_spelling_wins_over_case_variants(self):
		self.assertEqual(pick_device_name(DEVICE, [DEVICE.upper(), DEVICE]), DEVICE)

	def test_several_case_variants_without_the_exact_one_are_ambiguous(self):
		mixed = DEVICE[:8].upper() + DEVICE[8:]
		with self.assertRaises(AmbiguousDeviceError) as raised:
			pick_device_name(DEVICE.upper(), [DEVICE, mixed])
		self.assertEqual(raised.exception.names, sorted([DEVICE, mixed]))


class TestCaseVariantRefusal(unittest.TestCase):
	def test_same_device_is_not_refused(self):
		self.assertIsNone(case_variant_refusal(STORED, STORED["user"], STORED["pos_profile"]))
		self.assertIsNone(
			case_variant_refusal(STORED, STORED["user"], STORED["pos_profile"], "restaurant_pos")
		)

	def test_another_user_is_refused(self):
		message = case_variant_refusal(STORED, "phone@example.com", STORED["pos_profile"], "waiter")
		self.assertIn("another user", message)
		self.assertIn(DEVICE, message)

	def test_another_profile_is_refused(self):
		message = case_variant_refusal(STORED, STORED["user"], "Outlet Two")
		self.assertIn("another POS Profile", message)

	def test_another_device_type_is_refused(self):
		# H3-CASE: the hub's own API user re-registering a live till's id, upper-cased, as a waiter
		message = case_variant_refusal(STORED, STORED["user"], STORED["pos_profile"], "waiter")
		self.assertIn("as a restaurant_pos device", message)

	def test_stored_device_without_a_type_is_a_till(self):
		stored = dict(STORED, device_type=None)
		self.assertIsNone(case_variant_refusal(stored, STORED["user"], STORED["pos_profile"], "till"))
		self.assertIn(
			"as a till device", case_variant_refusal(stored, STORED["user"], STORED["pos_profile"], "kds")
		)


if __name__ == "__main__":
	unittest.main()
