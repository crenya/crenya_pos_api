import json

import frappe
from frappe.utils import cint

import crenya_pos_api
from crenya_pos_api.sync.context import (
	PROTOCOL_VERSION,
	check_protocol_version,
	get_device_context,
	require_login,
)
from crenya_pos_api.sync.customer_payment import FEATURE_SETTLE_INVOICES
from crenya_pos_api.sync.device_user import acting_user, key_kind
from crenya_pos_api.sync.errors import InvalidRequestError, raise_api_error
from crenya_pos_api.sync.events import process_batch
from crenya_pos_api.sync.fawtara import COMPLIANCE_APP
from crenya_pos_api.sync.pull import pull_changes as _pull_changes
from crenya_pos_api.sync.registry import extend_features
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
	capabilities = {
		"protocol_version": PROTOCOL_VERSION,
		"app_version": crenya_pos_api.__version__,
		"frappe_version": frappe.__version__,
		"erpnext_version": _app_version("erpnext") if "erpnext" in installed else None,
		"server_time": utc_now_iso(),
		"site_timezone": get_system_timezone(),
		# a device user answers as the person who paired it (the name the device shows as signed in)
		"user": acting_user(),
		# "device" when the call is made with a device user's key, "user" with a person's
		"key_kind": key_kind(),
		"features": {
			"sales": True,
			"returns": True,
			"shifts": True,
			"tax_templates": True,
			"loyalty": True,
			"promotions": True,
			"verify_page": True,
			"batches": True,
			"open_returns": True,
			"tax_wording": True,
			"payment_terminals": True,
			"scale_rules": True,
			"uom_entity": True,
			# app.crenya.com's Refused sales → Retry: the `sync_retry` pull entity (0.11.0)
			"sync_retry": True,
			"fawtara": COMPLIANCE_APP in installed,
			# protocol 2: entities, aggregates and invoice data of other apps (crenya_pos_* hooks)
			"extensions": True,
			# per-device users (0.12.0): sign-in and `device.rekey_device` return the key of a
			# device user and the key `pull_changes(pin_seal=1)` seals PIN hashes with
			"device_users": True,
			"pin_seal": True,
			# 0.14.0: a `Customer Payment` may name the bills it settles, in order (`invoices`)
			FEATURE_SETTLE_INVOICES: 1,
		},
	}
	# other apps' crenya_pos_capabilities hooks add their own flags; core flags are kept
	extend_features(capabilities["features"])
	return capabilities


@frappe.whitelist(methods=["POST"])
def pull_changes(
	device_id: str | None = None,
	entity: str | None = None,
	cursor: str | None = None,
	limit: int | str | None = None,
	protocol_version: int | str | None = None,
	pin_seal: int | str | None = None,
):
	check_protocol_version(protocol_version)
	ctx = get_device_context(device_id)
	# the device holds its PIN seal key (0.12.0): it receives sealed PIN hashes, never raw ones
	ctx.pin_seal = bool(cint(pin_seal))
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
