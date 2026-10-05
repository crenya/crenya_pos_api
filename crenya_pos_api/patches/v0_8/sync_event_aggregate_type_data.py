"""Crenya Sync Event.aggregate_type is Data now (other apps add aggregate types); index creation."""

import frappe

EVENT_DOCTYPE = "Crenya Sync Event"


def execute():
	# Property Setters that widened the old Select (a workaround for new types) no longer apply
	frappe.db.delete(
		"Property Setter",
		{
			"doc_type": EVENT_DOCTYPE,
			"field_name": "aggregate_type",
			"property": ("in", ["options", "fieldtype"]),
		},
	)
	frappe.clear_cache(doctype=EVENT_DOCTYPE)
	frappe.db.add_index(EVENT_DOCTYPE, ["creation"])
