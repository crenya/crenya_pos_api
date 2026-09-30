"""Locale data for the till: currency display, dialling codes, cash denominations.

Nothing here is hardcoded: currency data comes from the Currency record (number format falling back
to System Settings), dialling codes from Frappe's country data and denominations from
Crenya Cash Denomination records configured on the site.
"""

import frappe
from frappe.utils import cint
from frappe.utils.caching import request_cache

from crenya_pos_api.utils.decimal import as_decimal, format_number
from crenya_pos_api.utils.phone import build_phone_country_codes, phone_country_code

DENOMINATION_DOCTYPE = "Crenya Cash Denomination"
DENOMINATION_KINDS = ("note", "coin")
CURRENCY_FIELDS = [
	"symbol",
	"symbol_on_right",
	"fraction",
	"fraction_units",
	"number_format",
	"smallest_currency_fraction_value",
]


def _text(value):
	if value is None:
		return None
	return str(value).strip() or None


def currency_view(code, row, precision, fallback_number_format=None):
	"""Bootstrap `currency` object from a Currency record's values (pure)."""
	row = row or {}
	smallest = as_decimal(row.get("smallest_currency_fraction_value"))
	return {
		"code": code,
		"symbol": _text(row.get("symbol")),
		"symbol_on_right": bool(cint(row.get("symbol_on_right"))),
		"fraction": _text(row.get("fraction")),
		"fraction_units": cint(row.get("fraction_units")) or None,
		"number_format": _text(row.get("number_format")) or _text(fallback_number_format) or "",
		"precision": int(precision),
		"smallest_currency_fraction_value": format_number(smallest, precision) if smallest > 0 else None,
	}


def format_denominations(rows):
	"""`[{value, label, kind}]`, highest value first (pure); rows without a positive value are skipped.

	Values keep their own decimals (a coin may be smaller than the money precision).
	"""
	result = []
	for row in rows:
		value = as_decimal(row.get("value"))
		if value <= 0:
			continue
		kind = row.get("kind")
		result.append(
			{
				"value": format_number(value),
				"label": _text(row.get("label")) or format_number(value),
				"kind": kind if kind in DENOMINATION_KINDS else DENOMINATION_KINDS[0],
				"_sort": value,
			}
		)
	result.sort(key=lambda row: row["_sort"], reverse=True)
	for row in result:
		del row["_sort"]
	return result


def system_number_format():
	return frappe.get_system_settings("number_format") or frappe.db.get_default("number_format")


def currency_info(currency, precision):
	row = frappe.get_cached_value("Currency", currency, CURRENCY_FIELDS, as_dict=True) if currency else None
	return currency_view(currency, row, precision, system_number_format())


@request_cache
def country_info():
	"""Frappe's country data (country name -> code, isd, currency, …), read once per request."""
	from frappe.geo.country_info import get_all

	return get_all()


@request_cache
def phone_country_codes(company_country=None):
	return build_phone_country_codes(country_info(), company_country)


def company_phone_country_code(country):
	"""Dialling code of the company's country, "" when unknown (no fallback country)."""
	return phone_country_code(country, country_info())


def cash_denominations(currency):
	if not currency:
		return []
	rows = frappe.get_all(
		DENOMINATION_DOCTYPE,
		filters={"currency": currency, "enabled": 1},
		fields=["value", "label", "kind"],
		order_by="value desc",
	)
	return format_denominations(rows)
