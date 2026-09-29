import copy
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from crenya_pos_api.sync.errors import SyncError
from crenya_pos_api.sync.hashing import payload_hash
from crenya_pos_api.sync.validation import (
	validate_envelope,
	validate_invoice_payload,
	validate_shift_payload,
)
from crenya_pos_api.tests.test_unit_validation import SALE
from crenya_pos_api.utils.dates import parse_iso_utc, to_site_naive

SHIFT = {
	"local_id": "5d7e1c2a-2222-4a5b-9c1d-000000000012",
	"shift_number": "SH-D02-000012",
	"pos_profile": "Muscat Main",
	"company": "Crenya LLC",
	"cashier": "cashier1@store.om",
	"opened_at": "2026-09-29T04:00:00Z",
	"closed_at": "2026-09-29T12:05:00Z",
	"opening_float": "20.000",
	"sales_count": 212,
	"returns_count": 3,
	"sales_total": "845.250",
	"returns_total": "-4.800",
	"net_total": "840.450",
	"tax_total": "40.021",
	"payments": [
		{"mode_of_payment": "Cash", "expected": "512.300", "counted": "511.800", "difference": "-0.500"},
		{"mode_of_payment": "Card", "expected": "348.150", "counted": "348.150", "difference": "0.000"},
	],
	"invoice_local_ids": ["inv-1", "inv-2"],
	"notes": None,
}


def mutate(fn, base=SHIFT):
	payload = copy.deepcopy(base)
	fn(payload)
	return payload


class TestShiftEnvelope(unittest.TestCase):
	def test_shift_aggregate_is_accepted(self):
		event = {
			"event_id": "9f1c2d3e-aaaa-bbbb-cccc-000000000099",
			"aggregate_type": "Crenya POS Shift",
			"operation": "submit",
			"local_id": SHIFT["local_id"],
			"sequence_no": 7,
			"payload_hash": payload_hash(SHIFT),
			"payload": SHIFT,
		}
		env = validate_envelope(event)
		self.assertEqual(env["aggregate_type"], "Crenya POS Shift")
		self.assertEqual(env["local_id"], SHIFT["local_id"])

		with self.assertRaises(SyncError):
			validate_envelope(dict(event, operation="cancel"))


class TestShiftPayload(unittest.TestCase):
	def test_valid_shift_is_normalized(self):
		data = validate_shift_payload(copy.deepcopy(SHIFT))
		self.assertEqual(data["local_id"], SHIFT["local_id"])
		self.assertEqual(data["shift_number"], "SH-D02-000012")
		self.assertEqual(data["opened_at"], datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc))
		self.assertEqual(data["closed_at"], datetime(2026, 9, 29, 12, 5, tzinfo=timezone.utc))
		self.assertEqual(data["opening_float"], Decimal("20.000"))
		self.assertEqual((data["sales_count"], data["returns_count"]), (212, 3))
		self.assertEqual(data["returns_total"], Decimal("-4.800"))
		self.assertEqual(data["net_total"], Decimal("840.450"))
		self.assertEqual(data["tax_total"], Decimal("40.021"))
		self.assertEqual(
			data["payments"][0],
			{
				"mode_of_payment": "Cash",
				"expected": Decimal("512.300"),
				"counted": Decimal("511.800"),
				"difference": Decimal("-0.500"),
			},
		)
		self.assertEqual(data["invoice_local_ids"], ["inv-1", "inv-2"])
		self.assertIsNone(data["notes"])

	def test_optional_parts(self):
		payload = mutate(lambda p: [p.pop(key) for key in ("cashier", "invoice_local_ids", "notes")])
		payload["payments"] = []
		data = validate_shift_payload(payload)
		self.assertIsNone(data["cashier"])
		self.assertEqual(data["invoice_local_ids"], [])
		self.assertEqual(data["payments"], [])

		data = validate_shift_payload(mutate(lambda p: p.update(payments=None, sales_count="4")))
		self.assertEqual(data["payments"], [])
		self.assertEqual(data["sales_count"], 4)

	def test_difference_defaults_to_counted_minus_expected(self):
		payload = mutate(lambda p: p["payments"][0].pop("difference"))
		self.assertEqual(validate_shift_payload(payload)["payments"][0]["difference"], Decimal("-0.500"))

	def test_duplicate_invoice_ids_are_collapsed(self):
		payload = mutate(lambda p: p.update(invoice_local_ids=["a", "b", "a"]))
		self.assertEqual(validate_shift_payload(payload)["invoice_local_ids"], ["a", "b"])

	def test_offsets_are_converted_to_utc(self):
		payload = mutate(lambda p: p.update(opened_at="2026-09-29T08:00:00+04:00"))
		self.assertEqual(
			validate_shift_payload(payload)["opened_at"], datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)
		)

	def assertInvalid(self, payload, fragment):
		with self.assertRaises(SyncError) as ctx:
			validate_shift_payload(payload)
		self.assertEqual(ctx.exception.code, "validation")
		self.assertIn(fragment, ctx.exception.message)

	def test_rejections(self):
		cases = [
			(lambda p: p.update(local_id=""), "local_id"),
			(lambda p: p.update(shift_number=None), "shift_number"),
			(lambda p: p.update(shift_number="S" * 141), "shift_number"),
			(lambda p: p.update(pos_profile=""), "pos_profile"),
			(lambda p: p.pop("company"), "company"),
			(lambda p: p.update(cashier=42), "cashier"),
			(lambda p: p.update(opened_at=None), "opened_at"),
			(lambda p: p.update(opened_at="2026-09-29 04:00:00"), "opened_at"),
			(lambda p: p.update(opened_at="2026-09-29T04:00:00"), "opened_at"),
			(lambda p: p.update(closed_at="2026-02-30T12:00:00Z"), "closed_at"),
			(lambda p: p.update(closed_at=1759147500), "closed_at"),
			(lambda p: p.update(closed_at="2026-09-29T03:59:59Z"), "before opened_at"),
			(lambda p: p.update(opening_float="-1"), "opening_float"),
			(lambda p: p.update(opening_float=20.0), "opening_float"),
			(lambda p: p.pop("opening_float"), "opening_float"),
			(lambda p: p.update(sales_count=-1), "sales_count"),
			(lambda p: p.update(sales_count=True), "sales_count"),
			(lambda p: p.update(returns_count="three"), "returns_count"),
			(lambda p: p.update(sales_total="abc"), "sales_total"),
			(lambda p: p.pop("returns_total"), "returns_total"),
			(lambda p: p.update(net_total=840.45), "net_total"),
			(lambda p: p.pop("tax_total"), "tax_total"),
			(lambda p: p.update(payments="Cash"), "payments"),
			(lambda p: p.update(payments=["Cash"]), "payments[0]"),
			(lambda p: p["payments"][0].pop("mode_of_payment"), "payments[0].mode_of_payment"),
			(lambda p: p["payments"][1].update(mode_of_payment="Cash"), "more than once"),
			(lambda p: p["payments"][0].update(expected=None), "payments[0].expected"),
			(lambda p: p["payments"][0].update(counted="x"), "payments[0].counted"),
			(lambda p: p["payments"][1].update(difference=0.0), "payments[1].difference"),
			(lambda p: p.update(invoice_local_ids="inv-1"), "invoice_local_ids"),
			(lambda p: p.update(invoice_local_ids=["ok", ""]), "invoice_local_ids[1]"),
			(lambda p: p.update(invoice_local_ids=[1]), "invoice_local_ids[0]"),
			(lambda p: p.update(notes="n" * 2001), "notes"),
		]
		for fn, fragment in cases:
			with self.subTest(fragment=fragment):
				self.assertInvalid(mutate(fn), fragment)


class TestInvoiceShiftLocalId(unittest.TestCase):
	def test_older_payload_without_key(self):
		payload = copy.deepcopy(SALE)
		payload.pop("shift_local_id", None)
		self.assertIsNone(validate_invoice_payload(payload)["shift_local_id"])

	def test_shift_local_id(self):
		payload = dict(copy.deepcopy(SALE), shift_local_id=" shift-1 ")
		self.assertEqual(validate_invoice_payload(payload)["shift_local_id"], "shift-1")
		payload["shift_local_id"] = None
		self.assertIsNone(validate_invoice_payload(payload)["shift_local_id"])

	def test_invalid_shift_local_id(self):
		for value in (12, "s" * 141, ["shift-1"]):
			payload = dict(copy.deepcopy(SALE), shift_local_id=value)
			with self.subTest(value=value), self.assertRaises(SyncError) as ctx:
				validate_invoice_payload(payload)
			self.assertIn("shift_local_id", ctx.exception.message)


class TestUtcTimestamps(unittest.TestCase):
	def test_parse(self):
		utc = timezone.utc
		self.assertEqual(parse_iso_utc("2026-09-29T12:05:00Z"), datetime(2026, 9, 29, 12, 5, tzinfo=utc))
		self.assertEqual(
			parse_iso_utc("2026-09-29T12:05:00.5Z"), datetime(2026, 9, 29, 12, 5, 0, 500000, tzinfo=utc)
		)
		self.assertEqual(parse_iso_utc("2026-09-29T00:30:00-0130"), datetime(2026, 9, 29, 2, 0, tzinfo=utc))
		self.assertEqual(parse_iso_utc("2026-09-29T02:00:00+04:00"), datetime(2026, 9, 28, 22, 0, tzinfo=utc))

	def test_parse_rejects(self):
		for value in (
			None,
			"",
			"2026-09-29",
			"2026-09-29T12:05:00",
			"2026-13-01T00:00:00Z",
			"2026-09-29T24:00:00Z",
			"2026-09-29T12:05:00+25:00",
			"2026-09-29T12:05:00.1234567Z",
		):
			with self.subTest(value=value), self.assertRaises(ValueError):
				parse_iso_utc(value)

	def test_to_site_naive(self):
		moment = datetime(2026, 9, 29, 20, 30, tzinfo=timezone.utc)
		self.assertEqual(to_site_naive(moment, "Asia/Muscat"), datetime(2026, 9, 30, 0, 30))
		self.assertEqual(to_site_naive(moment, "UTC"), datetime(2026, 9, 29, 20, 30))
		self.assertEqual(to_site_naive(moment, "Not/AZone"), datetime(2026, 9, 29, 20, 30))
		self.assertEqual(to_site_naive(moment, None), datetime(2026, 9, 29, 20, 30))
		self.assertIsNone(to_site_naive(moment, "Asia/Muscat").tzinfo)


if __name__ == "__main__":
	unittest.main()
