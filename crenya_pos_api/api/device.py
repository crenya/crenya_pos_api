import frappe

from crenya_pos_api.sync import device as device_service
from crenya_pos_api.sync.context import check_protocol_version, get_device_context


@frappe.whitelist(methods=["POST"])
def list_pos_profiles(protocol_version: int | str | None = None):
	check_protocol_version(protocol_version)
	return device_service.list_profiles()


@frappe.whitelist(methods=["POST"])
def register_device(
	device_id: str | None = None,
	device_name: str | None = None,
	pos_profile: str | None = None,
	app_version: str | None = None,
	platform: str | None = None,
	protocol_version: int | str | None = None,
	device_type: str | None = None,
):
	check_protocol_version(protocol_version)
	return device_service.register(device_id, device_name, pos_profile, app_version, platform, device_type)


@frappe.whitelist(methods=["POST"])
def get_bootstrap(device_id: str | None = None, protocol_version: int | str | None = None):
	check_protocol_version(protocol_version)
	ctx = get_device_context(device_id)
	return device_service.bootstrap(ctx)
