"""Extension registry and protocol 2 payload additions, without a site (hooks are faked)."""

import copy
import unittest

from crenya_pos_api import extension
from crenya_pos_api.sync import registry
from crenya_pos_api.sync.aggregates import CustomerHandler, SalesInvoiceHandler, ShiftHandler
from crenya_pos_api.sync.errors import ExtensionHookError, SyncError
from crenya_pos_api.sync.hashing import payload_hash
from crenya_pos_api.sync.pull import ENTITIES
from crenya_pos_api.sync.validation import (
	AGGREGATE_TYPES,
	MAX_LINE_NOTES_LENGTH,
	validate_envelope,
	validate_invoice_payload,
)
from crenya_pos_api.tests import extension_dummy as dummy
from crenya_pos_api.tests.test_unit_validation import SALE, make_event

DUMMY = "crenya_pos_api.tests.extension_dummy"


def hooks(**overrides):
	result = copy.deepcopy(dummy.HOOKS)
	result.update(overrides)
	return result


def task_event(operation="create", **payload):
	body = {"local_id": "task-1", "title": "Table 4"}
	body.update(payload)
	return {
		"event_id": "evt-task-1",
		"aggregate_type": dummy.TASK_AGGREGATE,
		"operation": operation,
		"local_id": body["local_id"],
		"sequence_no": 1,
		"payload_hash": payload_hash(body),
		"payload": body,
	}


def invoice(**line_overrides):
	payload = copy.deepcopy(SALE)
	second = dict(payload["items"][0], line_no=2, item_code="MILK-EXTRA")
	payload["items"].append(second)
	for index, values in line_overrides.items():
		payload["items"][int(index)].update(values)
	return payload


class TestBuiltinsWithoutHooks(unittest.TestCase):
	"""No site: exactly the 0.7 entities, aggregates and operations."""

	def test_entities_are_the_builtins_in_order(self):
		self.assertEqual(list(registry.entities()), list(ENTITIES))
		self.assertIs(registry.entity("item"), ENTITIES["item"])
		self.assertIsNone(registry.entity("restaurant_table"))
		self.assertIsNone(registry.entity(["item"]))

	def test_aggregates_are_the_builtins_in_order(self):
		handlers = registry.aggregates()
		self.assertEqual(tuple(handlers), AGGREGATE_TYPES)
		self.assertIsInstance(handlers["Customer"], CustomerHandler)
		self.assertIsInstance(handlers["Sales Invoice"], SalesInvoiceHandler)
		self.assertIsInstance(handlers["Crenya POS Shift"], ShiftHandler)
		for handler in handlers.values():
			self.assertEqual(handler.operations, ("submit",))
			self.assertIsNone(handler.retention_days)
			self.assertEqual(handler.result_doctype, handler.aggregate_type)
		self.assertEqual(handlers["Sales Invoice"].local_id_field, "crenya_local_id")
		self.assertEqual(handlers["Crenya POS Shift"].local_id_field, "local_id")
		self.assertIsNone(registry.handler("Crenya Order"))
		self.assertIsNone(registry.handler(None))

	def test_function_hooks_are_empty(self):
		self.assertEqual(registry.bootstrap_hooks(), [])
		self.assertEqual(registry.capability_hooks(), [])
		self.assertEqual(registry.invoice_extenders(), [])
		self.assertEqual(registry.device_roles(), ("Crenya POS User",))
		features = {"sales": True}
		self.assertEqual(registry.extend_features(features), {"sales": True})

	def test_unknown_aggregate_message_is_unchanged(self):
		with self.assertRaises(SyncError) as ctx:
			validate_envelope(make_event(SALE, aggregate_type="Crenya Order"))
		self.assertEqual(
			ctx.exception.message, "aggregate_type must be one of Customer, Sales Invoice, Crenya POS Shift"
		)
		with self.assertRaises(SyncError) as ctx:
			validate_envelope(make_event(SALE, operation="create"))
		self.assertEqual(ctx.exception.message, "operation 'create' is not supported for Sales Invoice")
		with self.assertRaises(SyncError):
			validate_envelope(make_event(SALE, aggregate_type={"not": "hashable"}))

	def test_public_surface(self):
		for name in extension.__all__:
			self.assertTrue(hasattr(extension, name), name)
		self.assertEqual(extension.PROTOCOL_VERSION, 2)
		self.assertIs(extension.registry, registry)


class TestRegistryMerge(unittest.TestCase):
	def test_hook_entities_follow_the_builtins(self):
		with dummy.fake_hooks(site_ready=True):
			names = list(registry.entities())
			self.assertEqual(names, [*ENTITIES, dummy.ENTITY])
			self.assertIsInstance(registry.entity(dummy.ENTITY), dummy.BrandSpec)
			self.assertIs(registry.entity("item"), ENTITIES["item"])

	def test_hook_aggregates_follow_the_builtins(self):
		with dummy.fake_hooks(site_ready=True):
			self.assertEqual(list(registry.aggregates()), [*AGGREGATE_TYPES, dummy.TASK_AGGREGATE])
			task = registry.handler(dummy.TASK_AGGREGATE)
			self.assertIsInstance(task, dummy.TaskAggregate)
			# aggregate_type comes from the hook key
			self.assertEqual(task.aggregate_type, dummy.TASK_AGGREGATE)
			self.assertEqual(task.operations, ("create", "rename", "close"))
			self.assertEqual(task.retention_days, dummy.TASK_RETENTION_DAYS)
			self.assertEqual(task.result_doctype, dummy.TASK_DOCTYPE)
			self.assertIs(registry.handler_for_doctype(dummy.TASK_DOCTYPE), task)
			self.assertIs(registry.handler_for_doctype("Sales Invoice"), registry.handler("Sales Invoice"))

	def test_results_are_cached_per_request(self):
		with dummy.fake_hooks(site_ready=True):
			self.assertIs(registry.aggregates(), registry.aggregates())
			self.assertIs(registry.entities(), registry.entities())
		# the context manager clears the cache: no hook leaks into the next request
		self.assertEqual(list(registry.aggregates()), list(AGGREGATE_TYPES))

	def test_later_app_wins_for_the_same_name(self):
		entities = {dummy.ENTITY: [f"{DUMMY}.ItemlessSpec", f"{DUMMY}.BrandSpec"]}
		with dummy.fake_hooks(hooks(crenya_pos_pull_entities=entities), site_ready=True):
			self.assertIsInstance(registry.entity(dummy.ENTITY), dummy.BrandSpec)

	def test_function_hooks_in_order(self):
		with dummy.fake_hooks(site_ready=True):
			self.assertEqual(registry.bootstrap_hooks(), [dummy.extend_bootstrap])
			self.assertEqual(registry.capability_hooks(), [dummy.features])
			self.assertEqual(registry.invoice_extenders(), [dummy.extend_invoice])
			self.assertEqual(registry.device_roles(), ("Crenya POS User", dummy.DEVICE_ROLE))

	def test_capabilities_keep_core_flags(self):
		with dummy.fake_hooks(site_ready=True):
			features = registry.extend_features({"sales": True, "extensions": True})
		self.assertEqual(features, {"sales": True, "extensions": True, dummy.APP: True})

	def test_envelope_accepts_extension_operations(self):
		with dummy.fake_hooks(site_ready=True):
			for operation in ("create", "rename", "close"):
				env = validate_envelope(task_event(operation))
				self.assertEqual((env["aggregate_type"], env["operation"]), (dummy.TASK_AGGREGATE, operation))
			with self.assertRaises(SyncError) as ctx:
				validate_envelope(task_event("submit"))
			self.assertEqual(ctx.exception.code, "validation")
			self.assertIn(f"not supported for {dummy.TASK_AGGREGATE}", ctx.exception.message)
			# the built-ins still accept only submit
			validate_envelope(make_event(SALE))
			with self.assertRaises(SyncError):
				validate_envelope(make_event(SALE, operation="create"))
			with self.assertRaises(SyncError) as ctx:
				validate_envelope(make_event(SALE, aggregate_type="Payment Entry"))
			self.assertIn(dummy.TASK_AGGREGATE, ctx.exception.message)

	def test_dummy_handler_validates_its_payload(self):
		with dummy.fake_hooks(site_ready=True):
			task = registry.handler(dummy.TASK_AGGREGATE)
			self.assertEqual(task.validate("create", {"local_id": "a", "title": " T "})["title"], "T")
			with self.assertRaises(SyncError):
				task.validate("create", {"local_id": "a"})


class TestRegistryErrors(unittest.TestCase):
	def assertHookError(self, hook_values, *fragments, call=registry.aggregates):
		with dummy.fake_hooks(hooks(**hook_values), site_ready=True):
			with self.assertRaises(ExtensionHookError) as ctx:
				call()
		self.assertEqual(ctx.exception.code, "internal")
		self.assertTrue(ctx.exception.retryable)
		for fragment in fragments:
			self.assertIn(fragment, str(ctx.exception))
		return ctx.exception

	def test_missing_module(self):
		self.assertHookError(
			{"crenya_pos_aggregates": {"Crenya Order": "no_such_app.sync.orders.OrderAggregate"}},
			"crenya_pos_aggregates",
			"cannot import no_such_app.sync.orders",
			"ModuleNotFoundError",
		)

	def test_missing_attribute(self):
		self.assertHookError(
			{"crenya_pos_pull_entities": {"table": f"{DUMMY}.NoSuchSpec"}},
			"crenya_pos_pull_entities",
			"has no attribute 'NoSuchSpec'",
			call=registry.entities,
		)

	def test_not_a_dotted_path(self):
		self.assertHookError(
			{"crenya_pos_bootstrap": ["extend"]},
			"crenya_pos_bootstrap",
			"not a dotted path",
			call=registry.bootstrap_hooks,
		)

	def test_wrong_base_class(self):
		self.assertHookError(
			{"crenya_pos_aggregates": {"Crenya Order": f"{DUMMY}.BrandSpec"}},
			"not an AggregateHandler subclass",
		)
		self.assertHookError(
			{"crenya_pos_pull_entities": {"orders": f"{DUMMY}.TaskAggregate"}},
			"not an EntitySpec subclass",
			call=registry.entities,
		)

	def test_builtins_cannot_be_replaced(self):
		self.assertHookError(
			{"crenya_pos_aggregates": {"Sales Invoice": f"{DUMMY}.TaskAggregate"}},
			"'Sales Invoice' is a built-in aggregate",
		)
		self.assertHookError(
			{"crenya_pos_pull_entities": {"item": f"{DUMMY}.BrandSpec"}},
			"'item' is a built-in entity",
			call=registry.entities,
		)

	def test_bad_names_and_shapes(self):
		self.assertHookError(
			{"crenya_pos_pull_entities": {"Bad Name": f"{DUMMY}.BrandSpec"}},
			"lower case",
			call=registry.entities,
		)
		self.assertHookError(
			{"crenya_pos_pull_entities": {"spec_without_doctype": f"{DUMMY}.ItemlessSpec"}},
			"has no doctype",
			call=registry.entities,
		)
		self.assertHookError(
			{"crenya_pos_aggregates": {"Other Name": f"{DUMMY}.NamedAggregate"}},
			"declares aggregate_type 'Named'",
		)
		self.assertHookError(
			{"crenya_pos_aggregates": {"Bad Ops": f"{DUMMY}.BadOperationsAggregate"}},
			"operation 'Fire!'",
		)
		self.assertHookError(
			{"crenya_pos_aggregates": {"Bad Retention": f"{DUMMY}.BadRetentionAggregate"}},
			"retention_days",
		)
		self.assertHookError(
			{"crenya_pos_capabilities": [f"{DUMMY}.TASK_AGGREGATE"]},
			"is not callable",
			call=registry.capability_hooks,
		)
		self.assertHookError(
			{"crenya_pos_device_roles": [""]}, "is not a role name", call=registry.device_roles
		)

	def test_capability_hook_must_return_a_dict(self):
		with dummy.fake_hooks(hooks(crenya_pos_capabilities=[f"{DUMMY}.not_a_dict"]), site_ready=True):
			with self.assertRaises(ExtensionHookError):
				registry.extend_features({})

	def test_broken_hook_is_not_cached(self):
		broken = {"crenya_pos_aggregates": {"Crenya Order": "no_such_app.Order"}}
		with dummy.fake_hooks(hooks(**broken), site_ready=True):
			for _attempt in range(2):
				with self.assertRaises(ExtensionHookError):
					registry.aggregates()

	def test_broken_aggregate_hook_fails_the_envelope_loudly(self):
		broken = {"crenya_pos_aggregates": {"Crenya Order": "no_such_app.Order"}}
		with dummy.fake_hooks(hooks(**broken), site_ready=True):
			with self.assertRaises(ExtensionHookError):
				validate_envelope(make_event(SALE))


class TestInvoiceAdditions(unittest.TestCase):
	def test_absent_keys_normalize_to_empty(self):
		data = validate_invoice_payload(copy.deepcopy(SALE))
		self.assertEqual(data["extensions"], {})
		self.assertIsNone(data["items"][0]["notes"])
		self.assertIsNone(data["items"][0]["parent_line_no"])

	def test_line_notes(self):
		data = validate_invoice_payload(invoice(**{"0": {"notes": "  no onions  "}}))
		self.assertEqual(data["items"][0]["notes"], "no onions")
		validate_invoice_payload(invoice(**{"0": {"notes": "x" * MAX_LINE_NOTES_LENGTH}}))
		for bad in ("x" * (MAX_LINE_NOTES_LENGTH + 1), 5, ["a"]):
			with self.assertRaises(SyncError, msg=repr(bad)) as ctx:
				validate_invoice_payload(invoice(**{"0": {"notes": bad}}))
			self.assertIn("items[0].notes", ctx.exception.message)

	def test_parent_line_no(self):
		data = validate_invoice_payload(invoice(**{"1": {"parent_line_no": 1}}))
		self.assertEqual(data["items"][1]["parent_line_no"], 1)
		self.assertEqual(
			validate_invoice_payload(invoice(**{"1": {"parent_line_no": "1"}}))["items"][1]["parent_line_no"],
			1,
		)
		cases = {
			"must not be the line itself": {"1": {"parent_line_no": 2}},
			"is not a line_no of this invoice": {"1": {"parent_line_no": 7}},
			"must be >= 1": {"1": {"parent_line_no": 0}},
			"must be an integer": {"1": {"parent_line_no": "one"}},
			"forms a loop": {"0": {"parent_line_no": 2}, "1": {"parent_line_no": 1}},
		}
		for fragment, overrides in cases.items():
			with self.assertRaises(SyncError, msg=fragment) as ctx:
				validate_invoice_payload(invoice(**overrides))
			self.assertIn(fragment, ctx.exception.message)

	def test_nested_parents(self):
		payload = invoice(**{"1": {"parent_line_no": 1}})
		payload["items"].append(dict(payload["items"][0], line_no=3, parent_line_no=2))
		data = validate_invoice_payload(payload)
		self.assertEqual([line["parent_line_no"] for line in data["items"]], [None, 1, 2])

	def test_extensions_pass_through_unvalidated(self):
		payload = copy.deepcopy(SALE)
		payload["extensions"] = {dummy.APP: {"table": "T4", "covers": 3, "anything": [1, {"x": None}]}}
		data = validate_invoice_payload(payload)
		self.assertEqual(data["extensions"], payload["extensions"])
		for bad in ("T4", ["T4"], 3):
			payload["extensions"] = bad
			with self.assertRaises(SyncError):
				validate_invoice_payload(payload)

	def test_extensions_are_part_of_the_payload_hash(self):
		with_extension = dict(copy.deepcopy(SALE), extensions={dummy.APP: {"table": "T4"}})
		other_table = dict(copy.deepcopy(SALE), extensions={dummy.APP: {"table": "T5"}})
		self.assertNotEqual(payload_hash(with_extension), payload_hash(SALE))
		self.assertNotEqual(payload_hash(with_extension), payload_hash(other_table))
		env = validate_envelope(make_event(with_extension))
		self.assertEqual(env["payload"]["extensions"], {dummy.APP: {"table": "T4"}})
