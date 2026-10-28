"""The common block and the date range of app.crenya.com's report answers: the shape
crenya_restaurant's owner API answers, so the web's shared till-admin pages read both products
alike. Money is a decimal string at the currency's precision."""

from datetime import date, datetime

import frappe
from frappe import _
from frappe.utils import get_system_timezone, now

from crenya_pos_api.sync.context import money_precision
from crenya_pos_api.sync.locale_data import currency_info, rounding_method, rounding_of

# the longest range a list answers
MAX_RANGE_DAYS = 366


def parse_date(value) -> date:
	"""A YYYY-MM-DD date, the whole value; anything else is frappe.ValidationError."""
	if isinstance(value, date) and not isinstance(value, datetime):
		return value
	if isinstance(value, str) and len(value) == 10:
		try:
			return date.fromisoformat(value)
		except ValueError:
			pass
	raise frappe.ValidationError(_("Dates must be YYYY-MM-DD."))


def date_range(from_date, to_date, max_days=MAX_RANGE_DAYS) -> tuple[date, date]:
	"""(from, to) as dates; frappe.ValidationError when not YYYY-MM-DD, reversed or longer than
	`max_days` (MAX_RANGE_DAYS by default)."""
	first, last = parse_date(from_date), parse_date(to_date)
	if last < first:
		raise frappe.ValidationError(_("From Date must be before To Date"))
	if (last - first).days + 1 > max_days:
		raise frappe.ValidationError(_("Choose at most {0} days.").format(max_days))
	return first, last


def money_view(currency: str) -> dict:
	"""The currency block: the Currency record's display, the money precision ERPNext books it at,
	and the site's rounding (`half_up` | `half_even`)."""
	precision = money_precision(currency)
	return {**currency_info(currency, precision), "rounding": rounding_of(rounding_method())}


def tax_label(company: str) -> str:
	return frappe.get_cached_value("Company", company, "crenya_tax_name") or _("Tax")


def common(company: str, currency: str) -> dict:
	"""The common block of every data answer."""
	return {
		"company": company,
		"currency": money_view(currency),
		"tax_label": tax_label(company),
		"time_zone": get_system_timezone(),
		"generated_at": now()[:19],
	}
