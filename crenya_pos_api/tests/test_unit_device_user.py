import unittest
from unittest import mock

import frappe

from crenya_pos_api.sync import device_user

QUICK = "pbkdf2_sha256$1000$EBESExQVFhcYGRobHB0eHw==$BL2V/7cFPO+C+io4h8uWRwlPzaECmmRJGm440pxErWA="
# the seal of QUICK under the key of 32 bytes of 7, as pos-core's `pin::seal_server` makes it
# (the same vector is in pos-core's pin.rs tests)
SEALED = "crenya_pin1$45ee51a3$1000$EBESExQVFhcYGRobHB0eHw==$Da7ytos+5siZSXWBAzkjVuX4WDj4fVbFsH6ZeXS8Wsc="


class TestSealPinHash(unittest.TestCase):
	def test_matches_pos_cores_format(self):
		self.assertEqual(device_user.seal_pin_hash(QUICK, bytes([7]) * 32), SEALED)

	def test_the_pbkdf2_key_is_not_in_it(self):
		self.assertNotIn(
			"BL2V/7cFPO+C+io4h8uWRwlPzaECmmRJGm440pxErWA=", device_user.seal_pin_hash(QUICK, b"k" * 32)
		)

	def test_not_a_usable_hash(self):
		key = bytes([7]) * 32
		for bad in (
			None,
			"",
			"4826",
			"pbkdf2_sha1$1000$AA==$AA==",
			"pbkdf2_sha256$x$EBESExQVFhcYGRobHB0eHw==$BL2V",
			"pbkdf2_sha256$1000$not base64$BL2V/7cFPO+C+io4h8uWRwlPzaECmmRJGm440pxErWA=",
			"pbkdf2_sha256$1000$EBESExQVFhcYGRobHB0eHw==$AAAA",
		):
			self.assertIsNone(device_user.seal_pin_hash(bad, key), bad)


class TestDeviceUserName(unittest.TestCase):
	def name(self, device_id, site="shop.example.com"):
		with mock.patch.object(frappe.local, "site", site, create=True):
			return device_user.device_user_name(device_id)

	def test_uuid_and_site_host(self):
		uid = "0b8a3a55-2f65-4a3c-9c32-4f6a4f3c1c11"
		self.assertEqual(self.name(uid), f"device-{uid}@shop.example.com")

	def test_ids_that_differ_only_in_case_are_one_user(self):
		self.assertEqual(self.name("ABCDEF12-3456"), self.name("abcdef12-3456"))

	def test_a_site_with_no_dot_gets_local_and_a_domain_safe_name(self):
		self.assertEqual(self.name("abcdef12-3456", site="test_site"), "device-abcdef12-3456@test-site.local")
		self.assertEqual(
			self.name("abcdef12-3456", site="Shop.Example.com"), "device-abcdef12-3456@shop.example.com"
		)

	def test_an_id_a_mailbox_cannot_hold_is_hashed(self):
		long_id = "a" * 100
		name = self.name(long_id)
		local = name.split("@")[0]
		self.assertLessEqual(len(local), device_user.MAX_LOCAL_PART)
		self.assertEqual(name, self.name(long_id.upper()))
		self.assertNotEqual(self.name("a" * 101), name)
		self.assertTrue(self.name("id:with:colons").startswith("device-"))
		self.assertNotIn(":", self.name("id:with:colons"))


class TestKeys(unittest.TestCase):
	def test_pin_seal_key_is_per_device_and_stable(self):
		with mock.patch("frappe.utils.password.get_encryption_key", return_value="site-secret"):
			a = device_user.pin_seal_key("DEV-A-0001")
			self.assertEqual(a, device_user.pin_seal_key("dev-a-0001"))
			self.assertNotEqual(a, device_user.pin_seal_key("dev-b-0001"))
			self.assertEqual(len(a), 32)
		with mock.patch("frappe.utils.password.get_encryption_key", return_value="another-site"):
			self.assertNotEqual(a, device_user.pin_seal_key("dev-a-0001"))
