"""Plan caps at device registration and the workspace entitlement in every device reply.

The rule tests need no site; `TestEntitlementOnSite` drives the real endpoints.
"""

import unittest
import uuid
from unittest import mock

import frappe

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.sync import device as device_module
from crenya_pos_api.sync import entitlement as entitlement_module
from crenya_pos_api.sync import errors
from crenya_pos_api.sync.context import DEVICE_DOCTYPE
from crenya_pos_api.sync.entitlement import get_entitlement, registration_refusal
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.extension_dummy import fake_hooks

HOOK = "crenya_pos_entitlement"
HERE = "crenya_pos_api.tests.test_unit_entitlement"
TILL = "till"
RESTAURANT = "restaurant_pos"

# what the test hook answers (tests change it)
PLAN = {}


def plan(max_terminals=0, max_outlets=0, restaurant=1, pos=1, status="Active", paid_until="2026-12-31"):
	return {
		"status": status,
		"paid_until": paid_until,
		"max_outlets": max_outlets,
		"max_terminals": max_terminals,
		"addons": {"restaurant": restaurant, "pos": pos},
	}


def set_plan(**values):
	PLAN.clear()
	PLAN.update(plan(**values))
	return dict(PLAN)


# one entry per call of the test hook
CALLS = []


def entitlement_hook():
	CALLS.append(1)
	return dict(PLAN)


def raising_hook():
	raise RuntimeError("settings unreadable")


def malformed_hook():
	return "Active"


def new_id():
	return str(uuid.uuid4())


def device(device_type=TILL, pos_profile="Outlet A", enabled=1, name=None):
	return {
		"name": name or new_id(),
		"device_type": device_type,
		"pos_profile": pos_profile,
		"enabled": enabled,
	}


def refusal(entitlement, devices, device_type=TILL, pos_profile="Outlet A", device_id=None):
	return registration_refusal(entitlement, device_id or new_id(), device_type, pos_profile, devices)


class TestRegistrationRefusal(unittest.TestCase):
	def test_cap_refuses_new_terminal(self):
		devices = [device(), device(RESTAURANT)]
		self.assertEqual(
			refusal(plan(max_terminals=2), devices), "This plan allows 2 tills. Ask the owner to upgrade."
		)
		self.assertEqual(
			refusal(plan(max_terminals=2), devices, RESTAURANT),
			"This plan allows 2 tills. Ask the owner to upgrade.",
		)
		# below the cap, and 0 = unlimited
		self.assertIsNone(refusal(plan(max_terminals=3), devices))
		self.assertIsNone(refusal(plan(max_terminals=0), devices * 50))

	def test_disabled_device_frees_a_slot(self):
		devices = [device(), device(enabled=0)]
		self.assertIsNone(refusal(plan(max_terminals=2), devices))
		devices.append(device())
		self.assertIsNotNone(refusal(plan(max_terminals=2), devices))

	def test_same_device_reregisters_at_cap(self):
		till = device()
		devices = [till, device()]
		self.assertIsNone(refusal(plan(max_terminals=1), devices, device_id=till["name"]))
		# ids differing only in case are the same device
		self.assertIsNone(refusal(plan(max_terminals=1), devices, device_id=till["name"].upper()))
		# at its current profile also past the outlet cap and with its add-on off
		self.assertIsNone(refusal(plan(max_outlets=1, pos=0), devices, device_id=till["name"]))
		# a disabled device is left to register_device, which refuses it
		off = device(enabled=0)
		self.assertIsNone(refusal(plan(max_terminals=1, pos=0), [off, device()], device_id=off["name"]))

	def test_reregistered_terminal_moving_outlet_needs_an_outlet_slot(self):
		till = device(pos_profile="Outlet A")
		devices = [till, device(pos_profile="Outlet A")]
		self.assertEqual(
			refusal(plan(max_outlets=1), devices, pos_profile="Outlet B", device_id=till["name"]),
			"This plan allows 1 outlets. Ask the owner to upgrade.",
		)
		# a profile other terminals already use is no new outlet
		devices.append(device(pos_profile="Outlet B"))
		self.assertIsNone(
			refusal(plan(max_outlets=2), devices, pos_profile="Outlet B", device_id=till["name"])
		)
		# the device itself is not counted: the only terminal of Outlet A may move anywhere
		alone = device(pos_profile="Outlet A")
		self.assertIsNone(
			refusal(plan(max_outlets=1), [alone], pos_profile="Outlet Z", device_id=alone["name"])
		)
		# moving outlet is not a new terminal: the terminal cap and add-on are not checked
		self.assertIsNone(
			refusal(plan(max_terminals=1, pos=0), devices, pos_profile="Outlet B", device_id=till["name"])
		)

	def test_reregistered_non_terminal_becoming_terminal_is_checked_as_new(self):
		kds = device("kds")
		devices = [kds, device()]
		self.assertEqual(
			refusal(plan(max_terminals=1), devices, TILL, device_id=kds["name"]),
			"This plan allows 1 tills. Ask the owner to upgrade.",
		)
		self.assertEqual(
			refusal(plan(pos=0), devices, TILL, device_id=kds["name"]),
			"New POS (Tauri) is not switched on for this workspace.",
		)
		self.assertEqual(
			refusal(plan(max_outlets=1), devices, TILL, "Outlet B", device_id=kds["name"]),
			"This plan allows 1 outlets. Ask the owner to upgrade.",
		)
		self.assertIsNone(refusal(plan(max_terminals=2), devices, TILL, device_id=kds["name"]))
		# staying a kds is never refused
		self.assertIsNone(refusal(plan(max_terminals=1, pos=0), devices, "kds", device_id=kds["name"]))

	def test_outlet_cap_refuses_new_profile(self):
		devices = [device(pos_profile="Outlet A"), device(RESTAURANT, pos_profile="Outlet A")]
		self.assertEqual(
			refusal(plan(max_outlets=1), devices, pos_profile="Outlet B"),
			"This plan allows 1 outlets. Ask the owner to upgrade.",
		)
		# a profile already counted never trips it
		self.assertIsNone(refusal(plan(max_outlets=1), devices, pos_profile="Outlet A"))
		# a disabled terminal's profile is not an outlet any more
		devices = [device(pos_profile="Outlet A", enabled=0), device(pos_profile="Outlet B")]
		self.assertIsNone(refusal(plan(max_outlets=2), devices, pos_profile="Outlet C"))
		self.assertIsNotNone(refusal(plan(max_outlets=1), devices, pos_profile="Outlet C"))

	def test_kds_and_waiter_not_counted(self):
		devices = [device("kds", "Outlet B"), device("waiter", "Outlet C"), device()]
		# only the one till counts as a terminal and Outlet A as the only outlet
		self.assertIsNone(refusal(plan(max_terminals=2, max_outlets=2), devices, pos_profile="Outlet D"))
		# kds and waiter are never refused: not by caps, not by add-ons
		full = plan(max_terminals=1, max_outlets=1, restaurant=0, pos=0)
		self.assertIsNone(refusal(full, devices, "kds", "Outlet Z"))
		self.assertIsNone(refusal(full, devices, "waiter", "Outlet Z"))
		# a device stored before device types existed is a till
		self.assertIsNotNone(refusal(plan(max_terminals=1), [device(None)]))

	def test_addon_off_refuses_registration(self):
		self.assertEqual(
			refusal(plan(restaurant=0), [], RESTAURANT),
			"New Restaurant (Tauri) is not switched on for this workspace.",
		)
		self.assertEqual(
			refusal(plan(pos=0), [], TILL), "New POS (Tauri) is not switched on for this workspace."
		)
		self.assertIsNone(refusal(plan(restaurant=0), [], TILL))
		self.assertIsNone(refusal(plan(pos=0), [], RESTAURANT))

	def test_no_hook_is_unrestricted(self):
		with fake_hooks({}, site_ready=True):
			self.assertIsNone(get_entitlement())
		self.assertIsNone(refusal(None, [device()] * 5))

	def test_hook_error_is_unrestricted(self):
		for path in (f"{HERE}.raising_hook", f"{HERE}.malformed_hook", f"{HERE}.no_such_hook", "nodots"):
			with (
				self.subTest(path=path),
				fake_hooks({HOOK: [path]}, site_ready=True),
				mock.patch.object(entitlement_module, "_log_hook_error") as log,
			):
				self.assertIsNone(get_entitlement())
				log.assert_called_once()

	def test_logging_failure_never_escapes(self):
		with (
			fake_hooks({HOOK: [f"{HERE}.raising_hook"]}, site_ready=True),
			mock.patch.object(frappe, "cache", mock.MagicMock(**{"get_value.side_effect": RuntimeError})),
			mock.patch.object(frappe, "log_error", side_effect=RuntimeError("no Error Log")) as log,
		):
			self.assertIsNone(get_entitlement())
			# the cache failing does not stop the attempt to log
			log.assert_called_once()

	def test_hook_error_is_logged_once_per_window(self):
		seen = {}
		cache = mock.MagicMock()
		cache.get_value.side_effect = seen.get
		cache.set_value.side_effect = lambda key, value, expires_in_sec=None: seen.__setitem__(key, value)
		with (
			fake_hooks({HOOK: [f"{HERE}.raising_hook"]}, site_ready=True),
			mock.patch.object(frappe, "cache", cache),
			mock.patch.object(frappe, "log_error") as log,
		):
			for _call in range(3):
				self.assertIsNone(get_entitlement())
			log.assert_called_once()
			cache.set_value.assert_called_once_with(
				entitlement_module._ERROR_KEY,
				1,
				expires_in_sec=entitlement_module._ERROR_LOG_WINDOW_SECONDS,
			)
			# the window over (key expired): logged again
			seen.clear()
			get_entitlement()
			self.assertEqual(log.call_count, 2)

	def test_last_hook_wins_and_is_normalised(self):
		set_plan(max_terminals="3", max_outlets=None, restaurant="1", pos=0, paid_until=None)
		with fake_hooks({HOOK: [f"{HERE}.raising_hook", f"{HERE}.entitlement_hook"]}, site_ready=True):
			self.assertEqual(
				get_entitlement(),
				{
					"status": "Active",
					"paid_until": None,
					"max_outlets": 0,
					"max_terminals": 3,
					"addons": {"restaurant": 1, "pos": 0},
				},
			)


class TestEntitlementOnSite(FrappeTestCase):
	"""register_device, get_bootstrap and the sync replies against a real site."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.profile = fixtures.setup_fixtures()
		cls.device_id = new_id()
		device_api.register_device(
			device_id=cls.device_id, device_name="Till E", pos_profile=cls.profile.name
		)
		frappe.db.commit()

	def setUp(self):
		set_plan()
		frappe.local.response.pop("entitlement", None)

	def hook(self):
		return fake_hooks({HOOK: [f"{HERE}.entitlement_hook"]})

	def terminals(self):
		return frappe.db.count(DEVICE_DOCTYPE, {"enabled": 1, "device_type": ["in", [TILL, RESTAURANT]]})

	def test_bootstrap_and_sync_carry_entitlement(self):
		set_plan(max_terminals=7, status="Grace", paid_until="2026-11-01")
		with self.hook():
			CALLS.clear()
			bootstrap = device_api.get_bootstrap(device_id=self.device_id)
			self.assertEqual(bootstrap["entitlement"], PLAN)
			self.assertEqual(frappe.local.response["entitlement"], PLAN)
			# read once per request, shared by the reply and the bootstrap
			self.assertEqual(len(CALLS), 1)

			frappe.local.response.pop("entitlement", None)
			sync_api.pull_changes(device_id=self.device_id, entity="item")
			self.assertEqual(frappe.local.response["entitlement"], PLAN)

			frappe.local.response.pop("entitlement", None)
			self.assertEqual(sync_api.push_batch(device_id=self.device_id, events=[]), [])
			self.assertEqual(frappe.local.response["entitlement"], PLAN)

		# no hook: null, and the bootstrap still answers
		frappe.local.response.pop("entitlement", None)
		with fake_hooks({}):
			self.assertIsNone(device_api.get_bootstrap(device_id=self.device_id)["entitlement"])
			sync_api.pull_changes(device_id=self.device_id, entity="item")
			self.assertIn("entitlement", frappe.local.response)
			self.assertIsNone(frappe.local.response["entitlement"])

	def test_register_device_applies_the_caps_on_site(self):
		set_plan(max_terminals=self.terminals())
		with self.hook():
			with self.assertRaises(errors.DevicePermissionError) as raised:
				device_api.register_device(device_id=new_id(), pos_profile=self.profile.name)
			self.assertIn(f"This plan allows {self.terminals()} tills.", str(raised.exception))
			# the registered till reinstalls at the cap
			again = device_api.register_device(device_id=self.device_id, pos_profile=self.profile.name)
			self.assertEqual(again["device_id"], self.device_id)
			# a kitchen display is not a terminal
			kds = device_api.register_device(
				device_id=new_id(), pos_profile=self.profile.name, device_type="kds"
			)
			self.assertEqual(kds["device_type"], "kds")

			set_plan(pos=0)
			with self.assertRaises(errors.DevicePermissionError) as raised:
				device_api.register_device(device_id=new_id(), pos_profile=self.profile.name)
			self.assertIn("New POS (Tauri) is not switched on", str(raised.exception))

	def test_concurrent_registration_rechecks_the_cap(self):
		"""Two new tills racing for the last slot: the loser's retry sees the winner and is refused."""
		set_plan(max_terminals=self.terminals() + 1)
		rows = device_module._device_rows()
		winner = {"name": new_id(), "pos_profile": self.profile.name, "enabled": 1, "device_type": TILL}
		with (
			self.hook(),
			mock.patch.object(device_module, "_device_rows", side_effect=[rows, [*rows, winner]]),
			mock.patch(
				"frappe.model.document.Document.insert", side_effect=frappe.DuplicateEntryError
			) as insert,
			mock.patch.object(frappe.db, "rollback"),
		):
			with self.assertRaises(errors.DevicePermissionError) as raised:
				device_api.register_device(device_id=new_id(), pos_profile=self.profile.name)
			self.assertIn(f"This plan allows {self.terminals() + 1} tills.", str(raised.exception))
			insert.assert_called_once()

	def test_hook_error_never_blocks_registration(self):
		with (
			fake_hooks({HOOK: [f"{HERE}.raising_hook"]}),
			mock.patch.object(entitlement_module, "_log_hook_error"),
		):
			fresh = device_api.register_device(device_id=new_id(), pos_profile=self.profile.name)
			self.assertTrue(fresh["device_short"])
