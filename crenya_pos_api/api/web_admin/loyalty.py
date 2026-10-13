"""A shop's loyalty programs on app.crenya.com: ERPNext's Loyalty Programs of the shop's company
(`web.loyalty_shape`), listed, created and changed by the shop's managers (`scope.manage`), so no
owner needs the desk to set loyalty up. The tills use them as before (`sync/loyalty.py`).

A program is never deleted or renamed here: set its `to_date` to end it. Its redemption account
and cost center must be the company's (not groups); ERPNext's own checks still decide.
"""

from datetime import date

import frappe
from frappe import _
from frappe.utils import cint, getdate, today

from crenya_pos_api.api.web_admin.scope import manage
from crenya_pos_api.sync.context import money_precision
from crenya_pos_api.utils.dates import format_date, format_db_datetime
from crenya_pos_api.utils.decimal import format_money, format_number
from crenya_pos_api.web.context import check_modified, invalid, refuse
from crenya_pos_api.web.loyalty_shape import PLAIN_FIELDS, loyalty_problem, to_loyalty_program
from crenya_pos_api.web.reports import money_view

PROGRAM_DOCTYPE = "Loyalty Program"
TIER_DOCTYPE = "Loyalty Program Collection"
# how many names each Link choice offers (a datalist; any other may be typed)
MAX_CHOICES = 200


def _program_types() -> list:
	field = frappe.get_meta(PROGRAM_DOCTYPE).get_field("loyalty_program_type")
	return [option for option in (field.options or "").split("\n") if option] if field else []


def _active(row, on_date: date) -> bool:
	if row.from_date and getdate(row.from_date) > on_date:
		return False
	return not row.to_date or getdate(row.to_date) >= on_date


def _programs(ctx) -> dict:
	precision = money_precision(ctx.currency)
	rows = frappe.get_all(
		PROGRAM_DOCTYPE,
		filters={"company": ctx.company},
		fields=[
			"name",
			"loyalty_program_name",
			"auto_opt_in",
			"conversion_factor",
			"expiry_duration",
			"modified",
			*PLAIN_FIELDS,
		],
		order_by="loyalty_program_name asc",
	)
	tiers = {}
	for tier in frappe.get_all(
		TIER_DOCTYPE,
		filters={"parenttype": PROGRAM_DOCTYPE, "parent": ["in", [row.name for row in rows] or [""]]},
		fields=["parent", "tier_name", "min_spent", "collection_factor"],
		order_by="idx asc",
	):
		tiers.setdefault(tier.parent, []).append(
			{
				"tier_name": tier.tier_name,
				"min_spent": format_money(tier.min_spent or 0, precision),
				"collection_factor": format_number(tier.collection_factor or 0),
			}
		)
	on_date = getdate(today())
	return {
		"currency": money_view(ctx.currency),
		"rows": [
			{
				"name": row.name,
				"loyalty_program_name": row.loyalty_program_name,
				"loyalty_program_type": row.loyalty_program_type,
				"from_date": format_date(row.from_date),
				"to_date": format_date(row.to_date),
				"customer_group": row.customer_group or None,
				"customer_territory": row.customer_territory or None,
				"auto_opt_in": bool(cint(row.auto_opt_in)),
				"conversion_factor": format_number(row.conversion_factor or 0),
				"expiry_duration": cint(row.expiry_duration),
				"expense_account": row.expense_account or None,
				"cost_center": row.cost_center or None,
				"collection_rules": tiers.get(row.name, []),
				"active": _active(row, on_date),
				"modified": format_db_datetime(row.modified),
			}
			for row in rows
		],
		"choices": {
			"program_types": _program_types(),
			"customer_groups": frappe.get_all(
				"Customer Group", pluck="name", order_by="name asc", limit=MAX_CHOICES
			),
			"territories": frappe.get_all("Territory", pluck="name", order_by="name asc", limit=MAX_CHOICES),
			"expense_accounts": frappe.get_all(
				"Account",
				filters={"company": ctx.company, "is_group": 0, "disabled": 0, "root_type": "Expense"},
				pluck="name",
				order_by="name asc",
				limit=MAX_CHOICES,
			),
			"cost_centers": frappe.get_all(
				"Cost Center",
				filters={"company": ctx.company, "is_group": 0, "disabled": 0},
				pluck="name",
				order_by="name asc",
				limit=MAX_CHOICES,
			),
		},
	}


def _check_links(ctx, data):
	"""The redemption account and cost center are the company's, not groups; the customer group
	and territory exist."""
	for doctype, field in (("Account", "expense_account"), ("Cost Center", "cost_center")):
		row = frappe.db.get_value(doctype, data[field], ["company", "is_group"], as_dict=True)
		if not row or row.company != ctx.company or cint(row.is_group):
			invalid(_("{0} {1} is not a {0} of {2}").format(doctype, data[field], ctx.company))
	for doctype, field in (("Customer Group", "customer_group"), ("Territory", "customer_territory")):
		if data.get(field) and not frappe.db.exists(doctype, data[field]):
			invalid(_("{0} {1} does not exist").format(doctype, data[field]))


@frappe.whitelist(methods=["GET", "POST"])
def loyalty_programs(outlet: str) -> dict:
	"""The loyalty programs of the shop's company, with whether each is active today, and the
	choices the editor offers."""
	return _programs(manage(outlet))


@frappe.whitelist(methods=["POST"])
def save_loyalty_program(outlet: str, program: dict) -> dict:
	"""Create (no `name`) or change (with the `modified` `loyalty_programs` answered) a loyalty
	program of the shop's company. Answers `loyalty_programs`."""
	ctx = manage(outlet)
	data = frappe.parse_json(program)
	problem = loyalty_problem(data, money_precision(ctx.currency), _program_types())
	if problem:
		invalid(problem)
	_check_links(ctx, data)
	name = data.get("name")
	if name:
		if frappe.db.get_value(PROGRAM_DOCTYPE, name, "company") != ctx.company:
			refuse(_("Loyalty program {0} is not a program of {1}").format(name, ctx.company))
		check_modified(PROGRAM_DOCTYPE, name, data.get("modified"))
		doc = frappe.get_doc(PROGRAM_DOCTYPE, name)
	else:
		if frappe.db.exists(PROGRAM_DOCTYPE, data["loyalty_program_name"].strip()):
			invalid(_("A loyalty program named {0} already exists").format(data["loyalty_program_name"]))
		doc = frappe.new_doc(PROGRAM_DOCTYPE)
	doc.update(to_loyalty_program(data))
	doc.company = ctx.company
	doc.save(ignore_permissions=True)
	ctx.log(doc)
	return _programs(ctx)
