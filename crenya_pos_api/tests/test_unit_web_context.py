"""The shared web-admin context of app.crenya.com (crenya_pos_api/web/context.py, moved from
crenya_restaurant's admin.py and api/web_admin.py): who changed what from where, the errors and the
small parsers, on the pure code (no site)."""

import unittest
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from unittest import mock

import frappe

from crenya_pos_api.web import context


class CommentedDoc:
	def __init__(self):
		self.comments = []

	def add_comment(self, comment_type, text):
		self.comments.append((comment_type, text))


class Thrown(Exception):
	pass


def throw(message, exc=None, title=None):
	raise Thrown(message, exc, title)


@mock.patch.object(context, "_", lambda text: text)
class TestErrors(unittest.TestCase):
	@mock.patch.object(context.frappe, "throw", side_effect=throw)
	def test_refuse_and_invalid(self, _throw):
		with self.assertRaises(Thrown) as caught:
			context.refuse("no")
		self.assertEqual(caught.exception.args, ("no", frappe.PermissionError, "Not allowed"))
		with self.assertRaises(Thrown) as caught:
			context.invalid("bad")
		self.assertEqual(caught.exception.args, ("bad", frappe.ValidationError, "Check the details"))

	@mock.patch.object(context.frappe, "throw", side_effect=throw)
	def test_checked_turns_a_value_error_into_invalid(self, _throw):
		self.assertEqual(context.checked(int, "12"), 12)
		with self.assertRaises(Thrown) as caught:
			context.checked(context.parse_flag, "maybe", "active")
		self.assertEqual(caught.exception.args[:2], ("active must be true or false", frappe.ValidationError))

	@mock.patch.object(context.frappe, "throw", side_effect=throw)
	def test_check_modified_refuses_a_stale_save(self, _throw):
		stored = datetime(2026, 10, 13, 9, 0, 0, 123456)
		db = mock.Mock()
		db.get_value.return_value = stored
		with mock.patch.object(context.frappe, "db", db, create=True):
			context.check_modified("POS Profile", "Main", "2026-10-13 09:00:00.123456")
			db.get_value.assert_called_once_with("POS Profile", "Main", "modified", for_update=True)
			with self.assertRaises(Thrown) as caught:
				context.check_modified("POS Profile", "Main", "2026-10-13 08:00:00.000000")
		message, exc, _title = caught.exception.args
		self.assertIs(exc, frappe.TimestampMismatchError)
		self.assertIn("2026-10-13 08:00:00.000000", message)
		self.assertIn("2026-10-13 09:00:00.123456", message)


class TestParsers(unittest.TestCase):
	def test_parse_flag(self):
		self.assertTrue(context.parse_flag("1", "active"))
		self.assertFalse(context.parse_flag(0, "active"))
		self.assertTrue(context.parse_flag("true", "active"))
		self.assertFalse(context.parse_flag(" False ", "active"))
		self.assertIsNone(context.parse_flag(None, "active"))
		self.assertTrue(context.parse_flag("", "active", default=True))
		with self.assertRaises(ValueError):
			context.parse_flag("maybe", "active")

	def test_setting_value_is_json(self):
		self.assertEqual(context.setting_value(timedelta(hours=2, minutes=30)), "02:30:00")
		self.assertEqual(context.setting_value(timedelta(hours=26)), "02:00:00")
		self.assertEqual(context.setting_value(time(23, 59, 59)), "23:59:59")
		self.assertEqual(
			context.setting_value(datetime(2026, 10, 13, 9, 0, 0, 5)), "2026-10-13 09:00:00.000005"
		)
		self.assertEqual(context.setting_value(date(2026, 10, 13)), "2026-10-13")
		for value in ("x", 1, None, 2.5):
			self.assertEqual(context.setting_value(value), value)

	def test_is_system_manager(self):
		with mock.patch.object(context.frappe, "get_roles", return_value=["System Manager"], create=True):
			self.assertTrue(context.is_system_manager("a@example.com"))
		with mock.patch.object(context.frappe, "get_roles", return_value=["Crenya POS User"], create=True):
			self.assertFalse(context.is_system_manager("a@example.com"))


class TestWebAdminContext(unittest.TestCase):
	def test_change_of_the_user_alone(self):
		ctx = context.WebAdminContext("owner@example.com")
		self.assertEqual(ctx.source, "app.crenya.com")
		self.assertEqual(
			(ctx.approver, ctx.outlet, ctx.profile, ctx.company, ctx.currency),
			("owner@example.com", None, None, None, None),
		)
		doc = CommentedDoc()
		ctx.log(doc)
		self.assertEqual(doc.comments, [("Info", "Changed from app.crenya.com by owner@example.com")])

	def test_outlet_brings_company_and_currency(self):
		profile = SimpleNamespace(name="Main", company="Muscat Shop LLC", currency="OMR")
		with mock.patch.object(context.frappe, "get_doc", return_value=profile, create=True) as get_doc:
			ctx = context.WebAdminContext("manager@example.com", "Main")
		get_doc.assert_called_once_with("POS Profile", "Main")
		self.assertEqual(
			(ctx.outlet, ctx.profile, ctx.company, ctx.currency), ("Main", profile, "Muscat Shop LLC", "OMR")
		)

	def test_profile_without_currency_takes_the_company_one(self):
		profile = SimpleNamespace(name="Main", company="Shop", currency=None)
		with (
			mock.patch.object(context.frappe, "get_doc", return_value=profile, create=True),
			mock.patch.object(context.frappe, "get_cached_value", return_value="AED", create=True) as company,
		):
			ctx = context.WebAdminContext("manager@example.com", "Main")
		self.assertEqual(ctx.currency, "AED")
		company.assert_called_once_with("Company", "Shop", "default_currency")


if __name__ == "__main__":
	unittest.main()
