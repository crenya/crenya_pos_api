"""Row-level access for Crenya POS Device: POS users only see devices registered to them."""

import frappe


def _is_manager(user):
	return "System Manager" in frappe.get_roles(user)


def device_query_conditions(user=None):
	user = user or frappe.session.user
	if user == "Administrator" or _is_manager(user):
		return ""
	return f"`tabCrenya POS Device`.`user` = {frappe.db.escape(user)}"


def device_has_permission(doc, ptype=None, user=None):
	user = user or frappe.session.user
	if user == "Administrator" or _is_manager(user):
		return True
	return doc.user == user
