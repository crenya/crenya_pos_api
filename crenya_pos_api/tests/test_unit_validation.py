import copy
import unittest
from decimal import Decimal

from crenya_pos_api.sync.errors import SyncError
from crenya_pos_api.sync.hashing import payload_hash
from crenya_pos_api.sync.validation import (
	is_open_return,
	validate_customer_payload,
	validate_envelope,
	validate_invoice_payload,
)

SALE = {
	"local_id": "7b0e9c3e-1111-4a5b-9c1d-000000000001",
	"offline_number": "OFF-D02-000123",
	"pos_profile": "Muscat Main",
	"company": "Crenya LLC",
	"customer": "Walk-in Customer",
	"customer_local_id": None,
	"posting_date": "2026-09-29",
	"posting_time": "14:03:11",
	"currency": "OMR",
	"is_return": 0,
	"return_against": None,
	"return_against_local_id": None,
	"taxes_and_charges": "Oman VAT 5% - CR",
	"buyer_vatin": None,
	"cashier": "cashier1@store.om",
	"remarks": None,
	"items": [
		{
			"line_no": 1,
			"item_code": "MILK-1L",
			"item_name": "Milk 1L",
			"qty": "2",
			"uom": "Nos",
			"conversion_factor": "1",
			"price_list_rate": "0.600",
			"discount_percentage": "0",
			"rate": "0.600",
			"amount": "1.200",
			"item_tax_template": None,
			"against_line_no": None,
			"against_row_name": None,
		}
	],
	"payments": [{"mode_of_payment": "Cash", "amount": "1.200"}],
	"client_totals": {
		"net_total": "1.143",
		"total_taxes": "0.057",
		"grand_total": "1.200",
		"rounding_adjustment": "0.000",
		"rounded_total": "1.200",
		"paid_amount": "1.200",
	},
}


def make_return():
	payload = copy.deepcopy(SALE)
	payload["is_return"] = 1
	payload["return_against_local_id"] = "orig-local-id"
	payload["items"][0].update({"qty": "-1", "amount": "-0.600", "against_line_no": 1})
	payload["payments"] = [{"mode_of_payment": "Cash", "amount": "-0.600"}]
	payload["client_totals"]["grand_total"] = "-0.600"
	return payload


def make_open_return():
	"""A return without an invoice: no original invoice, no original rows, a reason."""
	payload = make_return()
	payload["return_against_local_id"] = None
	payload["items"][0]["against_line_no"] = None
	payload["remarks"] = "Damaged pack"
	return payload


def make_event(body, **overrides):
	payload = body
	event = {
		"event_id": "9f1c2d3e-aaaa-bbbb-cccc-000000000001",
		"aggregate_type": "Sales Invoice",
		"operation": "submit",
		"local_id": payload["local_id"],
		"sequence_no": 1,
		"payload_hash": payload_hash(payload),
		"payload": payload,
	}
	event.update(overrides)
	return event


class TestEnvelope(unittest.TestCase):
	def test_valid_envelope(self):
		env = validate_envelope(make_event(SALE))
		self.assertEqual(env["aggregate_type"], "Sales Invoice")
		self.assertEqual(env["payload_hash"], payload_hash(SALE))

	def test_upper_case_hash_is_normalized(self):
		env = validate_envelope(make_event(SALE, payload_hash=payload_hash(SALE).upper()))
		self.assertEqual(env["payload_hash"], payload_hash(SALE))

	def test_rejections(self):
		cases = [
			{"event_id": ""},
			{"event_id": "bad id with spaces"},
			{"event_id": None},
			{"aggregate_type": "Payment Entry"},
			{"operation": "cancel"},
			{"local_id": ""},
			{"sequence_no": "x"},
			{"sequence_no": -1},
			{"payload_hash": "abc"},
			{"payload": "not-an-object"},
			{"local_id": "other-local-id"},
		]
		for override in cases:
			with self.assertRaises(SyncError, msg=repr(override)) as ctx:
				validate_envelope(make_event(SALE, **override))
			self.assertEqual(ctx.exception.code, "validation")

	def test_non_dict_event(self):
		with self.assertRaises(SyncError):
			validate_envelope(["event"])


class TestInvoicePayload(unittest.TestCase):
	def test_valid_sale_is_normalized(self):
		data = validate_invoice_payload(copy.deepcopy(SALE))
		self.assertEqual(data["is_return"], 0)
		line = data["items"][0]
		self.assertEqual(line["qty"], Decimal("2"))
		self.assertEqual(line["rate"], Decimal("0.600"))
		self.assertEqual(line["conversion_factor"], Decimal("1"))
		self.assertEqual(
			data["payments"], [{"mode_of_payment": "Cash", "amount": Decimal("1.200"), "reference_no": None}]
		)
		self.assertEqual(data["client_totals"]["grand_total"], Decimal("1.200"))

	def test_valid_return(self):
		data = validate_invoice_payload(make_return())
		self.assertEqual(data["is_return"], 1)
		self.assertEqual(data["items"][0]["qty"], Decimal("-1"))
		self.assertEqual(data["items"][0]["against_line_no"], 1)

	def test_payments_are_merged_per_mode(self):
		payload = copy.deepcopy(SALE)
		payload["payments"] = [
			{"mode_of_payment": "Cash", "amount": "1.000"},
			{"mode_of_payment": "Card", "amount": "0.100"},
			{"mode_of_payment": "Cash", "amount": "0.100"},
		]
		data = validate_invoice_payload(payload)
		self.assertEqual(
			data["payments"],
			[
				{"mode_of_payment": "Cash", "amount": Decimal("1.100"), "reference_no": None},
				{"mode_of_payment": "Card", "amount": Decimal("0.100"), "reference_no": None},
			],
		)

	def test_conversion_factor_defaults_to_one(self):
		payload = copy.deepcopy(SALE)
		del payload["items"][0]["conversion_factor"]
		self.assertEqual(validate_invoice_payload(payload)["items"][0]["conversion_factor"], Decimal("1"))

	def assertInvalid(self, payload, fragment):
		with self.assertRaises(SyncError) as ctx:
			validate_invoice_payload(payload)
		self.assertEqual(ctx.exception.code, "validation")
		self.assertIn(fragment, ctx.exception.message)

	def test_sale_rules(self):
		def mutate(fn):
			payload = copy.deepcopy(SALE)
			fn(payload)
			return payload

		self.assertInvalid(mutate(lambda p: p.update(items=[])), "items")
		self.assertInvalid(mutate(lambda p: p["items"][0].update(qty="-1")), "items[0].qty")
		self.assertInvalid(mutate(lambda p: p["items"][0].update(qty="0")), "items[0].qty")
		self.assertInvalid(mutate(lambda p: p["items"][0].update(qty=2.0)), "items[0].qty")
		self.assertInvalid(mutate(lambda p: p["items"][0].update(rate="-0.1")), "items[0].rate")
		self.assertInvalid(mutate(lambda p: p["items"][0].update(rate="abc")), "items[0].rate")
		self.assertInvalid(mutate(lambda p: p["items"][0].update(item_code="")), "items[0].item_code")
		self.assertInvalid(mutate(lambda p: p["items"][0].update(conversion_factor="0")), "conversion_factor")
		self.assertInvalid(mutate(lambda p: p["items"][0].update(discount_percentage="101")), "discount")
		self.assertInvalid(mutate(lambda p: p["items"][0].update(against_line_no=1)), "not a return")
		self.assertInvalid(mutate(lambda p: p["items"].append(dict(p["items"][0]))), "line_no")
		self.assertInvalid(mutate(lambda p: p.update(posting_date="29-09-2026")), "posting_date")
		self.assertInvalid(mutate(lambda p: p.update(posting_date="2026-02-30")), "posting_date")
		self.assertInvalid(mutate(lambda p: p.update(posting_time="25:00:00")), "posting_time")
		self.assertInvalid(mutate(lambda p: p.update(pos_profile=None)), "pos_profile")
		self.assertInvalid(mutate(lambda p: p.update(company="")), "company")
		self.assertInvalid(mutate(lambda p: p.update(is_return=2)), "is_return")
		self.assertInvalid(mutate(lambda p: p.update(return_against="SINV-1")), "return_against")
		self.assertInvalid(mutate(lambda p: p.update(client_totals=None)), "client_totals")
		self.assertInvalid(mutate(lambda p: p["client_totals"].pop("grand_total")), "grand_total")
		self.assertInvalid(
			mutate(lambda p: p.update(payments=[{"mode_of_payment": "Cash", "amount": "-1"}])),
			"payments[0].amount",
		)
		self.assertInvalid(mutate(lambda p: p.update(payments=[{"amount": "1"}])), "mode_of_payment")
		self.assertInvalid(mutate(lambda p: p.update(payments="Cash")), "payments")

	def test_return_rules(self):
		payload = make_return()
		payload["items"][0]["qty"] = "1"
		self.assertInvalid(payload, "negative on a return")

		payload = make_return()
		payload["payments"][0]["amount"] = "0.600"
		self.assertInvalid(payload, "negative on a return")

	def test_return_without_reference_is_allowed(self):
		payload = make_open_return()
		data = validate_invoice_payload(payload)
		self.assertIsNone(data["return_against"])
		self.assertIsNone(data["return_against_local_id"])
		self.assertTrue(is_open_return(data))
		self.assertEqual(data["remarks"], "Damaged pack")


class TestOpenReturnPayload(unittest.TestCase):
	def assertInvalid(self, payload, fragment):
		with self.assertRaises(SyncError) as ctx:
			validate_invoice_payload(payload)
		self.assertEqual(ctx.exception.code, "validation")
		self.assertIn(fragment, ctx.exception.message)

	def test_valid_open_return_is_normalized(self):
		payload = make_open_return()
		payload["items"][0]["batch_no"] = "PARA-B1"
		data = validate_invoice_payload(payload)
		line = data["items"][0]
		self.assertEqual(data["is_return"], 1)
		self.assertEqual(line["qty"], Decimal("-1"))
		self.assertEqual(line["rate"], Decimal("0.600"))
		self.assertEqual(line["uom"], "Nos")
		self.assertEqual(line["conversion_factor"], Decimal("1"))
		self.assertEqual(line["batch_no"], "PARA-B1")
		self.assertIsNone(line["against_line_no"])
		self.assertEqual(
			data["payments"], [{"mode_of_payment": "Cash", "amount": Decimal("-0.600"), "reference_no": None}]
		)

	def test_open_return_flags(self):
		self.assertTrue(is_open_return(validate_invoice_payload(make_open_return())))
		self.assertFalse(is_open_return(validate_invoice_payload(make_return())))
		self.assertFalse(is_open_return(validate_invoice_payload(copy.deepcopy(SALE))))
		by_name = make_return()
		by_name.update(return_against="SINV-0001", return_against_local_id=None)
		self.assertFalse(is_open_return(validate_invoice_payload(by_name)))

	def test_reason_is_required(self):
		for remarks in (None, "", "   "):
			with self.subTest(remarks=remarks):
				payload = make_open_return()
				payload["remarks"] = remarks
				self.assertInvalid(payload, "remarks (the return reason) is required")
		payload = make_open_return()
		del payload["remarks"]
		self.assertInvalid(payload, "remarks")

	def test_reason_is_optional_against_an_invoice(self):
		payload = make_return()
		payload["remarks"] = None
		self.assertIsNone(validate_invoice_payload(payload)["remarks"])

	def test_rows_must_not_reference_an_original(self):
		payload = make_open_return()
		payload["items"][0]["against_line_no"] = 1
		self.assertInvalid(payload, "items[0] references an original row")
		payload = make_open_return()
		payload["items"][0]["against_row_name"] = "abc123"
		self.assertInvalid(payload, "items[0] references an original row")

	def test_quantity_and_payment_signs(self):
		payload = make_open_return()
		payload["items"][0]["qty"] = "1"
		self.assertInvalid(payload, "items[0].qty must be negative on a return")
		payload = make_open_return()
		payload["items"][0]["qty"] = "0"
		self.assertInvalid(payload, "items[0].qty must not be zero")
		payload = make_open_return()
		payload["payments"][0]["amount"] = "0.600"
		self.assertInvalid(payload, "payments[0].amount must be negative on a return")

	def test_no_loyalty_redemption(self):
		payload = make_open_return()
		payload["loyalty"] = {"points": 10, "amount": "0.100"}
		self.assertInvalid(payload, "cannot be redeemed on a return")


class TestCustomerPayload(unittest.TestCase):
	def test_valid(self):
		data = validate_customer_payload(
			{
				"local_id": "c-1",
				"customer_name": " Ahmed Al Balushi ",
				"mobile_no": "+96891234567",
				"email_id": None,
			}
		)
		self.assertEqual(data["customer_name"], "Ahmed Al Balushi")
		self.assertIsNone(data["email_id"])
		self.assertIsNone(data["customer_group"])

	def test_name_required(self):
		with self.assertRaises(SyncError):
			validate_customer_payload({"local_id": "c-1", "customer_name": "  "})

	def test_non_string_rejected(self):
		with self.assertRaises(SyncError):
			validate_customer_payload({"local_id": "c-1", "customer_name": "A", "mobile_no": 968})


if __name__ == "__main__":
	unittest.main()
