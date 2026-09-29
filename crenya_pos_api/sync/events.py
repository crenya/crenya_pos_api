"""push_batch processing: idempotent, one savepoint + commit per event.

Each event either commits its document together with an `ok` Crenya Sync Event,
or is rolled back to its savepoint and leaves only an `error` Sync Event (with
`attempts` incremented) so that retries stay possible.
"""

import frappe
from frappe.utils import cint, now

from crenya_pos_api.sync.context import EVENT_DOCTYPE
from crenya_pos_api.sync.customer import create_customer, customer_result_fields
from crenya_pos_api.sync.errors import PAYLOAD_CONFLICT, SyncError, classify_exception, error_dict
from crenya_pos_api.sync.hashing import payload_hash
from crenya_pos_api.sync.invoice_builder import invoice_result_fields, submit_invoice
from crenya_pos_api.sync.shift import create_shift, shift_result_fields
from crenya_pos_api.sync.validation import (
	AGGREGATE_CUSTOMER,
	AGGREGATE_SALES_INVOICE,
	AGGREGATE_SHIFT,
	validate_customer_payload,
	validate_envelope,
	validate_invoice_payload,
	validate_shift_payload,
)

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_DUPLICATE = "duplicate"

EVENT_FIELDS = ["name", "device", "status", "payload_hash", "result_doctype", "result_name", "attempts"]
MAX_NOTE_LENGTH = 2000


def empty_result(event_id, doctype=None):
	return {
		"event_id": event_id,
		"status": STATUS_ERROR,
		"doctype": doctype,
		"name": None,
		"docstatus": None,
		"modified": None,
		"totals": None,
		"fawtara_status": None,
		"error": None,
	}


def error_result(event_id, error, doctype=None):
	result = empty_result(event_id, doctype)
	result["error"] = error
	return result


def document_result(event_id, status, doctype, name):
	"""Result for an already persisted document (fresh values, not a cached copy)."""
	result = empty_result(event_id, doctype)
	result["status"] = status
	result["name"] = name
	if not name or not frappe.db.exists(doctype, name):
		return result

	doc = frappe.get_doc(doctype, name)
	result.update(_result_fields(doctype, doc))
	return result


def _result_fields(doctype, doc):
	if doctype == AGGREGATE_SALES_INVOICE:
		return invoice_result_fields(doc)
	if doctype == AGGREGATE_SHIFT:
		return shift_result_fields(doc)
	return customer_result_fields(doc)


def _find_by_local_id(doctype, local_id):
	if doctype == AGGREGATE_SHIFT:
		# Crenya POS Shift is named after its local_id
		return frappe.db.exists(AGGREGATE_SHIFT, local_id) or None
	return frappe.db.get_value(doctype, {"crenya_local_id": local_id}, "name")


def _load_event(event_id, for_update=False):
	return frappe.db.get_value(EVENT_DOCTYPE, event_id, EVENT_FIELDS, as_dict=True, for_update=for_update)


def _note(notes):
	text = "\n".join(notes) if notes else None
	if text and len(text) > MAX_NOTE_LENGTH:
		text = text[: MAX_NOTE_LENGTH - 3] + "..."
	return text


def _record_ok(ctx, env, existing, doctype, name, notes):
	timestamp = now()
	values = {
		"status": STATUS_OK,
		"result_doctype": doctype,
		"result_name": name,
		"error_code": None,
		"error_message": None,
		"note": _note(notes),
		"processed_at": timestamp,
	}
	if existing:
		event = frappe.get_doc(EVENT_DOCTYPE, env["event_id"])
		event.update(values)
		event.attempts = cint(event.attempts) + 1
		event.save(ignore_permissions=True)
		return

	event = frappe.new_doc(EVENT_DOCTYPE)
	event.update(values)
	event.update(
		{
			"event_id": env["event_id"],
			"device": ctx.device_id,
			"aggregate_type": env["aggregate_type"],
			"operation": env["operation"],
			"local_id": env["local_id"],
			"sequence_no": env["sequence_no"],
			"payload_hash": env["payload_hash"],
			"attempts": 1,
			"received_at": timestamp,
		}
	)
	event.insert(ignore_permissions=True)


def _record_error(ctx, env, error):
	"""Persist a failed attempt. Never downgrades an ok event or one owned by another payload."""
	existing = _load_event(env["event_id"], for_update=True)
	if existing and (
		existing.status == STATUS_OK
		or existing.payload_hash != env["payload_hash"]
		or existing.device != ctx.device_id
	):
		return

	values = {
		"status": STATUS_ERROR,
		"error_code": error["code"],
		"error_message": error["message"],
		"processed_at": now(),
	}
	if existing:
		event = frappe.get_doc(EVENT_DOCTYPE, env["event_id"])
		event.update(values)
		event.attempts = cint(event.attempts) + 1
		event.save(ignore_permissions=True)
		return

	event = frappe.new_doc(EVENT_DOCTYPE)
	event.update(values)
	event.update(
		{
			"event_id": env["event_id"],
			"device": ctx.device_id,
			"aggregate_type": env["aggregate_type"],
			"operation": env["operation"],
			"local_id": env["local_id"],
			"sequence_no": env["sequence_no"],
			"payload_hash": env["payload_hash"],
			"attempts": 1,
			"received_at": values["processed_at"],
		}
	)
	event.insert(ignore_permissions=True)


def _idempotent_result(ctx, env):
	"""Return (result, existing_event). result is set when the event was already applied."""
	existing = _load_event(env["event_id"], for_update=True)
	if existing:
		if existing.payload_hash != env["payload_hash"]:
			raise SyncError(PAYLOAD_CONFLICT, "event_id was already used with a different payload")
		if existing.device != ctx.device_id:
			raise SyncError(PAYLOAD_CONFLICT, "event_id was already used by another device")
		if existing.status == STATUS_OK:
			doctype = existing.result_doctype or env["aggregate_type"]
			return document_result(env["event_id"], STATUS_DUPLICATE, doctype, existing.result_name), existing

	doctype = env["aggregate_type"]
	name = _find_by_local_id(doctype, env["local_id"])
	if name:
		# the document exists but the till never saw the response
		matched_by = "local_id" if doctype == AGGREGATE_SHIFT else "crenya_local_id"
		_record_ok(ctx, env, existing, doctype, name, [f"Matched existing document by {matched_by}"])
		return document_result(env["event_id"], STATUS_DUPLICATE, doctype, name), existing

	return None, existing


def _apply(ctx, env):
	result, existing = _idempotent_result(ctx, env)
	if result:
		return result

	notes = []
	if env["aggregate_type"] == AGGREGATE_CUSTOMER:
		data = validate_customer_payload(env["payload"])
		doc = create_customer(ctx, data)
		fields = customer_result_fields(doc)
	elif env["aggregate_type"] == AGGREGATE_SHIFT:
		data = validate_shift_payload(env["payload"])
		doc = create_shift(ctx, data, notes)
		fields = shift_result_fields(doc)
	else:
		data = validate_invoice_payload(env["payload"])
		doc = submit_invoice(ctx, data, notes)
		fields = invoice_result_fields(doc)

	_record_ok(ctx, env, existing, env["aggregate_type"], doc.name, notes)

	result = empty_result(env["event_id"], env["aggregate_type"])
	result.update(fields)
	result["status"] = STATUS_OK
	return result


def _discard(savepoint):
	"""Undo the failed event. Everything before it is committed, so a full rollback is
	safe and also drops after-commit callbacks queued by the failed documents."""
	try:
		frappe.db.rollback(save_point=savepoint)
	except Exception:
		# the server may already have aborted the transaction (deadlock), dropping the savepoint
		pass
	frappe.db.rollback()


def process_event(ctx, raw_event, index=0):
	event_id = raw_event.get("event_id") if isinstance(raw_event, dict) else None
	aggregate_type = raw_event.get("aggregate_type") if isinstance(raw_event, dict) else None

	try:
		env = validate_envelope(raw_event)
	except SyncError as e:
		return error_result(event_id if isinstance(event_id, str) else None, e.as_dict(), aggregate_type)

	if payload_hash(env["payload"]) != env["payload_hash"]:
		return error_result(
			env["event_id"],
			error_dict(PAYLOAD_CONFLICT, "payload_hash does not match the canonical payload"),
			env["aggregate_type"],
		)

	savepoint = f"crenya_sync_event_{index}"
	frappe.db.savepoint(savepoint)
	try:
		result = _apply(ctx, env)
		# one commit per event is the protocol: a later failing event must not
		# roll back documents the till already got an "ok" for
		frappe.db.commit()  # nosemgrep
		return result
	except Exception as exc:
		_discard(savepoint)

		if isinstance(exc, frappe.UniqueValidationError | frappe.DuplicateEntryError):
			# a concurrent request may have created the document first
			try:
				duplicate, _existing = _idempotent_result(ctx, env)
			except SyncError as conflict:
				frappe.db.rollback()
				return error_result(env["event_id"], conflict.as_dict(), env["aggregate_type"])
			except Exception:
				frappe.db.rollback()
				duplicate = None
			if duplicate:
				# per-event commit, see above
				frappe.db.commit()  # nosemgrep
				return duplicate
			frappe.db.rollback()

		error, needs_log = classify_exception(exc)
		if needs_log:
			frappe.log_error(title=f"Crenya POS sync event {env['event_id']} failed")

		if error["code"] != PAYLOAD_CONFLICT:
			try:
				_record_error(ctx, env, error)
			except Exception:
				frappe.db.rollback()
				frappe.log_error(title=f"Crenya POS could not record failed event {env['event_id']}")
		# keep the failed-attempt record (per-event commit, see above)
		frappe.db.commit()  # nosemgrep
		return error_result(env["event_id"], error, env["aggregate_type"])
	finally:
		frappe.clear_messages()


def process_batch(ctx, events):
	# persist the device heartbeat and start every event from a clean transaction
	frappe.db.commit()  # nosemgrep
	return [process_event(ctx, event, index) for index, event in enumerate(events)]
