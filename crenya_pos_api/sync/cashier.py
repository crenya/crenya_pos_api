"""Cashier PINs and cashier resolution for pushed documents.

The PIN (4 to 6 ASCII digits) is typed into the User field `crenya_pos_pin`
(Password). A User validate hook replaces it with a salted PBKDF2 hash in
`crenya_pos_pin_hash` and clears the Password field, so the PIN itself is never
stored reversibly (not in the User table and not in `__Auth`). Tills receive
the hash through the `cashier` pull entity and check the PIN offline.

Cashier records also carry `roles`: the roles the user holds that open the pulling device's
type (Crenya POS User, and the roles other apps register with `crenya_pos_device_roles` for
that type), never any other ERPNext role, so a device can tell, for example, which staff may
approve a manager-only action. A retail till (type "till") only ever sees Crenya POS User and
roles hooked for every device type.

Hash format: pbkdf2_sha256$<iterations>$<salt base64>$<key base64>
(PBKDF2-HMAC-SHA256, 120000 iterations, 16-byte salt, 32-byte key, standard
base64 with padding).
"""

import base64
import binascii
import hashlib
import hmac
import os
import re

import frappe
from frappe import _
from frappe.utils import cint

from crenya_pos_api.sync.context import device_roles, profile_users

PIN_FIELD = "crenya_pos_pin"
PIN_HASH_FIELD = "crenya_pos_pin_hash"

PIN_ALGORITHM = "pbkdf2_sha256"
PIN_ITERATIONS = 120000
PIN_SALT_BYTES = 16
PIN_KEY_BYTES = 32
PIN_MIN_LENGTH = 4
PIN_MAX_LENGTH = 6

# [0-9], not \d: \d also matches Arabic-Indic and other Unicode digits
_PIN_RE = re.compile(rf"[0-9]{{{PIN_MIN_LENGTH},{PIN_MAX_LENGTH}}}")
_ITERATIONS_RE = re.compile(r"[1-9][0-9]{0,7}")


class InvalidPin(ValueError):
	pass


def check_pin_format(pin):
	"""Return the PIN if it is 4 to 6 ASCII digits, else raise InvalidPin."""
	if not isinstance(pin, str) or not _PIN_RE.fullmatch(pin):
		raise InvalidPin(f"POS PIN must be {PIN_MIN_LENGTH} to {PIN_MAX_LENGTH} digits (0-9)")
	return pin


def is_masked_pin(value):
	"""True for the asterisks Frappe puts back into a Password field it has stored."""
	return isinstance(value, str) and bool(value) and set(value) == {"*"}


def _b64(data):
	return base64.b64encode(data).decode("ascii")


def _derive(pin, salt, iterations):
	return hashlib.pbkdf2_hmac("sha256", pin.encode("ascii"), salt, iterations, dklen=PIN_KEY_BYTES)


def hash_pin(pin, salt=None):
	"""Encoded PBKDF2 hash of a PIN. `salt` (bytes) is random unless given (tests)."""
	check_pin_format(pin)
	if salt is None:
		salt = os.urandom(PIN_SALT_BYTES)
	if not isinstance(salt, bytes | bytearray) or not salt:
		raise ValueError("salt must be non-empty bytes")
	salt = bytes(salt)
	key = _derive(pin, salt, PIN_ITERATIONS)
	return f"{PIN_ALGORITHM}${PIN_ITERATIONS}${_b64(salt)}${_b64(key)}"


def verify_pin(pin, encoded):
	"""Check a PIN against an encoded hash (constant-time compare). Malformed input → False."""
	try:
		check_pin_format(pin)
		algorithm, iterations, salt, key = encoded.split("$")
		if algorithm != PIN_ALGORITHM or not _ITERATIONS_RE.fullmatch(iterations):
			return False
		salt = base64.b64decode(salt, validate=True)
		key = base64.b64decode(key, validate=True)
	except (InvalidPin, AttributeError, ValueError, binascii.Error):
		return False
	if not salt or not key:
		return False
	iterations = int(iterations)
	return hmac.compare_digest(_derive(pin, salt, iterations), key)


# User hook


def _stored_pin(doc):
	"""A PIN that an earlier save kept in __Auth (only possible if this hook was bypassed)."""
	from frappe.utils.password import get_decrypted_password

	if doc.is_new():
		return None
	return get_decrypted_password(doc.doctype, doc.name, PIN_FIELD, raise_exception=False)


def _forget_stored_pin(doc):
	"""Drop any encrypted copy of the PIN field from __Auth."""
	from frappe.utils.password import remove_encrypted_password

	if doc.name:
		remove_encrypted_password(doc.doctype, doc.name, PIN_FIELD)


def user_validate(doc, method=None):
	"""doc_events hook on User: hash a newly entered PIN and clear the Password field.

	Runs before Frappe's own password handling (`_save_passwords`), which then
	sees an empty field and deletes the field's __Auth row as well. An empty
	field keeps the current hash.
	"""
	value = doc.get(PIN_FIELD)
	if not value:
		return

	if is_masked_pin(value):
		# the form sends asterisks back when the PIN was stored the Frappe way
		# (hook bypassed): hash that stored PIN if it is valid, never keep it
		value = _stored_pin(doc)
		doc.set(PIN_FIELD, None)
		_forget_stored_pin(doc)
		if value:
			try:
				doc.set(PIN_HASH_FIELD, hash_pin(value))
			except InvalidPin:
				pass
		return

	try:
		encoded = hash_pin(value)
	except InvalidPin:
		frappe.throw(
			_("POS PIN must be {0} to {1} digits (0-9).").format(PIN_MIN_LENGTH, PIN_MAX_LENGTH),
			title=_("Invalid POS PIN"),
		)

	doc.set(PIN_HASH_FIELD, encoded)
	doc.set(PIN_FIELD, None)
	_forget_stored_pin(doc)


def clear_user_pin(user):
	"""Remove a user's PIN; the next `cashier` pull delivers `pin_hash: null`."""
	doc = frappe.get_doc("User", user)
	doc.set(PIN_FIELD, None)
	doc.set(PIN_HASH_FIELD, None)
	doc.save()
	_forget_stored_pin(doc)
	return {"user": doc.name, "pin_set": False}


# device roles of staff


def device_roles_of(users, device_type):
	"""{user: [roles of `device_type` the user holds]} for the users holding at least one, each
	list in `device_roles(device_type)` order (Crenya POS User first, then the hooked roles).
	No other role of the user is ever returned."""
	users = sorted({user for user in users or [] if user})
	if not users:
		return {}
	roles = device_roles(device_type)
	held = {}
	for row in frappe.get_all(
		"Has Role",
		filters={"parent": ["in", users], "parenttype": "User", "role": ["in", list(roles)]},
		fields=["parent", "role"],
	):
		held.setdefault(row.parent, set()).add(row.role)
	return {user: [role for role in roles if role in held[user]] for user in users if user in held}


def staff_roles(profile, device_type):
	"""{user: [roles of `device_type`]} of the staff a device of `profile` and `device_type`
	knows as enabled cashiers: enabled users (not Guest) with a role for that type, limited to
	the profile's Applicable for Users when that table is filled. Users in name order."""
	has_role = frappe.qb.DocType("Has Role")
	user = frappe.qb.DocType("User")
	query = (
		frappe.qb.from_(has_role)
		.join(user)
		.on(user.name == has_role.parent)
		.select(has_role.parent)
		.distinct()
		.where(
			(has_role.parenttype == "User")
			& has_role.role.isin(list(device_roles(device_type)))
			& (user.enabled == 1)
			& (user.name != "Guest")
		)
	)
	allowed = profile_users(profile)
	if allowed:
		query = query.where(has_role.parent.isin(allowed))
	held = device_roles_of(query.run(pluck=True), device_type)
	return {name: held[name] for name in sorted(held)}


# cashier on pushed documents


def enabled_user(user):
	"""The User name (as stored) if `user` exists and is enabled, else None."""
	if not user or not isinstance(user, str):
		return None
	row = frappe.db.get_value("User", user, ["name", "enabled"], as_dict=True)
	if not row or not cint(row.enabled):
		return None
	return row.name


def _allowed_set(profile_doc):
	"""None = everyone allowed (empty Applicable for Users table)."""
	users = {row.user for row in profile_doc.get("applicable_for_users") or [] if row.user}
	return users or None


def pos_profile_on_update(doc, method=None):
	"""Applicable for Users changes don't touch any User, so bump `modified` of the
	PIN holders whose access changed; tills then pull them as enabled / disabled."""
	before = doc.get_doc_before_save()
	old, new = (_allowed_set(before) if before else None), _allowed_set(doc)
	if old == new:
		return
	if not frappe.get_meta("User").has_field(PIN_HASH_FIELD):
		return
	pin_holders = set(frappe.get_all("User", filters={PIN_HASH_FIELD: ["is", "set"]}, pluck="name"))
	if old is None or new is None:
		affected = pin_holders
	else:
		affected = pin_holders & (old ^ new)
	if not affected:
		return
	user = frappe.qb.DocType("User")
	(
		frappe.qb.update(user).set(user.modified, frappe.utils.now()).where(user.name.isin(sorted(affected)))
	).run()
