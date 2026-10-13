"""Setup checks: what an outlet needs, read live and shown on an outlet's settings page of
app.crenya.com. Every check is a row `{key, status ("ok" | "warn" | "missing"), label, detail,
fix}`: `label` and `fix` are fixed English sentences (the web app has their Arabic), `detail` the
values read. Nothing here changes a setting. Products add their own checks with `check`.

Money and rounding: `money_setup_checks`, whether ERPNext's precision and rounding of the
outlet's currency are the tills'. The tills round at the currency's decimals with Banker's
Rounding (pos-core money.rs); a site that rounds or keeps fewer decimals otherwise books totals a
smallest unit away, or refuses a third decimal.
"""

import math

import frappe
from frappe import _
from frappe.utils import cint

from crenya_pos_api.compat import frappe_major, number_format_precisions
from crenya_pos_api.sync.locale_data import BANKERS, rounding_method, system_number_format

OK = "ok"
WARN = "warn"
MISSING = "missing"

# the money fields whose precision ERPNext books a till's bill and expense at
MONEY_FIELDS = (
	("Sales Invoice", "grand_total", "Sales Invoice grand total"),
	("Sales Invoice Item", "rate", "Sales Invoice item rate"),
	("Purchase Invoice", "grand_total", "Purchase Invoice grand total"),
)
# `frappe.model.meta.get_field_precision`'s float precision while System Settings leave it empty
FRAPPE_DEFAULT_FLOAT_PRECISION = 3
# Frappe's number format while System Settings leave it empty (`get_precision_from_currency_format`
# on v15, `frappe.locale.get_number_format` on v16)
FRAPPE_DEFAULT_NUMBER_FORMAT = "#,###.##"
# Frappe 15 rounds a new child row before it knows the currency (v16 reads the parent's)
TRAP_FRAPPE_MAJOR = 15


def check(key, status, label, detail=None, fix=None):
	"""One setup check row; `fix` only when the check is not ok."""
	return {
		"key": key,
		"status": status,
		"label": label,
		"detail": detail,
		"fix": None if status == OK else fix,
	}


# money and rounding


def fraction_decimals(fraction_units) -> int | None:
	"""The decimals a Currency's Fraction Units mean (1000 baisa → 3, 100 fils → 2, 1 → 0); None
	when it is empty or not a power of ten."""
	units = cint(fraction_units)
	if units <= 0:
		return None
	decimals = round(math.log10(units))
	return decimals if 10**decimals == units else None


def money_facts(currency) -> dict:
	"""What ERPNext's precision and rounding of `currency` depend on, read live."""
	row = frappe.get_cached_value("Currency", currency, ["number_format", "fraction_units"], as_dict=True)
	row = row or {}
	return {
		"currency": currency,
		"frappe_major": frappe_major(),
		"rounding_method": rounding_method(),
		"currency_precision": cint(frappe.db.get_default("currency_precision")) or None,
		"float_precision": cint(frappe.db.get_default("float_precision")) or None,
		"number_format": system_number_format() or None,
		"use_number_format_from_currency": bool(
			cint(frappe.get_system_settings("use_number_format_from_currency"))
		),
		"currency_number_format": row.get("number_format") or None,
		"fraction_units": cint(row.get("fraction_units")) or None,
		"precisions": {
			label: cint(frappe.get_precision(doctype, fieldname, currency=currency))
			for doctype, fieldname, label in MONEY_FIELDS
		},
	}


def money_setup_checks(currency) -> list[dict]:
	"""The money and rounding checks of an outlet's currency on this site (`money_checks` of its
	`money_facts`); none without a currency."""
	if not currency:
		return []
	return money_checks(money_facts(currency), number_format_precisions())


def money_checks(facts: dict, formats: dict) -> list[dict]:
	"""The checks of `money_facts` (pure). `formats` is {number format: decimals}
	(`compat.number_format_precisions`)."""
	currency = facts["currency"]
	# an empty system format is Frappe's default, as get_precision reads it
	facts = {**facts, "number_format": facts["number_format"] or FRAPPE_DEFAULT_NUMBER_FORMAT}
	system_format = facts["number_format"]
	system_decimals = formats.get(system_format)
	own_format = facts["currency_number_format"]
	# the format a Currency without its own shows in (the tills' `currency_view` too)
	shown_format = own_format or system_format
	format_decimals = formats.get(shown_format)
	units_decimals = fraction_decimals(facts["fraction_units"])
	# the currency's own decimals: its Fraction Units, else its Number Format
	decimals = units_decimals if units_decimals is not None else format_decimals
	suggested = ", ".join(f for f, d in formats.items() if decimals is not None and d == decimals)
	return [
		_rounding_check(facts),
		_precision_check(facts, decimals, system_decimals, format_decimals, suggested),
		_float_check(facts, decimals),
		_new_rows_check(facts, decimals, system_decimals),
		_currency_format_check(currency, own_format, shown_format, format_decimals, units_decimals, facts),
	]


def _rounding_check(facts):
	method = facts["rounding_method"]
	return check(
		"rounding_method",
		OK if method == BANKERS else WARN,
		_("Rounding method matches the tills (Banker's Rounding)"),
		_(method),
		_("System Settings → Rounding Method → Banker's Rounding (affects every document from now on)"),
	)


def _precision_check(facts, decimals, system_decimals, format_decimals, suggested):
	currency = facts["currency"]
	label = _("Money precision covers the currency's decimals")
	if decimals is None:
		return check(
			"money_precision",
			WARN,
			label,
			_("The decimals of {0} are unknown").format(currency),
			_("Currency → Fraction Units → the number of smallest units in one (1000 for 3 decimals)"),
		)
	precisions = facts["precisions"]
	short = {name: p for name, p in precisions.items() if p < decimals}
	detail = _("{0} has {1} decimals: {2}").format(
		currency, decimals, ", ".join(f"{_(name)} {p}" for name, p in precisions.items())
	)
	if not short:
		return check("money_precision", OK, label, detail)
	if facts["currency_precision"] is not None:
		# a set Currency Precision is every currency's
		fix = _("System Settings → Currency Precision → the currency's decimals, or empty")
	elif facts["use_number_format_from_currency"] and (format_decimals or 0) < decimals:
		fix = _("Currency → Number Format → a format with the currency's decimals")
	elif (system_decimals or 0) < decimals:
		fix = _("System Settings → Number Format → a format with the currency's decimals")
	else:
		# a field's own precision (a Property Setter or a Customize Form change)
		fix = _("Customize Form → the field's Precision → empty")
	if suggested:
		detail = _("{0} (a format with {1} decimals: {2})").format(detail, decimals, suggested)
	return check("money_precision", MISSING, label, detail, fix)


def _float_check(facts, decimals):
	precision = facts["float_precision"] or FRAPPE_DEFAULT_FLOAT_PRECISION
	detail = (
		str(precision) if facts["float_precision"] else _("empty: {0}").format(FRAPPE_DEFAULT_FLOAT_PRECISION)
	)
	return check(
		"float_precision",
		OK if decimals is None or precision >= decimals else WARN,
		_("Float precision covers the currency's decimals"),
		detail,
		_("System Settings → Float Precision → at least the currency's decimals"),
	)


def _new_rows_check(facts, decimals, system_decimals):
	"""Frappe 15: with Use Number Format from Currency on and Currency Precision empty, a new
	invoice row is rounded at the system Number Format's decimals before its currency is known,
	so a third decimal of a till's bill or expense is lost and the booking fails."""
	trapped = (
		facts["frappe_major"] == TRAP_FRAPPE_MAJOR
		and facts["use_number_format_from_currency"]
		and (facts["currency_precision"] or 0) < (decimals or 0)
		and (system_decimals or 0) < (decimals or 0)
	)
	return check(
		"new_rows",
		MISSING if trapped else OK,
		_("New invoice rows keep the currency's decimals"),
		_("Frappe {0} rounds a new row at the system Number Format {1} ({2} decimals)").format(
			facts["frappe_major"], facts["number_format"], system_decimals
		)
		if trapped
		else None,
		_(
			"System Settings → Use Number Format from Currency → off, and Number Format → a format with the currency's decimals"
		),
	)


def _currency_format_check(currency, own_format, shown_format, format_decimals, units_decimals, facts):
	label = _("The currency's Number Format matches its Fraction Units")
	shown = own_format or _("empty: {0}").format(shown_format)
	if units_decimals is None:
		return check(
			"currency_format",
			WARN,
			label,
			_("{0}: Fraction Units {1}").format(currency, facts["fraction_units"] or _("empty")),
			_("Currency → Fraction Units → the number of smallest units in one (1000 for 3 decimals)"),
		)
	return check(
		"currency_format",
		OK if format_decimals == units_decimals else WARN,
		label,
		_("{0}: Number Format {1} ({2} decimals), Fraction Units {3} ({4} decimals)").format(
			currency, shown, format_decimals, facts["fraction_units"], units_decimals
		),
		_("Currency → Number Format → a format with as many decimals as its Fraction Units"),
	)
