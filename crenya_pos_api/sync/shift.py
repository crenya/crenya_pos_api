"""Record a cashier shift closed at the till (Crenya POS Shift).

A shift is a report of what the till counted; it depends on nothing and never
blocks invoice sync. Invoices of the shift may arrive before or after it, so
the server only records how many of them (by `crenya_shift_id`) it already has
at the time of the push.
"""

import frappe
from frappe.utils import cint, flt, get_system_timezone

from crenya_pos_api.sync.cashier import enabled_user
from crenya_pos_api.sync.context import money_precision
from crenya_pos_api.sync.errors import VALIDATION, SyncError
from crenya_pos_api.utils.dates import format_db_datetime, to_site_naive

SHIFT_DOCTYPE = "Crenya POS Shift"
SHIFT_MONEY_FIELDS = ("opening_float", "sales_total", "returns_total", "net_total", "tax_total")
PAYMENT_MONEY_FIELDS = ("expected", "counted", "difference")


def _money(value, precision):
	return flt(f"{value:f}", precision)


def _resolve_cashier(data, notes):
	"""Same rule as the invoice's POS Cashier: an existing, enabled user; else the device user."""
	cashier = data.get("cashier")
	user = enabled_user(cashier)
	if user:
		return user
	if cashier:
		notes.append(f"Till cashier {cashier} is not an enabled user; recorded as {frappe.session.user}")
	return frappe.session.user


def count_shift_invoices(shift_local_id):
	return frappe.db.count("Sales Invoice", {"crenya_shift_id": shift_local_id})


def create_shift(ctx, data, notes):
	profile = ctx.profile
	if data["pos_profile"] != profile.name:
		raise SyncError(
			VALIDATION,
			f"Shift POS Profile {data['pos_profile']} does not match device profile {profile.name}",
		)
	if data["company"] != ctx.company:
		raise SyncError(VALIDATION, f"Shift company {data['company']} does not match {ctx.company}")

	for row in data["payments"]:
		if not frappe.db.exists("Mode of Payment", row["mode_of_payment"]):
			raise SyncError(VALIDATION, f"Mode of payment {row['mode_of_payment']} does not exist")

	precision = money_precision()
	time_zone = get_system_timezone()
	invoice_count = count_shift_invoices(data["local_id"])

	reported = len(data["invoice_local_ids"])
	if reported and reported != invoice_count:
		notes.append(f"Till reported {reported} invoices for the shift; {invoice_count} on the server")

	doc = frappe.new_doc(SHIFT_DOCTYPE)
	doc.update(
		{
			"local_id": data["local_id"],
			"shift_number": data["shift_number"],
			"device": ctx.device_id,
			"pos_profile": profile.name,
			"company": ctx.company,
			"cashier": _resolve_cashier(data, notes),
			"opened_at": to_site_naive(data["opened_at"], time_zone),
			"closed_at": to_site_naive(data["closed_at"], time_zone),
			"sales_count": data["sales_count"],
			"returns_count": data["returns_count"],
			"invoice_count_on_server": invoice_count,
			"notes": data.get("notes"),
		}
	)
	for field in SHIFT_MONEY_FIELDS:
		doc.set(field, _money(data[field], precision))
	for row in data["payments"]:
		values = {"mode_of_payment": row["mode_of_payment"]}
		for field in PAYMENT_MONEY_FIELDS:
			values[field] = _money(row[field], precision)
		doc.append("payments", values)

	# the device was authorized by get_device_context; nobody may create shifts in the desk
	doc.insert(ignore_permissions=True)
	return doc


def shift_result_fields(doc):
	return {
		"doctype": SHIFT_DOCTYPE,
		"name": doc.name,
		"docstatus": cint(doc.docstatus),
		"modified": format_db_datetime(doc.modified),
		"totals": None,
		"fawtara_status": None,
	}
