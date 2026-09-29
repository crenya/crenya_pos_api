import unittest

import frappe

from crenya_pos_api.sync import errors
from crenya_pos_api.sync.errors import SyncError, classify_exception, clean_message, error_dict


class StockOverReturnLike(frappe.ValidationError):
	"""Stand-in for ERPNext's own ValidationError subclasses."""


class TestClassification(unittest.TestCase):
	def assertCode(self, exc, code, retryable, logged):
		error, needs_log = classify_exception(exc)
		self.assertEqual(error["code"], code)
		self.assertEqual(error["retryable"], retryable)
		self.assertEqual(needs_log, logged)
		return error

	def test_sync_errors_keep_their_code(self):
		self.assertCode(SyncError(errors.DEPENDENCY_MISSING, "customer"), "dependency_missing", True, False)
		self.assertCode(SyncError(errors.TOTAL_MISMATCH, "1 vs 2"), "total_mismatch", False, False)
		self.assertCode(SyncError(errors.PAYLOAD_CONFLICT, "x"), "payload_conflict", False, False)

	def test_validation_errors(self):
		error = self.assertCode(frappe.ValidationError("Stock is <b>short</b>"), "validation", False, False)
		self.assertEqual(error["message"], "Stock is short")
		self.assertCode(StockOverReturnLike("over return"), "validation", False, False)
		self.assertCode(frappe.DoesNotExistError("Item X not found"), "validation", False, False)
		self.assertCode(frappe.LinkValidationError("bad link"), "validation", False, False)
		self.assertCode(frappe.MandatoryError("missing"), "validation", False, False)
		self.assertCode(frappe.UniqueValidationError("dup"), "validation", False, False)
		self.assertCode(frappe.DuplicateEntryError("dup name"), "validation", False, False)

	def test_permission_errors(self):
		self.assertCode(frappe.PermissionError("nope"), "permission", False, False)
		self.assertCode(errors.DevicePermissionError("other user"), "permission", False, False)

	def test_request_level_errors(self):
		self.assertCode(errors.DeviceNotRegisteredError("gone"), "device_not_registered", False, False)
		self.assertCode(errors.ProtocolUnsupportedError("v2"), "protocol_unsupported", False, False)
		self.assertCode(errors.UnsupportedTaxError("actual"), "unsupported_tax", False, False)

	def test_unexpected_errors_are_internal_and_logged(self):
		error = self.assertCode(KeyError("boom"), "internal", True, True)
		self.assertNotIn("boom", error["message"])
		self.assertCode(ZeroDivisionError(), "internal", True, True)

	def test_deadlocks_are_retryable_internal(self):
		self.assertCode(frappe.QueryDeadlockError("deadlock"), "internal", True, True)
		self.assertCode(frappe.QueryTimeoutError("lock wait"), "internal", True, True)


class TestErrorShape(unittest.TestCase):
	def test_error_dict_defaults_retryable_from_code(self):
		self.assertEqual(
			error_dict("dependency_missing", "later"),
			{"code": "dependency_missing", "message": "later", "retryable": True},
		)
		self.assertFalse(error_dict("validation", "no")["retryable"])

	def test_sync_error_as_dict(self):
		self.assertEqual(
			SyncError("internal", "x").as_dict(), {"code": "internal", "message": "x", "retryable": True}
		)
		self.assertFalse(SyncError("internal", "x", retryable=False).as_dict()["retryable"])

	def test_clean_message(self):
		self.assertEqual(clean_message("<p>Row  1:\n <b>bad</b></p>"), "Row 1: bad")
		self.assertEqual(len(clean_message("x" * 5000)), errors.MAX_MESSAGE_LENGTH)
		self.assertEqual(clean_message(None), "")

	def test_retryable_codes_match_protocol_table(self):
		self.assertEqual(errors.RETRYABLE_CODES, {"dependency_missing", "internal"})


if __name__ == "__main__":
	unittest.main()
