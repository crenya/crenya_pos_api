# Copyright (c) 2026, sammish and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from crenya_pos_api.sync.locale_data import DENOMINATION_KINDS


class CrenyaCashDenomination(Document):
	def validate(self):
		self.label = (self.label or "").strip()
		if not self.label:
			frappe.throw(_("Label is required"))
		if self.kind not in DENOMINATION_KINDS:
			frappe.throw(_("Kind must be one of {0}").format(", ".join(DENOMINATION_KINDS)))
		self.value = flt(self.value)
		if self.value <= 0:
			frappe.throw(_("Value must be greater than zero"))
		self.validate_unique_value()

	def validate_unique_value(self):
		duplicate = frappe.db.exists(
			self.doctype,
			{"currency": self.currency, "value": self.value, "name": ["!=", self.name or ""]},
		)
		if duplicate:
			frappe.throw(
				_("A denomination of {0} {1} already exists ({2})").format(
					self.get_formatted("value"), self.currency, duplicate
				),
				frappe.UniqueValidationError,
			)
