"""The Customer Credit Facility event (0.16.0), the pure parts (no site): the approval subject, the
payload validation and the grant flag of an existing customer."""

import unittest
from decimal import Decimal

import frappe

from crenya_pos_api.sync import credit
from crenya_pos_api.sync.credit import CREDIT_ENABLED_FIELD, GRANT_FLAG, TILL_ENABLE_GRANT
from crenya_pos_api.sync.credit_facility import (
	AGGREGATE_CREDIT_FACILITY,
	facility_subject,
	validate_credit_facility_payload,
)
from crenya_pos_api.sync.errors import VALIDATION, SyncError
from crenya_pos_api.tests.test_unit_credit_facility import MODIFIED, FakeCustomer

APPROVAL = {
	"action": "credit_facility",
	"subject": "credit_terms:CUST-1|50|30",
	"approved_by": "manager@example.com",
	"approved_at": "2026-10-28T08:15:30Z",
}


def payload(**overrides):
	data = {
		"local_id": "L1",
		"customer": "CUST-1",
		"credit_limit": "50.000",
		"credit_days": 30,
		"approval": dict(APPROVAL),
	}
	data.update(overrides)
	return {key: value for key, value in data.items() if value is not ...}


class TestFacilitySubject(unittest.TestCase):
	def test_facility_subject(self):
		self.assertEqual(AGGREGATE_CREDIT_FACILITY, "Customer Credit Facility")
		self.assertEqual(facility_subject("CUST-1", Decimal("50.000"), 30), "credit_terms:CUST-1|50|30")
		self.assertEqual(facility_subject("CUST-1", None, None), "credit_terms:CUST-1|-|-")
		self.assertEqual(facility_subject("CUST-1", Decimal("0.500"), 30), "credit_terms:CUST-1|0.5|30")

	def test_shared_fixture_table_with_pos_core(self):
		# the same table pos-core's fmt_plain is tested with (Task 15): only None is "-"
		limits = [
			(Decimal("50.000"), "50"),
			(Decimal("100"), "100"),
			(Decimal("0.500"), "0.5"),
			(Decimal("1234.560"), "1234.56"),
			(Decimal("0"), "0"),
			(Decimal("0.000"), "0"),
			(None, "-"),
		]
		for limit, shown in limits:
			self.assertEqual(facility_subject("C", limit, None), f"credit_terms:C|{shown}|-", msg=limit)
		for days, shown in ((0, "0"), (30, "30"), (365, "365"), (None, "-")):
			self.assertEqual(facility_subject("C", None, days), f"credit_terms:C|-|{shown}", msg=days)
		# Decimal("50").normalize() is 5E+1: the fixed-point format is required
		self.assertEqual(facility_subject("C", Decimal("50"), 0), "credit_terms:C|50|0")
		self.assertEqual(facility_subject("C", Decimal("100.00"), 1), "credit_terms:C|100|1")

	def test_omr_amounts(self):
		self.assertEqual(facility_subject("OMR-1", Decimal("250.500"), 14), "credit_terms:OMR-1|250.5|14")
		self.assertEqual(facility_subject("OMR-1", Decimal("0.001"), 7), "credit_terms:OMR-1|0.001|7")


class TestPayloadValidation(unittest.TestCase):
	def refused(self, fragment, **overrides):
		with self.assertRaises(SyncError) as caught:
			validate_credit_facility_payload(payload(**overrides))
		self.assertEqual(caught.exception.code, VALIDATION)
		self.assertIn(fragment, caught.exception.message)

	def test_payload_validation(self):
		self.refused("customer", customer=...)
		self.refused("customer", customer="")
		self.refused("approval.action", approval={**APPROVAL, "action": "credit_sale"})
		self.refused("approval.approved_at", approval={**APPROVAL, "approved_at": "yesterday"})
		self.refused("approval.approved_at", approval={**APPROVAL, "approved_at": "2026-10-28T08:15:30"})
		self.refused("approval.approved_by", approval={**APPROVAL, "approved_by": ""})
		self.refused("approval.subject", approval={k: v for k, v in APPROVAL.items() if k != "subject"})
		self.refused("approval", approval=...)
		self.refused("approval", approval="manager")
		self.refused("credit_limit", credit_limit="-5")
		self.refused("credit_limit", credit_limit="12.5555")
		self.refused("credit_days", credit_days=366)
		self.refused("credit_days", credit_days="3.5")

	def test_valid_payload_normalised(self):
		data = validate_credit_facility_payload(payload())
		self.assertEqual((data["customer"], data["credit_limit"], data["credit_days"]), ("CUST-1", "50.000", 30))
		self.assertEqual(data["subject_limit"], Decimal("50.000"))
		self.assertEqual(data["approval"]["approved_by"], "manager@example.com")

	def test_absent_keys_stay_absent_and_null_is_kept(self):
		data = validate_credit_facility_payload(payload(credit_limit=..., credit_days=...))
		self.assertNotIn("credit_limit", data)
		self.assertNotIn("credit_days", data)
		data = validate_credit_facility_payload(payload(credit_limit=None, credit_days=None))
		self.assertIsNone(data["credit_limit"])
		self.assertIsNone(data["subject_limit"])
		self.assertIsNone(data["credit_days"])

	def test_zero_limit_clears_but_the_subject_keeps_zero(self):
		data = validate_credit_facility_payload(payload(credit_limit="0.000"))
		self.assertIsNone(data["credit_limit"])
		self.assertEqual(facility_subject("CUST-1", data["subject_limit"], 30), "credit_terms:CUST-1|0|30")


class TestGrantOnUpdate(unittest.TestCase):
	def granted(self, before="saved", **flags):
		doc = FakeCustomer(
			None if before is None else {CREDIT_ENABLED_FIELD: 0}, modified=MODIFIED, **{CREDIT_ENABLED_FIELD: 1}
		)
		doc["flags"] = frappe._dict(flags)
		return doc

	def test_only_on_the_save_of_an_existing_customer(self):
		self.assertTrue(credit._granted_on_update(self.granted(**{GRANT_FLAG: TILL_ENABLE_GRANT})))
		# inside insert(): no
		self.assertFalse(credit._granted_on_update(self.granted(in_insert=True, **{GRANT_FLAG: TILL_ENABLE_GRANT})))
		# a customer never saved: no
		self.assertFalse(credit._granted_on_update(self.granted(before=None, **{GRANT_FLAG: TILL_ENABLE_GRANT})))
		# other sources and non-strings: no
		for value in ("till", "web", "desk", "", None, 1, True, ["till_enable"]):
			self.assertFalse(credit._granted_on_update(self.granted(**{GRANT_FLAG: value})), msg=value)
		self.assertFalse(credit._granted_on_update(None))

	def test_the_update_source_is_no_grant_on_create(self):
		doc = self.granted(before=None, in_insert=True, **{GRANT_FLAG: TILL_ENABLE_GRANT})
		self.assertFalse(credit._granted_on_create(doc))
		# and the create sources are none on an update
		self.assertFalse(credit._granted_on_update(self.granted(**{GRANT_FLAG: "till"})))
		self.assertEqual(credit.GRANT_SOURCES, {"till", "web", "till_enable"})
