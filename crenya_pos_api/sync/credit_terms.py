"""Credit days as a House Account payment terms template (0.16.0, House Account phase 2).

A customer's "credit days" is ERPNext's own payment terms: a *Payment Terms Template* named
"House Account N days" with one row (100 %, "Day(s) after invoice date", N days), set as the
Customer's `payment_terms`. ERPNext then sets the due date of every invoice (the till's included:
a cash or split bill is `is_pos` with `due_date` = posting + N and no payment schedule, a wholly
on-account bill a one-row schedule), so nothing is computed here. The template has no company and a
detail row saves without a payment term.

Which terms a customer gets follows ERPNext's order: the Customer's, else its Customer Group's,
else the Company's (`customer_terms`). `apply_customer_terms` changes a Customer document in memory
only (the caller saves it and writes the audit comment): this company's credit-limit row and/or the
customer's own days. It is the one place the limit row logic lives (`save_credit_limit` calls it).
"""

import re

import frappe
from frappe import _

from crenya_pos_api.utils.decimal import as_decimal, format_money

TEMPLATE_PREFIX = "House Account "
TEMPLATE_SUFFIX = " days"
MAX_CREDIT_DAYS = 365
DAYS_BASIS = "Day(s) after invoice date"

TEMPLATE = "Payment Terms Template"
DETAIL = "Payment Terms Template Detail"
DAYS_ERROR = f"Credit days must be a whole number from 0 to {MAX_CREDIT_DAYS}"


class _Unset:
	"""The sentinel of a part `apply_customer_terms` leaves alone (None is a value: clear)."""

	def __repr__(self):
		return "UNSET"

	def __bool__(self):
		return False


UNSET = _Unset()


def template_name(days: int) -> str:
	"""The template's name for `days` (pure): "House Account 30 days"."""
	return f"{TEMPLATE_PREFIX}{days}{TEMPLATE_SUFFIX}"


def parse_days(value) -> int | None:
	"""The credit days typed (pure): None or blank is None; an int or a string of ASCII digits from 0
	to 365 is that int; ValueError for anything else (a bool, a float, a sign, a decimal point)."""
	if value is None or (isinstance(value, str) and not value.strip()):
		return None
	if isinstance(value, bool):
		raise ValueError(DAYS_ERROR)
	if isinstance(value, str):
		text = value.strip()
		if not re.fullmatch(r"[0-9]+", text):
			raise ValueError(DAYS_ERROR)
		value = int(text)
	if not isinstance(value, int) or not 0 <= value <= MAX_CREDIT_DAYS:
		raise ValueError(DAYS_ERROR)
	return value


def _whole_days(days) -> int:
	"""`days` as given to a function that needs a number: not blank, in range, not a bool."""
	parsed = parse_days(days)
	if parsed is None or isinstance(days, str):
		raise ValueError(DAYS_ERROR)
	return parsed


def template_days(template: str | None) -> int | None:
	"""N when `template` has exactly one row, based on the invoice date in days, for 100 % of the
	invoice; None for any other template (or none)."""
	if not template:
		return None
	rows = frappe.get_all(
		DETAIL,
		filters={"parent": template, "parenttype": TEMPLATE},
		fields=["due_date_based_on", "invoice_portion", "credit_days"],
		limit_page_length=3,
	)
	if len(rows) != 1:
		return None
	row = rows[0]
	if row.due_date_based_on != DAYS_BASIS or as_decimal(row.invoice_portion) != 100:
		return None
	return int(row.credit_days or 0)


def _checked(name: str, days: int) -> str:
	if template_days(name) != days:
		frappe.throw(_("Payment Terms Template {0} is not {1} days").format(name, days))
	return name


def days_template(days: int) -> str:
	"""The template for `days`, made when missing. An existing template of that name that is not
	N days is refused (ValidationError); a second worker making it at the same time is read back."""
	days = _whole_days(days)
	name = template_name(days)
	if frappe.db.exists(TEMPLATE, name):
		return _checked(name, days)
	doc = frappe.get_doc(
		{
			"doctype": TEMPLATE,
			"template_name": name,
			"terms": [
				{
					"description": name,
					"invoice_portion": 100,
					"due_date_based_on": DAYS_BASIS,
					"credit_days": days,
				}
			],
		}
	)
	try:
		doc.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		return _checked(name, days)
	return doc.name


def customer_terms(customer: str, company: str) -> dict:
	"""The payment terms ERPNext applies to `customer`'s invoices of `company`: the Customer's own
	template, else its Customer Group's, else the Company's. {"payment_terms": template or None,
	"credit_days": N or None (None when the template is not a plain N days one),
	"credit_days_source": "customer" | "group" | "company" | None (None when no days)}."""
	template, source = None, None
	own, group = frappe.db.get_value("Customer", customer, ["payment_terms", "customer_group"]) or (
		None,
		None,
	)
	if own:
		template, source = own, "customer"
	elif group:
		template = frappe.db.get_value("Customer Group", group, "payment_terms")
		source = "group" if template else None
	if not template and company:
		template = frappe.db.get_value("Company", company, "payment_terms")
		source = "company" if template else None
	days = template_days(template)
	return {
		"payment_terms": template or None,
		"credit_days": days,
		"credit_days_source": source if days is not None else None,
	}


def apply_customer_terms(doc, company, *, limit=UNSET, days=UNSET) -> dict:
	"""Change the Customer document `doc` (new or saved) in memory; the caller saves it.

	`limit` (Decimal or None) sets or removes this company's *Credit Limit* row, other companies' rows
	untouched: None removes it, except a row that bypasses the check, which is kept at 0; further rows
	of the company are dropped. `days` (int or None) is the customer's own credit days: None clears
	`payment_terms`, an int sets it to that days' template. UNSET leaves the part alone. Returns
	{"old_limit", "new_limit", "old_days", "new_days"} (limits Decimal or None, days int or None)."""
	rows = [row for row in doc.get("credit_limits") or [] if row.company == company]
	old_limit = as_decimal(rows[0].credit_limit) if rows and as_decimal(rows[0].credit_limit) > 0 else None
	old_days = template_days(doc.get("payment_terms"))
	new_limit, new_days = old_limit, old_days
	if limit is not UNSET:
		new_limit = limit
		if limit is None:
			for row in rows:
				if row.get("bypass_credit_limit_check"):
					row.credit_limit = 0
				else:
					doc.remove(row)
		elif rows:
			rows[0].credit_limit = float(limit)
			for extra in rows[1:]:
				doc.remove(extra)
		else:
			doc.append("credit_limits", {"company": company, "credit_limit": float(limit)})
	if days is not UNSET:
		if days is None:
			doc.payment_terms = None
			new_days = None
		else:
			doc.payment_terms = days_template(days)
			new_days = int(days)
	return {"old_limit": old_limit, "new_limit": new_limit, "old_days": old_days, "new_days": new_days}


def terms_audit_text(company, change, source, user, precision) -> str:
	"""The comment a credit terms change leaves on the Customer (pure)."""

	def limit(value):
		return "no limit of its own" if value is None else format_money(value, precision)

	def shown_days(value):
		return "none" if value is None else str(value)

	return (
		f"Credit terms for {company} changed: limit {limit(change['old_limit'])} → "
		f"{limit(change['new_limit'])}, days {shown_days(change['old_days'])} → "
		f"{shown_days(change['new_days'])} from {source} by {user}"
	)


__all__ = [
	"DAYS_BASIS",
	"MAX_CREDIT_DAYS",
	"TEMPLATE_PREFIX",
	"TEMPLATE_SUFFIX",
	"UNSET",
	"apply_customer_terms",
	"customer_terms",
	"days_template",
	"parse_days",
	"template_days",
	"template_name",
	"terms_audit_text",
]
