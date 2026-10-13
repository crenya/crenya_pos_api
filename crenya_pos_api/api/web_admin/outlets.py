"""Shops on app.crenya.com: the outlets, their devices and the till's installers. The answers
are crenya_restaurant.api.web_admin's shapes (`Outlet`, `Devices`, `Downloads`), so the web's
shared till-admin pages read both products alike. Who manages a shop: `scope`."""

import frappe

from crenya_pos_api.api.web_admin.scope import manage, managed_shops
from crenya_pos_api.sync import central
from crenya_pos_api.sync.context import DEFAULT_DEVICE_TYPE, is_system_manager, require_login
from crenya_pos_api.web import devices as web_devices

# the device types a shop has, in the list's order, with their labels (English, Arabic); a
# device of another type (an app's own) is listed after them under its type name
DEVICE_TYPE_LABELS = {DEFAULT_DEVICE_TYPE: ("Retail till", "نقطة بيع التجزئة")}


@frappe.whitelist(methods=["GET", "POST"])
def outlets() -> dict:
	"""The shops the signed-in user manages, by name, each with its company and the company's
	currency (empty when there is none). A cashier gets none."""
	return {
		"outlets": [
			{
				"name": row.name,
				"outlet_name": row.name,
				"company": row.company,
				"currency": frappe.get_cached_value("Company", row.company, "default_currency"),
				"can_manage": True,
			}
			for row in managed_shops()
		]
	}


def _devices(outlet: str) -> dict:
	return web_devices.devices_answer(
		outlet, True, type_order=tuple(DEVICE_TYPE_LABELS), type_labels=DEVICE_TYPE_LABELS
	)


@frappe.whitelist(methods=["GET", "POST"])
def devices(outlet: str) -> dict:
	"""The shop's tills (version, last seen, open shift) and the plan's terminal cap."""
	return _devices(manage(outlet).outlet)


@frappe.whitelist(methods=["POST"])
def set_device_enabled(outlet: str, device: str, enabled: bool | int | str, modified: str) -> dict:
	"""Deactivate or reactivate a till of the shop (`web.devices.set_device_enabled`), never
	delete it; `modified` is the one `devices` answered. Answers `devices`."""
	ctx = manage(outlet)
	web_devices.set_device_enabled(ctx, device, enabled, modified)
	return _devices(ctx.outlet)


@frappe.whitelist(methods=["GET", "POST"])
def downloads() -> dict:
	"""The installers of the newest stable crenya pos release (the central manifest): {version,
	pub_date, notes, installers: [{platform, label, label_ar, url, sha256, size}]}, or
	{installers: []}. For a System Manager or a manager of a shop."""
	require_login()
	if not is_system_manager() and not managed_shops():
		raise frappe.PermissionError
	return central.downloads(central.RETAIL)
