"""The outlet settings form of app.crenya.com (pure): built from custom field definitions in
order, and the problems of a change. Which settings only a System Manager changes
(`owner_only`) and which are never written from the web (`read_only`, besides the fields the
desk shows read-only) are the caller's: each product passes its own."""

import re
from decimal import Decimal

LAYOUT_FIELDTYPES = ("Section Break", "Column Break", "Tab Break")
NUMERIC_FIELDTYPES = ("Int", "Float", "Percent", "Currency")
TEXT_FIELDTYPES = ("Data", "Link", "Small Text", "Text", "Long Text")
_TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9])?$")
_NUMBER_RE = re.compile(r"^\s*-?(\d+(\.\d*)?|\.\d+)\s*$")


def settings_read_only(fields, read_only=frozenset()):
	"""The settings never written from app.crenya.com: `read_only` and the fields the desk shows
	read-only."""
	return set(read_only) | {field["fieldname"] for field in fields if field.get("read_only")}


def settings_form(fields, owner_editable, owner_only=frozenset(), read_only=frozenset()):
	"""The sections of the settings form, from custom field definitions in order: a Section Break
	opens a section, Column Breaks are dropped. Each field: fieldname, label, fieldtype, options,
	reqd, description, read_only (always for `settings_read_only`, and for `owner_only` unless
	`owner_editable`)."""
	locked = settings_read_only(fields, read_only)
	sections = []
	for field in fields:
		fieldtype = field["fieldtype"]
		if fieldtype == "Section Break":
			sections.append({"title": field.get("label") or "", "fields": []})
			continue
		if fieldtype in LAYOUT_FIELDTYPES:
			continue
		if not sections:
			sections.append({"title": "", "fields": []})
		fieldname = field["fieldname"]
		sections[-1]["fields"].append(
			{
				"fieldname": fieldname,
				"label": field.get("label") or fieldname,
				"fieldtype": fieldtype,
				"options": field.get("options") or None,
				"reqd": bool(field.get("reqd")),
				"description": field.get("description") or None,
				"read_only": fieldname in locked or (fieldname in owner_only and not owner_editable),
			}
		)
	return [section for section in sections if section["fields"]]


def settings_change_problem(values, fields, owner_editable, owner_only=frozenset(), read_only=frozenset()):
	"""Why app.crenya.com may not write `values` ({fieldname: value}), as ("invalid" | "refused",
	message), or None. Invalid: not an object, a field that is not one of `fields`, a read-only
	one (named; never silently dropped), or a value its fieldtype does not take
	(`setting_value_ok`). Refused: an `owner_only` field from someone who is not a System Manager
	(`owner_editable` false)."""
	if not isinstance(values, dict):
		return "invalid", "values must be an object of setting: value"
	known = {field["fieldname"] for field in fields if field["fieldtype"] not in LAYOUT_FIELDTYPES}
	unknown = sorted(name for name in values if name not in known)
	if unknown:
		return "invalid", f"Not an outlet setting: {', '.join(str(name) for name in unknown)}"
	locked = settings_read_only(fields, read_only)
	locked = sorted(name for name in values if name in locked)
	if locked:
		return "invalid", f"Cannot be changed here: {', '.join(locked)}"
	refused = sorted(name for name in values if name in owner_only)
	if refused and not owner_editable:
		return "refused", f"Only a System Manager changes {', '.join(refused)}"
	by_name = {field["fieldname"]: field for field in fields}
	for name in sorted(values):
		field = by_name[name]
		if not setting_value_ok(field, values[name]):
			return "invalid", f"Invalid value for {field.get('label') or name} ({name})"
	return None


def setting_value_ok(field, value):
	"""Whether `value` fits the setting's fieldtype, so nothing is saved as a silent 0 or a
	stringified list: Check 0 / 1; Int, Float, Percent, Currency a number (a whole one for Int);
	Select one of its options; Time "HH:MM" or "HH:MM:SS" below 24:00 (empty: the default); Data,
	Link and the text fields a string (empty: cleared)."""
	fieldtype = field["fieldtype"]
	if fieldtype == "Check":
		return not isinstance(value, float) and value in (0, 1)
	if fieldtype in NUMERIC_FIELDTYPES:
		if isinstance(value, bool):
			return False
		if isinstance(value, str):
			if not _NUMBER_RE.match(value):
				return False
			value = Decimal(value.strip())
		elif isinstance(value, int | float):
			value = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
		else:
			return False
		if not value.is_finite():
			return False
		return fieldtype != "Int" or value == value.to_integral_value()
	if fieldtype == "Select":
		return isinstance(value, str) and value in (field.get("options") or "").split("\n")
	if fieldtype == "Time":
		return value is None or value == "" or (isinstance(value, str) and bool(_TIME_RE.match(value)))
	if fieldtype in TEXT_FIELDTYPES or fieldtype == "Datetime":
		return value is None or isinstance(value, str)
	return False
