"""Error codes of the sync protocol and mapping of exceptions onto them."""

import re

import frappe

VALIDATION = "validation"
PERMISSION = "permission"
DEVICE_NOT_REGISTERED = "device_not_registered"
DEPENDENCY_MISSING = "dependency_missing"
TOTAL_MISMATCH = "total_mismatch"
PAYLOAD_CONFLICT = "payload_conflict"
PROTOCOL_UNSUPPORTED = "protocol_unsupported"
UNSUPPORTED_TAX = "unsupported_tax"
INTERNAL = "internal"

RETRYABLE_CODES = frozenset({DEPENDENCY_MISSING, INTERNAL})

MAX_MESSAGE_LENGTH = 1000
_TAG_RE = re.compile(r"<[^>]+>")


class SyncError(Exception):
	"""A per-event business failure with a protocol error code."""

	def __init__(self, code, message, retryable=None):
		super().__init__(message)
		self.code = code
		self.message = message
		self.retryable = code in RETRYABLE_CODES if retryable is None else bool(retryable)

	def as_dict(self):
		return error_dict(self.code, self.message, self.retryable)


class CrenyaAPIError(frappe.ValidationError):
	"""Request-level failure. `code` is echoed in the JSON error body as `error.code`."""

	code = VALIDATION
	retryable = False


class ProtocolUnsupportedError(CrenyaAPIError):
	code = PROTOCOL_UNSUPPORTED
	http_status_code = 400


class DeviceNotRegisteredError(CrenyaAPIError):
	code = DEVICE_NOT_REGISTERED
	http_status_code = 403


class UnsupportedTaxError(CrenyaAPIError):
	code = UNSUPPORTED_TAX


class InvalidRequestError(CrenyaAPIError):
	code = VALIDATION


class DevicePermissionError(frappe.PermissionError):
	code = PERMISSION
	retryable = False


def clean_message(message):
	"""Plain-text, bounded message suitable for display on the till."""
	text = _TAG_RE.sub("", str(message or "")).strip()
	text = re.sub(r"\s+", " ", text)
	if len(text) > MAX_MESSAGE_LENGTH:
		text = text[: MAX_MESSAGE_LENGTH - 3] + "..."
	return text


def error_dict(code, message, retryable=None):
	if retryable is None:
		retryable = code in RETRYABLE_CODES
	return {"code": code, "message": clean_message(message), "retryable": bool(retryable)}


def _is_transient_db_error(exc):
	transient = tuple(
		cls
		for cls in (
			getattr(frappe, "QueryDeadlockError", None),
			getattr(frappe, "QueryTimeoutError", None),
		)
		if cls
	)
	return bool(transient) and isinstance(exc, transient)


def classify_exception(exc):
	"""Map an exception raised while processing an event to (error dict, needs_logging).

	- SyncError / CrenyaAPIError carry their own code.
	- frappe.PermissionError -> permission
	- frappe.ValidationError (and subclasses: stock, over-return, links, mandatory...) -> validation
	- frappe.DuplicateEntryError -> validation
	- deadlocks / lock timeouts -> internal (retryable)
	- anything else -> internal (retryable) and must be logged
	"""
	if isinstance(exc, SyncError):
		return exc.as_dict(), False

	if isinstance(exc, CrenyaAPIError | DevicePermissionError):
		return error_dict(exc.code, str(exc), exc.retryable), False

	if isinstance(exc, frappe.PermissionError):
		return error_dict(PERMISSION, str(exc) or "Not permitted", False), False

	if _is_transient_db_error(exc):
		return error_dict(INTERNAL, "Temporary database contention, retry later", True), True

	if isinstance(exc, frappe.ValidationError | frappe.DuplicateEntryError):
		return error_dict(VALIDATION, str(exc) or type(exc).__name__, False), False

	return error_dict(INTERNAL, f"Unexpected server error ({type(exc).__name__})", True), True


def raise_api_error(exc_class, message):
	"""Throw a request-level error and expose `error: {code, message, retryable}` in the response body."""
	try:
		frappe.local.response["error"] = error_dict(exc_class.code, message, exc_class.retryable)
	except (AttributeError, RuntimeError):
		# no request context (unit tests / console)
		pass
	frappe.throw(clean_message(message), exc_class)
