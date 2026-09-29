import frappe

from crenya_pos_api.sync.context import check_protocol_version, get_device_context
from crenya_pos_api.sync.returns import get_invoice_for_return as _get_invoice_for_return


@frappe.whitelist(methods=["POST"])
def get_invoice_for_return(device_id=None, invoice=None, protocol_version=None):
	check_protocol_version(protocol_version)
	ctx = get_device_context(device_id)
	return _get_invoice_for_return(ctx, invoice)
