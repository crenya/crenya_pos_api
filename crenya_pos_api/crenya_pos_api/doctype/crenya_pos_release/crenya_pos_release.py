# Copyright (c) 2026, sammish and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, now

from crenya_pos_api.sync.update import (
	CHANNELS,
	PRIVATE_FILES_PREFIX,
	STABLE,
	duplicate_targets,
	parse_release_version,
	validate_target,
)


class CrenyaPOSRelease(Document):
	def before_naming(self):
		self.version = (self.version or "").strip()

	def validate(self):
		self.validate_version()
		if self.channel not in CHANNELS:
			self.channel = STABLE
		self.validate_artifacts()
		if cint(self.published):
			if not self.artifacts:
				frappe.throw(_("Add at least one artifact before publishing"))
			if not self.pub_date:
				self.pub_date = now()

	def validate_version(self):
		try:
			parse_release_version(self.version)
		except ValueError:
			frappe.throw(_("Version must be X.Y.Z (for example 0.2.0), not {0}").format(self.version))
		if not self.is_new() and self.has_value_changed("version"):
			frappe.throw(_("Version cannot be changed"))

	def validate_artifacts(self):
		for row in self.artifacts:
			try:
				validate_target(row.target)
			except ValueError:
				frappe.throw(_("Row {0}: unknown target {1}").format(row.idx, row.target))
			if row.file and not row.file.startswith(PRIVATE_FILES_PREFIX):
				frappe.throw(_("Row {0}: attach the file as private").format(row.idx))
			row.signature = (row.signature or "").strip()
			if not row.signature:
				frappe.throw(_("Row {0}: signature is required").format(row.idx))

		duplicates = duplicate_targets(self.artifacts)
		if duplicates:
			frappe.throw(_("Only one artifact per target is allowed: {0}").format(", ".join(duplicates)))
