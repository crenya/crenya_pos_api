"""The shared settings form of app.crenya.com (crenya_pos_api/web/forms.py, moved from
crenya_restaurant's admin.py): the form built from custom field definitions, and the problems of
a change, with the owner-only and read-only settings given by the caller (no site)."""

import unittest

from crenya_pos_api.web import forms

OWNER_ONLY = frozenset({"x_tip_item"})
READ_ONLY = frozenset({"x_mode"})

SETTINGS = [
	{"fieldname": "s1", "fieldtype": "Section Break", "label": "Outlet"},
	{"fieldname": "x_mode", "fieldtype": "Check", "label": "Mode"},
	{"fieldname": "x_view", "fieldtype": "Select", "label": "View", "options": "grid\nplan"},
	{"fieldname": "c1", "fieldtype": "Column Break"},
	{"fieldname": "x_tip_item", "fieldtype": "Link", "label": "Tip Item", "options": "Item"},
	{"fieldname": "s2", "fieldtype": "Section Break", "label": "Empty"},
	{"fieldname": "s3", "fieldtype": "Section Break", "label": "QR"},
	{"fieldname": "x_signed_at", "fieldtype": "Datetime", "label": "Signed Since", "read_only": 1},
	{"fieldname": "x_hours", "fieldtype": "Small Text", "description": "One range per line"},
]


def form(owner_editable, fields=SETTINGS):
	return forms.settings_form(fields, owner_editable, owner_only=OWNER_ONLY, read_only=READ_ONLY)


def problem(values, owner_editable=False, fields=SETTINGS):
	return forms.settings_change_problem(
		values, fields, owner_editable, owner_only=OWNER_ONLY, read_only=READ_ONLY
	)


class TestSettingsForm(unittest.TestCase):
	def test_owner_only_is_a_parameter(self):
		f = [{"fieldname": "x", "fieldtype": "Check", "label": "X"}]
		self.assertTrue(
			forms.settings_form(f, False, owner_only=frozenset({"x"}))[0]["fields"][0]["read_only"]
		)
		self.assertEqual(
			forms.settings_change_problem({"x": 1}, f, False, owner_only=frozenset({"x"}))[0], "refused"
		)
		# without the parameter nothing is owner-only
		self.assertFalse(forms.settings_form(f, False)[0]["fields"][0]["read_only"])
		self.assertIsNone(forms.settings_change_problem({"x": 1}, f, False))

	def test_read_only_is_a_parameter(self):
		f = [{"fieldname": "x", "fieldtype": "Check", "label": "X"}]
		for owner_editable in (False, True):
			self.assertTrue(
				forms.settings_form(f, owner_editable, read_only=frozenset({"x"}))[0]["fields"][0][
					"read_only"
				]
			)
			kind, message = forms.settings_change_problem(
				{"x": 1}, f, owner_editable, read_only=frozenset({"x"})
			)
			self.assertEqual(kind, "invalid")
			self.assertIn("x", message)

	def test_form_follows_the_sections(self):
		sections = form(False)
		self.assertEqual([section["title"] for section in sections], ["Outlet", "QR"])
		self.assertEqual(
			[field["fieldname"] for field in sections[0]["fields"]], ["x_mode", "x_view", "x_tip_item"]
		)
		fields = {field["fieldname"]: field for section in sections for field in section["fields"]}
		self.assertEqual(
			fields["x_view"],
			{
				"fieldname": "x_view",
				"label": "View",
				"fieldtype": "Select",
				"options": "grid\nplan",
				"reqd": False,
				"description": None,
				"read_only": False,
			},
		)
		self.assertEqual(fields["x_hours"]["label"], "x_hours")
		for fieldname in ("x_mode", "x_signed_at", "x_tip_item"):
			self.assertTrue(fields[fieldname]["read_only"], fieldname)
		owner = {
			field["fieldname"]: field["read_only"] for section in form(True) for field in section["fields"]
		}
		self.assertFalse(owner["x_tip_item"])
		self.assertTrue(owner["x_mode"])
		self.assertTrue(owner["x_signed_at"])

	def test_fields_before_any_section_get_an_untitled_one(self):
		fields = [{"fieldname": "x", "fieldtype": "Data", "label": "X"}, *SETTINGS]
		self.assertEqual(forms.settings_form(fields, True)[0]["title"], "")

	def test_change_problems(self):
		self.assertIsNone(problem({"x_view": "plan", "x_hours": ""}))
		self.assertIsNone(problem({}))
		self.assertIsNone(problem({"x_tip_item": "TIP"}, owner_editable=True))
		self.assertEqual(problem({"x_tip_item": "TIP"})[0], "refused")
		self.assertIn("x_tip_item", problem({"x_tip_item": "TIP"})[1])
		for values, named in (
			({"selling_price_list": "x", "x_view": "plan"}, "selling_price_list"),
			({"c1": 1}, "c1"),
			({"x_mode": 0}, "x_mode"),
			({"x_signed_at": None}, "x_signed_at"),
		):
			for owner_editable in (False, True):
				kind, message = problem(values, owner_editable)
				self.assertEqual(kind, "invalid", values)
				self.assertIn(named, message)
		# an unknown or read-only field is named before an owner-only one is refused
		self.assertEqual(problem({"x_tip_item": "TIP", "nope": 1})[0], "invalid")
		self.assertEqual(problem(["x_view"])[0], "invalid")


VALUE_FIELDS = [
	{"fieldname": "start", "fieldtype": "Time", "label": "Business Day Starts At"},
	{"fieldname": "lines", "fieldtype": "Int", "label": "Most Lines"},
	{"fieldname": "warn", "fieldtype": "Percent", "label": "Amber At"},
	{"fieldname": "beep", "fieldtype": "Check", "label": "Beep"},
	{"fieldname": "view", "fieldtype": "Select", "label": "View", "options": "grid\nplan"},
	{"fieldname": "title", "fieldtype": "Data", "label": "Name Shown to Guests"},
	{"fieldname": "item", "fieldtype": "Link", "label": "Tip Item", "options": "Item"},
	{"fieldname": "hours", "fieldtype": "Small Text", "label": "Opening Hours"},
]


class TestSettingValues(unittest.TestCase):
	def problem(self, name, value):
		return forms.settings_change_problem({name: value}, VALUE_FIELDS, owner_editable=True)

	def test_bad_values_are_named(self):
		for name, value, label in (
			("start", "25:00", "Business Day Starts At"),
			("start", "24:00", "Business Day Starts At"),
			("start", "abc", "Business Day Starts At"),
			("start", "2:00", "Business Day Starts At"),
			("start", 7200, "Business Day Starts At"),
			("title", ["a"], "Name Shown to Guests"),
			("title", {"a": 1}, "Name Shown to Guests"),
			("item", ["TIP"], "Tip Item"),
			("hours", ["11:00-15:00"], "Opening Hours"),
			("lines", "x", "Most Lines"),
			("lines", "", "Most Lines"),
			("lines", None, "Most Lines"),
			("lines", "2.5", "Most Lines"),
			("lines", True, "Most Lines"),
			("warn", "abc", "Amber At"),
			("warn", float("nan"), "Amber At"),
			("warn", [70], "Amber At"),
			("beep", 2, "Beep"),
			("beep", "1", "Beep"),
			("beep", 0.5, "Beep"),
			("view", "tiles", "View"),
			("view", "", "View"),
		):
			kind, message = self.problem(name, value)
			self.assertEqual(kind, "invalid", (name, value))
			self.assertEqual(message, f"Invalid value for {label} ({name})")

	def test_good_values(self):
		for name, value in (
			("start", "02:00"),
			("start", "23:59:59"),
			("start", ""),
			("start", None),
			("lines", 20),
			("lines", "20"),
			("lines", 20.0),
			("warn", "70.5"),
			("warn", 70),
			("beep", 0),
			("beep", 1),
			("beep", True),
			("view", "plan"),
			("title", "Café"),
			("title", None),
			("item", ""),
			("hours", "11:30-15:00\n18:00-02:00"),
		):
			self.assertIsNone(self.problem(name, value), (name, value))

	def test_fieldtype_groups(self):
		self.assertEqual(forms.LAYOUT_FIELDTYPES, ("Section Break", "Column Break", "Tab Break"))
		self.assertEqual(forms.NUMERIC_FIELDTYPES, ("Int", "Float", "Percent", "Currency"))
		self.assertEqual(forms.TEXT_FIELDTYPES, ("Data", "Link", "Small Text", "Text", "Long Text"))
		# a fieldtype the form does not know is never written
		self.assertFalse(forms.setting_value_ok({"fieldtype": "Table"}, []))


if __name__ == "__main__":
	unittest.main()
