"""The Crenya POS Control Log (0.17.0): a retail till's store-control events of a closed shift, in
chunks of at most 200, one row per till event (device, seq) however often a chunk is resent.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_control_log
"""

import frappe

from crenya_pos_api.api import device as device_api
from crenya_pos_api.sync.control_log import LOG_DOCTYPE, device_seq, shift_control_events
from crenya_pos_api.tests.sync_case import SyncCase, new_id

AGGREGATE = "Crenya POS Control Log"
SHIFT = "Crenya POS Shift"


class TestControlLog(SyncCase):
	_seq = 0

	def seqs(self, count):
		"""Till event ids no other test of this run used."""
		base = int(frappe.db.sql("select ifnull(max(seq), 0) from `tabCrenya POS Control Event`")[0][0])
		start = max(base, TestControlLog._seq) + 1
		TestControlLog._seq = start + count
		return list(range(start, start + count))

	def events(self, seqs, **overrides):
		rows = []
		for index, seq in enumerate(seqs):
			row = {
				"seq": seq,
				"event_type": "line_voided",
				"at": f"2026-11-04T08:{index // 60 % 60:02d}:{index % 60:02d}Z",
				"actor": "Administrator",
				"approver": "Administrator",
				"action": "line_void",
				"subject": "milk",
				"amount": "1.250",
				"item_code": "_TC-MILK-1L",
				"line_no": 1,
				"reason": None,
				"outside_shift": False,
				"detail": {"shift": "x"},
			}
			row.update(overrides)
			rows.append(row)
		return rows

	def chunk(self, shift_local_id, seqs, chunk=1, chunks=1, local_id=None, **overrides):
		return {
			"local_id": local_id or f"{shift_local_id}:{chunk}",
			"shift_local_id": shift_local_id,
			"pos_profile": self.profile.name,
			"chunk": chunk,
			"chunks": chunks,
			"events": self.events(seqs, **overrides),
		}

	def push_chunk(self, payload, event_id=None, device_id=None):
		event = self.event(payload, AGGREGATE, event_id=event_id)
		if device_id:
			from crenya_pos_api.api import sync as sync_api

			return sync_api.push_batch(device_id=device_id, events=[event])[0]
		return self.push_one(event)

	def event_count(self, log_name):
		return frappe.db.count("Crenya POS Control Event", {"parent": log_name, "parenttype": LOG_DOCTYPE})

	def test_chunk_stored_with_events(self):
		shift = new_id()
		seqs = self.seqs(3)
		payload = self.chunk(shift, seqs, actor="nobody@nowhere.example", event_type="tax_template_changed")
		result = self.assertOk(self.push_chunk(payload))
		self.assertEqual((result["doctype"], result["stored"], result["duplicates"]), (AGGREGATE, 3, 0))
		log = frappe.get_doc(LOG_DOCTYPE, result["name"])
		self.assertEqual(
			(log.device, log.shift_local_id, log.pos_profile), (self.device_id, shift, self.profile.name)
		)
		self.assertEqual(len(log.events), 3)
		row = log.events[0]
		self.assertEqual(row.device_seq, device_seq(self.device_id, seqs[0]))
		self.assertEqual(row.event_type, "tax_template_changed")
		self.assertAlmostEqual(row.amount, 1.25)
		# an actor that is not an enabled user: stored empty, the raw value kept (never refused)
		self.assertIsNone(row.actor)
		self.assertEqual(row.approver, "Administrator")
		self.assertEqual(frappe.parse_json(row.detail)["actor_raw"], "nobody@nowhere.example")

	def test_resent_chunk_stores_no_duplicates(self):
		shift = new_id()
		seqs = self.seqs(5)
		payload = self.chunk(shift, seqs)
		event_id = new_id()
		first = self.assertOk(self.push_chunk(payload, event_id=event_id))
		# the same event again (a timeout at the till): the stored log, nothing new
		again = self.push_chunk(payload, event_id=event_id)
		self.assertEqual((again["status"], again["name"]), ("duplicate", first["name"]))
		# the same chunk under a new event id: found by (device, local_id)
		resent = self.push_chunk(payload)
		self.assertEqual((resent["status"], resent["name"]), ("duplicate", first["name"]))
		self.assertEqual(frappe.db.count(LOG_DOCTYPE, {"device": self.device_id, "shift_local_id": shift}), 1)
		# the same seqs in another chunk (a new event id, a new local id): every one a duplicate
		other = self.assertOk(self.push_chunk(self.chunk(shift, seqs, chunk=2, chunks=2)))
		self.assertEqual((other["stored"], other["duplicates"]), (0, 5))
		self.assertEqual(self.event_count(first["name"]), 5)
		self.assertEqual(self.event_count(other["name"]), 0)
		events, truncated = shift_control_events(shift, self.device_id)
		self.assertEqual([row["seq"] for row in events], seqs)
		self.assertFalse(truncated)

	def test_log_before_shift_linked_when_shift_arrives(self):
		shift_payload = self.shift_payload()
		shift = shift_payload["local_id"]
		log = self.assertOk(self.push_chunk(self.chunk(shift, self.seqs(2))))
		self.assertFalse(frappe.db.get_value(LOG_DOCTYPE, log["name"], "shift"))
		self.assertOk(self.push_one(self.event(shift_payload, SHIFT)))
		self.assertEqual(frappe.db.get_value(LOG_DOCTYPE, log["name"], "shift"), shift)
		# a chunk after its shift is linked at once
		later = self.assertOk(self.push_chunk(self.chunk(shift, self.seqs(1), chunk=2, chunks=2)))
		self.assertEqual(frappe.db.get_value(LOG_DOCTYPE, later["name"], "shift"), shift)

	def test_other_device_seq_not_a_duplicate(self):
		other = new_id()
		device_api.register_device(
			device_id=other, device_name="Till 2", pos_profile=self.profile.name, app_version="0.11.0"
		)
		frappe.db.commit()
		shift = new_id()
		seqs = self.seqs(2)
		self.assertOk(self.push_chunk(self.chunk(shift, seqs)))
		result = self.assertOk(self.push_chunk(self.chunk(shift, seqs), device_id=other))
		self.assertEqual((result["stored"], result["duplicates"]), (2, 0))

	def test_events_oldest_first_and_truncated(self):
		shift = new_id()
		seqs = self.seqs(4)
		payload = self.chunk(shift, seqs)
		# the till's order is not the time order
		payload["events"].reverse()
		for index, row in enumerate(payload["events"]):
			row["at"] = f"2026-11-04T09:00:0{index}Z"
		self.assertOk(self.push_chunk(payload))
		events, truncated = shift_control_events(shift, self.device_id, limit=3)
		self.assertEqual([row["seq"] for row in events], list(reversed(seqs))[:3])
		self.assertTrue(truncated)

	def test_wrong_profile_refused(self):
		payload = self.chunk(new_id(), self.seqs(1))
		payload["pos_profile"] = "_Test Crenya Other"
		self.assertError(self.push_chunk(payload), "validation", False)

	def test_bootstrap_has_control_log_feature_for_till_only(self):
		features = device_api.get_bootstrap(device_id=self.device_id).get("features") or {}
		self.assertEqual(features.get("control_log"), 1)
		kds = new_id()
		device_api.register_device(
			device_id=kds, device_name="Kitchen", pos_profile=self.profile.name, device_type="kds"
		)
		frappe.db.commit()
		self.assertNotIn("control_log", device_api.get_bootstrap(device_id=kds).get("features") or {})
		# and a kds cannot push one
		result = self.push_chunk(self.chunk(new_id(), self.seqs(1)), device_id=kds)
		self.assertError(result, "permission", False)

	def test_old_till_shift_unchanged(self):
		# a 0.10.0 till's shift (store controls and on-account summary, no control log): recorded as before
		payload = self.shift_payload(
			store_controls={
				"line_voids": {"count": 2, "amount": "1.500"},
				"bill_clears": {"count": 0, "amount": "0.000"},
				"no_sales": 1,
				"approvals": 3,
			},
			on_account={
				"sales_count": 0,
				"sales_amount": "0.000",
				"returned_amount": "0.000",
				"received_count": 0,
				"received_amount": "0.000",
			},
		)
		result = self.assertOk(self.push_one(self.event(payload, SHIFT)))
		row = frappe.db.get_value(
			SHIFT, result["name"], ["line_voids_count", "no_sales_count", "approvals_count"], as_dict=True
		)
		self.assertEqual((row.line_voids_count, row.no_sales_count, row.approvals_count), (2, 1, 3))
		self.assertEqual(shift_control_events(payload["local_id"], self.device_id), ([], False))
		self.assertFalse(frappe.db.exists(LOG_DOCTYPE, {"shift": result["name"]}))
