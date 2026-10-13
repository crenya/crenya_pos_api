"""Who reads and manages a shop on app.crenya.com (not whitelisted: the endpoints' shared check).

A shop is a retail POS Profile: one that is not a restaurant outlet (crenya_restaurant's
`crenya_restaurant_mode` is not 1, or the site has no such field). A System Manager manages every
shop; a holder of the approver role (`APPROVER_ROLE`, Crenya POS Manager) the shops whose
Applicable for Users lists them, or lists nobody (every user works there, as on the till). For a
shop, reading is managing: there is no report-only role. Anyone else gets frappe.PermissionError.
"""

import frappe
from frappe.utils import cint

from crenya_pos_api.sync.approvals import APPROVER_ROLE
from crenya_pos_api.sync.context import profile_users
from crenya_pos_api.web.context import WebAdminContext

# crenya_restaurant's POS Profile field that makes an outlet a restaurant
RESTAURANT_MODE_FIELD = "crenya_restaurant_mode"


def manages(roles, user, allowed_users) -> bool:
	"""Whether a user with `roles` manages a shop whose Applicable for Users is `allowed_users`
	(pure): a System Manager every shop; an approver one listing them, or listing nobody."""
	roles = set(roles or ())
	if "System Manager" in roles:
		return True
	return APPROVER_ROLE in roles and (not allowed_users or user in allowed_users)


def shop_profiles(outlet: str | None = None) -> list:
	"""[{name, company}] of the shops (of `outlet` alone when given), by name."""
	filters = {"name": outlet} if outlet else {}
	fields = ["name", "company"]
	if frappe.get_meta("POS Profile").has_field(RESTAURANT_MODE_FIELD):
		fields.append(RESTAURANT_MODE_FIELD)
	rows = frappe.get_all("POS Profile", filters=filters, fields=fields, order_by="name asc")
	return [row for row in rows if not cint(row.get(RESTAURANT_MODE_FIELD))]


def managed_shops() -> list:
	"""The shops the session user manages ([{name, company}], by name)."""
	user = frappe.session.user
	if user in (None, "Guest"):
		return []
	roles = frappe.get_roles(user)
	if "System Manager" not in roles and APPROVER_ROLE not in roles:
		return []
	return [
		row
		for row in shop_profiles()
		if manages(roles, user, set(profile_users(frappe.get_cached_doc("POS Profile", row.name))))
	]


def manage(outlet) -> WebAdminContext:
	"""The WebAdminContext of a shop the session user manages; frappe.PermissionError for any
	other (or no) outlet, a restaurant outlet included."""
	if not outlet or not isinstance(outlet, str) or not shop_profiles(outlet):
		raise frappe.PermissionError
	user = frappe.session.user
	ctx = WebAdminContext(user, outlet)
	if not manages(frappe.get_roles(user), user, set(profile_users(ctx.profile))):
		raise frappe.PermissionError
	return ctx
