"""Cashier PIN administration."""

import frappe
from frappe import _

from crenya_pos_api.sync.cashier import clear_user_pin


@frappe.whitelist(methods=["POST"])
def clear_pin(user=None):
	"""Remove a cashier's POS PIN (System Manager only). Tills drop it on their next pull."""
	frappe.only_for("System Manager")
	if not user or not isinstance(user, str) or not frappe.db.exists("User", user):
		frappe.throw(_("User {0} does not exist").format(user), frappe.DoesNotExistError)
	return clear_user_pin(user)
