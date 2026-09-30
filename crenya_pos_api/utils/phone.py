"""Dialling codes from Frappe's country data (`frappe.geo.country_info`), used by the till to complete
customer phone numbers.

Pure helpers: they take the country data as a dict (country name -> info with `code` and `isd`), so
they run without a site. No country is assumed: an unknown country has no dialling code.
"""

import unicodedata


def isd_code(value):
	"""A country's `isd` as `+<digits>`, or "" when it has none."""
	if value is None or isinstance(value, bool):
		return ""
	text = str(value).strip()
	if not text:
		return ""
	return text if text.startswith("+") else f"+{text}"


def _country_name(country):
	return country.strip() if isinstance(country, str) else ""


def _sort_name(name):
	"""Case- and accent-insensitive sort key, so "Åland Islands" sorts with the A's."""
	plain = "".join(ch for ch in unicodedata.normalize("NFKD", name) if not unicodedata.combining(ch))
	return plain.casefold()


def phone_country_code(country, country_info):
	"""Dialling code of an ERPNext Country name, or "" when the country is empty or unknown."""
	name = _country_name(country)
	info = country_info.get(name) if name else None
	return isd_code(info.get("isd")) if isinstance(info, dict) else ""


def build_phone_country_codes(country_info, company_country=None):
	"""`[{iso, name, code}]` for every country with a dialling code: the company's country first,
	then all others by name."""
	first = _country_name(company_country)
	rows = []
	for name, info in country_info.items():
		if not isinstance(info, dict):
			continue
		code = isd_code(info.get("isd"))
		if not code:
			continue
		rows.append({"iso": str(info.get("code") or "").strip().upper(), "name": name, "code": code})
	rows.sort(key=lambda row: (row["name"] != first, _sort_name(row["name"]), row["name"]))
	return rows
