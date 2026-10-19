"""A customer payment that names the bills it settles (0.14.0): the payload's `invoices` and
`advance_confirmed`, the golden allocations shared with pos-core, the one lock name and the
approvals block another product's till asks for. No site (the invoice reads are faked)."""

import json
import os
import unittest
from decimal import Decimal
from unittest import mock

import frappe

from crenya_pos_api.sync import approvals, events
from crenya_pos_api.sync import customer_payment as payments
from crenya_pos_api.sync.approvals import APPROVAL_ACTIONS, APPROVER_ROLE, CREDIT_ACTIONS
from crenya_pos_api.sync.customer_payment import (
	AGGREGATE_CUSTOMER_PAYMENT,
	FEATURE_SETTLE_INVOICES,
	MAX_TICKED_INVOICES,
	allocate,
	ticked_invoices,
	validate_customer_payment_payload,
)
from crenya_pos_api.sync.errors import VALIDATION, SyncError
from crenya_pos_api.sync.events import customer_lock_key
from crenya_pos_api.utils.decimal import as_decimal, format_money

GOLDEN = os.path.join(os.path.dirname(__file__), "golden", "credit_allocations.json")


def payload(**over):
	p = {
		"local_id": "PAY-1",
		"business_date": "2026-10-19",
		"customer": "CUST-0042",
		"mode_of_payment": "Cash",
		"amount": "15.000",
		"currency": "OMR",
	}
	p.update(over)
	return p


def refused(test, **over):
	with test.assertRaises(SyncError) as caught:
		validate_customer_payment_payload(payload(**over))
	test.assertEqual(caught.exception.code, VALIDATION)
	return caught.exception.message


class TestTickedPayload(unittest.TestCase):
	def test_feature_name(self):
		self.assertEqual(FEATURE_SETTLE_INVOICES, "credit_settle_invoices")
		self.assertEqual(MAX_TICKED_INVOICES, 200)

	def test_without_invoices_the_payload_reads_as_before(self):
		data = validate_customer_payment_payload(payload())
		self.assertIsNone(data["invoices"])
		self.assertFalse(data["advance_confirmed"])

	def test_invoices_keep_their_order_and_drop_duplicates(self):
		data = validate_customer_payment_payload(
			payload(invoices=[" SINV-3 ", "local:6f1c", "SINV-1", "SINV-3", "local:6f1c"])
		)
		self.assertEqual(data["invoices"], ["SINV-3", "local:6f1c", "SINV-1"])
		self.assertEqual(validate_customer_payment_payload(payload(invoices=[]))["invoices"], [])

	def test_a_name_of_140_and_a_till_id_of_140_are_taken(self):
		names = ["S" * 140, "local:" + "a" * 140]
		self.assertEqual(validate_customer_payment_payload(payload(invoices=names))["invoices"], names)

	def test_invoices_list_too_long_refused(self):
		self.assertIn("200", refused(self, invoices=[f"SINV-{n}" for n in range(201)]))
		ok = validate_customer_payment_payload(payload(invoices=[f"SINV-{n}" for n in range(200)]))
		self.assertEqual(len(ok["invoices"]), 200)

	def test_what_is_refused(self):
		self.assertIn("list", refused(self, invoices="SINV-1"))
		self.assertIn("list", refused(self, invoices={"SINV-1": 1}))
		self.assertIn("invoice names", refused(self, invoices=["SINV-1", 7]))
		self.assertIn("invoice names", refused(self, invoices=["  "]))
		self.assertIn("140", refused(self, invoices=["S" * 141]))
		self.assertIn("till id", refused(self, invoices=["local:"]))
		self.assertIn("till id", refused(self, invoices=["local:" + "a" * 141]))

	def test_advance_confirmed_never_refuses(self):
		for value, expected in (
			(True, True),
			(1, True),
			("1", True),
			("true", True),
			(False, False),
			(0, False),
			(None, False),
			("yes please", False),
			({"x": 1}, False),
			([1], False),
			(2, False),
		):
			data = validate_customer_payment_payload(payload(advance_confirmed=value))
			self.assertIs(data["advance_confirmed"], expected, value)


def fake_invoice_reads(all_open, effective):
	"""frappe.get_all for Sales Invoice as `ticked_invoices` and `open_invoices` read it: `all_open`
	rows can be found by their till id (a `local:<id>` key is the invoice of till id <id>), only the
	`effective` ones are outstanding now."""

	def row(entry):
		key = entry["key"]
		return frappe._dict(
			name=key,
			crenya_local_id=key[len("local:") :] if key.startswith("local:") else None,
			grand_total=entry["outstanding"],
			outstanding_amount=entry["outstanding"],
			due_date=None,
		)

	def get_all(doctype, filters=None, fields=None, order_by=None, **kwargs):
		assert doctype == "Sales Invoice", doctype
		filters = filters or {}
		if "crenya_local_id" in filters:
			wanted = set(filters["crenya_local_id"][1])
			return [row(e) for e in all_open if row(e).crenya_local_id in wanted]
		assert filters.get("docstatus") == 1 and filters.get("is_return") == 0, filters
		assert filters.get("outstanding_amount") == [">", 0], filters
		rows = [row(e) for e in effective]
		if "name" in filters:
			wanted = set(filters["name"][1])
			rows = [r for r in rows if r.name in wanted]
		return rows

	return mock.patch.object(payments.frappe, "get_all", side_effect=get_all)


class TestGoldenCreditAllocations(unittest.TestCase):
	"""Every case of the golden file (shared byte for byte with pos-core) through `ticked_invoices`
	and `allocate`. The header rule (R10): the effective open bills are `open` minus `gone`; every
	ticked key not among them is skipped."""

	@classmethod
	def setUpClass(cls):
		with open(GOLDEN, encoding="utf-8") as f:
			cls.cases = json.load(f)

	def test_the_golden_file_has_cases(self):
		self.assertGreaterEqual(len(self.cases), 12)

	def test_golden_credit_allocations(self):
		for case in self.cases:
			with self.subTest(case["name"]):
				precision = case["precision"]
				gone = set(case["gone"])
				effective = [e for e in case["open"] if e["key"] not in gone]
				notes = []
				with fake_invoice_reads(case["open"], effective):
					if case["ticked"]:
						candidates, skipped = ticked_invoices("CUST", "CO", case["ticked"], notes)
					else:
						candidates, skipped = payments.open_invoices("CUST", "CO"), []
				rows, left = allocate(case["amount"], candidates, precision)
				expect = case["expect"]
				self.assertEqual(
					[
						{
							"key": r["name"],
							"allocated": format_money(r["allocated"], precision),
							"left": format_money(
								as_decimal(r["outstanding"]) - as_decimal(r["allocated"]), precision
							),
						}
						for r in rows
					],
					expect["rows"],
				)
				self.assertEqual(format_money(left, precision), expect["advance"])
				self.assertEqual(skipped, expect["skipped"])
				# every skip is noted on the Sync Event
				self.assertEqual(len(notes), len(skipped))
				for key in skipped:
					self.assertTrue(any(key in note for note in notes), (key, notes))

	def test_a_bill_named_twice_is_allocated_once(self):
		bill = {"key": "local:6f1c", "outstanding": "13.000"}
		notes = []
		with fake_invoice_reads([bill], [bill]):
			rows, skipped = ticked_invoices("CUST", "CO", ["local:6f1c", "local:6f1c"], notes)
		self.assertEqual([r["name"] for r in rows], ["local:6f1c"])
		self.assertEqual(skipped, [])
		self.assertEqual(rows[0]["outstanding"], "13.000")
		self.assertEqual(allocate("20.000", rows, 3)[1], Decimal("7.000"))


class TestSkippedInResult(unittest.TestCase):
	def doc(self, remarks, ticked=None, skipped=None):
		"""A Payment Entry: `ticked` None is one loaded again (a resend), else a fresh booking."""
		doc = frappe._dict(remarks=remarks, references=[], unallocated_amount=0)
		doc.flags = frappe._dict()
		if ticked is not None:
			doc.flags.crenya_ticked = ticked
			doc.flags.crenya_skipped = skipped or []
		return doc

	def test_a_fresh_booking_answers_its_skips(self):
		self.assertEqual(payments.skipped_invoices(self.doc("x", True, ["B2"])), ["B2"])
		self.assertEqual(payments.skipped_invoices(self.doc("x", True, [])), [])

	def test_a_fresh_booking_without_ticks_never_reads_the_remarks(self):
		"""A cashier's note may end like the skipped part: a payment that named no bills has none."""
		note = "Received at the till T1 by c; skipped ticked invoices: X"
		self.assertIsNone(payments.skipped_invoices(self.doc(note, False)))
		# a ticked booking answers its own skips, not the note's
		self.assertEqual(payments.skipped_invoices(self.doc(note, True, [])), [])

	def test_a_stored_entry_reads_them_from_the_remarks(self):
		remarks = "Received at the till T1 by c; skipped ticked invoices: B2, local:7a2b"
		self.assertEqual(payments.skipped_invoices(self.doc(remarks)), ["B2", "local:7a2b"])

	def test_a_payment_without_ticks_has_no_skipped(self):
		self.assertIsNone(payments.skipped_invoices(self.doc("Received at the till T1 by c; shift S")))
		self.assertIsNone(payments.skipped_invoices(self.doc(None)))


class TestOneLockName(unittest.TestCase):
	def test_customer_lock_key(self):
		self.assertEqual(customer_lock_key("CUST-0042"), "customer:CUST-0042")

	def test_named_lock_name_equals_event_lock_name(self):
		event = {"aggregate_type": AGGREGATE_CUSTOMER_PAYMENT, "payload": {"customer": "CUST-0042"}}
		name = events.lock_name(customer_lock_key("CUST-0042"))
		self.assertEqual(events._lock_name(event), name)
		self.assertTrue(name.startswith("crenya_pos:"))
		self.assertEqual(len(name), len("crenya_pos:") + 40)

	def locked(self, calls):
		db = mock.Mock()
		db.commit.side_effect = lambda: calls.append(("commit", None))
		db.rollback.side_effect = lambda: calls.append(("rollback", None))
		return (
			mock.patch.object(
				events, "_acquire_lock", side_effect=lambda n: calls.append(("get", n)) or True
			),
			mock.patch.object(events, "_release_lock", side_effect=lambda n: calls.append(("release", n))),
			mock.patch.object(events.frappe, "db", db),
		)

	def test_named_lock_commits_the_work_before_it_releases(self):
		calls = []
		acquire, release, db = self.locked(calls)
		with acquire, release, db:
			with events.named_lock("customer:C"):
				calls.append(("work", None))
		name = events.lock_name("customer:C")
		self.assertEqual(calls, [("get", name), ("work", None), ("commit", None), ("release", name)])

	def test_named_lock_rolls_back_then_releases_when_the_block_fails(self):
		calls = []
		acquire, release, db = self.locked(calls)
		with acquire, release, db, self.assertRaises(RuntimeError):
			with events.named_lock("customer:C"):
				calls.append(("work", None))
				raise RuntimeError("refused")
		name = events.lock_name("customer:C")
		self.assertEqual(calls, [("get", name), ("work", None), ("rollback", None), ("release", name)])

	def test_named_lock_busy(self):
		with (
			mock.patch.object(events, "_acquire_lock", return_value=False),
			mock.patch.object(events, "_release_lock") as release,
		):
			with self.assertRaises(frappe.ValidationError) as caught:
				with events.named_lock("customer:C"):
					self.fail("ran without the lock")
		self.assertIn("busy", str(caught.exception))
		release.assert_not_called()


def profile(**values):
	return frappe._dict(values)


class TestApprovalsBootstrapForAProduct(unittest.TestCase):
	def every_on(self, **over):
		values = {field: 1 for _key, field, *_ in APPROVAL_ACTIONS}
		values.update(over)
		return profile(**values)

	def test_approvals_bootstrap_defaults_unchanged(self):
		for switch in (0, 1):
			p = self.every_on(crenya_credit_sales_enabled=switch, crenya_max_discount_percent=12.5)
			block = approvals.approvals_bootstrap(p)
			self.assertEqual(approvals.approvals_bootstrap(p, roles=None, actions=None), block)
			self.assertEqual(block["approver_roles"], [APPROVER_ROLE])
			self.assertEqual(block["max_discount_percent"], "12.5")
			self.assertEqual(block["required"], [key for key, *_ in APPROVAL_ACTIONS])
			if switch:
				self.assertEqual([row["action"] for row in block["labels"]], list(CREDIT_ACTIONS))
			else:
				self.assertNotIn("labels", block)

	def test_roles_replace_the_approver_role(self):
		block = approvals.approvals_bootstrap(self.every_on(), roles=["Restaurant Manager"])
		self.assertEqual(block["approver_roles"], ["Restaurant Manager"])
		self.assertEqual(block["required"], [key for key, *_ in APPROVAL_ACTIONS])

	def test_approvals_bootstrap_credit_only(self):
		p = self.every_on(crenya_approve_credit_sale=0, crenya_max_discount_percent=10)
		block = approvals.approvals_bootstrap(p, roles=["Restaurant Manager"], actions=CREDIT_ACTIONS)
		self.assertEqual(
			block,
			{
				"approver_roles": ["Restaurant Manager"],
				"max_discount_percent": None,
				"required": ["credit_over_limit"],
				"labels": [
					{
						"action": "credit_over_limit",
						"label": "Sell on account over the limit",
						"label_ar": "بيع آجل فوق الحد",
					},
					{"action": "credit_sale", "label": "Sell on account", "label_ar": "بيع بالآجل"},
				],
			},
		)
		self.assertIsInstance(block["labels"], list)
		# the switch does not decide the labels when the actions are named; a discount limit is never sent
		bare = approvals.approvals_bootstrap(profile(), roles=["R"], actions=("credit_sale",))
		# one action named alone, as a string or a list, is that action
		for one in ("credit_sale", ["credit_sale"]):
			self.assertEqual(approvals.approvals_bootstrap(profile(), roles=["R"], actions=one), bare)
		self.assertEqual(
			bare,
			{
				"approver_roles": ["R"],
				"max_discount_percent": None,
				"required": [],
				"labels": [{"action": "credit_sale", "label": "Sell on account", "label_ar": "بيع بالآجل"}],
			},
		)


if __name__ == "__main__":
	unittest.main()
