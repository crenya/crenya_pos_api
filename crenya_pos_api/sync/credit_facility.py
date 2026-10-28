"""Customer Credit Facility (0.16.0, House Account phase 2): a restaurant till enables an EXISTING
customer's House Account (credit limit and credit days) under a manager's approval.

The event is security-sensitive (it grants credit), so every refusal is final (a non-retryable
`SyncError`) and nothing is changed unless every check passes. The order, each a refusal:

1. the device is a customer-flag device that may take credit (never a retail "till", a waiter or a
   kds) and the outlet sells on account;
2. the customer is the outlet's, not the walk-in, not an inter-company customer, not disabled
   (`facility_customer_refusal`), and not one the outlet's credit product leaves out (a service
   provider, `excluded_refusal`);
3. the approval's subject is the one this server derives from the payload (`facility_subject`);
4. a resend with the values the customer already has is answered as it is (no save, no refusal; it
   comes after the subject check, so a forged subject is refused whatever the values);
5. every function of the `crenya_pos_credit_facility_checkers` hook accepts (none registered: refused).

Then the customer is switched on, its terms applied (`credit_terms.apply_customer_terms`), the grant
flag set (`credit.GRANT_FLAG` = `TILL_ENABLE_GRANT`, the one thing that lets a device past the
facility editor check on an existing customer) and the Customer saved, with a Comment.

No lock is taken here: the handler's `lock_key` is the customer's, which the event framework holds for
the whole event (a nested `named_lock` would commit the event's open transaction).
"""

from decimal import Decimal
from types import SimpleNamespace

import frappe
from frappe.utils import cint

from crenya_pos_api.sync import registry
from crenya_pos_api.sync.aggregates import AggregateHandler
from crenya_pos_api.sync.context import money_precision
from crenya_pos_api.sync.credit import (
	CREDIT_ENABLED_FIELD,
	GRANT_FLAG,
	NOT_FOUND,
	SWITCHED_OFF,
	TILL_ENABLE_GRANT,
	_credit_on,
	customer_in_outlet,
	is_credit_device,
	is_customer_flag_device,
)
from crenya_pos_api.sync.credit_terms import (
	UNSET,
	apply_customer_terms,
	current_terms,
	parse_days,
	parse_limit,
	terms_audit_text,
)
from crenya_pos_api.sync.errors import PERMISSION, VALIDATION, SyncError
from crenya_pos_api.sync.events import customer_lock_key
from crenya_pos_api.sync.validation import (
	AGGREGATE_CREDIT_FACILITY,
	MAX_ID_LENGTH,
	_fail,
	_required_str,
)
from crenya_pos_api.utils.dates import format_db_datetime, parse_iso_utc
from crenya_pos_api.utils.decimal import DecimalParseError, format_money, parse_decimal

APPROVAL_ACTION = "credit_facility"
SUBJECT_PREFIX = "credit_terms:"
# the till's deterministic event key (crenya_restaurant `house_account.facility_local_id`): one per customer
LOCAL_ID_PREFIX = "credit-facility:"
NO_CHECKER = "No app accepts a House Account grant here"
WRONG_SUBJECT = "The approval was for other details"


def plain_number(value) -> str:
	"""A decimal as the till writes it in an approval subject (pure): no trailing zeros, no exponent
	(`Decimal("50.000")` is "50", `Decimal("0.500")` is "0.5", 0 is "0")."""
	return format(Decimal(value).normalize(), "f")


def facility_subject(customer, limit, days) -> str:
	"""What a manager approves, as the till words it (pure): `credit_terms:{customer}|{limit}|{days}`.
	`limit` (Decimal, or None) and `days` (int, or None) read "-" only when None: 0 is "0"."""
	shown_limit = "-" if limit is None else plain_number(limit)
	shown_days = "-" if days is None else str(int(days))
	return f"{SUBJECT_PREFIX}{customer}|{shown_limit}|{shown_days}"


def validate_credit_facility_payload(payload) -> dict:
	"""The raw payload as `apply_credit_facility` takes it (pure; SyncError VALIDATION otherwise):
	`local_id`, `customer` (an ERP name), the terms (`credit_limit` / `credit_days` as the Customer
	event's `credit_facility`: an absent key stays absent, null is cleared) and the `approval`
	{action "credit_facility", subject, approved_by, approved_at (ISO-8601 UTC)}.

	`credit_limit` is the validated decimal STRING or None (the outlet's precision is applied at
	`apply`); `subject_limit` is the number the subject is written from (0 stays 0, not "no limit")."""
	data = {
		"local_id": _required_str(payload, "local_id"),
		"customer": _required_str(payload, "customer"),
	}
	if data["local_id"] != LOCAL_ID_PREFIX + data["customer"]:
		_fail(f"local_id must be {LOCAL_ID_PREFIX}{{customer}}")
	if "credit_limit" in payload:
		raw = payload["credit_limit"]
		try:
			limit = parse_limit(raw, 3)
		except ValueError as e:
			_fail(f"credit_limit: {e}")
		data["credit_limit"] = None if limit is None else str(raw).strip()
		try:
			data["subject_limit"] = parse_decimal(raw, "credit_limit", allow_none=True)
		except DecimalParseError as e:
			_fail(f"credit_limit: {e}")
	if "credit_days" in payload:
		try:
			data["credit_days"] = parse_days(payload["credit_days"])
		except ValueError as e:
			_fail(f"credit_days: {e}")
	approval = payload.get("approval")
	if not isinstance(approval, dict):
		_fail("approval is required")
	if approval.get("action") != APPROVAL_ACTION:
		_fail(f"approval.action must be {APPROVAL_ACTION!r}")
	data["approval"] = {
		"action": APPROVAL_ACTION,
		"subject": _required_str(approval, "subject", "approval.subject", max_length=MAX_ID_LENGTH * 2),
		"approved_by": _required_str(approval, "approved_by", "approval.approved_by"),
		"approved_at": _required_str(approval, "approved_at", "approval.approved_at"),
	}
	try:
		parse_iso_utc(data["approval"]["approved_at"])
	except ValueError:
		_fail("approval.approved_at must be an ISO-8601 UTC timestamp, e.g. 2026-10-28T08:15:30Z")
	return data


def _subject_of(data) -> str:
	return facility_subject(data["customer"], data.get("subject_limit"), data.get("credit_days"))


def _refuse(message, code=VALIDATION):
	raise SyncError(code, message, retryable=False)


def facility_customer_refusal(ctx, customer) -> str | None:
	"""Why `customer` (a saved Customer document of the device's outlet or not) cannot be put on a
	House Account from this outlet, or None: not the outlet's (one answer for a customer that is not
	there and one outside its customer groups: nothing is told about other customers), the walk-in,
	an inter-company customer (it represents a company), or disabled."""
	if not customer or not customer.get("name"):
		return NOT_FOUND
	if customer.name == ctx.profile.get("customer"):
		return "The walk-in customer cannot have a House Account"
	if not customer_in_outlet(ctx, customer.name):
		return NOT_FOUND
	if cint(customer.get("is_internal_customer")) or customer.get("represents_company"):
		return "A customer that represents a company cannot have a House Account"
	if cint(customer.get("disabled")):
		return "A disabled customer cannot have a House Account"
	return None


def _excluded_refusal(ctx, customer) -> str | None:
	"""The refusal of the outlet's credit product (a restaurant's service providers), or None."""
	from crenya_pos_api.api.web_admin.credit import excluded_refusal
	from crenya_pos_api.api.web_admin.scope import outlet_product

	product_ctx = SimpleNamespace(
		company=ctx.company, outlet=ctx.profile.name, credit_product=outlet_product(ctx.profile.name)
	)
	return excluded_refusal(product_ctx, customer)


def _already_so(doc, company, terms) -> bool:
	"""Whether the customer already has the House Account on with exactly the `terms` asked for
	(a part left out is not compared)."""
	if not cint(doc.get(CREDIT_ENABLED_FIELD)):
		return False
	limit, days = current_terms(doc, company)
	if "limit" in terms and terms["limit"] != limit:
		return False
	return not ("days" in terms and terms["days"] != days)


def _wanted_limit(ctx, data):
	"""The limit the payload asks for at the outlet's precision (Decimal, or None: no limit row)."""
	try:
		return parse_limit(data["credit_limit"], money_precision(ctx.currency))
	except ValueError as e:
		raise SyncError(VALIDATION, f"credit_limit: {e}", retryable=False)


def apply_credit_facility(ctx, data, notes):
	"""Enable the House Account of an existing customer (see the module); returns the saved
	Customer. Every refusal is a non-retryable `SyncError`."""
	if not (is_credit_device(ctx.device_type) and is_customer_flag_device(ctx.device_type)):
		_refuse("This device cannot enable a House Account", PERMISSION)
	if not _credit_on(ctx):
		_refuse(SWITCHED_OFF)
	name = data["customer"]
	doc = frappe.get_doc("Customer", name) if frappe.db.exists("Customer", name) else None
	refusal = facility_customer_refusal(ctx, doc)
	if refusal:
		_refuse(refusal)
	refusal = _excluded_refusal(ctx, name)
	if refusal:
		_refuse(refusal)
	terms = {}
	if "credit_limit" in data:
		terms["limit"] = _wanted_limit(ctx, data)
	if "credit_days" in data:
		terms["days"] = data["credit_days"]
	if data["approval"]["subject"] != _subject_of(data):
		_refuse(WRONG_SUBJECT, PERMISSION)

	# a resend: answered as it is (after the subject check: a forged subject is refused even for the
	# values the customer has), before the checkers are asked again
	if _already_so(doc, ctx.company, terms):
		notes.append("House Account already enabled with these values; nothing changed")
		return doc

	checker_notes = []
	checkers = registry.credit_facility_checkers()
	if not checkers:
		_refuse(NO_CHECKER, PERMISSION)
	for checker in checkers:
		reason = checker(ctx, doc, data, checker_notes)
		if reason is None:
			continue
		if not isinstance(reason, str) or not reason.strip():
			_refuse(f"{registry.CREDIT_FACILITY_CHECKER_HOOK}: a checker must return None or a reason", PERMISSION)
		_refuse(reason.strip(), PERMISSION)
	notes.extend(checker_notes)

	doc.set(CREDIT_ENABLED_FIELD, 1)
	change = apply_customer_terms(
		doc, ctx.company, limit=terms.get("limit", UNSET), days=terms.get("days", UNSET)
	)
	doc.flags[GRANT_FLAG] = TILL_ENABLE_GRANT
	# ERPNext's own checks speak here (a limit below what the customer owes is refused, its words kept)
	doc.save(ignore_permissions=True, ignore_version=False)
	text = "; ".join(checker_notes) or terms_audit_text(
		ctx.company,
		change,
		f"the till {_device_label(ctx)}",
		data["approval"]["approved_by"],
		money_precision(ctx.currency),
	)
	# a Comment is shown as HTML and the text holds the device name and the approver's name
	doc.add_comment("Info", frappe.utils.escape_html(text))
	return doc


def _device_label(ctx):
	name = (ctx.device or {}).get("device_name")
	return f"{name} ({ctx.device_id})" if name else ctx.device_id


def facility_result(ctx, doc) -> dict:
	"""The `facility` result field: the customer's House Account as it stands."""
	limit, days = current_terms(doc, ctx.company)
	precision = money_precision(ctx.currency)
	return {
		"customer": doc.name,
		"credit_enabled": bool(cint(doc.get(CREDIT_ENABLED_FIELD))),
		"credit_limit": None if limit is None else format_money(limit, precision),
		"credit_days": days,
	}


class CreditFacilityHandler(AggregateHandler):
	"""Enable an existing customer's House Account from a till (manager approval)."""

	aggregate_type = AGGREGATE_CREDIT_FACILITY
	doctype = "Customer"
	operations = ("submit",)

	def validate(self, operation, payload):
		return validate_credit_facility_payload(payload)

	def apply(self, ctx, operation, data, notes):
		doc = apply_credit_facility(ctx, data, notes)
		# read back by result_fields, which has no device context
		doc.flags.crenya_facility = facility_result(ctx, doc)
		return doc

	def lock_key(self, payload):
		"""Events for one customer run one after another (the key of its receipts and credit returns)."""
		customer = payload.get("customer")
		return customer_lock_key(customer) if customer and isinstance(customer, str) else None

	def result_fields(self, doc):
		return {
			"doctype": "Customer",
			"name": doc.name,
			"docstatus": cint(doc.get("docstatus")),
			"modified": format_db_datetime(doc.get("modified")),
			"totals": None,
			"fawtara_status": None,
			"facility": doc.flags.get("crenya_facility"),
		}


__all__ = [
	"AGGREGATE_CREDIT_FACILITY",
	"CreditFacilityHandler",
	"apply_credit_facility",
	"facility_customer_refusal",
	"facility_subject",
	"validate_credit_facility_payload",
]
