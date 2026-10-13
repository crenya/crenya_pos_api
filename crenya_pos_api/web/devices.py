"""An outlet's devices on app.crenya.com: the list with each device's open shift and the plan's
terminal cap, and deactivating or reactivating one. Product-neutral: the retail web admin and
crenya_restaurant's both answer `devices` / `set_device_enabled` with this code, each passing its
own device type order and labels. Who may read or change an outlet is the caller's check.

A device is never deleted. Deactivating one refuses it on its next call and frees its plan slot
at once; reactivating it is checked against the plan's caps (`device_enable_refusal`). A device
in use (a shift open, or seen within DEVICE_IN_USE_HOURS) is not deactivated: a hub relays its
paired devices' events under their own ids, and a relayed event of a disabled device is refused,
which holds up the hub's sync for the whole outlet.
"""

from datetime import date, datetime, timedelta

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime

from crenya_pos_api.sync.context import DEFAULT_DEVICE_TYPE, DEVICE_DOCTYPE
from crenya_pos_api.sync.entitlement import TERMINAL_ADDONS, device_enable_refusal
from crenya_pos_api.utils.dates import format_db_datetime
from crenya_pos_api.web.context import WebAdminContext, check_modified, checked, invalid, parse_flag, refuse

SHIFT_DOCTYPE = "Crenya POS Shift"
# the Crenya POS Device columns the list reads
DEVICE_COLUMNS = (
	"name",
	"device_name",
	"device_short",
	"device_type",
	"pos_profile",
	"user",
	"paired_by",
	"platform",
	"app_version",
	"update_channel",
	"registered_on",
	"last_seen",
	"enabled",
	"modified",
)
DEVICE_IN_USE_HOURS = 24
DEVICE_IN_USE_REFUSAL = (
	f"This device was used in the last {DEVICE_IN_USE_HOURS} hours (or has an open shift). "
	"Close its shift and stop using it before deactivating.",
	f"استُخدم هذا الجهاز في آخر {DEVICE_IN_USE_HOURS} ساعة (أو لديه وردية مفتوحة). "
	"أغلق ورديته وتوقف عن استخدامه قبل إيقافه.",
)

__all__ = [
	"DEVICE_COLUMNS",
	"DEVICE_IN_USE_HOURS",
	"DEVICE_IN_USE_REFUSAL",
	"device_in_use",
	"device_rows",
	"devices_answer",
	"full_names",
	"labelled",
	"open_shifts",
	"plan",
	"set_device_enabled",
	"wire",
]


def wire(value):
	"""A date or datetime as the web reads it: "YYYY-MM-DD HH:MM:SS" / "YYYY-MM-DD"."""
	if isinstance(value, datetime):
		return value.strftime("%Y-%m-%d %H:%M:%S")
	if isinstance(value, date):
		return value.isoformat()
	return value


def labelled(labels: dict) -> list[dict]:
	"""[{value, label, label_ar}] of {value: (English, Arabic)}."""
	return [{"value": value, "label": en, "label_ar": ar} for value, (en, ar) in labels.items()]


def full_names(users) -> dict:
	"""{user: full name (the user id when it has none)} of `users`."""
	users = sorted({user for user in users if user})
	rows = frappe.get_all("User", filters={"name": ["in", users or [""]]}, fields=["name", "full_name"])
	return {row.name: row.full_name or row.name for row in rows}


def device_rows(outlets, types=None, enabled_only=True) -> list:
	"""The Crenya POS Devices of `outlets` (of `types` alone when given; the enabled ones alone
	with `enabled_only`), by name: DEVICE_COLUMNS.

	Read with the query builder: frappe.get_all drops `last_seen` from its fields (Frappe strips
	any field containing `_seen`, one of its optional columns, when the table has no `_seen`)."""
	device = frappe.qb.DocType(DEVICE_DOCTYPE)
	# a site whose deploy skipped `bench migrate` has no `paired_by` column yet
	meta = frappe.get_meta(DEVICE_DOCTYPE)
	columns = [column for column in DEVICE_COLUMNS if column != "paired_by" or meta.has_field(column)]
	query = (
		frappe.qb.from_(device)
		.select(*(device[column] for column in columns))
		.where(device.pos_profile.isin(list(outlets) or [""]))
	)
	if enabled_only:
		query = query.where(device.enabled == 1)
	if types is not None:
		query = query.where(device.device_type.isin(list(types) or [""]))
	return query.orderby(device.device_name).run(as_dict=True)


def plan() -> dict | None:
	"""{max_terminals (0: unlimited), terminals_used} of the workspace's plan, the caps applied to
	registering and re-enabling a terminal; None without a plan (no entitlement hook)."""
	from crenya_pos_api.sync import entitlement

	current = entitlement.get_entitlement()
	if current is None:
		return None
	used = sum(
		1
		for row in entitlement.device_rows()
		if cint(row.get("enabled")) and (row.get("device_type") or DEFAULT_DEVICE_TYPE) in TERMINAL_ADDONS
	)
	return {"max_terminals": current["max_terminals"], "terminals_used": used}


def open_shifts(devices) -> dict:
	"""{device: its newest shift not closed (name, shift_number, opened_at)} of `devices`."""
	shifts = {}
	for row in frappe.get_all(
		SHIFT_DOCTYPE,
		filters={"device": ["in", list(devices) or [""]], "closed_at": ["is", "not set"]},
		fields=["name", "device", "shift_number", "opened_at"],
		order_by="opened_at desc",
	):
		shifts.setdefault(row.device, row)
	return shifts


def device_in_use(last_seen, open_shift, now) -> bool:
	"""Whether a device is still in use (pure): a shift open, or seen within DEVICE_IN_USE_HOURS."""
	if open_shift:
		return True
	return bool(last_seen) and get_datetime(last_seen) > now - timedelta(hours=DEVICE_IN_USE_HOURS)


def devices_answer(outlet: str, can_manage: bool, type_order=(), type_labels=None) -> dict:
	"""Every Crenya POS Device of the outlet, the types of `type_order` first in that order (then
	any other), each with its unclosed shift and whether it is in use (then it is not
	deactivated); the plan's terminal cap; `can_manage` (`set_device_enabled`) as the caller
	decided; `choices.device_types` from `type_labels` ({type: (English, Arabic)})."""
	rank = {device_type: index for index, device_type in enumerate(type_order)}
	rows = sorted(
		device_rows([outlet], enabled_only=False),
		key=lambda row: rank.get(row.device_type or DEFAULT_DEVICE_TYPE, len(rank)),
	)
	shifts = open_shifts(row.name for row in rows)
	# a device signs in as its own device user (0.12.0): the list names the person who paired it
	people = {row.name: row.get("paired_by") or row.user for row in rows}
	users = full_names(people.values())
	now = now_datetime()
	return {
		"outlet": outlet,
		"can_manage": bool(can_manage),
		"rows": [
			{
				"name": row.name,
				"device_name": row.device_name or row.name,
				"device_short": row.device_short or None,
				"device_type": row.device_type or DEFAULT_DEVICE_TYPE,
				"counts_for_plan": (row.device_type or DEFAULT_DEVICE_TYPE) in TERMINAL_ADDONS,
				"platform": row.platform or None,
				"app_version": row.app_version or None,
				"update_channel": row.update_channel or None,
				"registered_on": wire(row.registered_on),
				"last_seen": wire(row.last_seen),
				"enabled": cint(row.enabled),
				"user": people[row.name] or None,
				"user_name": users.get(people[row.name]),
				"modified": format_db_datetime(row.modified),
				"in_use": device_in_use(row.last_seen, row.name in shifts, now),
				"open_shift": {
					"name": shifts[row.name].name,
					"shift_number": shifts[row.name].shift_number,
					"opened_at": wire(shifts[row.name].opened_at),
				}
				if row.name in shifts
				else None,
			}
			for row in rows
		],
		"plan": plan(),
		"choices": {"device_types": labelled(type_labels or {})},
	}


def set_device_enabled(ctx: WebAdminContext, device, enabled, modified):
	"""Deactivate (`enabled` false) or reactivate a device of `ctx.outlet`, never delete it;
	`modified` is the one the devices list answered. A device in use is not deactivated;
	reactivating one past the plan's caps is refused in the cap's words. The caller has checked
	that the user manages the outlet, and answers its devices list after this."""
	flag = checked(parse_flag, enabled, "enabled")
	if flag is None:
		invalid(_("enabled is required"))
	row = (
		frappe.db.get_value(DEVICE_DOCTYPE, device, ["name", "pos_profile", "enabled"], as_dict=True)
		if isinstance(device, str) and device
		else None
	)
	if row is None:
		invalid(_("Device {0} does not exist").format(device))
	if row.pos_profile != ctx.outlet:
		refuse(_("Device {0} is not a device of outlet {1}").format(device, ctx.outlet))
	if not flag and cint(row.enabled):
		device_row = frappe.qb.DocType(DEVICE_DOCTYPE)
		last_seen = (
			frappe.qb.from_(device_row).select(device_row.last_seen).where(device_row.name == row.name).run()
		)[0][0]
		if device_in_use(last_seen, row.name in open_shifts([row.name]), now_datetime()):
			invalid("\n".join(DEVICE_IN_USE_REFUSAL))
	if flag and not cint(row.enabled):
		# locks the device rows until the commit, so it runs before the device's own row is locked
		refusal = device_enable_refusal(row.name)
		if refusal:
			refuse(refusal)
	check_modified(DEVICE_DOCTYPE, row.name, modified)
	if bool(cint(row.enabled)) != flag:
		doc = frappe.get_doc(DEVICE_DOCTYPE, row.name)
		doc.enabled = int(flag)
		doc.save(ignore_permissions=True)
		ctx.log(doc)
		# releases the device rows the plan check locked
		frappe.db.commit()  # nosemgrep
