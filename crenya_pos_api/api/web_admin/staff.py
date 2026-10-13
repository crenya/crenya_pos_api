"""Shop staff on app.crenya.com (People → a user → Shop till): a user's retail till privilege,
6-digit till PIN and the shops they work at. System Manager only, as the restaurant's; the rules
are `web.staff`'s, the privileges `sync.approvals.RETAIL_PRIVILEGES` (cashier: Crenya POS User;
manager: also Crenya POS Manager, who approves at the till). From a browser session only
(`require_web_session`): a till or device key is refused. The answer is the restaurant's
`staff_get` shape plus `privileges`, the options the web offers."""

import frappe

from crenya_pos_api.api.web_admin.scope import shop_profiles
from crenya_pos_api.sync.approvals import RETAIL_PRIVILEGE_LABELS, RETAIL_PRIVILEGES
from crenya_pos_api.sync.cashier import PIN_HASH_FIELD, clear_user_pin
from crenya_pos_api.sync.context import is_system_manager, profile_users
from crenya_pos_api.web import staff
from crenya_pos_api.web.context import WebAdminContext, require_web_session
from crenya_pos_api.web.devices import labelled


def _staff_user(user):
	require_web_session()
	# not frappe.only_for, which lets every user through while tests run
	if not is_system_manager():
		raise frappe.PermissionError
	return staff.web_staff_user(user)


def _outlet_users() -> dict:
	"""{shop: its Applicable for Users} of every shop."""
	return {
		row.name: set(profile_users(frappe.get_cached_doc("POS Profile", row.name)))
		for row in shop_profiles()
	}


@frappe.whitelist(methods=["GET", "POST"])
def staff_get(user: str) -> dict:
	"""A user's shop till access: privilege (cashier, manager or None), whether a till PIN is set
	(never the PIN), the shops they work at (listed, or a shop listing nobody), every shop with
	`everyone` (it lists nobody: every user works there) and `only_user` (this user is the only
	one it lists; `staff_save` will not take them off), and the privileges offered."""
	doc = _staff_user(user)
	listed = _outlet_users()
	return {
		"user": doc.name,
		"privilege": staff.privilege_of([row.role for row in doc.get("roles") or []], RETAIL_PRIVILEGES),
		"has_pin": bool(doc.get(PIN_HASH_FIELD)),
		"outlets": [name for name, users in listed.items() if staff.works_at(users, doc.name)],
		"all_outlets": [
			{
				"name": name,
				"outlet_name": name,
				"everyone": staff.open_to_everyone(users),
				"only_user": staff.only_user(users, doc.name),
			}
			for name, users in listed.items()
		],
		"privileges": labelled(RETAIL_PRIVILEGE_LABELS),
	}


@frappe.whitelist(methods=["POST"])
def staff_save(
	user: str, privilege: str | None, pin: str | None = None, outlets: list[str] | None = None
) -> dict:
	"""Set a user's privilege (null or empty: no shop till role), till PIN (empty: unchanged) and
	shops (None: unchanged); answers `staff_get`."""
	doc = _staff_user(user)
	outlets = frappe.parse_json(outlets) if isinstance(outlets, str) else outlets
	staff.save_web_staff(
		frappe.session.user, doc, privilege or None, pin, outlets, _outlet_users(), RETAIL_PRIVILEGES
	)
	return staff_get(doc.name)


@frappe.whitelist(methods=["POST"])
def staff_clear_pin(user: str) -> dict:
	"""Remove a user's till PIN; answers `staff_get`. A user without a PIN is left as is."""
	doc = _staff_user(user)
	if doc.get(PIN_HASH_FIELD):
		clear_user_pin(doc.name)
		WebAdminContext(frappe.session.user).log(doc)
	return staff_get(doc.name)
