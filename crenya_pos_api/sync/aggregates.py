"""Push aggregate handlers: how one kind of till event is validated, applied and reported.

`push_batch` resolves every event's `aggregate_type` to a handler (built-ins below, plus
the `crenya_pos_aggregates` hook of other apps, see `crenya_pos_api.sync.registry`) and
keeps the protocol around it: envelope and payload-hash checks, idempotency on
`event_id`, one savepoint and commit per event, the Crenya Sync Event record and the
result shape. A handler only deals with its own payload and document.
"""

import frappe
from frappe.utils import cint

from crenya_pos_api.sync.customer import create_customer, customer_result_fields
from crenya_pos_api.sync.invoice_builder import invoice_result_fields, submit_invoice
from crenya_pos_api.sync.shift import create_shift, shift_result_fields
from crenya_pos_api.sync.validation import (
	AGGREGATE_CUSTOMER,
	AGGREGATE_SALES_INVOICE,
	AGGREGATE_SHIFT,
	validate_customer_payload,
	validate_invoice_payload,
	validate_shift_payload,
)
from crenya_pos_api.utils.dates import format_db_datetime


class AggregateHandler:
	"""Base class of a push aggregate (register a subclass with the `crenya_pos_aggregates` hook).

	Attributes:
	        aggregate_type: the envelope's `aggregate_type` (set from the hook key when left None).
	        doctype: DocType of the documents `apply` returns and `find_by_local_id` names;
	                None means the same as `aggregate_type`.
	        operations: envelope `operation` values the handler accepts; anything else is a
	                `validation` error before the payload is looked at.
	        retention_days: days successful Crenya Sync Events of this type are kept; None uses
	                the site setting (`crenya_pos_event_retention_days`, at least 90, default 120).
	        local_id_field: field named in the Sync Event note when `find_by_local_id` matched.

	Every method runs inside the event's savepoint: raise `SyncError` (or any Frappe
	exception) to fail the event; nothing it wrote is kept then.
	"""

	aggregate_type = None
	doctype = None
	operations = ("submit",)
	retention_days = None
	local_id_field = "local_id"

	@property
	def result_doctype(self):
		return self.doctype or self.aggregate_type

	def validate(self, operation, payload):
		"""Check the raw payload (no database access needed) and return the normalized data
		that `apply` receives. Raise `SyncError(VALIDATION, message naming the field)`."""
		raise NotImplementedError

	def apply(self, ctx, operation, data, notes):
		"""Write the change and return the resulting document (anything with `doctype` and
		`name`). `ctx` is the device's `DeviceContext`; append human-readable remarks to
		`notes` (they are stored on the Crenya Sync Event)."""
		raise NotImplementedError

	def find_by_local_id(self, local_id):
		"""Idempotency fallback, called for every event before `validate`/`apply` whose
		`event_id` was not applied yet: return the name of an existing document that already
		reflects this event (it was written but the till never saw the response), or None.

		It runs for every operation, so an aggregate changed by several operations (an order
		that is created, then updated) must return None, or check the operation it can tell
		from its own data; `event_id` idempotency always applies."""
		return None

	def result_fields(self, doc):
		"""Fields merged into the push result for the document (`doctype`, `name`,
		`docstatus`, `modified`, `totals`, `fawtara_status`)."""
		return {
			"doctype": doc.doctype,
			"name": doc.name,
			"docstatus": cint(doc.get("docstatus")),
			"modified": format_db_datetime(doc.get("modified")),
			"totals": None,
			"fawtara_status": None,
		}


class CustomerHandler(AggregateHandler):
	"""Customer captured offline at the till."""

	aggregate_type = AGGREGATE_CUSTOMER
	local_id_field = "crenya_local_id"

	def validate(self, operation, payload):
		return validate_customer_payload(payload)

	def apply(self, ctx, operation, data, notes):
		return create_customer(ctx, data)

	def find_by_local_id(self, local_id):
		return frappe.db.get_value(AGGREGATE_CUSTOMER, {"crenya_local_id": local_id}, "name")

	def result_fields(self, doc):
		return customer_result_fields(doc)


class SalesInvoiceHandler(AggregateHandler):
	"""Sale or return, submitted as an ERPNext Sales Invoice (`crenya_pos_invoice_extenders`
	run between building and inserting it)."""

	aggregate_type = AGGREGATE_SALES_INVOICE
	local_id_field = "crenya_local_id"

	def validate(self, operation, payload):
		return validate_invoice_payload(payload)

	def apply(self, ctx, operation, data, notes):
		return submit_invoice(ctx, data, notes)

	def find_by_local_id(self, local_id):
		return frappe.db.get_value(AGGREGATE_SALES_INVOICE, {"crenya_local_id": local_id}, "name")

	def result_fields(self, doc):
		return invoice_result_fields(doc)


class ShiftHandler(AggregateHandler):
	"""Cashier shift closed at the till (Crenya POS Shift, named after its local_id)."""

	aggregate_type = AGGREGATE_SHIFT

	def validate(self, operation, payload):
		return validate_shift_payload(payload)

	def apply(self, ctx, operation, data, notes):
		return create_shift(ctx, data, notes)

	def find_by_local_id(self, local_id):
		return frappe.db.exists(AGGREGATE_SHIFT, local_id) or None

	def result_fields(self, doc):
		return shift_result_fields(doc)


def builtin_handlers():
	"""The aggregates every till speaks, in protocol order."""
	return {
		handler.aggregate_type: handler
		for handler in (CustomerHandler(), SalesInvoiceHandler(), ShiftHandler())
	}
