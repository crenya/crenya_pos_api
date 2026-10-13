"""Store controls on existing sites (0.11.0): the Crenya POS Manager role and the POS Profile
*Store controls* fields, made before anything reads them (after_migrate runs after the patches).

The approval Checks default to on, for POS Profiles created from now on. A profile that existed
before this migrate keeps today's behaviour: every approval off, so an updated till asks for
nothing new until the owner switches actions on (app.crenya.com). The patch runs once, so every
row it sees existed before."""

import frappe

from crenya_pos_api.setup.install import make_custom_fields, make_role
from crenya_pos_api.sync.approvals import APPROVAL_ACTIONS


def execute():
	make_role()
	make_custom_fields()
	profile = frappe.qb.DocType("POS Profile")
	query = frappe.qb.update(profile)
	for _key, field, _label, _label_ar in APPROVAL_ACTIONS:
		query = query.set(profile[field], 0)
	query.run()
