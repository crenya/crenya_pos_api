import frappe

from crenya_pos_api.sync import loyalty as loyalty_service
from crenya_pos_api.sync.context import check_protocol_version, get_device_context


@frappe.whitelist(methods=["POST"])
def get_details(
	device_id: str | None = None,
	customer: str | None = None,
	protocol_version: int | str | None = None,
):
	"""Redeemable loyalty balance of a Customer (ERP name) for the till's company."""
	check_protocol_version(protocol_version)
	ctx = get_device_context(device_id)
	return loyalty_service.get_details(ctx, customer)
