import frappe

from crenya_pos_api.sync import credit as credit_service
from crenya_pos_api.sync.context import check_protocol_version, get_device_context


@frappe.whitelist(methods=["POST"])
def get_status(
	device_id: str | None = None,
	customer: str | None = None,
	protocol_version: int | str | None = None,
):
	"""A customer's credit limit and balance as ERPNext reads them, for the till's company:
	{customer, credit_limit, outstanding, available, over_limit, as_of}. `credit_limit` and
	`available` are null for a customer with no limit."""
	check_protocol_version(protocol_version)
	ctx = get_device_context(device_id)
	return credit_service.status_for(ctx, customer)


@frappe.whitelist(methods=["POST"])
def get_invoice_outstanding(
	device_id: str | None = None,
	invoice: str | None = None,
	protocol_version: int | str | None = None,
):
	"""A sale's total and what is still unpaid on it: {name, grand_total, outstanding, as_of}."""
	check_protocol_version(protocol_version)
	ctx = get_device_context(device_id)
	return credit_service.invoice_outstanding_for(ctx, invoice)
