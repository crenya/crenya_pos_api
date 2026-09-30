import frappe

from crenya_pos_api.sync import fawtara as fawtara_service
from crenya_pos_api.sync.context import check_protocol_version, get_device_context


@frappe.whitelist(methods=["POST"])
def get_status(
	device_id: str | None = None,
	local_ids: list | str | None = None,
	protocol_version: int | str | None = None,
):
	"""Fawtara status and ASP document id of up to 50 till invoices (by `crenya_local_id`)."""
	check_protocol_version(protocol_version)
	ctx = get_device_context(device_id)
	return fawtara_service.get_status(ctx, local_ids)
