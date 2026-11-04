"""Errors of crenya pos devices in the site's Error Log (0.17.0, moved down from crenya_restaurant
0.15.3, where restaurant devices report them).

A device's crash or unexpected error becomes an Error Log row. copilot's Error Log shipper sends
Error Log rows on to Sentry, so a device carries no error SDK of its own (no trackers on devices).

- **Who.** An authorized device (its key) of one of the types the caller accepts reports its own
  errors. A hub (one of the caller's `hub_types`, e.g. a restaurant's outlet POS) also relays the
  errors of the devices paired with it, which hold no server credentials (waiter phones, kitchen
  displays): each error names its device, which must be an enabled device of an accepted type on the
  hub's POS Profile. Any other error is skipped.
- **How much.** At most MAX_PER_CALL errors per call, each field cut to its length below, and the
  caller's rate limit per reporting device (`hit`, HTTP 429 over it; a retail till: RETAIL_PER_HOUR,
  `retail_hit`). The same error of the same device is logged once per DEDUPE_SECONDS.
- **What.** The title is the caller's (`title_of` by default: the app, its version, the device and
  the error's first line; a retail till's `retail_title` names neither the till nor the version, so
  Sentry groups one bug of many tills as one issue); the message holds the app and version, the
  device's name and id, the kind, the device's time and the detail (a stack). Devices send their own
  error text only.
"""

import hashlib
import time
from collections.abc import Callable

import frappe
from frappe import _
from frappe.utils import cstr

from crenya_pos_api.web.context import invalid, refuse

DEVICE_DOCTYPE = "Crenya POS Device"
MAX_PER_CALL = 5
DEDUPE_SECONDS = 600
# a report's body is read up to this size (five errors at their longest fit well inside)
MAX_PAYLOAD_BYTES = 64 * 1024
# Error Log titles longer than this are cut
TITLE_MAX = 140
# field: longest text kept
LENGTHS = {"app": 40, "version": 20, "kind": 20, "message": 300, "detail": 4000, "at": 40}
# the app a title names when the device sent none
DEFAULT_APP = "crenya pos"
CACHE_PREFIX = "crenya_pos_api:device_error:"
# a retail till's reports per hour (ruling E2: copilot ships at most 100 events a run, so one broken
# till must not crowd out the rest of the site)
RETAIL_PER_HOUR = 30
# the bootstrap feature a retail till waits for before it sends its errors
FEATURE_DEVICE_ERRORS = "device_errors"


def clean(item):
	"""The fields of one reported error, cut to LENGTHS (pure); None without a message."""
	if not isinstance(item, dict):
		return None
	out = {field: cstr(item.get(field)).strip()[:length] for field, length in LENGTHS.items()}
	if not out["message"]:
		return None
	out["device_id"] = cstr(item.get("device_id")).strip()[:140]
	return out


def _first_line(error):
	return error["message"].splitlines()[0][:100]


def title_of(error, device_name, default_app=DEFAULT_APP):
	"""The Error Log title (pure): app (`default_app` when the device sent none), version, device and
	the message's first line."""
	app = " ".join(part for part in (error["app"] or default_app, error["version"]) if part)
	return f"{app} on {device_name}: {_first_line(error)}"[:TITLE_MAX]


def retail_title(error, device_name=None):
	"""A retail till's Error Log title (pure): "crenya pos till: <first line>". No till name and no
	version: Sentry groups rows by title, so one bug on every till is one issue (both are in the
	message)."""
	return f"{DEFAULT_APP} till: {_first_line(error)}"[:TITLE_MAX]


def check_payload_size(size):
	"""Refuses a report body larger than MAX_PAYLOAD_BYTES before it is parsed."""
	if size and size > MAX_PAYLOAD_BYTES:
		invalid(_("An error report is limited to {0} bytes").format(MAX_PAYLOAD_BYTES))


def _device_row(device_id):
	return frappe.db.get_value(
		DEVICE_DOCTYPE,
		device_id,
		["name", "device_name", "pos_profile", "enabled", "device_type"],
		as_dict=True,
	)


def _device_of(ctx, device_id, allowed_types, hub_types):
	"""The device row an error belongs to: the caller, or (when the caller is a hub) a device paired
	with it on the same POS Profile, of an accepted type; None for any other."""
	if not device_id or device_id == ctx.device_id:
		return ctx.device
	if ctx.device_type not in hub_types:
		return None
	row = _device_row(device_id)
	if (
		not row
		or not row.enabled
		or row.pos_profile != ctx.device.get("pos_profile")
		or row.device_type not in allowed_types
	):
		return None
	return row


def _seen_recently(device, error):
	"""Whether this device logged this error within DEDUPE_SECONDS (and marks it seen)."""
	digest = hashlib.sha256(f"{device}\0{error['kind']}\0{error['message']}".encode()).hexdigest()
	key = f"{CACHE_PREFIX}{digest}"
	if frappe.cache.get_value(key, expires=True):
		return True
	frappe.cache.set_value(key, 1, expires_in_sec=DEDUPE_SECONDS)
	return False


def log_errors(errors, device_of, reported_by, reference_doctype=DEVICE_DOCTYPE, title=None):
	"""Logs up to MAX_PER_CALL of `errors` (a list), each of the device `device_of(device_id)`
	answers (a row with name, device_name, device_type; None: skipped), referencing it as
	`reference_doctype`; `title(error, device_name)` makes the title (default `title_of`). Answers
	{"logged": n, "skipped": m}."""
	title = title or title_of
	logged = skipped = 0
	for item in errors[:MAX_PER_CALL]:
		error = clean(item)
		device = device_of(error["device_id"]) if error else None
		if not error or not device or _seen_recently(device["name"], error):
			skipped += 1
			continue
		device_name = device.get("device_name") or device["name"]
		app = " ".join(part for part in (error["app"], error["version"]) if part) or "?"
		frappe.log_error(
			title=title(error, device_name),
			message=(
				f"kind: {error['kind'] or 'error'}\nat (device): {error['at'] or '?'}\n"
				f"app: {app}\n"
				f"device: {device_name} / {device['name']} ({device.get('device_type') or '?'})\n"
				f"reported by: {reported_by}\n\n{error['detail'] or error['message']}"
			),
			reference_doctype=reference_doctype,
			reference_name=device["name"],
		)
		logged += 1
	return {"logged": logged, "skipped": skipped + max(0, len(errors) - MAX_PER_CALL)}


def record(
	ctx,
	errors,
	allowed_types: set[str],
	hit: Callable[[str], None],
	hub_types=frozenset(),
	title=None,
):
	"""Logs the reported errors of `ctx`'s device (and, for a hub of `hub_types`, of its paired
	devices). A device whose type is not in `allowed_types` is refused (PermissionError); `hit(device
	id)` counts the call against the caller's rate limit and raises over it. Answers {"logged": n,
	"skipped": m}."""
	if ctx.device_type not in allowed_types:
		refuse(_("This device does not report its errors here"))
	hit(ctx.device_id)
	if not isinstance(errors, list):
		invalid(_("errors must be a list"))
	return log_errors(
		errors,
		lambda device_id: _device_of(ctx, device_id, allowed_types, hub_types),
		ctx.device_id,
		title=title,
	)


def retail_hit(device_id):
	"""A retail till's rate limit: RETAIL_PER_HOUR reports per device in each clock hour, counted in
	the site's cache; frappe.TooManyRequestsError (HTTP 429) above it."""
	key = frappe.cache.make_key(f"{CACHE_PREFIX}rate:{device_id}:{int(time.time() // 3600)}")
	count = frappe.cache.incr(key)
	if count == 1:
		frappe.cache.expire(key, 3600)
	if count > RETAIL_PER_HOUR:
		frappe.throw(
			_("Too many error reports from this till: try again in an hour"), frappe.TooManyRequestsError
		)
