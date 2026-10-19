"""The Customer's House Account facility (0.14.0, restaurant spec D6/RC14), the pure parts: the
customer-flag device types, `credit_enabled_for`, `stamp_credit_change`, the two optional keys a
validated invoice keeps for the invoice extenders (R4), the field definitions and the audit text
(no site; hooks are faked)."""

import copy
import unittest
from types import SimpleNamespace
from unittest import mock

import frappe

from crenya_pos_api.api.web_admin.credit import facility_audit_text
from crenya_pos_api.setup.install import CREDIT_CUSTOMER_FIELDS, CUSTOM_FIELDS
from crenya_pos_api.sync import credit, registry
from crenya_pos_api.sync.credit import (
	CREDIT_CHANGED_AT_FIELD,
	CREDIT_ENABLED_FIELD,
	credit_enabled_for,
	customer_flag_device_types,
	is_customer_flag_device,
	sends_credit_flag,
	stamp_credit_change,
)
from crenya_pos_api.sync.errors import VALIDATION, SyncError
from crenya_pos_api.sync.validation import validate_invoice_payload
from crenya_pos_api.tests import extension_dummy as dummy
from crenya_pos_api.tests.test_unit_validation import SALE, make_return

FLAG_HOOK = registry.CREDIT_CUSTOMER_FLAG_HOOK
CREDIT_HOOK = registry.CREDIT_DEVICE_TYPES_HOOK

# the keys validate_invoice_payload answered in 0.13.2
KEYS_0_13_2 = {
	"local_id",
	"offline_number",
	"pos_profile",
	"company",
	"customer",
	"customer_local_id",
	"posting_date",
	"posting_time",
	"currency",
	"is_return",
	"return_against",
	"return_against_local_id",
	"taxes_and_charges",
	"buyer_vatin",
	"cashier",
	"remarks",
	"shift_local_id",
	"items",
	"payments",
	"client_totals",
	"loyalty",
	"on_account",
	"credit_offset",
	"extensions",
}
APPROVAL = {"action": "return", "approved_by": "manager@example.com", "approved_at": "2026-10-19T08:00:00Z"}


def hooks(flag_types=(), credit_types=()):
	return dummy.fake_hooks({FLAG_HOOK: list(flag_types), CREDIT_HOOK: list(credit_types)}, site_ready=True)


def sale(**over):
	payload = copy.deepcopy(SALE)
	payload.update(over)
	return payload


class TestCustomerFlagDeviceTypes(unittest.TestCase):
	def test_hook_name(self):
		self.assertEqual(FLAG_HOOK, "crenya_pos_credit_customer_flag_device_types")

	def test_none_without_the_hook(self):
		with hooks():
			self.assertEqual(customer_flag_device_types(), frozenset())
			self.assertFalse(is_customer_flag_device("till"))
			self.assertFalse(is_customer_flag_device(None))
		registry.clear_cache()
		self.assertEqual(customer_flag_device_types(), frozenset())

	def test_hooked_types_and_never_till(self):
		with hooks(["restaurant_pos", "till", "Bad Type"]):
			self.assertEqual(customer_flag_device_types(), frozenset({"restaurant_pos"}))
			self.assertTrue(is_customer_flag_device("restaurant_pos"))
			self.assertFalse(is_customer_flag_device("till"))
			self.assertFalse(is_customer_flag_device(""))
			self.assertFalse(is_customer_flag_device("waiter"))

	def test_the_device_type_helper_is_shared(self):
		"""R16: one hook reader for both lists."""
		calls = []
		real = registry._device_types

		def spy(hook, base):
			calls.append(hook)
			return real(hook, base)

		with hooks(["restaurant_pos"]), mock.patch.object(registry, "_device_types", spy):
			customer_flag_device_types()
		self.assertEqual(calls, [FLAG_HOOK])

	def test_sends_credit_flag_gate(self):
		def ctx(device_type, switch=1):
			return SimpleNamespace(device_type=device_type, profile={credit.CREDIT_SALES_FIELD: switch})

		with hooks(["restaurant_pos"], ["restaurant_pos"]):
			self.assertTrue(sends_credit_flag(ctx("restaurant_pos")))
			# the outlet does not sell on account
			self.assertFalse(sends_credit_flag(ctx("restaurant_pos", 0)))
			# a retail till never; nor a type that is not a flag type
			self.assertFalse(sends_credit_flag(ctx("till")))
			self.assertFalse(sends_credit_flag(ctx("waiter")))
		# a flag type that may not take credit gets nothing either
		with hooks(["restaurant_pos"], []):
			self.assertFalse(sends_credit_flag(ctx("restaurant_pos")))


class TestCreditEnabledFor(unittest.TestCase):
	def test_disabled_customer_reads_not_enabled(self):
		self.assertTrue(credit_enabled_for({CREDIT_ENABLED_FIELD: 1, "disabled": 0}))
		self.assertFalse(credit_enabled_for({CREDIT_ENABLED_FIELD: 1, "disabled": 1}))
		self.assertFalse(credit_enabled_for({CREDIT_ENABLED_FIELD: 0, "disabled": 0}))
		self.assertFalse(credit_enabled_for({CREDIT_ENABLED_FIELD: 0, "disabled": 1}))
		# a site before the patch: no field, not enabled
		self.assertFalse(credit_enabled_for({"disabled": 0}))
		self.assertTrue(credit_enabled_for(frappe._dict({CREDIT_ENABLED_FIELD: "1", "disabled": None})))


class FakeCustomer(frappe._dict):
	"""What stamp_credit_change reads of a Customer Document."""

	def __init__(self, before=None, has_field=True, **values):
		super().__init__(values)
		self["_before"] = before
		self["meta"] = SimpleNamespace(has_field=lambda field: has_field and field == CREDIT_CHANGED_AT_FIELD)

	def get_doc_before_save(self):
		return self["_before"]

	def set(self, key, value):
		self[key] = value


MODIFIED = "2026-10-19 08:00:00.123456"


class TestStampCreditChange(unittest.TestCase):
	def stamped(self, doc):
		stamp_credit_change(doc)
		return doc.get(CREDIT_CHANGED_AT_FIELD)

	def test_flag_change_stamps_the_saves_modified(self):
		before = {CREDIT_ENABLED_FIELD: 0, "disabled": 0}
		doc = FakeCustomer(before, modified=MODIFIED, **{CREDIT_ENABLED_FIELD: 1, "disabled": 0})
		self.assertEqual(self.stamped(doc), MODIFIED)

	def test_disable_stamps(self):
		before = {CREDIT_ENABLED_FIELD: 1, "disabled": 0}
		doc = FakeCustomer(before, modified=MODIFIED, **{CREDIT_ENABLED_FIELD: 1, "disabled": 1})
		self.assertEqual(self.stamped(doc), MODIFIED)

	def test_other_edits_leave_it(self):
		before = {CREDIT_ENABLED_FIELD: 1, "disabled": 0, "customer_name": "A"}
		doc = FakeCustomer(
			before,
			modified=MODIFIED,
			customer_name="B",
			**{CREDIT_ENABLED_FIELD: 1, "disabled": 0, CREDIT_CHANGED_AT_FIELD: "2026-01-01 00:00:00.000000"},
		)
		self.assertEqual(self.stamped(doc), "2026-01-01 00:00:00.000000")

	def test_a_new_customer(self):
		plain = FakeCustomer(None, modified=MODIFIED, **{CREDIT_ENABLED_FIELD: 0, "disabled": 0})
		self.assertIsNone(self.stamped(plain))
		enabled = FakeCustomer(None, modified=MODIFIED, **{CREDIT_ENABLED_FIELD: 1, "disabled": 0})
		self.assertEqual(self.stamped(enabled), MODIFIED)

	def test_a_site_without_the_field(self):
		before = {CREDIT_ENABLED_FIELD: 0, "disabled": 0}
		doc = FakeCustomer(before, has_field=False, modified=MODIFIED, disabled=1)
		self.assertIsNone(self.stamped(doc))


class TestInvoicePayloadKeys(unittest.TestCase):
	def refused(self, payload, fragment):
		with self.assertRaises(SyncError) as caught:
			validate_invoice_payload(payload)
		self.assertEqual(caught.exception.code, VALIDATION)
		self.assertIn(fragment, caught.exception.message)

	def test_invoice_payload_keeps_as_of_and_approval(self):
		# absent: None, and otherwise 0.13.2's answer (R4)
		data = validate_invoice_payload(copy.deepcopy(SALE))
		self.assertEqual(set(data), KEYS_0_13_2 | {"credit_customer_as_of", "approval"})
		self.assertEqual((data["credit_customer_as_of"], data["approval"]), (None, None))
		returned = validate_invoice_payload(make_return())
		self.assertEqual((returned["credit_customer_as_of"], returned["approval"]), (None, None))
		# present: kept
		self.assertEqual(
			validate_invoice_payload(sale(credit_customer_as_of=f" {MODIFIED} "))["credit_customer_as_of"],
			MODIFIED,
		)
		self.assertEqual(
			validate_invoice_payload(sale(credit_customer_as_of="2026-10-19T08:00:00Z"))[
				"credit_customer_as_of"
			],
			"2026-10-19T08:00:00Z",
		)
		self.assertIsNone(validate_invoice_payload(sale(credit_customer_as_of=""))["credit_customer_as_of"])
		payload = make_return()
		payload["approval"] = dict(APPROVAL, extra="dropped")
		self.assertEqual(validate_invoice_payload(payload)["approval"], APPROVAL)

	def test_bad_as_of_refused(self):
		self.refused(sale(credit_customer_as_of="yesterday"), "credit_customer_as_of")
		self.refused(sale(credit_customer_as_of="2026-10-19T08:00:00"), "credit_customer_as_of")
		self.refused(sale(credit_customer_as_of=1760860800), "credit_customer_as_of")
		payload = make_return()
		payload["credit_customer_as_of"] = MODIFIED
		self.refused(payload, "only allowed on a sale")

	def test_bad_approval_refused(self):
		self.refused(sale(approval=APPROVAL), "only allowed on a return")
		for bad, fragment in (
			("return", "approval must be an object"),
			({**APPROVAL, "action": ""}, "approval.action"),
			({k: v for k, v in APPROVAL.items() if k != "approved_by"}, "approval.approved_by"),
			({**APPROVAL, "approved_at": "2026-10-19 08:00"}, "approval.approved_at"),
		):
			payload = make_return()
			payload["approval"] = bad
			self.refused(payload, fragment)


class TestFieldsAndText(unittest.TestCase):
	def test_customer_fields_default_off(self):
		fields = {field["fieldname"]: field for field in CREDIT_CUSTOMER_FIELDS}
		self.assertEqual(set(fields), {CREDIT_ENABLED_FIELD, CREDIT_CHANGED_AT_FIELD})
		flag = fields[CREDIT_ENABLED_FIELD]
		self.assertEqual(
			(flag["fieldtype"], flag["default"], flag["label"]),
			("Check", "0", "House Account (credit facility)"),
		)
		stamp = fields[CREDIT_CHANGED_AT_FIELD]
		self.assertEqual((stamp["fieldtype"], stamp["read_only"]), ("Datetime", 1))
		names = [field["fieldname"] for field in CUSTOM_FIELDS["Customer"]]
		self.assertIn(CREDIT_ENABLED_FIELD, names)
		self.assertIn(CREDIT_CHANGED_AT_FIELD, names)

	def test_audit_text(self):
		self.assertEqual(
			facility_audit_text(True, "app.crenya.com", "a@example.com"),
			"House Account (credit facility) switched on from app.crenya.com by a@example.com",
		)
		self.assertIn("switched off", facility_audit_text(False, "app.crenya.com", "a@example.com"))
