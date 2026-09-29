"""Device / POS Profile resolution and authorization shared by all API methods."""

from dataclasses import dataclass

import frappe
from frappe.utils import cint, flt, now

from crenya_pos_api.sync.errors import (
	DeviceNotRegisteredError,
	DevicePermissionError,
	InvalidRequestError,
	ProtocolUnsupportedError,
	UnsupportedTaxError,
	raise_api_error,
)

PROTOCOL_VERSION = 1
DEVICE_DOCTYPE = "Crenya POS Device"
EVENT_DOCTYPE = "Crenya Sync Event"
POS_USER_ROLE = "Crenya POS User"
SUPPORTED_CHARGE_TYPES = ("On Net Total",)

DEFAULT_TOTAL_TOLERANCE = "0.010"
DEFAULT_PULL_LAG_SECONDS = 5


@dataclass
class DeviceContext:
	device: dict
	profile: "frappe.model.document.Document"

	@property
	def device_id(self):
		return self.device["name"]

	@property
	def company(self):
		return self.profile.company

	@property
	def currency(self):
		return self.profile.currency or frappe.get_cached_value(
			"Company", self.profile.company, "default_currency"
		)


def check_protocol_version(protocol_version):
	if protocol_version in (None, ""):
		return
	try:
		version = int(protocol_version)
	except (TypeError, ValueError):
		raise_api_error(InvalidRequestError, "protocol_version must be an integer")
	if version > PROTOCOL_VERSION:
		raise_api_error(
			ProtocolUnsupportedError,
			f"Client protocol {version} is newer than server protocol {PROTOCOL_VERSION}; "
			"update the server app",
		)


def require_login():
	if frappe.session.user in (None, "Guest"):
		raise frappe.PermissionError("Login required")


def is_system_manager(user=None):
	return "System Manager" in frappe.get_roles(user or frappe.session.user)


def user_can_use_profile(profile, user=None):
	"""Profile must be enabled and list the user (or have no user list, or user is System Manager)."""
	user = user or frappe.session.user
	if cint(profile.get("disabled")):
		return False
	if is_system_manager(user):
		return True
	users = profile_users(profile)
	return not users or user in users


def profile_users(profile):
	"""Users of the profile's Applicable for Users table (empty = everyone)."""
	return [row.user for row in profile.get("applicable_for_users") or [] if row.user]


def get_profile(pos_profile):
	if not pos_profile or not frappe.db.exists("POS Profile", pos_profile):
		raise_api_error(InvalidRequestError, f"POS Profile {pos_profile!r} does not exist")
	return frappe.get_cached_doc("POS Profile", pos_profile)


def assert_supported_taxes(profile):
	"""Only On Net Total taxes can be computed offline by the till."""
	template = profile.get("taxes_and_charges")
	if not template:
		return
	rows = frappe.get_all(
		"Sales Taxes and Charges",
		filters={"parent": template, "parenttype": "Sales Taxes and Charges Template"},
		fields=["idx", "charge_type"],
		order_by="idx asc",
	)
	unsupported = [row for row in rows if row.charge_type not in SUPPORTED_CHARGE_TYPES]
	if unsupported:
		row = unsupported[0]
		raise_api_error(
			UnsupportedTaxError,
			f"Taxes template {template} row {row.idx} uses charge type {row.charge_type!r}; "
			"only 'On Net Total' is supported offline",
		)


def get_device_context(device_id, touch=True):
	"""Load and authorize the device of the current request."""
	require_login()
	if not device_id or not isinstance(device_id, str):
		raise_api_error(DeviceNotRegisteredError, "device_id is required")

	device = frappe.db.get_value(
		DEVICE_DOCTYPE,
		device_id,
		["name", "device_id", "device_short", "device_name", "pos_profile", "user", "enabled"],
		as_dict=True,
	)
	if not device or not cint(device.enabled):
		raise_api_error(DeviceNotRegisteredError, f"Device {device_id} is not registered or is disabled")

	user = frappe.session.user
	if device.user != user and not is_system_manager(user):
		raise_api_error(DevicePermissionError, f"Device {device_id} is registered to another user")

	profile = get_profile(device.pos_profile)
	if not user_can_use_profile(profile, user):
		raise_api_error(DevicePermissionError, f"User {user} may not use POS Profile {profile.name}")

	if touch:
		frappe.db.set_value(DEVICE_DOCTYPE, device.name, "last_seen", now(), update_modified=False)

	return DeviceContext(device=device, profile=profile)


def get_total_tolerance():
	value = frappe.conf.get("crenya_pos_total_tolerance")
	return abs(flt(DEFAULT_TOTAL_TOLERANCE if value in (None, "") else value))


def get_pull_lag_seconds():
	value = frappe.conf.get("crenya_pos_pull_lag_seconds")
	return DEFAULT_PULL_LAG_SECONDS if value is None else max(cint(value), 0)


def get_update_stock(profile):
	"""POS Profile has no update_stock field in v15: tills move stock unless such a field says otherwise."""
	if profile.meta.has_field("update_stock"):
		return cint(profile.get("update_stock"))
	return 1


def is_rounded_total_disabled(profile):
	if cint(profile.get("disable_rounded_total")):
		return 1
	return cint(frappe.db.get_single_value("Global Defaults", "disable_rounded_total"))


def money_precision():
	return frappe.get_precision("Sales Invoice", "grand_total")


def qty_precision():
	return frappe.get_precision("Sales Invoice Item", "qty")
