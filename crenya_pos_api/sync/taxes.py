"""Sales Taxes and Charges Templates a till may switch between.

A till computes taxes offline, so only templates whose rows are all
`On Net Total` are offered. The POS Profile's own template is always listed
(it is checked separately by `assert_supported_taxes`, which fails with
`unsupported_tax`); every other template with another charge type is simply
left out.
"""

import frappe
from frappe.utils import cint

from crenya_pos_api.sync.context import SUPPORTED_CHARGE_TYPES
from crenya_pos_api.sync.errors import VALIDATION, SyncError
from crenya_pos_api.utils.decimal import format_number

TEMPLATE_DOCTYPE = "Sales Taxes and Charges Template"
TAX_ROW_FIELDS = [
	"parent",
	"idx",
	"charge_type",
	"account_head",
	"description",
	"rate",
	"included_in_print_rate",
]


def is_offline_template(rows):
	"""True when every row can be computed by the till (an empty template adds no tax)."""
	return all(row.get("charge_type") in SUPPORTED_CHARGE_TYPES for row in rows)


def select_tax_templates(templates, rows_by_template, default):
	"""Pure selection of the templates offered to the till.

	`templates`: [{name, title}] of the company's enabled templates;
	`rows_by_template`: {name: [tax rows in idx order]}; `default`: the POS
	Profile's template (may be missing from `templates`, e.g. disabled later).
	Returns the protocol list, default first, then by title.
	"""
	by_name = {row["name"]: row for row in templates}
	if default and default not in by_name:
		by_name[default] = {"name": default, "title": default}

	result = []
	for name, template in by_name.items():
		rows = rows_by_template.get(name) or []
		if name != default and not is_offline_template(rows):
			continue
		result.append(
			{
				"name": name,
				"title": template.get("title") or name,
				"is_default": name == default,
				"taxes": [
					{
						"account_head": row.get("account_head"),
						"description": row.get("description"),
						"rate": format_number(row.get("rate") or 0),
						"included_in_print_rate": bool(cint(row.get("included_in_print_rate"))),
					}
					for row in rows
				],
			}
		)
	result.sort(key=lambda row: (not row["is_default"], (row["title"] or "").casefold(), row["name"]))
	return result


def resolve_invoice_template(requested, allowed, default):
	"""Template to apply to a pushed invoice: the till's choice when allowed, else the profile default."""
	if not requested:
		return default
	if requested not in allowed:
		raise SyncError(
			VALIDATION,
			f"Taxes template {requested} is not available at this till "
			"(disabled, another company, or not all rows 'On Net Total')",
		)
	return requested


def get_tax_templates(profile):
	"""Protocol `profile.tax_templates` for the POS Profile's company."""
	default = profile.get("taxes_and_charges") or None
	templates = frappe.get_all(
		TEMPLATE_DOCTYPE,
		filters={"company": profile.company, "disabled": 0},
		fields=["name", "title"],
		order_by="name asc",
	)
	names = [row.name for row in templates]
	if default and default not in names:
		names.append(default)
	if not names:
		return []

	rows_by_template = {}
	for row in frappe.get_all(
		"Sales Taxes and Charges",
		filters={"parent": ["in", names], "parenttype": TEMPLATE_DOCTYPE},
		fields=TAX_ROW_FIELDS,
		order_by="parent asc, idx asc",
	):
		rows_by_template.setdefault(row.parent, []).append(row)

	if default and default not in {row.name for row in templates}:
		title = frappe.db.get_value(TEMPLATE_DOCTYPE, default, "title")
		templates.append(frappe._dict(name=default, title=title or default))
	return select_tax_templates(templates, rows_by_template, default)


def allowed_template_names(profile):
	return {row["name"] for row in get_tax_templates(profile)}
