"""The Crenya POS Manager role gets desk access (0.11.0), as Restaurant Manager has: a shop
manager signs in to app.crenya.com, which takes System Users only, and Frappe makes a user whose
roles all lack desk access a Website User. The role still has no DocPerm of its own, so desk
access grants nothing. Its enabled holders that are Website Users become System Users now (Frappe
recomputes the type only when a user is saved).
"""

import frappe

from crenya_pos_api.setup.install import make_role
from crenya_pos_api.sync.approvals import APPROVER_ROLE


def execute():
	make_role()
	frappe.db.set_value("Role", APPROVER_ROLE, "desk_access", 1)
	holders = frappe.get_all(
		"Has Role", filters={"parenttype": "User", "role": APPROVER_ROLE}, pluck="parent", distinct=True
	)
	user = frappe.qb.DocType("User")
	if holders:
		(
			frappe.qb.update(user)
			.set(user.user_type, "System User")
			.where(user.name.isin(holders) & (user.user_type == "Website User") & (user.enabled == 1))
		).run()
	frappe.clear_cache()
