"""A user's till access set from app.crenya.com: privilege, 6-digit till PIN and the outlets they
work at. Product-neutral: each product passes its privileges as {privilege: role or roles}
(crenya_restaurant `PRIVILEGE_ROLES`, retail `sync.approvals.RETAIL_PRIVILEGES`). The caller is
a System Manager, checked by the product's endpoint.

The PIN is only ever handed to this app's User hook (`sync.cashier.user_validate`), which hashes
it; it is never stored, logged or answered. A privilege never gives System Manager, and
Administrator and Guest are never changed. An outlet whose Applicable for Users is empty (every
user works there) is never changed, and a user is never taken off an outlet they are the only
user of (its table would become empty, which opens it to every user).
"""

import re

import frappe
from frappe import _
from frappe.utils import cstr

from crenya_pos_api.sync.cashier import PIN_FIELD
from crenya_pos_api.web.context import WebAdminContext, checked, invalid, refuse

# owner decision 2026-10-07: a PIN set from the web or a till is exactly 6 digits (the User hook
# itself accepts 4 to 6, which PINs set in the desk keep)
TILL_PIN_LENGTH = 6
_TILL_PIN_RE = re.compile(rf"[0-9]{{{TILL_PIN_LENGTH}}}")
PROTECTED_USERS = ("Administrator", "Guest")
PROTECTED_ROLES = ("System Manager", "Administrator")
# the privilege a manager keeps (`self_change_problem`)
MANAGER_PRIVILEGE = "manager"

__all__ = [
	"PROTECTED_ROLES",
	"PROTECTED_USERS",
	"TILL_PIN_LENGTH",
	"is_protected",
	"is_reserved",
	"listing_changes",
	"managed_roles",
	"needs_listing",
	"only_user",
	"open_to_everyone",
	"pin_problem",
	"privilege_of",
	"role_changes",
	"save_web_staff",
	"self_change_problem",
	"web_staff_user",
	"works_at",
]


# privileges and roles (pure)


def _roles(value) -> tuple:
	return (value,) if isinstance(value, str) else tuple(value or ())


def managed_roles(privilege_roles) -> tuple:
	"""Every role a privilege of `privilege_roles` gives, in order: the only roles a privilege
	change adds or removes. ValueError when one is a protected role (System Manager)."""
	roles = tuple(dict.fromkeys(role for value in privilege_roles.values() for role in _roles(value)))
	protected = [role for role in roles if role in PROTECTED_ROLES]
	if protected:
		raise ValueError(f"A privilege never gives {', '.join(protected)}")
	return roles


def privilege_of(roles, privilege_roles):
	"""The privilege a user with `roles` has: the one whose roles they all hold, the one giving
	the most roles first, the first in `privilege_roles` order on a tie; None for none."""
	roles = set(roles or ())
	best, best_count = None, 0
	for privilege, value in privilege_roles.items():
		wanted = _roles(value)
		if wanted and set(wanted) <= roles and len(wanted) > best_count:
			best, best_count = privilege, len(wanted)
	return best


def role_changes(roles, privilege, privilege_roles):
	"""(roles to add, roles to remove) so that, among `managed_roles`, the user holds exactly the
	privilege's roles (none for privilege None); no other role is ever added or removed.
	ValueError for an unknown privilege."""
	managed = managed_roles(privilege_roles)
	if privilege is not None and privilege not in privilege_roles:
		raise ValueError(f"unknown privilege {privilege}")
	target = () if privilege is None else _roles(privilege_roles[privilege])
	roles = set(roles or ())
	add = [role for role in target if role not in roles]
	remove = [role for role in managed if role not in target and role in roles]
	return add, remove


def is_reserved(user):
	"""Administrator and Guest are never given a till role or PIN, from anywhere."""
	return user in PROTECTED_USERS


def is_protected(user, roles):
	"""Administrator, Guest and System Managers are never read or changed from a till."""
	return is_reserved(user) or any(role in PROTECTED_ROLES for role in roles or ())


# outlets (pure): {outlet: its Applicable for Users}


def needs_listing(allowed_users, user):
	"""Whether a saved user must be added to the outlet's Applicable for Users: only when that
	table is not empty (an empty table means every user already)."""
	allowed = {name for name in allowed_users or () if name}
	return bool(allowed) and user not in allowed


def open_to_everyone(allowed_users):
	"""An empty Applicable for Users: every user works at the outlet."""
	return not {name for name in allowed_users or () if name}


def works_at(allowed_users, user):
	return open_to_everyone(allowed_users) or user in set(allowed_users)


def only_user(allowed_users, user):
	"""Whether `user` is the only user the outlet lists."""
	return {name for name in allowed_users or () if name} == {user}


def listing_changes(outlet_users, user, wanted):
	"""(outlets to list `user` on, outlets to take them off) so that, among `outlet_users`, the
	user works at exactly the `wanted` ones. An outlet whose table is empty is never changed.
	ValueError for a wanted outlet not in `outlet_users`, and for an outlet the user would leave
	while being the only user it lists."""
	if not isinstance(wanted, list | tuple) or not all(isinstance(name, str) for name in wanted):
		raise ValueError("outlets must be a list of outlet names")
	wanted = set(wanted)
	unknown = sorted(wanted - set(outlet_users))
	if unknown:
		raise ValueError(f"Not an outlet: {', '.join(unknown)}")
	add = sorted(name for name in wanted if needs_listing(outlet_users[name], user))
	remove = sorted(
		name for name, users in outlet_users.items() if name not in wanted and user in (users or ())
	)
	alone = [name for name in remove if only_user(outlet_users[name], user)]
	if alone:
		raise ValueError(
			f"{user} is the only user outlet {', '.join(alone)} lists: taking them off would open it "
			"to every user. Add another user to its Applicable for Users first"
		)
	return add, remove


# checks (pure)


def pin_problem(pin):
	"""A till PIN set from the web or a till is exactly 6 ASCII digits."""
	if not isinstance(pin, str) or not _TILL_PIN_RE.fullmatch(pin):
		return f"The POS PIN must be {TILL_PIN_LENGTH} digits (0-9)"
	return None


def self_change_problem(approver, user, privilege=None, enabled=None):
	"""A manager cannot disable themself or take their own manager privilege away."""
	if not user or user != approver:
		return None
	if enabled is not None and not enabled:
		return "You cannot disable your own user"
	if privilege is not None and privilege != MANAGER_PRIVILEGE:
		return "You cannot take your own manager privilege away"
	return None


# saving


def web_staff_user(user):
	"""The User document app.crenya.com reads and changes: any user but Administrator and Guest
	(a System Manager too: the owner often works the till). Checked on the input and on the
	stored name: user names compare without case in the database ("administrator" loads
	Administrator)."""
	user = cstr(user).strip()
	if is_reserved(user):
		refuse(_("User {0} cannot be given a till role or PIN").format(user))
	if not user or not frappe.db.exists("User", user):
		invalid(_("User {0} does not exist").format(user))
	doc = frappe.get_doc("User", user)
	if is_reserved(doc.name):
		refuse(_("User {0} cannot be given a till role or PIN").format(doc.name))
	return doc


def _set_privilege(doc, privilege, privilege_roles):
	"""Give the User document exactly the privilege's roles among `managed_roles`, never touching
	its other roles; whether its roles changed."""
	add, remove = role_changes([row.role for row in doc.get("roles") or []], privilege, privilege_roles)
	doc.set("roles", [row for row in doc.get("roles") or [] if row.role not in remove])
	for role in add:
		doc.append("roles", {"role": role})
	return bool(add or remove)


def _list_at_outlet(ctx, user):
	"""Add the user to the outlet's Applicable for Users when that table is not empty."""
	profile = frappe.get_doc("POS Profile", ctx.outlet)
	if not needs_listing([row.user for row in profile.applicable_for_users], user):
		return
	profile.append("applicable_for_users", {"user": user})
	profile.save(ignore_permissions=True)
	ctx.log(profile)
	frappe.clear_document_cache("POS Profile", ctx.outlet)


def _unlist_at_outlet(ctx, user):
	profile = frappe.get_doc("POS Profile", ctx.outlet)
	profile.set("applicable_for_users", [row for row in profile.applicable_for_users if row.user != user])
	profile.save(ignore_permissions=True)
	ctx.log(profile)
	frappe.clear_document_cache("POS Profile", ctx.outlet)


def save_web_staff(approver, doc, privilege, pin, outlets, outlet_users, privilege_roles):
	"""Set a user's till privilege (None: none of `privilege_roles`' roles), PIN (empty:
	unchanged) and outlets (None: unchanged; else exactly these among `outlet_users`, {outlet: its
	Applicable for Users}). Everything is checked before anything is saved. The till's own
	self-change rule does not apply: the caller is a System Manager and keeps that role whatever
	privilege they give themself."""
	if privilege is not None and privilege not in privilege_roles:
		invalid(_("Privilege must be one of {0}").format(", ".join(privilege_roles)))
	if pin:
		problem = pin_problem(pin)
		if problem:
			invalid(problem)
	add_to, remove_from = (
		([], []) if outlets is None else checked(listing_changes, outlet_users, doc.name, outlets)
	)
	changed = _set_privilege(doc, privilege, privilege_roles)
	if pin:
		# the User hook hashes it into crenya_pos_pin_hash and clears the field
		doc.set(PIN_FIELD, pin)
	if changed or pin:
		doc.save(ignore_permissions=True)
		WebAdminContext(approver).log(doc)
	for outlet in add_to:
		_list_at_outlet(WebAdminContext(approver, outlet), doc.name)
	for outlet in remove_from:
		_unlist_at_outlet(WebAdminContext(approver, outlet), doc.name)
