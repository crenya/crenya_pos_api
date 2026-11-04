"""A closed shift's store-control events (0.17.0, retail till 0.11.0): the Crenya POS Control Log.

At shift close the till queues that shift's `Store Control` audit events (line voids, bill clears,
no-sales, tax template changes, manager approvals granted, refused and used) as chunks of at most
200 events, each chunk one push event of the aggregate `Crenya POS Control Log` (local id
"<shift_local_id>:<chunk>"). The till sends them only when the bootstrap announces
`features.control_log` (sync.device.bootstrap), so an older server never sees one.

Idempotency, besides the framework's own on `event_id`:

- a chunk is one log per (device, local_id): a resend with a new event id finds the stored log
  (`find_for_device`), and a concurrent resend hits the unique index and is answered the same way;
- an event is one row per (device, seq), the column `device_seq` (unique): an event that is already
  stored, under any chunk, is skipped and counted in the answer's `duplicates`.

Nothing a till did is refused for what it is: an event type this server does not know is stored as
it is, and an actor or approver that is not an enabled user is stored empty with the raw value in
the event's `detail` (`actor_raw`, `approver_raw`). The shift may arrive before or after its log:
whichever comes second links the two (`shift`).
"""

import json

import frappe
from frappe.utils import cint, get_system_timezone

from crenya_pos_api.sync.aggregates import AggregateHandler
from crenya_pos_api.sync.cashier import enabled_user
from crenya_pos_api.sync.context import DEFAULT_DEVICE_TYPE, money_precision
from crenya_pos_api.sync.errors import PERMISSION, VALIDATION, SyncError
from crenya_pos_api.sync.validation import AGGREGATE_CONTROL_LOG, validate_control_log_payload
from crenya_pos_api.utils.dates import format_db_datetime, to_site_naive
from crenya_pos_api.utils.decimal import format_money, quantize

LOG_DOCTYPE = AGGREGATE_CONTROL_LOG
EVENT_DOCTYPE = "Crenya POS Control Event"
SHIFT_DOCTYPE = "Crenya POS Shift"
# the bootstrap feature a till waits for before it sends a control log
FEATURE_CONTROL_LOG = "control_log"


def device_seq(device, seq) -> str:
	"""The unique key of one till event on the server: `<device>:<seq>`."""
	return f"{device}:{seq}"


def _stored_seqs(device, seqs):
	"""The `device_seq` values of `seqs` that are already stored for `device`."""
	if not seqs:
		return set()
	keys = [device_seq(device, seq) for seq in seqs]
	return set(frappe.get_all(EVENT_DOCTYPE, filters={"device_seq": ["in", keys]}, pluck="device_seq"))


def _user_or_raw(value, key, detail):
	"""An enabled user stays; anything else is stored empty with the raw value in `detail`."""
	user = enabled_user(value)
	if value and not user:
		detail[f"{key}_raw"] = value
	return user


def _shift_of(ctx, shift_local_id):
	"""The device's own shift of that local id (a shift is named after its local id), or None."""
	shift = frappe.db.get_value(SHIFT_DOCTYPE, shift_local_id, ["name", "device"], as_dict=True)
	return shift.name if shift and shift.device == ctx.device_id else None


def apply_control_log(ctx, data, notes):
	"""Store one chunk (see the module); returns the inserted log, its counts in
	`doc.flags.crenya_control_log` ({stored, duplicates})."""
	if ctx.device_type != DEFAULT_DEVICE_TYPE:
		raise SyncError(PERMISSION, "Only a retail till sends a control log", retryable=False)
	profile = ctx.profile
	if data["pos_profile"] != profile.name:
		raise SyncError(
			VALIDATION,
			f"Control log POS Profile {data['pos_profile']} does not match device profile {profile.name}",
		)

	precision = money_precision(ctx.currency)
	time_zone = get_system_timezone()
	stored = _stored_seqs(ctx.device_id, [row["seq"] for row in data["events"]])
	doc = frappe.new_doc(LOG_DOCTYPE)
	doc.update(
		{
			"local_id": data["local_id"],
			"device": ctx.device_id,
			"pos_profile": profile.name,
			"company": ctx.company,
			"shift_local_id": data["shift_local_id"],
			"shift": _shift_of(ctx, data["shift_local_id"]),
			"chunk": data["chunk"],
			"chunks": data["chunks"],
		}
	)
	duplicates = 0
	for row in data["events"]:
		key = device_seq(ctx.device_id, row["seq"])
		if key in stored:
			duplicates += 1
			continue
		stored.add(key)
		amount = row["amount"]
		if amount is not None and quantize(amount, precision) != amount:
			raise SyncError(
				VALIDATION,
				f"event {row['seq']}: amount {amount} has more than {precision} decimals",
			)
		detail = json.loads(row["detail"]) if row["detail"] else {}
		actor = _user_or_raw(row["actor"], "actor", detail)
		approver = _user_or_raw(row["approver"], "approver", detail)
		doc.append(
			"events",
			{
				"seq": row["seq"],
				"device_seq": key,
				"event_type": row["event_type"],
				"at": to_site_naive(row["at"], time_zone),
				"actor": actor,
				"approver": approver,
				"action": row["action"],
				"subject": row["subject"],
				"amount": None if amount is None else float(format_money(amount, precision)),
				"item_code": row["item_code"],
				"line_no": row["line_no"],
				"reason": row["reason"],
				"outside_shift": row["outside_shift"],
				"detail": json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail else None,
			},
		)
	if duplicates:
		notes.append(f"{duplicates} event(s) already stored; skipped")
	# the device was authorized by get_device_context; nobody creates control logs in the desk
	doc.insert(ignore_permissions=True)
	doc.flags.crenya_control_log = {"stored": len(doc.events), "duplicates": duplicates}
	return doc


def table_ready() -> bool:
	"""Whether the site has the Control Log table (a code deploy runs before `bench migrate`)."""
	return bool(frappe.db.table_exists(LOG_DOCTYPE))


def link_shift(shift_doc):
	"""Point the logs that arrived before their shift at it (called by `create_shift`); nothing on a
	site not migrated yet."""
	if not table_ready():
		return
	log = frappe.qb.DocType(LOG_DOCTYPE)
	(
		frappe.qb.update(log)
		.set(log.shift, shift_doc.name)
		.where(
			(log.shift_local_id == shift_doc.local_id)
			& (log.device == shift_doc.device)
			& (log.shift.isnull() | (log.shift == ""))
		)
		.run()
	)


EVENT_COLUMNS = (
	"seq",
	"event_type",
	"at",
	"actor",
	"approver",
	"action",
	"subject",
	"amount",
	"item_code",
	"line_no",
	"reason",
	"outside_shift",
	"detail",
)


def shift_control_events(shift_local_id: str, device: str, limit: int = 2000) -> tuple[list[dict], bool]:
	"""The events a device sent with a shift, oldest first (by time, then the till's seq), at most
	`limit`, and whether there were more. Rows are plain dicts of EVENT_COLUMNS; `detail` parsed."""
	log = frappe.qb.DocType(LOG_DOCTYPE)
	event = frappe.qb.DocType(EVENT_DOCTYPE)
	rows = (
		frappe.qb.from_(event)
		.join(log)
		.on((event.parent == log.name) & (event.parenttype == LOG_DOCTYPE))
		.select(*[event[column] for column in EVENT_COLUMNS])
		.where((log.shift_local_id == shift_local_id) & (log.device == device))
		.orderby(event.at)
		.orderby(event.seq)
		.limit(cint(limit) + 1)
		.run(as_dict=True)
	)
	truncated = len(rows) > cint(limit)
	rows = rows[: cint(limit)]
	for row in rows:
		try:
			row["detail"] = json.loads(row["detail"]) if row.get("detail") else {}
		except ValueError:
			row["detail"] = {}
		row["outside_shift"] = bool(cint(row.get("outside_shift")))
	return [dict(row) for row in rows], truncated


class ControlLogHandler(AggregateHandler):
	"""One chunk of a closed shift's store-control events (retail till)."""

	aggregate_type = AGGREGATE_CONTROL_LOG
	operations = ("submit",)

	def validate(self, operation, payload):
		return validate_control_log_payload(payload)

	def apply(self, ctx, operation, data, notes):
		return apply_control_log(ctx, data, notes)

	def find_for_device(self, ctx, local_id):
		return frappe.db.get_value(LOG_DOCTYPE, {"device": ctx.device_id, "local_id": local_id}, "name")

	def result_fields(self, doc):
		counts = doc.flags.get("crenya_control_log") or {
			"stored": len(doc.get("events") or []),
			"duplicates": 0,
		}
		return {
			"doctype": LOG_DOCTYPE,
			"name": doc.name,
			"docstatus": cint(doc.get("docstatus")),
			"modified": format_db_datetime(doc.get("modified")),
			"totals": None,
			"fawtara_status": None,
			"stored": counts["stored"],
			"duplicates": counts["duplicates"],
		}


__all__ = [
	"AGGREGATE_CONTROL_LOG",
	"FEATURE_CONTROL_LOG",
	"ControlLogHandler",
	"apply_control_log",
	"link_shift",
	"shift_control_events",
	"validate_control_log_payload",
]
