# Copyright (c) 2026, sammish and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class CrenyaSyncEvent(Document):
	pass


def on_doctype_update():
	# the daily purge selects old events by creation
	frappe.db.add_index("Crenya Sync Event", ["creation"])
