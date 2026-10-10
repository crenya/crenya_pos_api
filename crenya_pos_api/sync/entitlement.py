"""What the workspace's plan allows: caps on NEW device registrations, and the licence state
sent to every device.

Another app (erptrue_app on Crenya workspaces) answers the `crenya_pos_entitlement` hook:

        crenya_pos_entitlement = "my_app.entitlement.get"   # fn() -> dict, never raises

        {"status": "" | "Active" | "Trial" | "Grace" | "Suspended",
         "paid_until": "YYYY-MM-DD" | None,
         "max_outlets": int, "max_terminals": int,          # 0 = unlimited
         "addons": {"restaurant": 0 | 1, "pos": 0 | 1}}

No hook means no restriction (get_entitlement() is None). A hook that cannot be loaded,
raises or answers another shape is logged and treated as no hook: it never blocks a till.
Caps only refuse a NEW device id; devices already registered are never cut off, and
selling never depends on any of this.
"""

import frappe
from frappe.utils import cint

from crenya_pos_api.sync.context import DEFAULT_DEVICE_TYPE
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
	try:
		if frappe.cache.get_value(_ERROR_KEY):
			return
		frappe.cache.set_value(_ERROR_KEY, 1, expires_in_sec=_ERROR_LOG_WINDOW_SECONDS)
	except Exception:
		pass
	frappe.log_error(title=f"{ENTITLEMENT_HOOK}: {path} failed; tills are unrestricted")


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


def registration_refusal(entitlement, device_id, device_type, pos_profile, devices):
	"""Why registering `device_id` as a `device_type` on `pos_profile` is refused, or None.

	`devices`: every Crenya POS Device row (`name`, `device_type`, `pos_profile`, `enabled`).
	A device id already registered (compared ignoring case) is never refused: it is not a new
	terminal. Only enabled terminals count; outlets are their distinct POS Profiles."""
	addon = TERMINAL_ADDONS.get(device_type or DEFAULT_DEVICE_TYPE)
	if entitlement is None or addon is None:
		return None
	wanted = device_id.casefold()
	if any(str(row.get("name") or "").casefold() == wanted for row in devices):
		return None

	if not cint(entitlement["addons"].get(addon)):
		return f"{ADDON_LABELS[addon]} is not switched on for this workspace."

	terminals = [
		row
		for row in devices
		if cint(row.get("enabled")) and (row.get("device_type") or DEFAULT_DEVICE_TYPE) in TERMINAL_ADDONS
	]
	max_terminals = entitlement["max_terminals"]
	if max_terminals and len(terminals) >= max_terminals:
		return f"This plan allows {max_terminals} tills. Ask the owner to upgrade."

	outlets = {row.get("pos_profile") for row in terminals}
	max_outlets = entitlement["max_outlets"]
	if max_outlets and pos_profile not in outlets and len(outlets) >= max_outlets:
		return f"This plan allows {max_outlets} outlets. Ask the owner to upgrade."
	return None


def attach_to_reply(entitlement):
	"""Send the entitlement beside `message` in the reply of a device call (old tills ignore it)."""
	try:
		frappe.local.response["entitlement"] = entitlement
	except (AttributeError, RuntimeError):
		# no request context (console / scheduler)
		pass
