"""Credit sales (0.13.0), the pure parts: the optional `on_account` and `credit_offset` of an
invoice, the shift's `on_account` summary and `received`, the approval actions and the profile
fields (no site)."""

import copy
import unittest
from decimal import Decimal

import frappe

from crenya_pos_api.setup.install import CREDIT_PROFILE_FIELDS, CUSTOM_FIELDS
from crenya_pos_api.sync.approvals import (
	APPROVAL_ACTIONS,
	APPROVAL_DEFAULT_OFF,
	approvals_bootstrap,
)
from crenya_pos_api.sync.errors import VALIDATION, SyncError
from crenya_pos_api.sync.validation import validate_invoice_payload, validate_shift_payload
from crenya_pos_api.tests.test_unit_shift import SHIFT, mutate
from crenya_pos_api.tests.test_unit_validation import SALE, make_return


def sale(**over):
	payload = copy.deepcopy(SALE)
	payload.update(over)
	return payload


class Refusing(unittest.TestCase):
	def refused(self, payload, fragment):
		with self.assertRaises(SyncError) as caught:
			validate_invoice_payload(payload)
		self.assertEqual(caught.exception.code, VALIDATION)
		self.assertIn(fragment, caught.exception.message)


class TestOnAccountPayload(Refusing):
	def test_old_till_invoice_without_on_account(self):
		data = validate_invoice_payload(copy.deepcopy(SALE))
		self.assertEqual((data["on_account"], data["credit_offset"]), (Decimal(0), Decimal(0)))

	def test_on_account_is_a_decimal(self):
		data = validate_invoice_payload(sale(on_account="60.000"))
		self.assertEqual(data["on_account"], Decimal("60.000"))
		self.assertEqual(validate_invoice_payload(sale(on_account=None))["on_account"], Decimal(0))
		self.assertEqual(validate_invoice_payload(sale(on_account="0"))["on_account"], Decimal(0))

	def test_bad_on_account_is_refused(self):
		self.refused(sale(on_account="-1.000"), "on_account")
		self.refused(sale(on_account=60.0), "on_account")
		self.refused(sale(on_account="abc"), "on_account")

	def test_a_return_carries_no_on_account(self):
		payload = make_return()
		payload["on_account"] = "1.000"
		self.refused(payload, "on_account")
		payload["on_account"] = "0"
		self.assertEqual(validate_invoice_payload(payload)["on_account"], Decimal(0))

	def test_credit_offset_only_on_a_return_of_an_invoice(self):
		payload = make_return()
		payload["credit_offset"] = "0.600"
		self.assertEqual(validate_invoice_payload(payload)["credit_offset"], Decimal("0.600"))
		self.refused(sale(credit_offset="1.000"), "credit_offset")
		payload["credit_offset"] = "-0.100"
		self.refused(payload, "credit_offset")
		open_return = make_return()
		open_return["return_against_local_id"] = None
		open_return["remarks"] = "Damaged"
		open_return["items"][0]["against_line_no"] = None
		open_return["credit_offset"] = "0.600"
		self.refused(open_return, "credit_offset")


class TestShiftOnAccount(unittest.TestCase):
	def test_old_till_shift_without_on_account(self):
		data = validate_shift_payload(copy.deepcopy(SHIFT))
		self.assertEqual(
			data["on_account"],
			{
				"sales_count": 0,
				"received_count": 0,
				"sales_amount": Decimal(0),
				"returned_amount": Decimal(0),
				"received_amount": Decimal(0),
			},
		)
		for row in data["payments"]:
			self.assertEqual(row["received"], Decimal(0))

	def test_on_account_and_received_parsed(self):
		def add(p):
			p["on_account"] = {
				"sales_count": 2,
				"sales_amount": "90.000",
				"returned_amount": "0.000",
				"received_count": 1,
				"received_amount": "40.000",
			}
			p["payments"][0]["received"] = "40.000"

		data = validate_shift_payload(mutate(add))
		self.assertEqual(data["on_account"]["sales_count"], 2)
		self.assertEqual(data["on_account"]["sales_amount"], Decimal("90.000"))
		self.assertEqual(data["on_account"]["received_amount"], Decimal("40.000"))
		self.assertEqual(data["payments"][0]["received"], Decimal("40.000"))
		self.assertEqual(data["payments"][1]["received"], Decimal(0))

	def test_bad_values_are_refused(self):
		for bad in (
			[],
			{"sales_count": -1},
			{"sales_count": "many"},
			{"sales_amount": "-1.000"},
			{"received_amount": 1.5},
		):
			with self.assertRaises(SyncError, msg=bad):
				validate_shift_payload(mutate(lambda p, bad=bad: p.update(on_account=bad)))
		with self.assertRaises(SyncError):
			validate_shift_payload(mutate(lambda p: p["payments"][0].update(received="-1.000")))
		with self.assertRaises(SyncError):
			validate_shift_payload(mutate(lambda p: p["payments"][0].update(received=2.5)))


class TestApprovalsAndFields(unittest.TestCase):
	def test_the_two_credit_actions(self):
		actions = {key: (field, label, label_ar) for key, field, label, label_ar in APPROVAL_ACTIONS}
		self.assertEqual(
			actions["credit_over_limit"],
			("crenya_approve_credit_over_limit", "Sell on account over the limit", "بيع آجل فوق الحد"),
		)
		self.assertEqual(
			actions["credit_sale"], ("crenya_approve_credit_sale", "Sell on account", "بيع بالآجل")
		)
		self.assertEqual(APPROVAL_DEFAULT_OFF, frozenset({"credit_sale"}))

	def test_defaults_come_from_the_table(self):
		fields = {field["fieldname"]: field for field in CUSTOM_FIELDS["POS Profile"]}
		for key, fieldname, *_ in APPROVAL_ACTIONS:
			self.assertEqual(fields[fieldname]["default"], "0" if key in APPROVAL_DEFAULT_OFF else "1", key)

	def test_a_new_profile_requires_the_over_limit_approval_only(self):
		every_default = {
			field: 0 if key in APPROVAL_DEFAULT_OFF else 1 for key, field, *_ in APPROVAL_ACTIONS
		}
		required = approvals_bootstrap(frappe._dict(every_default))["required"]
		self.assertIn("credit_over_limit", required)
		self.assertNotIn("credit_sale", required)

	def test_the_credit_section_is_one_switch_off_by_default(self):
		fields = {field["fieldname"]: field for field in CREDIT_PROFILE_FIELDS}
		self.assertEqual(fields["crenya_credit_section"]["label"], "Credit sales")
		self.assertEqual(fields["crenya_credit_sales_enabled"]["fieldtype"], "Check")
		self.assertEqual(fields["crenya_credit_sales_enabled"]["default"], "0")

	def test_a_payment_entry_keeps_the_receipts_local_id(self):
		field = next(f for f in CUSTOM_FIELDS["Payment Entry"] if f["fieldname"] == "crenya_local_id")
		self.assertEqual((field["unique"], field["read_only"]), (1, 1))


if __name__ == "__main__":
	unittest.main()


class TestCreditBeyondOutstanding(unittest.TestCase):
	def note(self, offset, outstanding):
		from crenya_pos_api.sync.invoice_builder import credit_beyond_outstanding_note

		original = frappe._dict(name="ACC-SINV-1", customer="CUST-1", outstanding_amount=outstanding)
		return credit_beyond_outstanding_note(Decimal(offset), original, 3)

	def test_a_credit_the_invoice_still_owes_has_no_note(self):
		self.assertIsNone(self.note("30.000", "60.000"))
		self.assertIsNone(self.note("60.000", "60.000"))

	def test_a_credit_beyond_the_outstanding_names_what_is_left_in_credit(self):
		note = self.note("30.000", "10.000")
		self.assertIn("more than invoice ACC-SINV-1's outstanding 10.000", note)
		self.assertIn("customer CUST-1 is left in credit by 20.000", note)

	def test_a_paid_invoice_owes_nothing(self):
		self.assertIn("left in credit by 30.000", self.note("30.000", "0"))
		# a negative outstanding (already in credit) counts as nothing owed
		self.assertIn("left in credit by 5.000", self.note("5.000", "-2.000"))

	def test_exact_at_three_decimals(self):
		self.assertIsNone(self.note("0.001", "0.001"))
		self.assertIn("left in credit by 0.001", self.note("0.002", "0.001"))
