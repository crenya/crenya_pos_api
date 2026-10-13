"""Cashier PIN administration."""

import frappe
from frappe import _

from crenya_pos_api.sync.cashier import clear_user_pin
from crenya_pos_api.web.context import require_web_session


@frappe.whitelist(methods=["POST"])
def clear_pin(user: str | None = None):
	"""Remove a cashier's POS PIN (System Manager only, from a browser session: never with a till's
	or device's key). Tills drop it on their next pull."""
	require_web_session()
	frappe.only_for("System Manager")
	if not user or not isinstance(user, str) or not frappe.db.exists("User", user):
		frappe.throw(_("User {0} does not exist").format(user), frappe.DoesNotExistError)
	return clear_user_pin(user)
