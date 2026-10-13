"""Device users: a till, waiter phone or kitchen display holds the API key of its own user, never a
person's.

Signing in on a device (`api.auth.login`) proves who authorises the pairing. It does not hand the
device that person's key: the device gets the key of a dedicated user `device-<device id>@<site
host>`, a System User with the device role (Crenya POS User) and no desk access, so a key copied off
the device opens nothing but that device's own sync. The person is kept as `paired_by` (on the
device user and on the Crenya POS Device) and the device's `user` is the device user.

A device that still holds a person's key (paired before this) keeps working while the site setting
`crenya_require_device_keys` is off, with a warning in the Error Log once a day per device. With it
on, the sync answers `rekey_required` and the device exchanges the key through
`device.rekey_device`, then deletes the old one.

The user fields are Custom Fields of User (`crenya_device_id`, `crenya_paired_by`, setup.install).
"""

import base64
import hashlib
import hmac
import re

import frappe
from frappe import _
from frappe.utils import cint, nowdate

DEVICE_ID_FIELD = "crenya_device_id"
PAIRED_BY_FIELD = "crenya_paired_by"
# the site setting (site_config.json): 1 = a device must hold a device user's key
REQUIRE_DEVICE_KEYS = "crenya_require_device_keys"
EMAIL_PREFIX = "device-"
# a mailbox name is at most 64 characters; a longer device id is named by its hash
MAX_LOCAL_PART = 64
KEY_KIND_USER = "user"
KEY_KIND_DEVICE = "device"

# the seal of a PIN hash: the device's key from `pin_seal_key`, the format of pos-core's `pin.rs`
_SEAL_KEY_CONTEXT = b"crenya pin seal key v1\0"
_KEY_ID_CONTEXT = b"crenya pin key id"
_SEAL_CONTEXT = b"crenya pin seal v1\0"
SEALED_SCHEME = "crenya_pin1"


def require_device_keys():
	"""Whether this site refuses a device that holds a person's key (`rekey_required`)."""
	return bool(cint(frappe.conf.get(REQUIRE_DEVICE_KEYS)))


def user_has_fields():
	"""False on a site whose deploy skipped `bench migrate` (the Custom Fields are not there)."""
	return frappe.get_meta("User").has_field(DEVICE_ID_FIELD)


def _device_row(user):
	"""(device id, paired by) of a device user; (None, None) for anyone else."""
	if not user or user in ("Guest", "Administrator") or not user_has_fields():
		return None, None
	row = frappe.db.get_value("User", user, [DEVICE_ID_FIELD, PAIRED_BY_FIELD])
	return (row[0], row[1]) if row and row[0] else (None, None)


def is_device_user(user=None):
	return bool(_device_row(user or frappe.session.user)[0])


def paired_by(user=None):
	"""The person who authorised the pairing of a device user; None for anyone else."""
	return _device_row(user or frappe.session.user)[1]


def device_id_of(user=None):
	return _device_row(user or frappe.session.user)[0]


def device_users():
	"""Every device user, for staff lists to leave out (they hold a device role but are no staff)."""
	if not user_has_fields():
		return set()
	return set(frappe.get_all("User", filters={DEVICE_ID_FIELD: ["is", "set"]}, pluck="name"))


def acting_user(user=None):
	"""Who a call is made for: the person who paired a device user, else the user itself (the name
	a till shows as signed in, the profiles it may choose from)."""
	user = user or frappe.session.user
	return paired_by(user) or user


def key_kind(user=None):
	return KEY_KIND_DEVICE if is_device_user(user) else KEY_KIND_USER


def site_host():
	"""The site's host (its name); a name with no dot (a bench test site) gets `.local`."""
	host = str(frappe.local.site)
	return host if "." in host else f"{host}.local"


def device_user_name(device_id):
	"""`device-<device id>@<site host>`, lower case (device ids that differ only in case are one
	device); a device id a mailbox cannot hold is replaced by its hash."""
	token = device_id.strip().lower()
	if not re.fullmatch(r"[a-z0-9._-]+", token) or len(EMAIL_PREFIX + token) > MAX_LOCAL_PART:
		token = hashlib.sha256(token.encode()).hexdigest()[:32]
	return f"{EMAIL_PREFIX}{token}@{site_host()}"


def device_roles_for(device_type=None):
	"""The roles a device user holds: `Crenya POS Device`, the device role every device type
	accepts and that has no desk access. Not Crenya POS User (a person's role) nor the roles
	hooked for restaurant types (managers, captains, kitchen): those are people's rights, and the
	person is named on each sale by the PIN typed at the device."""
	from crenya_pos_api.sync.context import DEVICE_ROLE

	return (DEVICE_ROLE,)


def ensure_api_keys(user):
	"""Returns the user's key pair, creating it the first time.

	Existing keys are kept: regenerating them in ERPNext signs the device out."""
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


def pin_seal_key(device_id):
	"""The 32-byte key PIN hashes are sealed with for this device, derived from the site's
	encryption key and the device id (nothing is stored), so a device and the server agree on it
	without a table. Shared with the device only at pairing and re-key."""
	from frappe.utils.password import get_encryption_key

	secret = get_encryption_key().encode()
	return hmac.new(secret, _SEAL_KEY_CONTEXT + device_id.strip().lower().encode(), hashlib.sha256).digest()


def seal_pin_hash(encoded, key):
	"""A server PIN hash (`pbkdf2_sha256$<rounds>$<salt>$<key>`) as the device's seal of it
	(`crenya_pin1$<key id>$<rounds>$<salt>$<HMAC of the PBKDF2 key>`, pos-core's format): the
	device checks a PIN by deriving the same PBKDF2 key and comparing seals, and the PBKDF2 key
	itself never leaves the server. None for anything that is not a usable hash."""
	try:
		algorithm, rounds, salt, derived = encoded.strip().split("$")
		raw = base64.b64decode(derived, validate=True)
		base64.b64decode(salt, validate=True)
	except (AttributeError, ValueError):
		return None
	if algorithm != "pbkdf2_sha256" or not rounds.isdigit() or len(raw) != 32:
		return None
	key_id = hmac.new(key, _KEY_ID_CONTEXT, hashlib.sha256).hexdigest()[:8]
	seal = hmac.new(key, _SEAL_CONTEXT + raw, hashlib.sha256).digest()
	return f"{SEALED_SCHEME}${key_id}${rounds}${salt}${base64.b64encode(seal).decode('ascii')}"


def _create(device_id, person, device_type):
	name = device_user_name(device_id)
	short = frappe.db.get_value("Crenya POS Device", {"device_id": device_id}, "device_short")
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": name,
			"first_name": f"Device {short or device_id[:8]}",
			"enabled": 1,
			"send_welcome_email": 0,
			"user_type": "System User",
			"roles": [{"role": role} for role in device_roles_for(device_type)],
			DEVICE_ID_FIELD: device_id.strip().lower(),
			PAIRED_BY_FIELD: person,
			# the roles passed are the roles it gets: another app's new-user rule (erptrue_app
			# grants a new user every role) must not widen a device
			"explicit_roles": 1,
		}
	)
	doc.insert(ignore_permissions=True)
	# Frappe turns a user whose roles have no desk access into a Website User on save; a device
	# user is a System User (the update check and the plan read the user type)
	frappe.db.set_value("User", name, "user_type", "System User", update_modified=False)
	return name


def ensure_device_user(device_id, person, device_type=None):
	"""The device user of `device_id`, made the first time, enabled, with `person` as `paired_by`.
	The roles of an existing one are not touched (an administrator may have changed them)."""
	name = frappe.db.get_value("User", {DEVICE_ID_FIELD: device_id.strip().lower()}, "name")
	if not name:
		name = _create(device_id, person, device_type)
		frappe.get_doc("User", name).add_comment("Info", _("Paired by {0}").format(person))
		return name
	changes = {}
	if frappe.db.get_value("User", name, PAIRED_BY_FIELD) != person:
		changes[PAIRED_BY_FIELD] = person
	if not cint(frappe.db.get_value("User", name, "enabled")):
		changes["enabled"] = 1
	if changes:
		frappe.db.set_value("User", name, changes, update_modified=False)
		frappe.get_doc("User", name).add_comment("Info", _("Paired again by {0}").format(person))
	return name


def credentials(device_id, person, device_type=None):
	"""What a device keeps after pairing or re-key: its device user's key pair and the key its PIN
	hashes are sealed with. `person` authorised it (role checked by the caller)."""
	user = ensure_device_user(device_id, person, device_type)
	api_key, api_secret = ensure_api_keys(user)
	return {
		"api_key": api_key,
		"api_secret": api_secret,
		"key_kind": KEY_KIND_DEVICE,
		"device_user": user,
		# hex, as pos-core keeps its keys
		"pin_seal_key": pin_seal_key(device_id).hex(),
	}


def set_enabled(device_id, enabled):
	"""Disable (or enable) the device user of a device; no user, nothing to do. A disabled user's
	key is refused by Frappe on the next call."""
	name = frappe.db.get_value("User", {DEVICE_ID_FIELD: str(device_id).strip().lower()}, "name")
	if name and cint(frappe.db.get_value("User", name, "enabled")) != cint(enabled):
		frappe.db.set_value("User", name, "enabled", cint(enabled), update_modified=False)
		frappe.get_doc("User", name).add_comment(
			"Info", _("Enabled with its device") if cint(enabled) else _("Disabled with its device")
		)


def device_on_update(doc, method=None):
	"""doc_events hook on Crenya POS Device: a deactivated device's user is disabled with it, a
	reactivated one's enabled again."""
	if doc.is_new() or doc.has_value_changed("enabled"):
		set_enabled(doc.device_id or doc.name, doc.enabled)


def device_on_trash(doc, method=None):
	set_enabled(doc.device_id or doc.name, 0)


# a person's key on a device


def warn_user_key(device, user):
	"""Say once a day per device (Error Log) that it called with a person's key. Never raises: a
	device call goes on."""
	key = f"crenya_pos_user_key:{device}:{nowdate()}"
	try:
		if frappe.cache.get_value(key):
			return
		frappe.cache.set_value(key, 1, expires_in_sec=24 * 3600)
		frappe.log_error(
			title="Crenya POS: a device is signed in with a person's API key",
			message=(
				f"Device {device} called with the API key of {user}. Anyone with access to that device "
				f"can use the key against this site. Update the device (it exchanges the key for a device "
				f"user's) and then set {REQUIRE_DEVICE_KEYS} in site_config.json."
			),
		)
	except Exception:
		pass
