# Copyright (c) 2026, sammish and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, cstr, flt, get_datetime

# One file directly under /private/files/: no sub-path, no "..", no encoded or query tricks.
PRIVATE_FILE_URL = re.compile(r"^/private/files/[^/\\?#%\x00]+$")


class POSExpense(Document):
	"""A payout from a till drawer (`local_id` set), booked as a paid Purchase Invoice by
	crenya_pos_api's expenses engine (crenya_pos_api.expenses.booking).

	The erpnext branch ships this doctype itself (it came from erptrue_app, whose legacy
	ERPTrue Expense booking is left out: a plain ERPNext site has no such doctype).
	"""

	def validate(self):
		if self.get("photo") and self.has_value_changed("photo"):
			self.validate_photo()
		if self.get("local_id"):
			self.validate_till_amounts()
		self.validate_log_append_only()

	def money_precision(self):
		"""Decimals of this expense's currency, by the rule ERPNext rounds the Purchase Invoice it
		is booked as (and the till's own totals): `frappe.get_precision` with the currency."""
		currency = self.get("currency") or (
			self.get("company") and frappe.get_cached_value("Company", self.company, "default_currency")
		)
		return cint(frappe.get_precision("Purchase Invoice", "grand_total", currency=currency or None))

	def validate_till_amounts(self):
		p = self.money_precision()
		if flt(self.amount, p) != flt(flt(self.net_amount, p) + flt(self.tax_amount, p), p):
			frappe.throw(
				_("Amount {0} must equal net {1} plus tax {2}.").format(
					self.amount, self.net_amount, self.tax_amount
				)
			)

	def validate_photo(self):
		url = self.photo
		if ".." in url or not PRIVATE_FILE_URL.match(url):
			frappe.throw(_("The bill photo must be a private file."))
		user = frappe.session.user
		files = frappe.get_all(
			"File",
			filters={"file_url": url, "is_private": 1},
			fields=["attached_to_doctype", "attached_to_name", "owner"],
		)
		if not any(
			(f.attached_to_doctype == self.doctype and f.attached_to_name == self.name)
			or (not f.attached_to_doctype and f.owner == user)
			for f in files
		):
			frappe.throw(_("The bill photo must be a file of this expense or one you uploaded."))

	def validate_log_append_only(self):
		"""The log is an audit trail: rows are appended, never edited or removed."""
		before = self.get_doc_before_save()
		if not before or not before.get("log"):
			return
		fields = [
			df
			for df in frappe.get_meta("POS Expense Log").fields
			if df.fieldtype not in ("Section Break", "Column Break")
		]

		def row(r):
			return tuple(
				cstr(get_datetime(r.get(df.fieldname)))
				if df.fieldtype == "Datetime" and r.get(df.fieldname)
				else cstr(r.get(df.fieldname))
				for df in fields
			)

		now = self.get("log") or []
		if len(now) < len(before.log) or any(row(a) != row(b) for a, b in zip(before.log, now, strict=False)):
			frappe.throw(_("The expense log only takes new rows; earlier rows cannot change."))
