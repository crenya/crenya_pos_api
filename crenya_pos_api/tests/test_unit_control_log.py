"""The Crenya POS Control Log payload (0.17.0), without a site: a chunk of at most 200 store-control
events, an event type of lower case letters and _, amounts at most 3 decimals as written, `detail`
at most 2 KB, and an unknown event type kept as it is."""

import copy
import unittest
from decimal import Decimal

from crenya_pos_api.sync.errors import VALIDATION, SyncError
from crenya_pos_api.sync.validation import (
	AGGREGATE_CONTROL_LOG,
	AGGREGATE_TYPES,
	MAX_CONTROL_EVENTS,
	validate_control_log_payload,
)

EVENT = {
	"seq": 1234,
	"event_type": "line_voided",
	"at": "2026-11-04T08:00:00Z",
	"actor": "cashier@x.om",
	"approver": "mgr@x.om",
	"action": "line_void",
	"subject": "MILK x2",
	"amount": "1.250",
	"item_code": "ITEM-1",
	"line_no": 3,
	"reason": None,
	"outside_shift": False,
	"detail": {"shift": "S-1"},
}
CHUNK = {
	"local_id": "S-1:1",
	"shift_local_id": "S-1",
	"pos_profile": "Shop 1",
	"chunk": 1,
	"chunks": 4,
	"events": [EVENT],
}


def chunk(**overrides):
	payload = copy.deepcopy(CHUNK)
	payload.update(overrides)
	return payload


def event(**overrides):
	row = copy.deepcopy(EVENT)
	row.update(overrides)
	return row


class TestControlLogPayload(unittest.TestCase):
	def assertRefused(self, payload, words):
		with self.assertRaises(SyncError) as caught:
			validate_control_log_payload(payload)
		self.assertEqual(caught.exception.code, VALIDATION)
		self.assertIn(words, str(caught.exception))

	def test_aggregate_is_a_builtin(self):
		self.assertEqual(AGGREGATE_CONTROL_LOG, "Crenya POS Control Log")
		self.assertEqual(AGGREGATE_TYPES[-1], AGGREGATE_CONTROL_LOG)

	def test_valid_chunk_normalized(self):
		data = validate_control_log_payload(chunk())
		self.assertEqual(
			(data["local_id"], data["shift_local_id"], data["chunk"], data["chunks"]), ("S-1:1", "S-1", 1, 4)
		)
		row = data["events"][0]
		self.assertEqual(row["seq"], 1234)
		self.assertEqual(row["amount"], Decimal("1.250"))
		self.assertEqual(row["at"].isoformat(), "2026-11-04T08:00:00+00:00")
		self.assertEqual(row["outside_shift"], 0)
		self.assertEqual(row["detail"], '{"shift":"S-1"}')

	def test_validate_rejects_more_than_200_events(self):
		events = [event(seq=index) for index in range(MAX_CONTROL_EVENTS + 1)]
		self.assertRefused(chunk(events=events), "at most 200 events")
		self.assertEqual(len(validate_control_log_payload(chunk(events=events[:200]))["events"]), 200)

	def test_validate_rejects_bad_event_type_pattern(self):
		for bad in ("Line_Voided", "line-voided", "", "x" * 41, "void2", None, 7):
			self.assertRefused(chunk(events=[event(event_type=bad)]), "event_type")

	def test_unknown_event_type_kept(self):
		data = validate_control_log_payload(chunk(events=[event(event_type="drawer_kicked_twice")]))
		self.assertEqual(data["events"][0]["event_type"], "drawer_kicked_twice")

	def test_amount_with_4_decimals_refused_for_omr(self):
		self.assertRefused(chunk(events=[event(amount="1.2500")]), "more than 3 decimals")
		self.assertRefused(chunk(events=[event(amount=1.25)]), "amount")
		# no amount (a no-sale) is fine
		self.assertIsNone(
			validate_control_log_payload(chunk(events=[event(amount=None)]))["events"][0]["amount"]
		)

	def test_detail_over_2kb_refused(self):
		self.assertRefused(chunk(events=[event(detail={"note": "x" * 2100})]), "2048 bytes")
		# multi-byte text counts in bytes
		self.assertRefused(chunk(events=[event(detail={"note": "ش" * 1100})]), "2048 bytes")
		self.assertRefused(chunk(events=[event(detail="text")]), "detail must be an object")

	def test_chunk_numbers_and_times(self):
		self.assertRefused(chunk(chunk=5, chunks=4), "chunk must not be greater")
		self.assertRefused(chunk(chunk=0), "chunk must be >= 1")
		self.assertRefused(chunk(events=[event(at="2026-11-04 08:00:00")]), "events[0].at")
		self.assertRefused(chunk(events=[event(seq=None)]), "events[0].seq")
		self.assertRefused(chunk(shift_local_id=" "), "shift_local_id is required")


if __name__ == "__main__":
	unittest.main()
