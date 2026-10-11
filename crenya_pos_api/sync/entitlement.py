"""What the workspace's plan allows: caps on NEW device registrations and on enabling a
disabled device again, and the licence state sent to every device.

Another app (erptrue_app on Crenya workspaces) answers the `crenya_pos_entitlement` hook:

        crenya_pos_entitlement = "my_app.entitlement.get"   # fn() -> dict, never raises

        {"status": "" | "Active" | "Trial" | "Grace" | "Suspended",
         "paid_until": "YYYY-MM-DD" | None,
         "max_outlets": int, "max_terminals": int,          # 0 = unlimited
         "addons": {"restaurant": 0 | 1, "pos": 0 | 1}}

No hook means no restriction (get_entitlement() is None). A hook that cannot be loaded,
raises or answers another shape is logged and treated as no hook: it never blocks a till.
Caps only refuse a NEW terminal (or a registered one moving to an outlet past the cap, or a
disabled one enabled again past the cap); devices already enabled are never cut off, and
selling never depends on any of this.
"""

import frappe
from frappe.utils import cint

from crenya_pos_api.sync.context import DEFAULT_DEVICE_TYPE, DEVICE_DOCTYPE
from crenya_pos_api.sync.registry import ENTITLEMENT_HOOK, entitlement_hook_path

# device types that are terminals (counted by the caps) and the add-on each one needs;
# other types (kds, waiter, ...) are never counted and never refused
TERMINAL_ADDONS = {"till": "pos", "restaurant_pos": "restaurant"}
ADDON_LABELS = {"pos": "New POS (Tauri)", "restaurant": "New Restaurant (Tauri)"}
ADDONS = ("restaurant", "pos")

# one Error Log per site and window while the hook is broken (tills call every few seconds)
_ERROR_KEY = "crenya_pos_entitlement_error"
_ERROR_LOG_WINDOW_SECONDS = 600


def _log_hook_error(path):
	"""Record a broken hook once per window; never raises (a device call must go on)."""
	try:
		if frappe.cache.get_value(_ERROR_KEY):
			return
		frappe.cache.set_value(_ERROR_KEY, 1, expires_in_sec=_ERROR_LOG_WINDOW_SECONDS)
	except Exception:
		pass
	try:
		frappe.log_error(title=f"{ENTITLEMENT_HOOK}: {path} failed; tills are unrestricted")
	except Exception:
		pass


def _normalise(value):
	"""The hook's answer in the contract's shape; ValueError when it is not that shape."""
	if not isinstance(value, dict) or not isinstance(value.get("addons"), dict):
		raise ValueError(f"expected a dict with addons, got {type(value).__name__}")
	missing = [addon for addon in ADDONS if addon not in value["addons"]]
	if missing:
		# a missing switch must not read as "off" and refuse every new till
		raise ValueError(f"addons has no {', '.join(missing)}")
	paid_until = value.get("paid_until")
	return {
		"status": str(value.get("status") or ""),
		"paid_until": str(paid_until) if paid_until else None,
		"max_outlets": max(cint(value.get("max_outlets")), 0),
		"max_terminals": max(cint(value.get("max_terminals")), 0),
		"addons": {addon: 1 if cint(value["addons"].get(addon)) else 0 for addon in ADDONS},
	}


def get_entitlement():
	"""The workspace's entitlement dict, or None (no hook, or a broken one): unrestricted."""
	path = None
	try:
		path = entitlement_hook_path()
		if not path:
			return None
		return _normalise(frappe.get_attr(path)())
	except Exception:
		_log_hook_error(path)
		return None


def device_rows(for_update=False):
	"""Every Crenya POS Device row the caps read: `name`, `pos_profile`, `enabled` and, once the
	column exists, `device_type`.

	`for_update`: a locking read. Under MariaDB's REPEATABLE READ a plain read sees the
	transaction's snapshot, which may predate a device enabled by a request we waited for; a
	locking read sees the latest committed rows and holds them until the caller commits, so a
	concurrent caller doing the same waits and then counts this one."""
	device = frappe.qb.DocType(DEVICE_DOCTYPE)
	columns = [device.name, device.pos_profile, device.enabled]
	if frappe.get_meta(DEVICE_DOCTYPE).has_field("device_type"):
		columns.append(device.device_type)
	query = frappe.qb.from_(device).select(*columns)
	if for_update:
		query = query.for_update()
	return query.run(as_dict=True)


def _is_terminal(row):
	return (row.get("device_type") or DEFAULT_DEVICE_TYPE) in TERMINAL_ADDONS


def registration_refusal(entitlement, device_id, device_type, pos_profile, devices):
	"""Why registering `device_id` as a `device_type` on `pos_profile` is refused, or None.

	`devices`: every Crenya POS Device row (`name`, `device_type`, `pos_profile`, `enabled`).
	Only enabled terminals count; outlets are their distinct POS Profiles.

	- A NEW terminal (no row for the id, compared ignoring case) needs its add-on on, a free
	  terminal slot and, on a POS Profile not counted yet, a free outlet slot.
	- A registered terminal re-registers at its current profile whatever the caps; moving to
	  a profile no other terminal uses needs a free outlet slot.
	- A registered non-terminal (kds, waiter, ...) becoming a terminal is checked as a new one.
	- A disabled device is left to register(), which refuses it."""
	addon = TERMINAL_ADDONS.get(device_type or DEFAULT_DEVICE_TYPE)
	if entitlement is None or addon is None:
		return None
	wanted = device_id.casefold()
	own = next((row for row in devices if str(row.get("name") or "").casefold() == wanted), None)
	if own is not None and not cint(own.get("enabled")):
		return None

	terminals = [row for row in devices if row is not own and cint(row.get("enabled")) and _is_terminal(row)]
	outlets = {row.get("pos_profile") for row in terminals}
	max_outlets = entitlement["max_outlets"]
	outlet_refusal = (
		f"This plan allows {max_outlets} outlets. Ask the owner to upgrade."
		if max_outlets and pos_profile not in outlets and len(outlets) >= max_outlets
		else None
	)

	if own is not None and _is_terminal(own):
		return outlet_refusal if pos_profile != own.get("pos_profile") else None

	if not cint(entitlement["addons"].get(addon)):
		return f"{ADDON_LABELS[addon]} is not switched on for this workspace."
	max_terminals = entitlement["max_terminals"]
	if max_terminals and len(terminals) >= max_terminals:
		return f"This plan allows {max_terminals} tills. Ask the owner to upgrade."
	return outlet_refusal


def device_enable_refusal(device_id):
	"""Why enabling the device `device_id` (any case) again is refused, or None.

	It is checked as a NEW terminal of its stored type and POS Profile (registration_refusal
	with its own row left out): its add-on must be on, a terminal slot free and, on a POS
	Profile no other enabled terminal uses, an outlet slot free. None without an entitlement
	(no hook: no caps), for a non-terminal type (kds, waiter, ...) and for an unknown id.

	The device rows are read with a locking read and stay locked until the caller commits, so
	two devices enabled at once cannot both take the last slot. Call it before locking the
	device's own row, then set `enabled` and commit."""
	entitlement = get_entitlement()
	if entitlement is None:
		return None
	devices = device_rows(for_update=True)
	wanted = str(device_id or "").casefold()
	own = next((row for row in devices if str(row.get("name") or "").casefold() == wanted), None)
	if own is None:
		return None
	others = [row for row in devices if row is not own]
	return registration_refusal(
		entitlement, own["name"], own.get("device_type"), own.get("pos_profile"), others
	)


def attach_to_reply(entitlement):
	"""Send the entitlement beside `message` in the reply of a device call (old tills ignore it)."""
	try:
		frappe.local.response["entitlement"] = entitlement
	except (AttributeError, RuntimeError):
		# no request context (console / scheduler)
		pass
