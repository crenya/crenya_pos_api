"""Who changes an outlet from app.crenya.com, and the errors and small parsers every web-admin
endpoint shares. The signed-in user is checked by the product's endpoint (a System Manager, or a
manager of the outlet); nothing here grants anything."""

from datetime import date, datetime, time, timedelta

import frappe
from frappe import _

from crenya_pos_api.sync.context import DeviceContext, is_system_manager
from crenya_pos_api.utils.dates import format_db_datetime

# where a change from the web was made, as its comment names it
WEB_SOURCE = "app.crenya.com"

__all__ = [
	"WEB_SOURCE",
	"WebAdminContext",
	"check_modified",
	"checked",
	"invalid",
	"is_system_manager",
	"parse_flag",
	"refuse",
	"require_web_session",
	"setting_value",
]


class WebAdminContext:
	"""A change made on app.crenya.com by the signed-in `approver` (no device and no PIN): its
	comments name WEB_SOURCE. `outlet` is the POS Profile changed (its company and currency come
	with it), None for a change of the user alone."""

	source = WEB_SOURCE

	def __init__(self, approver: str, outlet: str | None = None):
		self.approver = approver
		self.outlet = outlet
		self.profile = frappe.get_doc("POS Profile", outlet) if outlet else None
		self.company = self.profile.company if self.profile else None
		# the outlet currency by the device rule (the profile's, else the company's), so a web
		# answer reads amounts as a till does
		self.currency = DeviceContext(device={}, profile=self.profile).currency if self.profile else None

	def log(self, doc):
		doc.add_comment("Info", f"Changed from {self.source} by {self.approver}")


# errors


def refuse(message):
	frappe.throw(message, frappe.PermissionError, title=_("Not allowed"))


def require_web_session():
	"""Refuse a call signed with a key or token rather than a browser session. Staff, PINs and
	roles change only from app.crenya.com, which calls with the signed-in user's own session
	(their `sid` cookie) and never an `Authorization` header. A till or another device signs with
	its user's API key (`token <key>:<secret>`), so any `Authorization` header (token, Basic,
	Bearer) is refused here: neither a device nor a manager PIN typed at one changes an
	account."""
	if frappe.get_request_header("Authorization"):
		# owner decision 2026-10-13: staff and accounts change on app.crenya.com alone
		refuse(_("Staff and accounts are managed on app.crenya.com (Restaurants → Staff, People)"))


def invalid(message):
	frappe.throw(message, frappe.ValidationError, title=_("Check the details"))


def checked(fn, *args, **kwargs):
	"""Run a pure parser; its ValueError becomes a validation error."""
	try:
		return fn(*args, **kwargs)
	except ValueError as e:
		invalid(str(e))


def check_modified(doctype, name, modified):
	"""Refuse (Frappe's TimestampMismatchError and words) a save of a record changed since the
	answer that gave `modified` (on the desk, a till or by another user); the row stays locked
	until the request ends."""
	current = format_db_datetime(frappe.db.get_value(doctype, name, "modified", for_update=True))
	if current != modified:
		frappe.throw(
			_("Error: Document has been modified after you have opened it")
			+ f" ({modified}, {current}). "
			+ _("Please refresh to get the latest document."),
			frappe.TimestampMismatchError,
		)


# parsers


def parse_flag(value, field, default=None):
	"""A true / false answer (1, 0, "true", "false"); `default` when empty; ValueError otherwise."""
	if value is None or value == "":
		return default
	if value in (True, False, 0, 1):
		return bool(value)
	if isinstance(value, str) and value.strip().lower() in ("0", "1", "true", "false"):
		return value.strip().lower() in ("1", "true")
	raise ValueError(f"{field} must be true or false")


def setting_value(value):
	"""A stored value as JSON: a Time as "HH:MM:SS", a Datetime as the database writes it, a Date
	as ISO."""
	if isinstance(value, timedelta):
		seconds = int(value.total_seconds()) % (24 * 3600)
		value = time(seconds // 3600, (seconds // 60) % 60, seconds % 60)
	if isinstance(value, time):
		return value.strftime("%H:%M:%S")
	if isinstance(value, datetime):
		return format_db_datetime(value)
	if isinstance(value, date):
		return value.isoformat()
	return value
