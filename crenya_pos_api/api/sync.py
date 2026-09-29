import json

import frappe

import crenya_pos_api
from crenya_pos_api.sync.context import (
	PROTOCOL_VERSION,
	check_protocol_version,
	get_device_context,
	require_login,
)
from crenya_pos_api.sync.errors import InvalidRequestError, raise_api_error
from crenya_pos_api.sync.events import process_batch
from crenya_pos_api.sync.pull import pull_changes as _pull_changes
from crenya_pos_api.sync.validation import MAX_EVENTS_PER_BATCH
from crenya_pos_api.utils.dates import utc_now_iso


def _app_version(app):
	try:
		return frappe.get_attr(f"{app}.__version__")
	except Exception:
		return None


@frappe.whitelist(methods=["GET", "POST"])
def get_sync_capabilities(protocol_version: int | str | None = None):
	require_login()
	check_protocol_version(protocol_version)
	from frappe.utils import get_system_timezone

	installed = frappe.get_installed_apps()
	return {
		"protocol_version": PROTOCOL_VERSION,
		"app_version": crenya_pos_api.__version__,
		"frappe_version": frappe.__version__,
		"erpnext_version": _app_version("erpnext") if "erpnext" in installed else None,
		"server_time": utc_now_iso(),
		"site_timezone": get_system_timezone(),
		"user": frappe.session.user,
		"features": {
			"sales": True,
			"returns": True,
			"shifts": True,
			"fawtara": "oman_compliance" in installed,
		},
	}


@frappe.whitelist(methods=["POST"])
def pull_changes(
	device_id: str | None = None,
	entity: str | None = None,
	cursor: str | None = None,
	limit: int | str | None = None,
	protocol_version: int | str | None = None,
):
	check_protocol_version(protocol_version)
	ctx = get_device_context(device_id)
	return _pull_changes(ctx, entity, cursor, limit)


@frappe.whitelist(methods=["POST"])
def push_batch(
	device_id: str | None = None,
	events: list | str | None = None,
	protocol_version: int | str | None = None,
):
	check_protocol_version(protocol_version)
	ctx = get_device_context(device_id)

	if isinstance(events, str):
		try:
			events = json.loads(events)
		except ValueError:
			raise_api_error(InvalidRequestError, "events must be a JSON list")
	if not isinstance(events, list):
		raise_api_error(InvalidRequestError, "events must be a list")
	if len(events) > MAX_EVENTS_PER_BATCH:
		raise_api_error(InvalidRequestError, f"At most {MAX_EVENTS_PER_BATCH} events per batch")

	return process_batch(ctx, events)
