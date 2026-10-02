# Copyright (c) 2026, sammish and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.model.naming import append_number_if_name_exists

from crenya_pos_api.sync.terminals import validate_links, validate_settings


class CrenyaPOSPaymentTerminal(Document):
	def autoname(self):
		# readable: "<label> - <POS Profile>", or "<label> - <company abbreviation>" for every profile
		label = (self.label or "").strip() or self.provider
		scope = self.pos_profile or frappe.get_cached_value("Company", self.company, "abbr") or self.company
		self.name = append_number_if_name_exists(self.doctype, f"{label} - {scope}")

	def validate(self):
		validate_settings(self)
		validate_links(self)
