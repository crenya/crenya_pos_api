"""Store controls on existing sites (0.11.0): the Crenya POS Manager role and the POS Profile
*Store controls* fields, made before anything reads them (after_migrate runs after the patches),
and every existing POS Profile starting with every approval on, the fields' default."""

import frappe

from crenya_pos_api.setup.install import make_custom_fields, make_role
from crenya_pos_api.sync.approvals import APPROVAL_ACTIONS


def execute():
	make_role()
	make_custom_fields()
	profile = frappe.qb.DocType("POS Profile")
	query = frappe.qb.update(profile)
	for _key, field, _label, _label_ar in APPROVAL_ACTIONS:
		query = query.set(profile[field], 1)
	query.run()
