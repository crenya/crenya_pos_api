import frappe

from crenya_pos_api.sync import device as device_service
from crenya_pos_api.sync.context import check_protocol_version, get_device_context


@frappe.whitelist(methods=["POST"])
def list_pos_profiles(protocol_version=None):
	check_protocol_version(protocol_version)
	return device_service.list_profiles()


@frappe.whitelist(methods=["POST"])
def register_device(
	device_id=None,
	device_name=None,
	pos_profile=None,
	app_version=None,
	platform=None,
	protocol_version=None,
):
	check_protocol_version(protocol_version)
	return device_service.register(device_id, device_name, pos_profile, app_version, platform)


@frappe.whitelist(methods=["POST"])
def get_bootstrap(device_id=None, protocol_version=None):
	check_protocol_version(protocol_version)
	ctx = get_device_context(device_id)
	return device_service.bootstrap(ctx)
