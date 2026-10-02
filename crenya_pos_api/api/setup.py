"""Site setup the operator can trigger after a deploy."""

import frappe
from frappe import _

from crenya_pos_api.setup.install import ensure_setup
from crenya_pos_api.sync.context import is_system_manager


@frappe.whitelist(methods=["POST"])
def ensure_custom_fields() -> dict[str, object]:
	"""Create or update the app's role and custom fields (System Manager only, idempotent).

	The same work runs after install and after every migrate; this lets an operator (or a seeding
	script) apply it when a deploy did not run `after_migrate`.
	"""
	# checked here rather than with frappe.only_for, which lets everyone through while tests run
	if not is_system_manager():
		frappe.throw(_("Only a System Manager can set up Crenya POS custom fields"), frappe.PermissionError)
	return ensure_setup()
