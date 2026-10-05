"""Extension hooks, the device role check and device_type against a real ERPNext site.

The "other app" is crenya_pos_api.tests.extension_dummy, wired in by faking frappe.get_hooks.
push_batch commits per event, so documents created here persist on the test site.
"""

import uuid
from unittest import mock

import frappe
from frappe.utils import add_days, now_datetime

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

from crenya_pos_api import tasks
from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.sync import errors, registry
from crenya_pos_api.sync.context import get_device_context
from crenya_pos_api.sync.hashing import payload_hash
from crenya_pos_api.tests import extension_dummy as dummy
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests import test_integration_sync as sync_tests

EVENT = "Crenya Sync Event"
DEVICE = "Crenya POS Device"
NO_ROLE_USER = "_test_crenya_ext_norole@example.com"
HOOK_ROLE_USER = "_test_crenya_ext_hookrole@example.com"
EXT_BRAND = "_Test Crenya Ext Brand"


def new_id():
	return str(uuid.uuid4())


def ensure_user(email, roles):
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": "Crenya Ext", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	user.enabled = 1
	user.set("roles", [])
	user.save(ignore_permissions=True)
	if roles:
		user.add_roles(*roles)
	return email


def ensure_role(role):
	if not frappe.db.exists("Role", role):
		frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 0}).insert(
			ignore_permissions=True
		)


class TestCrenyaExtensions(FrappeTestCase):
	# the payload / push helpers of the sync tests (taken from the module, so the loader does
	# not collect TestCrenyaSync a second time)
	sale_payload = sync_tests.TestCrenyaSync.sale_payload
	event = sync_tests.TestCrenyaSync.event
	push = sync_tests.TestCrenyaSync.push
	push_one = sync_tests.TestCrenyaSync.push_one
	assertOk = sync_tests.TestCrenyaSync.assertOk
	assertError = sync_tests.TestCrenyaSync.assertError
	invoices_with_local_id = sync_tests.TestCrenyaSync.invoices_with_local_id

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		registry.clear_cache()

		cls._saved_precision = frappe.db.get_single_value("System Settings", "currency_precision")
		frappe.db.set_single_value("System Settings", "currency_precision", "3")
		frappe.db.set_default("currency_precision", "3")
		frappe.clear_cache()
		cls._saved_conf = {
			key: frappe.conf.get(key)
			for key in (
				"crenya_pos_pull_lag_seconds",
				"crenya_pos_total_tolerance",
				"crenya_pos_event_retention_days",
			)
		}
		frappe.conf.crenya_pos_pull_lag_seconds = 0
		frappe.conf.crenya_pos_total_tolerance = "0.010"
		frappe.conf.pop("crenya_pos_event_retention_days", None)

		cls.profile = fixtures.setup_fixtures()
		cls.walk_in = cls.profile.customer
		fixtures.ensure_brand(EXT_BRAND)
		dummy.ensure_task_doctype()
		ensure_role(dummy.DEVICE_ROLE)
		cls.device_id = new_id()
		cls.device = device_api.register_device(
			device_id=cls.device_id, device_name="Ext Till", pos_profile=cls.profile.name
		)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("System Settings", "currency_precision", cls._saved_precision or "")
		frappe.db.set_default("currency_precision", cls._saved_precision or "")
		for key, value in cls._saved_conf.items():
			if value is None:
				frappe.conf.pop(key, None)
			else:
				frappe.conf[key] = value
		frappe.db.commit()
		frappe.clear_cache()
		registry.clear_cache()
		super().tearDownClass()

	def tearDown(self):
		frappe.set_user("Administrator")
		registry.clear_cache()

	# helpers

	def task_event(self, operation, local_id, title="Table 4", event_id=None):
		payload = {"local_id": local_id, "title": title}
		return {
			"event_id": event_id or new_id(),
			"aggregate_type": dummy.TASK_AGGREGATE,
			"operation": operation,
			"local_id": local_id,
			"sequence_no": 1,
			"payload_hash": payload_hash(payload),
			"payload": payload,
		}

	def task(self, local_id):
		return frappe.db.get_value(
			dummy.TASK_DOCTYPE, {"local_id": local_id}, ["name", "title", "status", "revision"], as_dict=True
		)

	def pull_all(self, entity):
		records, cursor = [], None
		while True:
			page = sync_api.pull_changes(device_id=self.device_id, entity=entity, cursor=cursor, limit=1000)
			records.extend(page["records"])
			cursor = page["next_cursor"]
			if not page["has_more"]:
				return records

	# capabilities / bootstrap without hooks: the 0.7 shape plus the protocol 2 flags

	def test_protocol_2_without_hooks(self):
		caps = sync_api.get_sync_capabilities()
		self.assertEqual(caps["protocol_version"], 2)
		self.assertIs(caps["features"]["extensions"], True)
		self.assertNotIn(dummy.APP, caps["features"])
		data = device_api.get_bootstrap(device_id=self.device_id)
		self.assertEqual(data["device"]["device_type"], "till")
		self.assertNotIn(dummy.APP, data)
		self.assertEqual(self.device["device_type"], "till")

	# pull entity

	def test_custom_entity_pull(self):
		with self.assertRaises(errors.InvalidRequestError):
			sync_api.pull_changes(device_id=self.device_id, entity=dummy.ENTITY)

		with dummy.fake_hooks():
			records = self.pull_all(dummy.ENTITY)
			by_name = {record["name"]: record for record in records}
			self.assertEqual(
				by_name[EXT_BRAND], {"name": EXT_BRAND, "brand": EXT_BRAND, "device_type": "till"}
			)
			# built-in entities are untouched
			self.assertTrue(self.pull_all("uom"))
			with self.assertRaises(errors.InvalidRequestError) as ctx:
				sync_api.pull_changes(device_id=self.device_id, entity="nope")
			self.assertIn(dummy.ENTITY, str(ctx.exception))

	# push aggregate

	def test_custom_aggregate_is_rejected_without_the_hook(self):
		result = self.push_one(self.task_event("create", new_id()))
		self.assertError(result, "validation", False)
		self.assertIn(
			"aggregate_type must be one of Customer, Sales Invoice, Crenya POS Shift",
			result["error"]["message"],
		)

	def test_custom_aggregate_operations_and_idempotency(self):
		local_id = new_id()
		with dummy.fake_hooks():
			create = self.task_event("create", local_id)
			first = self.assertOk(self.push_one(create))
			self.assertEqual(first["doctype"], dummy.TASK_DOCTYPE)
			self.assertEqual(first["totals"], {"status": "open", "revision": 0})
			event = frappe.get_doc(EVENT, create["event_id"])
			self.assertEqual(
				(event.aggregate_type, event.operation, event.result_doctype, event.result_name),
				(dummy.TASK_AGGREGATE, "create", dummy.TASK_DOCTYPE, first["name"]),
			)
			self.assertEqual(event.note, "created on till")

			# same event twice: one effect
			again = self.push_one(create)
			self.assertEqual(again["status"], "duplicate")
			self.assertEqual((again["doctype"], again["name"]), (dummy.TASK_DOCTYPE, first["name"]))
			self.assertEqual(frappe.db.count(dummy.TASK_DOCTYPE, {"local_id": local_id}), 1)

			# a non-submit operation on the same aggregate, applied once even when re-sent
			rename = self.task_event("rename", local_id, title="Table 5")
			renamed = self.assertOk(self.push_one(rename))
			self.assertEqual(renamed["name"], first["name"])
			self.assertEqual(self.push_one(rename)["status"], "duplicate")
			task = self.task(local_id)
			self.assertEqual((task.title, task.revision), ("Table 5", 1))

			# same event_id, different payload: conflict, nothing applied
			conflict = self.task_event("rename", local_id, title="Table 9", event_id=rename["event_id"])
			self.assertError(self.push_one(conflict), "payload_conflict", False)
			self.assertEqual(self.task(local_id).title, "Table 5")
			self.assertEqual(frappe.db.get_value(EVENT, rename["event_id"], "status"), "ok")

			closed = self.assertOk(self.push_one(self.task_event("close", local_id)))
			self.assertEqual(closed["totals"]["status"], "closed")

			# operations the handler does not list, and its own payload validation
			self.assertError(self.push_one(self.task_event("submit", local_id)), "validation", False)
			bad = self.task_event("rename", local_id, title=" ")
			self.assertError(self.push_one(bad), "validation", False)

			# a handler's retryable error is recorded on a Data aggregate_type
			missing = self.task_event("rename", new_id())
			self.assertError(self.push_one(missing), "dependency_missing", True)
			recorded = frappe.get_doc(EVENT, missing["event_id"])
			self.assertEqual((recorded.status, recorded.aggregate_type), ("error", dummy.TASK_AGGREGATE))

			# built-in aggregates in the same batch are unaffected
			results = self.push(self.event(self.sale_payload()), self.task_event("create", new_id()))
			self.assertEqual([r["status"] for r in results], ["ok", "ok"])
			self.assertEqual([r["doctype"] for r in results], ["Sales Invoice", dummy.TASK_DOCTYPE])

	def test_broken_aggregate_hook_fails_each_event_retryably(self):
		broken = dict(dummy.HOOKS, crenya_pos_aggregates={"Crenya Order": "no_such_app.sync.Order"})
		payload = self.sale_payload()
		frappe.local.response.pop("error", None)
		with dummy.fake_hooks(broken):
			result = self.push_one(self.event(payload))
		self.assertError(result, "internal", True)
		self.assertIn("no_such_app.sync", result["error"]["message"])
		# the batch itself succeeded: no request-level error body next to the results
		self.assertIsNone(frappe.local.response.get("error"))
		self.assertFalse(self.invoices_with_local_id(payload["local_id"]))
		# fixed: the same event goes through
		self.assertOk(self.push_one(self.event(payload)))

	# bootstrap / capabilities hooks

	def test_bootstrap_and_capability_hooks(self):
		with dummy.fake_hooks():
			data = device_api.get_bootstrap(device_id=self.device_id)
			self.assertEqual(data[dummy.APP]["device_type"], "till")
			self.assertIn(dummy.ENTITY, data[dummy.APP]["entities"])
			self.assertEqual(data["device"]["device_id"], self.device_id)

			features = sync_api.get_sync_capabilities()["features"]
			self.assertIs(features[dummy.APP], True)
			# a hook cannot switch a core flag off
			self.assertIs(features["sales"], True)
			self.assertIs(features["extensions"], True)

	# invoice extenders

	def test_invoice_extender_line_notes_and_parent_lines(self):
		payload = self.sale_payload(lines=[(fixtures.MILK, "1", "0.600"), (fixtures.MILK, "1", "0.600")])
		payload["items"][0]["notes"] = "no onions"
		payload["items"][1]["parent_line_no"] = 1
		payload["extensions"] = {dummy.APP: {"table": "T4"}}
		event = self.event(payload)
		with dummy.fake_hooks():
			result = self.assertOk(self.push_one(event))
			invoice = frappe.get_doc("Sales Invoice", result["name"])
			self.assertEqual(invoice.remarks, "Table T4")
			self.assertIn("(no onions)", invoice.items[0].description)
			self.assertNotIn("no onions", invoice.items[1].description or "")
			self.assertIn(f"{dummy.APP}: table T4", frappe.db.get_value(EVENT, event["event_id"], "note"))
			self.assertEqual(self.push_one(event)["status"], "duplicate")
			self.assertEqual(len(self.invoices_with_local_id(payload["local_id"])), 1)

	def test_invoice_extender_can_reject(self):
		payload = self.sale_payload(extensions={dummy.APP: {"reject": True}})
		with dummy.fake_hooks():
			result = self.push_one(self.event(payload))
		self.assertError(result, "validation", False)
		self.assertIn("rejected by the extender", result["error"]["message"])
		self.assertFalse(self.invoices_with_local_id(payload["local_id"]))

	def test_extensions_without_an_extender_are_ignored(self):
		payload = self.sale_payload(extensions={dummy.APP: {"table": "T4"}})
		payload["items"][0]["notes"] = "extra hot"
		result = self.assertOk(self.push_one(self.event(payload)))
		self.assertNotEqual(frappe.db.get_value("Sales Invoice", result["name"], "remarks"), "Table T4")

	# device role (S1)

	def test_user_without_the_role_cannot_register_or_use_a_device(self):
		user = ensure_user(NO_ROLE_USER, ["Sales User", "Accounts User"])
		own_device = new_id()
		frappe.get_doc(
			{
				"doctype": DEVICE,
				"device_id": own_device,
				"device_name": "No Role Till",
				"device_short": f"X{uuid.uuid4().hex[:8]}",
				"pos_profile": self.profile.name,
				"user": user,
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			device_api.register_device(device_id=new_id(), device_name="X", pos_profile=self.profile.name)
		with self.assertRaises(frappe.PermissionError):
			device_api.list_pos_profiles()
		with self.assertRaises(frappe.PermissionError) as ctx:
			get_device_context(own_device)
		self.assertIn("Crenya POS User", str(ctx.exception))
		for call in (
			lambda: device_api.get_bootstrap(device_id=own_device),
			lambda: sync_api.pull_changes(device_id=own_device, entity="customer"),
			lambda: sync_api.pull_changes(device_id=own_device, entity="cashier"),
			lambda: sync_api.push_batch(device_id=own_device, events=[]),
		):
			with self.assertRaises(frappe.PermissionError):
				call()

		# with the role it works (the device is the user's own)
		frappe.set_user("Administrator")
		frappe.get_doc("User", user).add_roles("Crenya POS User")
		frappe.set_user(user)
		self.assertEqual(get_device_context(own_device).device_id, own_device)

	def test_role_from_the_device_roles_hook(self):
		user = ensure_user(HOOK_ROLE_USER, [dummy.DEVICE_ROLE])
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			device_api.register_device(device_id=new_id(), device_name="X", pos_profile=self.profile.name)

		with dummy.fake_hooks():
			device_id = new_id()
			registered = device_api.register_device(
				device_id=device_id, device_name="Service", pos_profile=self.profile.name, device_type="kds"
			)
			self.assertEqual(registered["device_type"], "kds")
			ctx = get_device_context(device_id)
			self.assertEqual((ctx.device_id, ctx.device_type), (device_id, "kds"))
			profiles = [row["name"] for row in device_api.list_pos_profiles()]
			self.assertIn(self.profile.name, profiles)

		# the hook is gone (app uninstalled): the role no longer opens the device
		with self.assertRaises(frappe.PermissionError):
			get_device_context(device_id)

	# device_type

	def test_device_type(self):
		device_id = new_id()
		registered = device_api.register_device(
			device_id=device_id, device_name="Kitchen", pos_profile=self.profile.name, device_type="kds"
		)
		self.assertEqual(registered["device_type"], "kds")
		self.assertEqual(frappe.db.get_value(DEVICE, device_id, "device_type"), "kds")
		self.assertEqual(get_device_context(device_id).device_type, "kds")
		self.assertEqual(device_api.get_bootstrap(device_id=device_id)["device"]["device_type"], "kds")

		# an older client re-registering without device_type keeps it
		again = device_api.register_device(
			device_id=device_id, device_name="Kitchen", pos_profile=self.profile.name
		)
		self.assertEqual(again["device_type"], "kds")
		changed = device_api.register_device(
			device_id=device_id, device_name="Kitchen", pos_profile=self.profile.name, device_type="waiter"
		)
		self.assertEqual(changed["device_type"], "waiter")

		for bad in ("KDS", "kds!", "x" * 41, "1till"):
			with self.assertRaises(errors.InvalidRequestError, msg=bad):
				device_api.register_device(
					device_id=new_id(), device_name="Bad", pos_profile=self.profile.name, device_type=bad
				)

		self.assertEqual(frappe.db.get_value(DEVICE, self.device_id, "device_type"), "till")
		self.assertEqual(get_device_context(self.device_id).device_type, "till")

	# purge

	def make_event(self, aggregate_type, status, days_old):
		name = new_id()
		frappe.get_doc(
			{
				"doctype": EVENT,
				"event_id": name,
				"device": self.device_id,
				"aggregate_type": aggregate_type,
				"operation": "submit",
				"local_id": new_id(),
				"sequence_no": 1,
				"payload_hash": "0" * 64,
				"status": status,
				"attempts": 1,
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value(
			EVENT, name, "creation", add_days(now_datetime(), -days_old), update_modified=False
		)
		return name

	def test_sync_event_creation_index(self):
		self.assertTrue(frappe.db.has_index(f"tab{EVENT}", "creation_index"))
		self.assertEqual(frappe.get_meta(EVENT).get_field("aggregate_type").fieldtype, "Data")

	def test_purge_uses_handler_retention_in_batches(self):
		gone = [self.make_event("Customer", "ok", 200) for _ in range(3)]
		gone += [self.make_event(dummy.TASK_AGGREGATE, "ok", 40) for _ in range(2)]
		gone.append(self.make_event("Gone App Thing", "ok", 200))
		kept = [
			self.make_event("Sales Invoice", "ok", 100),
			self.make_event("Customer", "error", 200),
			self.make_event(dummy.TASK_AGGREGATE, "ok", 10),
			self.make_event(dummy.TASK_AGGREGATE, "error", 40),
			self.make_event("Gone App Thing", "ok", 40),
		]
		frappe.db.commit()

		calls = []
		original = tasks._purge_batch

		def counting(cutoff, aggregate_types, known_types, batch_size):
			count = original(cutoff, aggregate_types, known_types, batch_size)
			calls.append((tuple(aggregate_types) if aggregate_types else None, count))
			return count

		with dummy.fake_hooks(), mock.patch.object(tasks, "_purge_batch", counting):
			deleted = tasks.purge_old_sync_events(batch_size=2)

		self.assertGreaterEqual(deleted, len(gone))
		for name in gone:
			self.assertFalse(frappe.db.exists(EVENT, name), name)
		for name in kept:
			self.assertTrue(frappe.db.exists(EVENT, name), name)
		# full batches are followed by another one; the custom type uses its own 30 days
		builtin_calls = [
			count for types, count in calls if types == ("Customer", "Sales Invoice", "Crenya POS Shift")
		]
		self.assertGreaterEqual(len(builtin_calls), 2)
		self.assertEqual(builtin_calls[0], 2)
		self.assertIn((dummy.TASK_AGGREGATE,), [types for types, _count in calls])

	def test_purge_without_hooks_keeps_the_default_retention(self):
		old_ok = self.make_event("Customer", "ok", 200)
		recent_ok = self.make_event("Customer", "ok", 100)
		frappe.db.commit()
		tasks.purge_old_sync_events()
		self.assertFalse(frappe.db.exists(EVENT, old_ok))
		self.assertTrue(frappe.db.exists(EVENT, recent_ok))
