# Copyright (c) 2026, sammish and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class CrenyaPOSControlLog(Document):
	pass


def on_doctype_update():
	"""One chunk per device and local id: a chunk sent twice at once is stored once (the second
	insert is a duplicate entry, answered as the first by the sync framework)."""
	frappe.db.add_unique("Crenya POS Control Log", ["device", "local_id"], constraint_name="device_local_id")
