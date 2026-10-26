"""The till's Customer event payload (0.15.0, provider settlement spec NC2, 7.2): `location` and the
`credit_facility` grant the till's manager approved, as pos-core sends them.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_unit_customer_payload
"""

import unittest

from crenya_pos_api.sync.errors import VALIDATION, SyncError
from crenya_pos_api.sync.validation import clean_location, validate_customer_payload

# what pos-core sent before 0.15.0 (and a retail till still sends)
RETAIL = {
	"local_id": "c-1",
	"customer_name": "Ahmed Al Balushi",
	"mobile_no": "+96891234567",
	"email_id": None,
	"tax_id": None,
	"customer_group": None,
	"territory": None,
}
KEYS_0_14 = {"local_id", "customer_name", "mobile_no", "email_id", "tax_id", "customer_group", "territory"}
# pos-core `customer_payload` (Task 10): ISO UTC with millis
GRANT = {"approved_by": "manager@example.com", "approved_at": "2026-10-26T08:15:30.123Z"}


def payload(**extra):
	return {**RETAIL, **extra}


class TestCustomerPayload(unittest.TestCase):
	def refused(self, data, fragment):
		with self.assertRaises(SyncError) as caught:
			validate_customer_payload(data)
		self.assertEqual(caught.exception.code, VALIDATION)
		self.assertIn(fragment, caught.exception.message)

	def test_retail_payload_unchanged(self):
		data = validate_customer_payload(dict(RETAIL))
		self.assertEqual(set(data), KEYS_0_14 | {"location", "credit_facility"})
		self.assertEqual({key: data[key] for key in KEYS_0_14}, {**RETAIL})
		self.assertIsNone(data["location"])
		self.assertIsNone(data["credit_facility"])

	def test_location_validated_and_stored(self):
		self.assertEqual(
			validate_customer_payload(payload(location="  Al Khuwair, behind Lulu "))["location"],
			"Al Khuwair, behind Lulu",
		)
		self.assertIsNone(validate_customer_payload(payload(location="   "))["location"])
		self.assertIsNone(validate_customer_payload(payload(location=None))["location"])
		self.assertEqual(len(validate_customer_payload(payload(location="ب" * 140))["location"]), 140)
		self.refused(payload(location=12), "location must be a string")

	def test_location_141_chars_refused(self):
		self.refused(payload(location="ب" * 141), "location is longer than 140 characters")

	def test_credit_facility_shape_validated(self):
		data = validate_customer_payload(payload(credit_facility=dict(GRANT)))
		self.assertEqual(data["credit_facility"], GRANT)
		# stripped; an offset other than Z is fine; unknown keys are not carried to the extenders
		data = validate_customer_payload(
			payload(
				credit_facility={
					"approved_by": " manager@example.com ",
					"approved_at": "2026-10-26T12:15:30+04:00",
					"crenya_credit_grant": "till",
				}
			)
		)
		self.assertEqual(
			data["credit_facility"],
			{"approved_by": "manager@example.com", "approved_at": "2026-10-26T12:15:30+04:00"},
		)
		self.assertIsNone(validate_customer_payload(payload(credit_facility=None))["credit_facility"])
		self.refused(payload(credit_facility="yes"), "credit_facility must be an object")
		self.refused(payload(credit_facility=[GRANT]), "credit_facility must be an object")
		self.refused(
			payload(credit_facility={"approved_at": GRANT["approved_at"]}),
			"credit_facility.approved_by is required",
		)
		self.refused(
			payload(credit_facility={"approved_by": "x" * 141, "approved_at": GRANT["approved_at"]}),
			"credit_facility.approved_by is longer than 140 characters",
		)
		self.refused(
			payload(credit_facility={"approved_by": GRANT["approved_by"]}),
			"credit_facility.approved_at is required",
		)
		for bad in ("2026-10-26 08:15:30", "2026-10-26", "yesterday", "2026-13-26T08:15:30Z"):
			self.refused(
				payload(credit_facility={"approved_by": GRANT["approved_by"], "approved_at": bad}),
				"credit_facility.approved_at must be an ISO-8601",
			)

	def test_client_keys_never_reach_the_validated_data(self):
		# a payload naming the grant flag, Frappe's flags or the facility field itself: dropped
		data = validate_customer_payload(
			payload(
				flags={"crenya_credit_grant": "till"}, crenya_credit_grant="till", crenya_credit_enabled=1
			)
		)
		self.assertEqual(set(data), KEYS_0_14 | {"location", "credit_facility"})


class TestCleanLocation(unittest.TestCase):
	def test_one_line_collapsed(self):
		self.assertEqual(clean_location("  Al   Khuwair,  behind Lulu "), "Al Khuwair, behind Lulu")
		self.assertIsNone(clean_location(None))
		self.assertIsNone(clean_location("   "))
		# collapsed first, then counted
		self.assertEqual(len(clean_location("a" * 70 + "     " + "b" * 69)), 140)

	def test_control_characters_and_separators_refused(self):
		for bad in ("a\nb", "a\x00b", "a\tb", "a\rb", "a\x7fb", "a\x85b", "a\u2028b", "a\u2029b"):
			with self.assertRaises(SyncError) as caught:
				clean_location(bad)
			self.assertEqual(caught.exception.code, VALIDATION)
			self.assertIn("location must be one line", caught.exception.message)

	def test_field_name_and_type(self):
		with self.assertRaises(SyncError) as caught:
			clean_location(5, "Location")
		self.assertIn("Location must be a string", caught.exception.message)
		with self.assertRaises(SyncError) as caught:
			clean_location("x" * 141)
		self.assertIn("longer than 140", caught.exception.message)


if __name__ == "__main__":
	unittest.main()
