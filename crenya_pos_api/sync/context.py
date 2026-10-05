"""Device / POS Profile resolution and authorization shared by all API methods."""

import re
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
from crenya_pos_api.utils.decimal import as_decimal, smallest_unit_tolerance

# 2: extension hooks (other apps' entities / aggregates / operations), invoice `extensions`,
# line `notes` / `parent_line_no`, `device_type`. Every addition is optional, so protocol 1
# tills keep working unchanged.
PROTOCOL_VERSION = 2
DEVICE_DOCTYPE = "Crenya POS Device"
EVENT_DOCTYPE = "Crenya Sync Event"
POS_USER_ROLE = "Crenya POS User"
SUPPORTED_CHARGE_TYPES = ("On Net Total",)
DEFAULT_DEVICE_TYPE = "till"
# device_type values: lower case letters, digits, _ and - (at most 40)
DEVICE_TYPE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")

# default totals tolerance: this many smallest units of the invoice currency
TOTAL_TOLERANCE_UNITS = 10
DEFAULT_PULL_LAG_SECONDS = 5


@dataclass
class DeviceContext:
	"""The registered, enabled device of the request, authorized for its user and POS Profile.

	`device` is the Crenya POS Device row (name, device_id, device_short, device_name,
	pos_profile, user, enabled, device_type); `profile` the cached POS Profile document."""

	device: dict
	profile: "frappe.model.document.Document"

	@property
	def device_id(self):
		return self.device["name"]

	@property
	def device_type(self):
		"""Kind of device ("till" unless it registered as something else, e.g. "kds")."""
		return self.device.get("device_type") or DEFAULT_DEVICE_TYPE

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


def device_roles(device_type=DEFAULT_DEVICE_TYPE):
	"""Roles that may register and use a device of `device_type`: Crenya POS User plus the
	`crenya_pos_device_roles` roles declared for every type or for this one."""
	from crenya_pos_api.sync.registry import device_roles as registered_roles

	return registered_roles(device_type)


def all_device_roles():
	"""Device roles of any device type (before a device is known: sign-in, profile list)."""
	from crenya_pos_api.sync.registry import all_device_roles as registered_roles

	return registered_roles()


def has_device_role(user=None, device_type=None):
	"""True when the user holds a role for `device_type`, or (None) for any device type."""
	roles = set(frappe.get_roles(user or frappe.session.user))
	wanted = all_device_roles() if device_type is None else device_roles(device_type)
	return any(role in roles for role in wanted)


def require_device_role(device_type=None):
	"""Signed in is not enough for till data (customers, PIN hashes, terminal secrets): the user
	must hold Crenya POS User, or a role another app allows with `crenya_pos_device_roles`.
	With `device_type`, only the roles of that type count: a retail till (type "till") accepts
	Crenya POS User and roles hooked for every type, never a role scoped to other device types."""
	require_login()
	user = frappe.session.user
	if has_device_role(user, device_type):
		return
	roles = all_device_roles() if device_type is None else device_roles(device_type)
	wanted = f"the {roles[0]} role" if len(roles) == 1 else f"one of the roles {', '.join(roles)}"
	scope = f" for {device_type} devices" if device_type is not None else ""
	raise_api_error(DevicePermissionError, f"User {user} does not have {wanted}{scope}")


def device_fields():
	"""Crenya POS Device columns of DeviceContext.device (device_type once the site is migrated)."""
	fields = ["name", "device_id", "device_short", "device_name", "pos_profile", "user", "enabled"]
	if frappe.get_meta(DEVICE_DOCTYPE).has_field("device_type"):
		fields.append("device_type")
	return fields


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
	require_device_role()
	if not device_id or not isinstance(device_id, str):
		raise_api_error(DeviceNotRegisteredError, "device_id is required")

	device = frappe.db.get_value(DEVICE_DOCTYPE, device_id, device_fields(), as_dict=True)
	if not device or not cint(device.enabled):
		raise_api_error(DeviceNotRegisteredError, f"Device {device_id} is not registered or is disabled")

	user = frappe.session.user
	if device.user != user and not is_system_manager(user):
		raise_api_error(DevicePermissionError, f"Device {device_id} is registered to another user")
	# roles scoped to other device types (e.g. restaurant staff) never open a retail till
	require_device_role(device.get("device_type") or DEFAULT_DEVICE_TYPE)

	profile = get_profile(device.pos_profile)
	if not user_can_use_profile(profile, user):
		raise_api_error(DevicePermissionError, f"User {user} may not use POS Profile {profile.name}")

	if touch:
		frappe.db.set_value(DEVICE_DOCTYPE, device.name, "last_seen", now(), update_modified=False)

	return DeviceContext(device=device, profile=profile)


def get_total_tolerance(precision):
	"""Allowed till/server difference: `site_config.crenya_pos_total_tolerance` when set, else
	10 x the smallest unit of a currency with `precision` decimals (Decimal)."""
	value = frappe.conf.get("crenya_pos_total_tolerance")
	if value in (None, ""):
		return smallest_unit_tolerance(precision, TOTAL_TOLERANCE_UNITS)
	return abs(as_decimal(flt(value)))


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


def money_precision(currency=None):
	"""Decimals of money amounts (the rules ERPNext applies to Sales Invoice totals)."""
	return frappe.get_precision("Sales Invoice", "grand_total", currency=currency)


def qty_precision():
	"""Decimals ERPNext rounds Sales Invoice Item `qty` to: the field's precision (property
	setters included), else System Settings float precision, as `frappe.get_precision` resolves it."""
	return cint(frappe.get_precision("Sales Invoice Item", "qty"))


def rate_precision(currency=None):
	"""Decimals ERPNext rounds Sales Invoice Item `rate` to: the field's precision (property
	setters included), else the currency precision, as `frappe.get_precision` resolves it."""
	return cint(frappe.get_precision("Sales Invoice Item", "rate", currency=currency))
