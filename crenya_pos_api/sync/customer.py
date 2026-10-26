"""Create a Customer captured offline at the till; the device is the authority, not desk roles."""

import frappe
from frappe.utils import cint

from crenya_pos_api.sync.registry import customer_extenders, extend_customer
from crenya_pos_api.utils.dates import format_db_datetime

# Customer Small Text (0.15.0, provider settlement NC2): the one line the cashier typed
LOCATION_FIELD = "crenya_location"


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


def create_customer(ctx, data, notes=None):
	"""Insert the till's new Customer. The `crenya_pos_customer_extenders` of other apps run on it
	before the insert (they get the validated payload, its `credit_facility` included) and the
	Comment texts they return are added after it. Without an extender `credit_facility` is dropped
	with a note: the customer is created without a House Account."""
	notes = [] if notes is None else notes
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
	if data.get("location") and customer.meta.has_field(LOCATION_FIELD):
		customer.set(LOCATION_FIELD, data["location"])
	if data.get("credit_facility") and not customer_extenders():
		notes.append("credit_facility ignored: no app handles a House Account grant here")
	# the House Account facility is switched on only by an extender, through the server-only grant
	# flag (sync.credit.GRANT_FLAG); nothing of the payload reaches `customer.flags`
	comments = extend_customer(ctx, customer, data, notes)
	# created for the authorised device (get_device_context in the push): a cashier needs no
	# desk role on Customer, as the invoice push posts its sale (0.9.3)
	customer.insert(ignore_permissions=True)
	# the device name is the cashier's text and a Comment is shown as HTML
	customer.add_comment("Info", f"Created at the till {frappe.utils.escape_html(_device_label(ctx))}")
	for text in comments:
		customer.add_comment("Info", text)
	return customer


def _device_label(ctx):
	name = (ctx.device or {}).get("device_name")
	return f"{name} ({ctx.device_id})" if name else ctx.device_id


def customer_result_fields(doc):
	return {
		"doctype": "Customer",
		"name": doc.name,
		"docstatus": cint(doc.docstatus),
		"modified": format_db_datetime(doc.modified),
		"totals": None,
		"fawtara_status": None,
	}
