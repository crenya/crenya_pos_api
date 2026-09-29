# Copyright (c) 2026, sammish and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class CrenyaPOSDevice(Document):
	def validate(self):
		if not self.is_new() and self.has_value_changed("device_short"):
			# the code is baked into offline invoice numbers already issued by the till
			frappe.throw(_("Device Code cannot be changed once assigned"))
