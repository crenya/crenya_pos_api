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
	"""A sale's total and what is still unpaid on it: {name, grand_total, outstanding, as_of,
	settlements}; `settlements` says how the bill was settled after the sale (CC12)."""
	check_protocol_version(protocol_version)
	ctx = get_device_context(device_id)
	return credit_service.invoice_outstanding_for(ctx, invoice)


@frappe.whitelist(methods=["POST"])
def get_open_invoices(
	device_id: str | None = None,
	customer: str | None = None,
	limit: int | str | None = None,
	protocol_version: int | str | None = None,
):
	"""A customer's open bills of the outlet's company, oldest first, with the advance on the account:
	{customer, outstanding, advance, as_of, more, invoices: [{name, local_id, offline_number, outlet,
	posting_date, grand_total, outstanding}]}. At most `limit` rows (default 200, never above 1000);
	`more` says there are others. `outstanding` is ERPNext's net balance (as `get_status`), not the
	sum of the rows and already net of `advance`: do not subtract the advance again."""
	check_protocol_version(protocol_version)
	ctx = get_device_context(device_id)
	return credit_service.open_invoices_for(ctx, customer, limit)
