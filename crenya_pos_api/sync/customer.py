"""Create a Customer captured offline at the till."""

import frappe
from frappe.utils import cint

from crenya_pos_api.utils.dates import format_db_datetime


def _is_leaf(doctype, name):
	return bool(name) and frappe.db.get_value(doctype, name, "is_group") == 0


def _default_customer_group(ctx, requested):
	if requested:
		return requested
	for row in ctx.profile.get("customer_groups") or []:
		if _is_leaf("Customer Group", row.customer_group):
			return row.customer_group
	selling_default = frappe.db.get_single_value("Selling Settings", "customer_group")
	if _is_leaf("Customer Group", selling_default):
		return selling_default
	if ctx.profile.customer:
		return frappe.db.get_value("Customer", ctx.profile.customer, "customer_group")
	return selling_default


def _default_territory(ctx, requested):
	if requested:
		return requested
	selling_default = frappe.db.get_single_value("Selling Settings", "territory")
	if selling_default:
		return selling_default
	if ctx.profile.customer:
		territory = frappe.db.get_value("Customer", ctx.profile.customer, "territory")
		if territory:
			return territory
	from frappe.utils.nestedset import get_root_of

	return get_root_of("Territory")


def create_customer(ctx, data):
	customer = frappe.new_doc("Customer")
	customer.update(
		{
			"customer_name": data["customer_name"],
			"customer_type": "Individual",
			"customer_group": _default_customer_group(ctx, data.get("customer_group")),
			"territory": _default_territory(ctx, data.get("territory")),
			"mobile_no": data.get("mobile_no"),
			"email_id": data.get("email_id"),
			"tax_id": data.get("tax_id"),
			"crenya_local_id": data["local_id"],
		}
	)
	customer.insert()
	return customer


def customer_result_fields(doc):
	return {
		"doctype": "Customer",
		"name": doc.name,
		"docstatus": cint(doc.docstatus),
		"modified": format_db_datetime(doc.modified),
		"totals": None,
		"fawtara_status": None,
	}
