"""A test-only "other app" built on crenya_pos_api.extension, wired in by faking frappe.get_hooks.

It imports only from crenya_pos_api.extension, like a real extension app must. The tests
register it through `fake_hooks()`, so CI needs no second app on the bench.
"""

from contextlib import contextmanager
from unittest import mock

import frappe

from crenya_pos_api.extension import (
	DEPENDENCY_MISSING,
	VALIDATION,
	AggregateHandler,
	EntitySpec,
	SyncError,
	registry,
)

APP = "crenya_test_ext"
TASK_AGGREGATE = "Test Crenya Task"
ENTITY = "test_brand"
DEVICE_ROLE = "_Test Crenya Device Role"
TASK_RETENTION_DAYS = 30

HOOKS = {
	"crenya_pos_pull_entities": {ENTITY: "crenya_pos_api.tests.extension_dummy.BrandSpec"},
	"crenya_pos_aggregates": {TASK_AGGREGATE: "crenya_pos_api.tests.extension_dummy.TaskAggregate"},
	"crenya_pos_bootstrap": ["crenya_pos_api.tests.extension_dummy.extend_bootstrap"],
	"crenya_pos_capabilities": ["crenya_pos_api.tests.extension_dummy.features"],
	"crenya_pos_invoice_extenders": ["crenya_pos_api.tests.extension_dummy.extend_invoice"],
	"crenya_pos_device_roles": [DEVICE_ROLE],
}


def _as_frappe_hooks(hooks):
	"""Shape the values like frappe.get_hooks merges them: dict hooks become {key: [path]}."""
	shaped = {}
	for name, value in hooks.items():
		if isinstance(value, dict):
			shaped[name] = {key: item if isinstance(item, list) else [item] for key, item in value.items()}
		else:
			shaped[name] = value
	return shaped


@contextmanager
def fake_hooks(hooks=None, site_ready=False):
	"""Answer the crenya_pos_* hooks from `hooks` (default: this module's HOOKS); every other
	hook still comes from the real apps. `site_ready` lets plain unit tests read hooks."""
	shaped = _as_frappe_hooks(HOOKS if hooks is None else hooks)
	original = frappe.get_hooks

	def get_hooks(hook=None, default="_KEEP_DEFAULT_LIST", app_name=None):
		if hook in shaped and app_name is None:
			return shaped[hook]
		if hook is not None and hook.startswith("crenya_pos_") and app_name is None:
			return [] if default == "_KEEP_DEFAULT_LIST" else default
		return original(hook, default, app_name)

	registry.clear_cache()
	patches = [mock.patch("frappe.get_hooks", get_hooks)]
	if site_ready:
		patches.append(mock.patch.object(registry, "_hooks_available", return_value=True))
	for patch in patches:
		patch.start()
	try:
		yield
	finally:
		for patch in reversed(patches):
			patch.stop()
		registry.clear_cache()


# pull entity


class BrandSpec(EntitySpec):
	doctype = "Brand"
	fields = ("name", "brand")

	def build(self, rows, ctx):
		return [{"name": row.name, "brand": row.brand, "device_type": ctx.device_type} for row in rows]


# push aggregate: a task created, renamed and closed by several events (not "submit")

TASK_DOCTYPE = "Test Crenya Ext Task"


def ensure_task_doctype():
	"""The dummy app's own DocType (a custom DocType, so no app files are needed)."""
	if frappe.db.exists("DocType", TASK_DOCTYPE):
		return
	frappe.get_doc(
		{
			"doctype": "DocType",
			"name": TASK_DOCTYPE,
			"module": "Crenya Pos Api",
			"custom": 1,
			"autoname": "hash",
			"fields": [
				{"fieldname": "local_id", "fieldtype": "Data", "label": "Local ID", "unique": 1},
				{"fieldname": "title", "fieldtype": "Data", "label": "Title"},
				{"fieldname": "status", "fieldtype": "Data", "label": "Status"},
				{"fieldname": "device", "fieldtype": "Data", "label": "Device"},
				{"fieldname": "revision", "fieldtype": "Int", "label": "Revision"},
			],
			"permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
		}
	).insert(ignore_permissions=True)


def _task_payload(payload):
	title = payload.get("title")
	if not isinstance(title, str) or not title.strip():
		raise SyncError(VALIDATION, "title is required")
	return {"local_id": payload.get("local_id"), "title": title.strip()}


class TaskAggregate(AggregateHandler):
	doctype = TASK_DOCTYPE
	operations = ("create", "rename", "close")
	retention_days = TASK_RETENTION_DAYS

	def validate(self, operation, payload):
		return _task_payload(payload)

	def apply(self, ctx, operation, data, notes):
		if operation == "create":
			doc = frappe.get_doc(
				{
					"doctype": TASK_DOCTYPE,
					"local_id": data["local_id"],
					"title": data["title"],
					"status": "open",
					"device": ctx.device_id,
					"revision": 0,
				}
			)
			doc.insert(ignore_permissions=True)
			notes.append(f"created on {ctx.device_type}")
			return doc

		name = frappe.db.get_value(TASK_DOCTYPE, {"local_id": data["local_id"]}, "name")
		if not name:
			raise SyncError(DEPENDENCY_MISSING, f"task {data['local_id']} is not on the server yet")
		doc = frappe.get_doc(TASK_DOCTYPE, name)
		if operation == "rename":
			doc.title = data["title"]
			doc.revision = (doc.revision or 0) + 1
		else:
			doc.status = "closed"
		doc.save(ignore_permissions=True)
		return doc

	def result_fields(self, doc):
		fields = super().result_fields(doc)
		fields["totals"] = {"status": doc.status, "revision": doc.revision}
		return fields


# function hooks


def extend_bootstrap(ctx, doc):
	doc[APP] = {"device_type": ctx.device_type, "entities": sorted(registry.entities())}


def features():
	# "sales" is a core flag: the merge keeps the core value
	return {APP: True, "sales": False}


def extend_invoice(ctx, doc, data, notes):
	ext = data["extensions"].get(APP)
	if not ext:
		return
	if ext.get("reject"):
		raise SyncError(VALIDATION, f"extensions.{APP}: rejected by the extender")
	lines = {line["line_no"]: line for line in data["items"]}
	for row in doc.items:
		line = lines[row.flags.crenya_line_no]
		if line["notes"]:
			row.description = f"{row.item_name} ({line['notes']})"
	doc.remarks = f"Table {ext.get('table')}"
	notes.append(f"{APP}: table {ext.get('table')}")


# misconfigured extensions (registry error tests)


class ItemlessSpec(EntitySpec):
	fields = ("name",)


class NamedAggregate(TaskAggregate):
	aggregate_type = "Named"


class BadOperationsAggregate(TaskAggregate):
	operations = ("create", "Fire!")


class BadRetentionAggregate(TaskAggregate):
	retention_days = 0


def not_a_dict():
	return ["flag"]
