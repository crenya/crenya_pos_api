"""Store controls on existing sites (0.11.0): the Crenya POS Manager role and the POS Profile
*Store controls* fields, made before anything reads them (after_migrate runs after the patches).

The approval Checks default to on, for POS Profiles created from now on. A profile that existed
before the fields did keeps today's behaviour: every approval off, so an updated till asks for
nothing new until the owner switches actions on (app.crenya.com). Only profiles created before
the fields' Custom Field records are touched.

Never run this patch again by hand (`bench execute`, or removing it from the Patch Log): it
would switch off the approvals that owners turned on since on those older profiles.
"""

import frappe

from crenya_pos_api.setup.install import make_custom_fields, make_role
from crenya_pos_api.sync.approvals import APPROVAL_ACTIONS


def execute():
	make_role()
	make_custom_fields()
	fields = [field for _key, field, _label, _label_ar in APPROVAL_ACTIONS]
	# when the store controls arrived on this site: the first run's make_custom_fields, above
	since = frappe.db.get_value("Custom Field", {"dt": "POS Profile", "fieldname": fields[0]}, "creation")
	if not since:
		return
	profile = frappe.qb.DocType("POS Profile")
	query = frappe.qb.update(profile).where(profile.creation < since)
	for field in fields:
		query = query.set(profile[field], 0)
	query.run()
