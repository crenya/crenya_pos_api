"""Till sign-in with ERPNext username + password.

The till exchanges the password once for the user's API key pair and keeps it
in the OS keychain; the password is never stored on the till.
"""

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from crenya_pos_api.sync.context import POS_USER_ROLE, all_device_roles, check_protocol_version

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
):
	check_protocol_version(protocol_version)
	return sign_in(usr, pwd)


def sign_in(usr, pwd):
	from frappe.auth import LoginManager

	if not usr or not pwd:
		frappe.throw(_("Username and password are required"), frappe.AuthenticationError)

	# Same checks as the desk login (lockout after failed attempts, disabled
	# users, password size, Activity Log). The instance is created without
	# __init__ so this request's session is left untouched.
	manager = LoginManager.__new__(LoginManager)
	manager.authenticate(user=usr, pwd=pwd)
	user = manager.user

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

	api_key, api_secret = _ensure_api_keys(user)
	return {
		"user": user,
		"full_name": frappe.db.get_value("User", user, "full_name"),
		"api_key": api_key,
		"api_secret": api_secret,
	}


def _ensure_api_keys(user):
	"""Returns the user's key pair, creating it on first sign-in.

	Existing keys are reused so several tills can share one cashier account;
	regenerating keys in ERPNext signs every till out.
	"""
	from frappe.utils.password import get_decrypted_password, set_encrypted_password

	api_key = frappe.db.get_value("User", user, "api_key")
	api_secret = api_key and get_decrypted_password("User", user, "api_secret", raise_exception=False)
	if api_key and api_secret:
		return api_key, api_secret

	if not api_key:
		api_key = frappe.generate_hash(length=15)
		frappe.db.set_value("User", user, "api_key", api_key, update_modified=False)
	api_secret = frappe.generate_hash(length=15)
	set_encrypted_password("User", user, api_secret, "api_secret")
	return api_key, api_secret
