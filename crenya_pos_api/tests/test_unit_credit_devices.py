"""The device types that may take credit (0.14.0): "till" always, plus what an app lists in the
`crenya_pos_credit_device_types` hook. No site (hooks are faked)."""

import unittest
from unittest import mock

from crenya_pos_api.sync import registry
from crenya_pos_api.sync.credit import credit_device_types, is_credit_device
from crenya_pos_api.tests import extension_dummy as dummy

HOOK = registry.CREDIT_DEVICE_TYPES_HOOK


def listing(*types):
	return dummy.fake_hooks({HOOK: list(types)}, site_ready=True)


class TestCreditDeviceTypes(unittest.TestCase):
	def test_hook_name(self):
		self.assertEqual(HOOK, "crenya_pos_credit_device_types")

	def test_till_is_always_a_credit_device(self):
		with listing():
			self.assertEqual(credit_device_types(), frozenset({"till"}))
			self.assertTrue(is_credit_device("till"))
			self.assertTrue(is_credit_device(""))
			self.assertTrue(is_credit_device(None))

	def test_till_is_a_credit_device_without_a_site(self):
		registry.clear_cache()
		self.assertEqual(credit_device_types(), frozenset({"till"}))

	def test_hook_adds_device_type(self):
		with listing("restaurant_pos"):
			self.assertEqual(credit_device_types(), frozenset({"till", "restaurant_pos"}))
			self.assertTrue(is_credit_device("restaurant_pos"))
			self.assertTrue(is_credit_device("till"))

	def test_unknown_device_type_refused(self):
		with listing("restaurant_pos"):
			for device_type in ("waiter", "kds", "restaurant_pos_x", "Restaurant_POS"):
				self.assertFalse(is_credit_device(device_type), device_type)

	def test_two_apps_lists_are_joined(self):
		with listing("restaurant_pos", "salon_pos"):
			self.assertEqual(credit_device_types(), frozenset({"till", "restaurant_pos", "salon_pos"}))

	def test_a_bad_entry_is_ignored(self):
		with listing("restaurant_pos", "Not A Type", 7, None):
			self.assertEqual(credit_device_types(), frozenset({"till", "restaurant_pos"}))

	def test_a_hook_that_raises_is_ignored(self):
		def broken(*args, **kwargs):
			raise RuntimeError("boom")

		with (
			listing("restaurant_pos"),
			mock.patch.object(registry, "_hook", broken),
			mock.patch.object(registry.frappe, "logger") as logger,
		):
			registry.clear_cache()
			self.assertEqual(credit_device_types(), frozenset({"till"}))
		warning = logger.return_value.warning
		warning.assert_called_once()
		message = warning.call_args.args[0]
		self.assertIn("crenya_pos_credit_device_types could not be read", message)
		self.assertIn("RuntimeError: boom", message)
		self.assertNotIn("is not a device type", message)

	def test_read_once_per_request(self):
		with listing("restaurant_pos"):
			first = credit_device_types()
			self.assertIs(credit_device_types(), first)

	def test_the_helper_is_shared(self):
		with listing("a_type"):
			self.assertEqual(
				registry._device_types("crenya_pos_credit_device_types", frozenset({"x"})),
				frozenset({"x", "a_type"}),
			)
