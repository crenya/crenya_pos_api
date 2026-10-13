"""Device sign-in with ERPNext username + password.

The person signing in authorises the pairing (role checked, audited, kept as `paired_by`); the
device gets the API key of its own device user (`sync.device_user`), never the person's. The
password is sent once and never stored on the device.
"""

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from crenya_pos_api.sync import device_user
from crenya_pos_api.sync.context import (
	DEVICE_DOCTYPE,
	POS_USER_ROLE,
	all_device_roles,
	check_protocol_version,
	find_device_name,
	is_system_manager,
	user_can_use_profile,
)
from crenya_pos_api.sync.errors import (
	DeviceNotRegisteredError,
	DevicePermissionError,
	InvalidRequestError,
	raise_api_error,
)

ALLOWED_ROLES = {POS_USER_ROLE, "System Manager"}


# Guest by design: this is the till's sign-in. Rate limited, and the password is
# checked with the desk login's lockout rules before anything is returned.
@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep
@rate_limit(limit=10, seconds=5 * 60)
def login(
	usr: str | None = None,
	pwd: str | None = None,
	device_id: str | None = None,
	protocol_version: int | str | None = None,
	device_type: str | None = None,
):
	check_protocol_version(protocol_version)
	return sign_in(usr, pwd, device_id, device_type)


def sign_in(usr, pwd, device_id=None, device_type=None):
	from frappe.auth import LoginManager

	if not usr or not pwd:
		frappe.throw(_("Username and password are required"), frappe.AuthenticationError)

	# Same checks as the desk login (lockout after failed attempts, disabled
	# users, password size, Activity Log). The instance is created without
	# __init__ so this request's session is left untouched.
	manager = LoginManager.__new__(LoginManager)
	manager.authenticate(user=usr, pwd=pwd)
	user = manager.user
	if device_user.is_device_user(user):
		frappe.throw(_("A device user has no password login"), frappe.AuthenticationError)

	from frappe.twofactor import should_run_2fa

	if should_run_2fa(user):
		frappe.throw(
			_("Two-factor authentication is enabled for {0}. Use a till user without 2FA.").format(user),
			frappe.PermissionError,
		)
	# plus the roles other apps allow on devices of any type (crenya_pos_device_roles); which
	# device a role may use is checked when the device registers and on every device call
	if not ALLOWED_ROLES.union(all_device_roles()).intersection(frappe.get_roles(user)):
		frappe.throw(_("{0} does not have the {1} role").format(user, POS_USER_ROLE), frappe.PermissionError)

	answer = {"user": user, "full_name": frappe.db.get_value("User", user, "full_name")}
	if not device_id or not device_user.user_has_fields():
		# a sign-in with no device (the oldest tills) or a site not migrated yet: the person's own
		# key, unless the site requires device keys
		if device_user.require_device_keys():
			raise_api_error(InvalidRequestError, "device_id is required to sign in a device")
		api_key, api_secret = device_user.ensure_api_keys(user)
		return {**answer, "api_key": api_key, "api_secret": api_secret, "key_kind": device_user.KEY_KIND_USER}

	_check_pairing(user, device_id)
	return {**answer, **device_user.credentials(device_id, user, device_type)}


def _check_pairing(person, device_id):
	"""A person may pair a device id that no device has, or one they may use: a device already
	registered is signed in again by a System Manager or by someone who may use its POS Profile
	(a device id is not a secret)."""
	from crenya_pos_api.sync.device import DEVICE_ID_RE

	if not isinstance(device_id, str) or not DEVICE_ID_RE.match(device_id):
		raise_api_error(InvalidRequestError, "device_id must be the till's UUID")
	name = find_device_name(device_id)
	if not name:
		return
	device = frappe.db.get_value(DEVICE_DOCTYPE, name, ["enabled", "pos_profile"], as_dict=True)
	if not frappe.utils.cint(device.enabled):
		raise_api_error(DeviceNotRegisteredError, f"Device {name} is disabled")
	if not is_system_manager(person) and not user_can_use_profile(
		frappe.get_cached_doc("POS Profile", device.pos_profile), person
	):
		raise_api_error(DevicePermissionError, f"User {person} may not sign in device {name}")


@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep
def reset_password(user: str):
	"""Frappe's forgot-password (hooked over it, `override_whitelisted_methods`): a device user has
	no mailbox and no password, so nothing is sent or reset for it; everyone else goes on to Frappe's
	own, rate limits included."""
	if device_user.is_device_user(user):
		frappe.throw(_("A device user has no password"), frappe.PermissionError)
	from frappe.core.doctype.user.user import reset_password as frappe_reset_password

	return frappe_reset_password(user=user)
