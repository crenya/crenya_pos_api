"""Scale barcode rules of a POS Profile (`crenya_scale_barcode_rules`).

A scale prints an EAN-13 label: prefix, PLU (the item's barcode or code), the
embedded weight or price, check digit. Each rule names a prefix and how to read
the rest; non-empty rules replace the till's local scale settings.
"""

import re

import frappe
from frappe import _
from frappe.utils import cint

RULES_FIELD = "crenya_scale_barcode_rules"
RULE_DOCTYPE = "Crenya POS Scale Barcode Rule"
VALUE_TYPES = {"Weight": "weight", "Price": "price"}
MIN_PLU_LENGTH = 1
MAX_PLU_LENGTH = 10
MAX_VALUE_DECIMALS = 6
# EAN-13 without its check digit: prefix + PLU leave at least one digit for the value
DATA_DIGITS = 12

_PREFIX_RE = re.compile(r"^\d{1,3}$")


def _text(value):
	return str(value).strip() if value is not None else ""


def value_decimals(value):
	"""Configured decimals as an int, None when blank (the till's default for the value type)."""
	text = _text(value)
	if text == "":
		return None
	if not text.isdigit():
		raise ValueError(text)
	return int(text)


def validate_rules(rows):
	"""Rules of a POS Profile (no database access); normalizes prefixes in place."""
	seen = {}
	for row in rows:
		idx = row.get("idx")
		prefix = _text(row.get("prefix"))
		row.prefix = prefix
		if not _PREFIX_RE.match(prefix):
			frappe.throw(_("Scale barcode rule {0}: prefix must be 1 to 3 digits").format(idx))

		plu_length = cint(row.get("plu_length"))
		if plu_length < MIN_PLU_LENGTH or plu_length > MAX_PLU_LENGTH:
			frappe.throw(
				_("Scale barcode rule {0}: PLU length must be between {1} and {2}").format(
					idx, MIN_PLU_LENGTH, MAX_PLU_LENGTH
				)
			)
		if len(prefix) + plu_length >= DATA_DIGITS:
			frappe.throw(
				_(
					"Scale barcode rule {0}: prefix and PLU length together must be less than {1} digits"
				).format(idx, DATA_DIGITS)
			)

		if row.get("value_type") not in VALUE_TYPES:
			frappe.throw(
				_("Scale barcode rule {0}: value type must be {1}").format(idx, " or ".join(VALUE_TYPES))
			)

		try:
			decimals = value_decimals(row.get("value_decimals"))
		except ValueError:
			decimals = -1
		if decimals is not None and not 0 <= decimals <= MAX_VALUE_DECIMALS:
			frappe.throw(
				_("Scale barcode rule {0}: value decimals must be blank or between 0 and {1}").format(
					idx, MAX_VALUE_DECIMALS
				)
			)

		for other, other_idx in seen.items():
			if other == prefix:
				frappe.throw(
					_("Scale barcode rules {0} and {1} have the same prefix {2}").format(
						other_idx, idx, prefix
					)
				)
			if other.startswith(prefix) or prefix.startswith(other):
				frappe.throw(
					_("Scale barcode rules {0} and {1}: prefix {2} overlaps prefix {3}").format(
						other_idx, idx, other, prefix
					)
				)
		seen[prefix] = idx


def pos_profile_validate(doc, method=None):
	"""doc_events hook on POS Profile."""
	validate_rules(doc.get(RULES_FIELD) or [])


def format_rules(rows):
	"""Bootstrap `profile.scale_barcode_rules` (pure); rows that no longer validate are skipped."""
	result = []
	for row in sorted(rows, key=lambda r: cint(r.get("idx"))):
		prefix = _text(row.get("prefix"))
		plu_length = cint(row.get("plu_length"))
		value_type = VALUE_TYPES.get(row.get("value_type"))
		try:
			decimals = value_decimals(row.get("value_decimals"))
		except ValueError:
			continue
		if (
			not _PREFIX_RE.match(prefix)
			or not MIN_PLU_LENGTH <= plu_length <= MAX_PLU_LENGTH
			or len(prefix) + plu_length >= DATA_DIGITS
			or not value_type
			or (decimals is not None and decimals > MAX_VALUE_DECIMALS)
		):
			continue
		result.append(
			{"prefix": prefix, "plu_length": plu_length, "value_type": value_type, "value_decimals": decimals}
		)
	return result


def scale_barcode_rules(profile):
	return format_rules(profile.get(RULES_FIELD) or [])
