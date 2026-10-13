"""Staff rules of app.crenya.com (crenya_pos_api/web/staff.py, moved from crenya_restaurant's
admin.py): privileges as a parameter (the retail map and a restaurant-like one), PINs, outlets."""

import unittest

from crenya_pos_api.sync.approvals import APPROVER_ROLE, RETAIL_PRIVILEGES
from crenya_pos_api.sync.context import POS_USER_ROLE
from crenya_pos_api.web import staff

# a restaurant-like map: one role per privilege, two privileges sharing a role
RESTAURANT_LIKE = {
	"manager": "R Manager",
	"cashier": "R Captain",
	"waiter": "R Captain",
	"kitchen": "R Kitchen",
}


class TestPrivileges(unittest.TestCase):
	def test_retail_privilege_of(self):
		self.assertEqual(staff.privilege_of([POS_USER_ROLE], RETAIL_PRIVILEGES), "cashier")
		self.assertEqual(staff.privilege_of([POS_USER_ROLE, APPROVER_ROLE], RETAIL_PRIVILEGES), "manager")
		self.assertIsNone(staff.privilege_of([APPROVER_ROLE], RETAIL_PRIVILEGES))
		self.assertIsNone(staff.privilege_of(["Accounts User"], RETAIL_PRIVILEGES))

	def test_restaurant_like_privilege_of(self):
		self.assertEqual(staff.privilege_of(["R Captain"], RESTAURANT_LIKE), "cashier")
		self.assertEqual(staff.privilege_of(["R Manager", "R Kitchen"], RESTAURANT_LIKE), "manager")
		self.assertEqual(staff.privilege_of(["R Kitchen"], RESTAURANT_LIKE), "kitchen")

	def test_retail_role_changes(self):
		self.assertEqual(staff.role_changes([], "cashier", RETAIL_PRIVILEGES), ([POS_USER_ROLE], []))
		self.assertEqual(
			staff.role_changes([POS_USER_ROLE], "manager", RETAIL_PRIVILEGES), ([APPROVER_ROLE], [])
		)
		self.assertEqual(
			staff.role_changes([POS_USER_ROLE, APPROVER_ROLE, "Accounts User"], "cashier", RETAIL_PRIVILEGES),
			([], [APPROVER_ROLE]),
		)
		self.assertEqual(
			staff.role_changes([POS_USER_ROLE, APPROVER_ROLE], None, RETAIL_PRIVILEGES),
			([], [POS_USER_ROLE, APPROVER_ROLE]),
		)
		with self.assertRaises(ValueError):
			staff.role_changes([], "kitchen", RETAIL_PRIVILEGES)

	def test_restaurant_like_role_changes(self):
		self.assertEqual(
			staff.role_changes(["R Captain", "Crenya POS User"], "kitchen", RESTAURANT_LIKE),
			(["R Kitchen"], ["R Captain"]),
		)

	def test_never_system_manager(self):
		with self.assertRaises(ValueError):
			staff.role_changes([], "boss", {"boss": ("System Manager",)})

	def test_protected(self):
		self.assertTrue(staff.is_protected("Administrator", []))
		self.assertTrue(staff.is_protected("Guest", []))
		self.assertTrue(staff.is_protected("owner@x", ["System Manager"]))
		self.assertFalse(staff.is_protected("cashier@x", [POS_USER_ROLE]))
		self.assertTrue(staff.is_reserved("Administrator"))
		self.assertFalse(staff.is_reserved("owner@x"))


class TestPinAndSelf(unittest.TestCase):
	def test_pin_must_be_six_digits(self):
		self.assertIsNone(staff.pin_problem("123456"))
		for bad in ("12345", "1234567", "12345a", "١٢٣٤٥٦", None, 123456):
			self.assertEqual(staff.pin_problem(bad), "The POS PIN must be 6 digits (0-9)", bad)

	def test_self_change(self):
		self.assertIsNone(staff.self_change_problem("m@x", "m@x", "manager", True))
		self.assertIn("disable", staff.self_change_problem("m@x", "m@x", "manager", False))
		self.assertIn("manager privilege", staff.self_change_problem("m@x", "m@x", "cashier", True))
		self.assertIsNone(staff.self_change_problem("m@x", "w@x", "cashier", False))


class TestListing(unittest.TestCase):
	def test_listing_changes(self):
		listed = {"A": {"m@x"}, "B": {"m@x", "s@x"}, "C": set()}
		self.assertEqual(staff.listing_changes(listed, "s@x", ["A"]), (["A"], ["B"]))
		self.assertEqual(staff.listing_changes(listed, "s@x", ["A", "B", "C"]), (["A"], []))
		self.assertEqual(staff.listing_changes(listed, "s@x", []), ([], ["B"]))

	def test_only_user_is_not_taken_off(self):
		with self.assertRaises(ValueError):
			staff.listing_changes({"A": {"s@x"}, "B": set()}, "s@x", ["B"])

	def test_unknown_or_bad_outlets(self):
		with self.assertRaises(ValueError):
			staff.listing_changes({"A": set()}, "s@x", ["A", "Restaurant"])
		for bad in ("A", [1], None):
			with self.assertRaises(ValueError):
				staff.listing_changes({"A": set()}, "s@x", bad)

	def test_flags(self):
		self.assertTrue(staff.open_to_everyone(set()))
		self.assertTrue(staff.works_at(set(), "s@x"))
		self.assertFalse(staff.works_at({"m@x"}, "s@x"))
		self.assertTrue(staff.only_user({"s@x"}, "s@x"))
		self.assertFalse(staff.needs_listing(set(), "s@x"))
		self.assertTrue(staff.needs_listing({"m@x"}, "s@x"))


if __name__ == "__main__":
	unittest.main()
