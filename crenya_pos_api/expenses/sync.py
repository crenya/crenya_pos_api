"""The till's expenses through sync: the `POS Expense` push aggregate (moved down from
crenya_restaurant, 0.13.0; the booking is `expenses.booking`). One handler for both products: the
device's type picks the product (`product.product_for`), which words the messages and says who
approves.

Push (`aggregate_type` "POS Expense", `local_id` the expense's; money as decimal strings at the
currency's decimals):

| operation | needs | does |
| --- | --- | --- |
| submit | approved_by when the outlet asks a PIN to add | creates the POS Expense (log `add`) and books it; the same local_id again answers the expense it made (another outlet's is refused) |
| amend | approved_by, a manager of the outlet | refused once Cancelled, or when `edited_at` is after its shift's close; log `edit` (before and after), then the PI is cancelled and amended |
| cancel | approved_by, reason (3 to 140) | the same checks; log `delete`, the PI cancelled, the row Cancelled (never deleted) |

payload: local_id, pos_profile, company, shift_local_id, business_date (YYYY-MM-DD), user,
approved_by, edited_at (ISO-8601 UTC), supplier, payee_name, bill_no, bill_date, mode_of_payment,
currency, lines [{category, description, amount}], tax_template, taxes [{account_head, rate,
amount}], net, tax, total, notes, reason. The outlet and company are the device's, never the
payload's. A booking that fails keeps the expense `Not booked`: the event still succeeds (the
cash has left the drawer).

Result: core's fields plus `expense` {status, purchase_invoice, posting_error}.
"""

import re
from datetime import date, datetime, timezone

import frappe
from frappe.utils import cint, get_datetime, get_system_timezone

from crenya_pos_api.expenses import booking
from crenya_pos_api.expenses.product import ExpenseProduct, product_for
from crenya_pos_api.sync.aggregates import AggregateHandler
from crenya_pos_api.sync.errors import (
	DEPENDENCY_MISSING,
	INTERNAL,
	PERMISSION,
	VALIDATION,
	SyncError,
)
from crenya_pos_api.utils.dates import format_db_datetime, to_site_naive
from crenya_pos_api.utils.decimal import DecimalParseError, parse_decimal

AGGREGATE_EXPENSE = booking.EXPENSE
OPERATIONS = ("submit", "amend", "cancel")
MAX_EXPENSE_LINES = 50
MAX_TAX_ROWS = 10
MIN_REASON_LENGTH = 3
MAX_ID_LENGTH = 140
MAX_NAME_LENGTH = 140
MAX_NOTES_LENGTH = 500
MAX_REASON_LENGTH = 140
SHIFT = "Crenya POS Shift"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,139}$")


# errors


def invalid(message):
	raise SyncError(VALIDATION, message)


def missing(message):
	raise SyncError(DEPENDENCY_MISSING, message)


def forbidden(message):
	raise SyncError(PERMISSION, message)


# primitives (the wording of the restaurant's, which this replaces)


def _decimal(value, field):
	try:
		return parse_decimal(value, field)
	except DecimalParseError as e:
		invalid(str(e))


def _id(data, key, field=None):
	field = field or key
	value = data.get(key)
	if value is None or (isinstance(value, str) and not value.strip()):
		invalid(f"{field} is required")
	if not isinstance(value, str) or not _ID_RE.match(value.strip()):
		invalid(f"{field} must be an id of letters, digits, '.', '_', ':' or '-' (at most {MAX_ID_LENGTH})")
	return value.strip()


def _str(data, key, field=None, max_length=MAX_NAME_LENGTH, required=False):
	field = field or key
	value = data.get(key)
	if value is None or (isinstance(value, str) and not value.strip()):
		if required:
			invalid(f"{field} is required")
		return None
	if not isinstance(value, str):
		invalid(f"{field} must be a string")
	value = value.strip()
	if len(value) > max_length:
		invalid(f"{field} is longer than {max_length} characters")
	return value


def _time(data, key):
	value = data.get(key)
	if value is None or value == "":
		return None
	if not isinstance(value, str) or not value.strip():
		invalid(f"{key} must be an ISO-8601 time")
	text = value.strip()
	if text.endswith(("Z", "z")):
		text = text[:-1] + "+00:00"
	try:
		moment = datetime.fromisoformat(text)
	except ValueError:
		invalid(f"{key} is not a valid ISO-8601 time: {value!r}")
	if moment.tzinfo is None:
		invalid(f"{key} must carry a time zone (Z or +hh:mm)")
	return moment.astimezone(timezone.utc)


def _date(data, key, required=False):
	value = data.get(key)
	if value is None or value == "":
		if required:
			invalid(f"{key} is required")
		return None
	text = value.strip() if isinstance(value, str) else ""
	if len(text) == 10:
		try:
			return date.fromisoformat(text)
		except ValueError:
			pass
	invalid(f"{key} must be a date YYYY-MM-DD")


def _list(value, field, max_items, required=False):
	if value is None:
		if required:
			invalid(f"{field} is required")
		return None
	if not isinstance(value, list):
		invalid(f"{field} must be a list")
	if required and not value:
		invalid(f"{field} must not be empty")
	if len(value) > max_items:
		invalid(f"{field} may have at most {max_items} entries")
	return value


def _object(value, field):
	if not isinstance(value, dict):
		invalid(f"{field} must be an object")
	return value


# the payload


def _lines(value):
	rows = _list(value, "lines", MAX_EXPENSE_LINES, required=True)
	lines = []
	for index, raw in enumerate(rows):
		label = f"lines[{index}]"
		row = _object(raw, label)
		amount = _decimal(row.get("amount"), f"{label}.amount")
		if amount <= 0:
			invalid(f"{label}.amount must be more than 0")
		lines.append(
			{
				"category": _str(row, "category", f"{label}.category", required=True),
				"description": _str(row, "description", f"{label}.description"),
				"amount": amount,
			}
		)
	return lines


def _taxes(value):
	rows = _list(value, "taxes", MAX_TAX_ROWS) or []
	taxes = []
	for index, raw in enumerate(rows):
		label = f"taxes[{index}]"
		row = _object(raw, label)
		rate = _decimal(row.get("rate"), f"{label}.rate")
		amount = _decimal(row.get("amount"), f"{label}.amount")
		if rate < 0 or amount < 0:
			invalid(f"{label} cannot be negative")
		taxes.append(
			{
				"account_head": _str(row, "account_head", f"{label}.account_head", required=True),
				"rate": rate,
				"amount": amount,
			}
		)
	return taxes


def validate_expense_payload(operation, payload):
	"""The checked event (pure): decimals as Decimal, dates as dates, edited_at aware UTC."""
	if operation not in OPERATIONS:
		invalid(f"operation {operation!r} is not supported for {AGGREGATE_EXPENSE}")
	payload = _object(payload, "payload")
	data = {
		"local_id": _id(payload, "local_id"),
		# read for the record only: the device's outlet and company decide
		"pos_profile": _str(payload, "pos_profile"),
		"company": _str(payload, "company"),
		"shift_local_id": _id(payload, "shift_local_id"),
		"business_date": _date(payload, "business_date", required=True),
		"user": _str(payload, "user", required=True),
		"approved_by": _str(payload, "approved_by"),
		"edited_at": _time(payload, "edited_at"),
		"supplier": _str(payload, "supplier"),
		"payee_name": _str(payload, "payee_name"),
		"bill_no": _str(payload, "bill_no"),
		"bill_date": _date(payload, "bill_date"),
		"mode_of_payment": _str(payload, "mode_of_payment", required=True),
		"currency": _str(payload, "currency", required=True),
		"lines": _lines(payload.get("lines")),
		"tax_template": _str(payload, "tax_template"),
		"taxes": _taxes(payload.get("taxes")),
		"net": _decimal(payload.get("net"), "net"),
		"tax": _decimal(payload.get("tax"), "tax"),
		"total": _decimal(payload.get("total"), "total"),
		"notes": _str(payload, "notes", max_length=MAX_NOTES_LENGTH),
		"reason": _str(payload, "reason", max_length=MAX_REASON_LENGTH),
	}
	if data["edited_at"] is None:
		invalid("edited_at is required")
	if data["total"] <= 0 or data["net"] < 0 or data["tax"] < 0:
		invalid("total must be more than 0, net and tax at least 0")
	if operation != "submit" and not data["approved_by"]:
		invalid(
			f"approved_by is required: an expense's {'edit' if operation == 'amend' else 'delete'} needs a manager PIN"
		)
	if operation == "cancel" and (not data["reason"] or len(data["reason"]) < MIN_REASON_LENGTH):
		invalid(
			f"reason is required to delete an expense ({MIN_REASON_LENGTH} to {MAX_REASON_LENGTH} characters)"
		)
	return data


# who approves


def approver_allowed(roles, user, allowed_users, approver_roles):
	"""Whether a user with `roles` approves at an outlet whose Applicable for Users is
	`allowed_users` (pure). The device names the approver (its manager PIN check is offline), so the
	server keeps it to a holder of one of the product's approver roles who works at this outlet
	(every user when the table is empty). A System Manager is no exception unless the product
	lists that role: one who is not on the outlet's list never reaches its devices."""
	roles = set(roles or ())
	if not any(role in roles for role in approver_roles):
		return False
	return not allowed_users or user in allowed_users


def check_approver(ctx, approver, product: ExpenseProduct):
	"""Refuses (`permission`) an `approved_by` that is not an enabled user who approves at the
	device's outlet: the device checked the PIN offline, the server the role."""
	if not approver or not cint(frappe.db.get_value("User", approver, "enabled")):
		forbidden(f"approved_by {approver or '(none)'} is not an enabled user")
	allowed = approver_allowed(
		frappe.get_roles(approver),
		approver,
		product.outlet_users(ctx.profile),
		product.approver_roles(ctx),
	)
	if not allowed:
		forbidden(f"{approver} is not a manager of outlet {ctx.profile.name}")


def till_user(user, notes):
	"""The till's user when an enabled user, else the device's session user (noted)."""
	if cint(frappe.db.get_value("User", user, "enabled")):
		return user
	notes.append(f"Till user {user} is not an enabled user; recorded as {frappe.session.user}")
	return frappe.session.user


# the handler


def _check_masters(ctx, data, precision):
	profile = ctx.profile
	if data["currency"] != ctx.currency:
		invalid(f"currency {data['currency']} is not the outlet's {ctx.currency}")
	modes = {row.mode_of_payment for row in profile.get("payments") or []}
	if data["mode_of_payment"] not in modes:
		invalid(f"Mode of payment {data['mode_of_payment']} is not enabled on outlet {profile.name}")
	if data["supplier"] and not booking.supplier_allowed(profile, data["supplier"]):
		invalid(f"Supplier {data['supplier']} is not an enabled expense supplier of outlet {profile.name}")
	for line in data["lines"]:
		if not frappe.db.exists(booking.CATEGORY, line["category"]):
			invalid(f"Expense category {line['category']} does not exist")
	template = data["tax_template"]
	heads = None
	if template:
		if template != profile.get(booking.TAX_TEMPLATE_FIELD):
			invalid(f"tax_template {template} is not the outlet's expense VAT template")
		rows = booking.template_rows(template, ctx.company)
		if not rows:
			invalid(f"tax_template {template} is not an enabled template of {ctx.company}")
		heads = [row.account_head for row in rows]
	problem = booking.split_check(data, heads, precision)
	if problem:
		invalid(problem)


def _values(data):
	return {
		"supplier": data["supplier"],
		"payee_name": data["payee_name"],
		"bill_no": data["bill_no"],
		"bill_date": data["bill_date"],
		"mode_of_payment": data["mode_of_payment"],
		"currency": data["currency"],
		"category": data["lines"][0]["category"],
		"amount": data["total"],
		"net_amount": data["net"],
		"tax_amount": data["tax"],
		"taxes_and_charges": data["tax_template"],
		"description": data["notes"],
	}


def _set_lines(doc, data):
	doc.set("lines", [])
	for line in data["lines"]:
		doc.append(
			"lines",
			{"category": line["category"], "description": line["description"], "amount": line["amount"]},
		)


def _edited_at(data):
	return to_site_naive(data["edited_at"], get_system_timezone())


def _check_open(doc, data):
	"""An edit or delete made after its shift's close is refused (Ruling 11): the drawer was counted."""
	closed_at = frappe.db.get_value(SHIFT, {"local_id": doc.shift}, "closed_at") if doc.shift else None
	if closed_at and _edited_at(data) > get_datetime(closed_at):
		invalid(
			"This expense's day is closed: it was counted at the day close. Record a new expense instead."
		)


def _existing(ctx, data):
	name = frappe.db.get_value(AGGREGATE_EXPENSE, {"local_id": data["local_id"]}, "name")
	if not name:
		missing(f"Expense {data['local_id']} has not reached the server yet")
	doc = frappe.get_doc(AGGREGATE_EXPENSE, name)
	if doc.outlet != ctx.profile.name:
		forbidden(f"Expense {data['local_id']} belongs to another outlet")
	return doc


def submit(ctx, data, notes, precision, product):
	if cint(ctx.profile.get(booking.ADD_PIN_FIELD)) or data["approved_by"]:
		check_approver(ctx, data["approved_by"], product)
	user = till_user(data["user"], notes)
	doc = frappe.new_doc(AGGREGATE_EXPENSE)
	doc.update(_values(data))
	doc.update(
		{
			"local_id": data["local_id"],
			"company": ctx.company,
			"outlet": ctx.profile.name,
			"device": ctx.device_id,
			"shift": data["shift_local_id"],
			"expense_date": data["business_date"],
			"user": user,
			"approved_by": data["approved_by"],
			"status": booking.NOT_BOOKED,
		}
	)
	_set_lines(doc, data)
	booking.add_log(
		doc,
		booking.ADD,
		user=user,
		approver=data["approved_by"],
		device=ctx.device_id,
		after=booking.snapshot(data, precision),
		at=_edited_at(data),
	)
	# the device was authorised by get_device_context; nobody writes these in the desk
	doc.insert(ignore_permissions=True)
	booking.book(doc, notes, product=product)
	return doc


def amend(ctx, data, notes, precision, product):
	doc = _existing(ctx, data)
	if doc.status == booking.CANCELLED:
		invalid("This expense was deleted; it cannot be changed")
	_check_open(doc, data)
	check_approver(ctx, data["approved_by"], product)
	doc.update(_values(data))
	_set_lines(doc, data)
	doc.approved_by = data["approved_by"]
	booking.add_log(
		doc,
		booking.EDIT,
		user=till_user(data["user"], notes),
		approver=data["approved_by"],
		device=ctx.device_id,
		before=booking.last_snapshot(doc),
		after=booking.snapshot(data, precision),
		at=_edited_at(data),
	)
	doc.save(ignore_permissions=True)
	booking.rebook(doc, notes, product=product)
	return doc


def cancel(ctx, data, notes, precision, product):
	doc = _existing(ctx, data)
	if doc.status == booking.CANCELLED:
		notes.append("Already deleted")
		return doc
	_check_open(doc, data)
	check_approver(ctx, data["approved_by"], product)
	booking.add_log(
		doc,
		booking.DELETE,
		user=till_user(data["user"], notes),
		approver=data["approved_by"],
		device=ctx.device_id,
		before=booking.last_snapshot(doc),
		reason=data["reason"],
		at=_edited_at(data),
	)
	doc.save(ignore_permissions=True)
	booking.unbook(doc, notes)
	return doc


APPLY = {"submit": submit, "amend": amend, "cancel": cancel}


def product_of_device(ctx) -> ExpenseProduct:
	"""The product of the device that pushes (refused for a kitchen display or waiter phone)."""
	product = product_for(ctx.device_type)
	if product is None:
		forbidden(f"Expenses are recorded on a till, not on a {ctx.device_type} device")
	return product


def apply_expense(ctx, operation, data, notes):
	if not booking.expenses_supported():
		invalid(booking.UNSUPPORTED)
	product = product_of_device(ctx)
	for key, own in (("pos_profile", ctx.profile.name), ("company", ctx.company)):
		if data[key] and data[key] != own:
			notes.append(f"Payload {key} {data[key]} ignored: the device's is {own}")
	precision = booking.currency_precision(ctx.currency)
	existing = frappe.db.get_value(
		AGGREGATE_EXPENSE, {"local_id": data["local_id"]}, ["name", "outlet"], as_dict=True
	)
	if operation == "submit" and existing:
		if existing.outlet != ctx.profile.name:
			forbidden(f"Expense {data['local_id']} belongs to another outlet")
		# sent again under a new event_id (the till never saw the answer): the expense it made
		notes.append(f"Matched existing expense {existing.name} by local_id")
		return frappe.get_doc(AGGREGATE_EXPENSE, existing.name)
	if operation != "cancel":
		# a delete books nothing new: the outlet's settings changing since never block it
		_check_masters(ctx, data, precision)
	try:
		return APPLY[operation](ctx, data, notes, precision, product)
	except frappe.TimestampMismatchError:
		# the expense or its invoice changed meanwhile (a manager's retry): the till sends it again
		raise SyncError(INTERNAL, "The expense changed on the server meanwhile, retry", retryable=True)


def expense_result(doc):
	return {
		"doctype": doc.doctype,
		"name": doc.name,
		"docstatus": 0,
		"modified": format_db_datetime(doc.get("modified")),
		"totals": None,
		"fawtara_status": None,
		"expense": {
			"status": doc.get("status") or None,
			"purchase_invoice": doc.get("purchase_invoice") or None,
			"posting_error": doc.get("posting_error") or None,
		},
	}


class ExpenseAggregate(AggregateHandler):
	"""An expense paid out of the till's drawer: added, edited, deleted (see the module docstring)."""

	aggregate_type = AGGREGATE_EXPENSE
	doctype = AGGREGATE_EXPENSE
	operations = OPERATIONS

	def validate(self, operation, payload):
		return validate_expense_payload(operation, payload)

	def apply(self, ctx, operation, data, notes):
		return apply_expense(ctx, operation, data, notes)

	def find_by_local_id(self, local_id):
		# submit, amend and cancel share the local_id, and core does not say which operation
		# asks: only event_id idempotency applies, and `submit` answers the expense it made
		return None

	def result_fields(self, doc):
		return expense_result(doc)
