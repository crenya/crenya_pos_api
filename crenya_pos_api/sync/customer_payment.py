"""A customer's payment taken at the till (aggregate `Customer Payment`, retail P1 task 8), booked as
one ERPNext Payment Entry (Receive).

Payload (money as a decimal string at the currency's decimals; all keys the till sends):
local_id, pos_profile, company, shift_local_id, business_date (YYYY-MM-DD), cashier,
customer_local_id (a customer made at the till) or customer (a name), mode_of_payment, amount,
currency, reference, note, received_at (ISO-8601 UTC). The outlet and company are the device's.
Optional (0.14.0, capability `credit_settle_invoices`): `invoices`, the bills the cashier ticked in
the order ticked (an ERPNext name, or `local:` + the till's id of a sale it made), and
`advance_confirmed`, which only goes into the remarks (a receipt is never refused for it).

Without `invoices` (every till before 0.14.0) the Payment Entry is allocated to the customer's
submitted, outstanding Sales Invoices of the company (not returns), `posting_date` then `creation`
oldest first, each up to its outstanding amount. With them, only to the ticked bills, re-read at
booking and in the order given; a ticked bill that is no longer outstanding, not the customer's or
the company's, a return, or a `local:` id no Sales Invoice has yet is skipped with a note (never a
refusal). What is left stays unallocated, an advance on the customer's account, never spread over
bills that were not ticked. The result adds `payment` {allocated, unallocated, invoices}, and
`skipped` for a payment that named bills.

The Payment Entry's `reference_no` is the reference the cashier typed (a card slip or transfer number, so
the bank statement can be reconciled), or the till's `local_id` when none was typed; the remarks keep both.

A resend never books twice: the Payment Entry keeps the till's `local_id` in `crenya_local_id` (NULL on
every entry that did not come from a till, never an empty string: the field is unique), looked up
before anything is booked. A payment ERPNext cannot book (a mode of payment without an account for the
company, say) is a refused sync event with a hint on the Refused page; the cash is already in the
drawer and the till's Z report counts it, and Retry books it once the account exists.
"""

from contextlib import contextmanager
from decimal import Decimal
from typing import NamedTuple

import frappe
from frappe import _
from frappe.utils import cint, flt

from crenya_pos_api.sync.aggregates import AggregateHandler
from crenya_pos_api.sync.cashier import enabled_user
from crenya_pos_api.sync.context import money_precision
from crenya_pos_api.sync.credit import OPEN_INVOICES_MAX_LIMIT, customer_in_outlet, is_credit_device
from crenya_pos_api.sync.errors import PERMISSION, VALIDATION, SyncError
from crenya_pos_api.sync.events import customer_lock_key
from crenya_pos_api.sync.invoice_builder import mode_of_payment_account, resolve_customer
from crenya_pos_api.sync.validation import (
	MAX_ID_LENGTH,
	_decimal,
	_fail,
	_optional_str,
	_required_str,
	_validate_date,
	_validate_utc_datetime,
)
from crenya_pos_api.utils.dates import format_db_datetime
from crenya_pos_api.utils.decimal import DecimalParseError, as_decimal, format_money, parse_decimal, quantize

AGGREGATE_CUSTOMER_PAYMENT = "Customer Payment"
PAYMENT_ENTRY = "Payment Entry"
LOCAL_ID_FIELD = "crenya_local_id"
MAX_NOTE_LENGTH = 500
# the capability (get_sync_capabilities) and the till bootstrap's feature: the server books the bills
# a receipt names in `invoices` (0.14.0)
FEATURE_SETTLE_INVOICES = "credit_settle_invoices"
# at most this many bills named by one receipt
# as many as the till can tick: its list loads up to OPEN_INVOICES_MAX_LIMIT bills ("Show more"), and a
# receipt it already took must never be refused at sync for the count
MAX_TICKED_INVOICES = OPEN_INVOICES_MAX_LIMIT
# a ticked bill the till sold itself and may not know the ERPNext name of: "local:" + its local id
LOCAL_INVOICE_PREFIX = "local:"
# the last part of the remarks of a payment that skipped ticked bills (`payment_result` reads it back)
SKIPPED_REMARK = "skipped ticked invoices: "
# the Payment Entry fields a booking may set through `extra` (0.15.0): the service provider a
# settlement is from (crenya_restaurant), and (0.16.0) a till payment's shift, cashier and device;
# any other field is refused
EXTRA_FIELDS = frozenset(
	{"crenya_service_provider", "crenya_shift_id", "crenya_cashier", "crenya_device"}
)


def _invoices(payload):
	"""The ticked bills (pure): None when absent, else a list of at most MAX_TICKED_INVOICES names, each
	an ERPNext name or `local:` + a till id, stripped, duplicates dropped keeping the first."""
	value = payload.get("invoices")
	if value is None:
		return None
	if not isinstance(value, list):
		_fail("invoices must be a list")
	if len(value) > MAX_TICKED_INVOICES:
		_fail(f"invoices may name at most {MAX_TICKED_INVOICES} bills")
	names = []
	for entry in value:
		if not isinstance(entry, str) or not entry.strip():
			_fail("invoices must be a list of invoice names")
		entry = entry.strip()
		if entry.startswith(LOCAL_INVOICE_PREFIX):
			local_id = entry[len(LOCAL_INVOICE_PREFIX) :].strip()
			if not local_id or len(local_id) > MAX_ID_LENGTH:
				_fail(f"invoices entry {entry[:40]!r}: a till id of 1 to {MAX_ID_LENGTH} characters")
			entry = LOCAL_INVOICE_PREFIX + local_id
		elif len(entry) > MAX_ID_LENGTH:
			_fail(f"invoices entry is longer than {MAX_ID_LENGTH} characters")
		if entry not in names:
			names.append(entry)
	return names


def _flag(value):
	"""True only for a clear yes (True, 1, "1", "true"); anything else is no, never a refusal."""
	if isinstance(value, bool):
		return value
	if isinstance(value, int):
		return value == 1
	return isinstance(value, str) and value.strip().lower() in ("1", "true")


def validate_customer_payment_payload(payload):
	"""The checked payload (pure): the amount as a Decimal above zero, the date and time checked, one
	way to name the customer."""
	amount = _decimal(payload.get("amount"), "amount")
	if amount <= 0:
		_fail("amount must be more than 0")
	customer_local_id = _optional_str(payload, "customer_local_id")
	customer = _optional_str(payload, "customer", max_length=140)
	if not customer_local_id and not customer:
		_fail("customer_local_id or customer is required")
	received_at = payload.get("received_at")
	return {
		"local_id": _required_str(payload, "local_id"),
		"pos_profile": _optional_str(payload, "pos_profile"),
		"company": _optional_str(payload, "company"),
		"shift_local_id": _optional_str(payload, "shift_local_id"),
		"business_date": _validate_date(payload.get("business_date"), "business_date"),
		"cashier": _optional_str(payload, "cashier"),
		"customer_local_id": customer_local_id,
		"customer": customer,
		"mode_of_payment": _required_str(payload, "mode_of_payment", max_length=140),
		"amount": amount,
		"currency": _required_str(payload, "currency", max_length=10),
		"reference": _optional_str(payload, "reference", max_length=140),
		"note": _optional_str(payload, "note", max_length=MAX_NOTE_LENGTH),
		"received_at": _validate_utc_datetime(received_at, "received_at") if received_at else None,
		"invoices": _invoices(payload),
		"advance_confirmed": _flag(payload.get("advance_confirmed")),
	}


def allocate(amount, invoices, precision):
	"""([{name, total, outstanding, due_date, allocated}], unallocated) of a payment of `amount` over
	`invoices` in the order given (pure): each takes at most its outstanding amount, the rest stays
	unallocated. Exact, in `precision` decimals."""
	left = quantize(as_decimal(amount), precision)
	rows = []
	for invoice in invoices:
		if left <= 0:
			break
		outstanding = quantize(as_decimal(invoice["outstanding"]), precision)
		take = min(left, outstanding)
		if take <= 0:
			continue
		rows.append({**invoice, "allocated": take})
		left -= take
	return rows, left


def deduction_total(deductions, precision):
	"""The sum of the deductions' amounts (pure), each at `precision` decimals; 0 for None or none."""
	total = quantize(as_decimal(0), precision)
	for row in deductions or []:
		total += quantize(as_decimal(row["amount"]), precision)
	return total


def open_invoices(customer, company, limit=None):
	"""The customer's submitted Sales Invoices of the company with something outstanding, oldest
	first (at most `limit` of them when given). Each row also names the bill's outlet, date and the
	till's ids, for `credit.get_open_invoices`; the allocation ignores them. Two receipts of one
	customer never read this at once: the handler's `lock_key` makes `events.process_event` run them
	one after another, each starting its snapshot after the previous commit (under REPEATABLE READ a
	lock taken after a plain read would still see the old balances)."""
	rows = frappe.get_all(
		"Sales Invoice",
		filters={
			"customer": customer,
			"company": company,
			"docstatus": 1,
			"is_return": 0,
			"outstanding_amount": [">", 0],
		},
		fields=[
			"name",
			"grand_total",
			"outstanding_amount",
			"due_date",
			"pos_profile",
			"posting_date",
			"crenya_local_id",
			"crenya_offline_number",
		],
		order_by="posting_date asc, creation asc, name asc",
		limit_page_length=limit or 0,
	)
	return [
		{
			"name": row.name,
			"total": row.grand_total,
			"outstanding": row.outstanding_amount,
			"due_date": row.due_date,
			"pos_profile": row.pos_profile,
			"posting_date": row.posting_date,
			"local_id": row.crenya_local_id,
			"offline_number": row.crenya_offline_number,
		}
		for row in rows
	]


def _erp_names(names, notes):
	"""([(name as ticked, ERPNext name)], skipped) of ticked bills: a `local:` id is looked up by the
	till's id, and one no Sales Invoice has yet is skipped with a note."""
	local_ids = [name[len(LOCAL_INVOICE_PREFIX) :] for name in names if name.startswith(LOCAL_INVOICE_PREFIX)]
	by_local_id = {}
	if local_ids:
		for row in frappe.get_all(
			"Sales Invoice",
			filters={"crenya_local_id": ["in", local_ids]},
			fields=["name", "crenya_local_id"],
		):
			by_local_id[row.crenya_local_id] = row.name
	resolved = []
	skipped = []
	for name in names:
		if name.startswith(LOCAL_INVOICE_PREFIX):
			erp_name = by_local_id.get(name[len(LOCAL_INVOICE_PREFIX) :])
			if not erp_name:
				skipped.append(name)
				notes.append(f"Ticked invoice {name} skipped: no Sales Invoice has this till id yet")
				continue
		else:
			erp_name = name
		resolved.append((name, erp_name))
	return resolved, skipped


def ticked_credit_notes(customer, company, names, notes):
	"""(rows in `ticked_invoices`' shape, `outstanding` negative, in the order of `names`, skipped
	names) of the credit notes a booking ticked (0.15.0, a provider's refunds), read now: only a
	submitted return (`is_return = 1`) of `customer` and `company` that still carries its own
	negative outstanding (`update_outstanding_for_self`, the till's refunds); anything else is
	skipped with a note, like a ticked bill."""
	if not isinstance(names, list) or not all(isinstance(name, str) and name for name in names):
		raise SyncError(VALIDATION, _("The credit notes must be a list of invoice names"))
	if len(names) > MAX_TICKED_INVOICES:
		raise SyncError(
			VALIDATION, _("A payment may name at most {0} credit notes").format(MAX_TICKED_INVOICES)
		)
	resolved, skipped = _erp_names(names, notes)
	found = {}
	erp_names = sorted({erp_name for _name, erp_name in resolved})
	if erp_names:
		for row in frappe.get_all(
			"Sales Invoice",
			filters={
				"name": ["in", erp_names],
				"customer": customer,
				"company": company,
				"docstatus": 1,
				"is_return": 1,
				"outstanding_amount": ["<", 0],
			},
			fields=["name", "grand_total", "outstanding_amount", "due_date"],
		):
			found[row.name] = row
	rows = []
	for name, erp_name in resolved:
		if any(row["name"] == erp_name for row in rows):
			notes.append(f"Ticked credit note {name} is {erp_name}, ticked already")
			continue
		row = found.get(erp_name)
		if row is None:
			skipped.append(name)
			notes.append(
				f"Ticked credit note {name} skipped: not a return of {customer} in {company} with "
				"something left to settle"
			)
			continue
		rows.append(
			{
				"name": row.name,
				"total": row.grand_total,
				"outstanding": row.outstanding_amount,
				"due_date": row.due_date,
			}
		)
	return rows, skipped


def ticked_invoices(customer, company, names, notes):
	"""(rows in `open_invoices`' shape in the order of `names`, skipped names) of the bills a receipt
	ticked, read now: a bill that is not a submitted, outstanding Sales Invoice (not a return) of
	`customer` and `company` is skipped, and so is a `local:` id no Sales Invoice has yet; each skip
	is noted on the Sync Event."""
	resolved, skipped = _erp_names(names, notes)
	found = {}
	erp_names = sorted({erp_name for _name, erp_name in resolved})
	if erp_names:
		for row in frappe.get_all(
			"Sales Invoice",
			filters={
				"name": ["in", erp_names],
				"customer": customer,
				"company": company,
				"docstatus": 1,
				"is_return": 0,
				"outstanding_amount": [">", 0],
			},
			fields=["name", "grand_total", "outstanding_amount", "due_date"],
		):
			found[row.name] = row
	rows = []
	for name, erp_name in resolved:
		if any(row["name"] == erp_name for row in rows):
			notes.append(f"Ticked invoice {name} is {erp_name}, ticked already")
			continue
		row = found.get(erp_name)
		if row is None:
			skipped.append(name)
			notes.append(
				f"Ticked invoice {name} skipped: not an outstanding invoice of {customer} in {company}"
			)
			continue
		rows.append(
			{
				"name": row.name,
				"total": row.grand_total,
				"outstanding": row.outstanding_amount,
				"due_date": row.due_date,
			}
		)
	return rows, skipped


def payment_for_local_id(local_id, device_id=None):
	"""The name of the Payment Entry that holds a till's `local_id`, or None (an empty id matches
	nothing: entries not made at a till keep NULL). With `device_id`, only an entry that device made
	(its remarks open with the till's id): another device's entry with the same id is not this
	device's payment, and booking it again meets the unique key instead of answering as a duplicate."""
	if not local_id:
		return None
	row = frappe.db.get_value(PAYMENT_ENTRY, {LOCAL_ID_FIELD: local_id}, ["name", "remarks"], as_dict=True)
	if not row:
		return None
	if device_id and not (row.remarks or "").startswith(_remarks_start(device_id)):
		return None
	return row.name


# every till receipt's remarks open with this and the device's id (`_remarks_start`)
TILL_REMARKS_PREFIX = "Received at the till "


def _remarks_start(device_id):
	return f"{TILL_REMARKS_PREFIX}{device_id} "


def _remarks(ctx, data, cashier):
	parts = [f"{_remarks_start(ctx.device_id)}by {cashier}"]
	if data["shift_local_id"]:
		parts.append(f"shift {data['shift_local_id']}")
	if data["reference"]:
		parts.append(f"reference {data['reference']}")
	if data["note"]:
		parts.append(data["note"])
	if data.get("advance_confirmed"):
		parts.append("advance confirmed at the till")
	return "; ".join(parts)


@contextmanager
def _as_administrator():
	"""The session is Administrator inside the block and the user it was before after it, however
	the block ends."""
	user = frappe.session.user
	frappe.set_user("Administrator")  # nosemgrep  # the booking only: restored in `finally`
	try:
		yield
	finally:
		frappe.set_user(user)  # nosemgrep  # back to the device user


class PaymentBooking(NamedTuple):
	"""One customer payment to book as a Payment Entry (Receive), from a till or from the web."""

	company: str
	customer: str
	mode_of_payment: str
	amount: Decimal
	currency: str
	posting_date: str
	# the idempotency key, kept in `crenya_local_id`
	local_id: str
	# what the cashier typed (card slip, transfer number), or None
	reference: str | None
	remarks: str
	# the ticked bills in order (names or `local:` ids); None or empty: oldest first
	invoices: list[str] | None
	# 0.15.0 (provider settlement, spec PS2). None everywhere: the 0.14.0 entry, field for field.
	# What the payer kept (a provider's commission): [{account, cost_center, amount: Decimal,
	# description}], one Payment Entry deduction row each; `amount` plus their sum is allocated
	deductions: list[dict] | None = None
	# Payment Entry fields to set, {fieldname: value}: only EXTRA_FIELDS
	extra: dict | None = None
	# ticked returns that carry their own negative outstanding (a provider's refunds): allocated in
	# full first, each growing what the ticked bills get
	credit_notes: list[str] | None = None
	# the receivable account the payment is credited to (`paid_from`; a provider's orders booked to
	# its receivable account before it changed); None: the customer's party account now
	party_account: str | None = None


def book_payment(ctx, data, notes):
	"""The submitted Payment Entry of a till's payment: the device's checks, then `post_payment_entry`."""
	if not is_credit_device(ctx.device_type):
		raise SyncError(
			PERMISSION, f"Customer payments are taken at a till, not on a {ctx.device_type} device"
		)
	for key, own in (("pos_profile", ctx.profile.name), ("company", ctx.company)):
		if data[key] and data[key] != own:
			notes.append(f"Payload {key} {data[key]} ignored: the device's is {own}")
	precision = money_precision(ctx.currency)
	if data["currency"] != ctx.currency:
		raise SyncError(VALIDATION, f"currency {data['currency']} is not the outlet's {ctx.currency}")
	if quantize(data["amount"], precision) != data["amount"]:
		raise SyncError(VALIDATION, f"amount {data['amount']} has more than {precision} decimals")
	modes = {row.mode_of_payment for row in ctx.profile.get("payments") or []}
	if data["mode_of_payment"] not in modes:
		raise SyncError(
			VALIDATION,
			f"Mode of payment {data['mode_of_payment']} is not enabled on outlet {ctx.profile.name}",
		)

	# waits (retryable) when the till made the customer and the customer event has not arrived
	customer = resolve_customer(ctx, data)
	if customer == ctx.profile.customer:
		raise SyncError(
			VALIDATION,
			"A payment on account needs a named customer, not the POS Profile's default customer",
		)
	if not customer_in_outlet(ctx, customer):
		raise SyncError(VALIDATION, f"Customer {customer} is not a customer of outlet {ctx.profile.name}")

	cashier = enabled_user(data["cashier"]) or frappe.session.user
	booking = PaymentBooking(
		company=ctx.company,
		customer=customer,
		mode_of_payment=data["mode_of_payment"],
		amount=data["amount"],
		currency=ctx.currency,
		posting_date=data["business_date"],
		local_id=data["local_id"],
		reference=data["reference"],
		remarks=_remarks(ctx, data, cashier),
		invoices=data.get("invoices"),
		extra={
			"crenya_shift_id": data["shift_local_id"],
			"crenya_cashier": cashier,
			"crenya_device": ctx.device_id,
		},
	)
	return post_payment_entry(booking, notes)


def post_payment_entry(booking, notes):
	"""The submitted Payment Entry (Receive) of `booking`: the accounts checked, allocated to the
	ticked bills in order (or oldest first when none are named), the rest an advance; posted as
	Administrator and owned by the session user. Refusals are SyncError(VALIDATION) with a hint.

	0.15.0: `party_account` (when given, checked) is `paid_from` instead of the customer's party
	account now. The ticked credit notes are allocated first, each its whole (negative) outstanding; then
	`amount` + the deductions + what the credit notes free is allocated over the bills.
	`paid_amount = received_amount = amount`, one deduction row each (Dr its account), and the
	`extra` fields set. ERPNext then books Dr `paid_to` amount, Dr each deduction, Cr the party
	amount + deductions, with nothing left over when the bills take it all."""
	extra = _checked_extra(booking.extra)
	precision = money_precision(booking.currency)
	deductions = _checked_deductions(booking.deductions, booking.company, precision)
	account = mode_of_payment_account(booking.mode_of_payment, booking.company)
	if not account:
		raise SyncError(
			VALIDATION,
			f"Mode of payment {booking.mode_of_payment} has no account for company {booking.company}: "
			"add the account to the mode of payment",
		)
	from erpnext.accounts.party import get_party_account

	customer = booking.customer
	if booking.party_account is not None:
		party_account = _checked_party_account(booking.party_account, booking.company)
	else:
		party_account = get_party_account("Customer", customer, booking.company)
	if not party_account:
		raise SyncError(VALIDATION, f"Customer {customer} has no receivable account for {booking.company}")
	account_currency = frappe.db.get_value("Account", party_account, "account_currency")
	if account_currency and account_currency != booking.currency:
		raise SyncError(
			VALIDATION,
			f"The receivable account {party_account} is in {account_currency}, not in the outlet's "
			f"{booking.currency}: a payment in the outlet's currency cannot be booked to it",
		)

	skipped = None
	remarks = booking.remarks
	credits = []
	if booking.credit_notes:
		credits, skipped = ticked_credit_notes(customer, booking.company, booking.credit_notes, notes)
	if booking.invoices:
		candidates, skipped_bills = ticked_invoices(customer, booking.company, booking.invoices, notes)
		skipped = (skipped or []) + skipped_bills
	else:
		candidates = open_invoices(customer, booking.company)
	if skipped:
		remarks = f"{remarks}; {SKIPPED_REMARK}{', '.join(skipped)}"
	credits = [{**row, "allocated": quantize(as_decimal(row["outstanding"]), precision)} for row in credits]
	freed = -sum((row["allocated"] for row in credits), as_decimal(0))
	pot = quantize(as_decimal(booking.amount), precision) + deduction_total(deductions, precision) + freed
	rows, _left = allocate(pot, candidates, precision)
	if credits:
		taken = sum((row["allocated"] for row in rows), as_decimal(0))
		if taken < freed:
			# ERPNext refuses a receipt whose allocations add up below zero
			raise SyncError(
				VALIDATION,
				_(
					"The ticked refunds ({0}) are more than the ticked orders take ({1}): tick the orders "
					"they belong to"
				).format(format_money(freed, precision), format_money(taken, precision)),
			)
		rows = credits + rows
	doc = frappe.new_doc(PAYMENT_ENTRY)
	doc.update(
		{
			"payment_type": "Receive",
			"party_type": "Customer",
			"party": customer,
			"company": booking.company,
			"posting_date": booking.posting_date,
			"mode_of_payment": booking.mode_of_payment,
			"paid_from": party_account,
			"paid_to": account,
			"paid_amount": flt(booking.amount, precision),
			"received_amount": flt(booking.amount, precision),
			"source_exchange_rate": 1,
			"target_exchange_rate": 1,
			# the till's id is the idempotency key (`crenya_local_id`); the reference a bank account's
			# entry requires is the one the cashier typed (card slip, transfer number) so the bank
			# statement can be matched, and only when none was typed the till's id (cash needs none)
			LOCAL_ID_FIELD: booking.local_id,
			"reference_no": booking.reference or booking.local_id,
			"reference_date": booking.posting_date,
			# ERPNext writes its own "Amount ... received from ..." over the remarks unless told these
			# are custom: they carry the till's id (`payment_for_local_id` reads the device from them)
			"custom_remarks": 1,
			"remarks": remarks,
		}
	)
	if extra:
		doc.update(extra)
	for row in rows:
		doc.append(
			"references",
			{
				"reference_doctype": "Sales Invoice",
				"reference_name": row["name"],
				"total_amount": row["total"],
				"outstanding_amount": row["outstanding"],
				"allocated_amount": flt(row["allocated"], precision),
				"due_date": row["due_date"],
			},
		)
	for row in deductions:
		doc.append(
			"deductions",
			{
				"account": row["account"],
				"cost_center": row["cost_center"],
				"amount": flt(row["amount"], precision),
				"description": row["description"],
			},
		)
	# The device was authorised by get_device_context and a till posts only through this handler.
	# ERPNext's Payment Entry code checks the session user's own right on the doctype in places no flag
	# reaches (`get_account_details`), and a device key must not be given that right (it would list
	# every payment): the entry is posted as Administrator for the length of the booking, then its
	# owner is put back to the device user so the trail shows who booked it. The remarks name the
	# device and the cashier.
	doc.flags.ignore_permissions = True
	user = frappe.session.user
	with _as_administrator():
		doc.insert(ignore_permissions=True)
		doc.submit()
	if user != "Administrator":
		frappe.db.set_value(
			PAYMENT_ENTRY, doc.name, {"owner": user, "modified_by": user}, update_modified=False
		)
		doc.owner = doc.modified_by = user
	# the answer of this booking (`payment_result`): whether bills were named, and which were skipped
	doc.flags.crenya_ticked = bool(booking.invoices or booking.credit_notes)
	doc.flags.crenya_skipped = list(skipped or [])
	return doc


def _checked_party_account(account, company):
	"""`account` when it is an enabled Receivable ledger of `company` in the company's currency (0.15.0,
	`PaymentBooking.party_account`); SyncError(VALIDATION) otherwise."""
	if not isinstance(account, str) or not account:
		raise SyncError(VALIDATION, _("The receivable account must be an account name"))
	found = frappe.db.get_value(
		"Account",
		account,
		("company", "is_group", "disabled", "account_type", "account_currency"),
		as_dict=True,
	)
	if not found or found.company != company or cint(found.is_group):
		raise SyncError(
			VALIDATION, _("Receivable account {0} is not a ledger account of {1}").format(account, company)
		)
	if cint(found.disabled):
		raise SyncError(VALIDATION, _("Receivable account {0} is disabled").format(account))
	if found.account_type != "Receivable":
		raise SyncError(
			VALIDATION,
			_("Account {0} is not a Receivable account: a payment is credited to a receivable").format(
				account
			),
		)
	company_currency = frappe.get_cached_value("Company", company, "default_currency")
	if found.account_currency and company_currency and found.account_currency != company_currency:
		raise SyncError(
			VALIDATION,
			_("Receivable account {0} is in {1}, not in the company's {2}").format(
				account, found.account_currency, company_currency
			),
		)
	return account


def _checked_extra(extra):
	"""The Payment Entry fields of `extra` ({} for None): only EXTRA_FIELDS, each a name or None."""
	if extra is None:
		return {}
	if not isinstance(extra, dict):
		raise SyncError(VALIDATION, _("The extra Payment Entry fields must be a mapping"))
	for fieldname, value in extra.items():
		if fieldname not in EXTRA_FIELDS:
			raise SyncError(
				VALIDATION, _("The Payment Entry field {0} cannot be set by a booking").format(fieldname)
			)
		if value is not None and not isinstance(value, str):
			raise SyncError(VALIDATION, _("The Payment Entry field {0} must be a name").format(fieldname))
	return dict(extra)


def _checked_deductions(deductions, company, precision):
	"""[{account, cost_center, amount: Decimal, description}] of `deductions` ([] for None): each an
	enabled ledger account of `company` in the company's currency that needs no party (not
	Receivable or Payable), a cost center of `company` (the company's default when none is given)
	and a finite amount above 0 at `precision` decimals at most."""
	if deductions is None:
		return []
	if not isinstance(deductions, list):
		raise SyncError(VALIDATION, _("The deductions must be a list"))
	checked = []
	for row in deductions:
		if not isinstance(row, dict) or not isinstance(row.get("account"), str) or not row["account"]:
			raise SyncError(VALIDATION, _("Each deduction needs an account"))
		account = row["account"]
		found = frappe.db.get_value(
			"Account",
			account,
			("company", "is_group", "disabled", "account_type", "account_currency"),
			as_dict=True,
		)
		if not found or found.company != company or cint(found.is_group):
			raise SyncError(
				VALIDATION, _("Deduction account {0} is not a ledger account of {1}").format(account, company)
			)
		if cint(found.disabled):
			raise SyncError(VALIDATION, _("Deduction account {0} is disabled").format(account))
		if found.account_type in ("Receivable", "Payable"):
			raise SyncError(
				VALIDATION,
				_("Deduction account {0} is a {1} account: choose an account that needs no party").format(
					account, found.account_type
				),
			)
		company_currency = frappe.get_cached_value("Company", company, "default_currency")
		if found.account_currency and company_currency and found.account_currency != company_currency:
			raise SyncError(
				VALIDATION,
				_("Deduction account {0} is in {1}, not in the company's {2}").format(
					account, found.account_currency, company_currency
				),
			)
		cost_center = row.get("cost_center") or frappe.get_cached_value("Company", company, "cost_center")
		if not cost_center:
			raise SyncError(
				VALIDATION,
				_("Set a default cost center on company {0}, or give the deduction one").format(company),
			)
		found = frappe.db.get_value("Cost Center", cost_center, ("company", "is_group"), as_dict=True)
		if not found or found.company != company or cint(found.is_group):
			raise SyncError(
				VALIDATION,
				_("Deduction cost center {0} is not a ledger cost center of {1}").format(
					cost_center, company
				),
			)
		try:
			amount = parse_decimal(row.get("amount"), "amount")
		except DecimalParseError:
			raise SyncError(
				VALIDATION, _("Deduction amount {0} is not a number").format(row.get("amount"))
			) from None
		if quantize(amount, precision) != amount:
			raise SyncError(
				VALIDATION,
				_("Deduction amount {0} has more than {1} decimals").format(amount, precision),
			)
		if amount <= 0:
			raise SyncError(VALIDATION, _("A deduction must be more than 0, not {0}").format(amount))
		description = row.get("description")
		checked.append(
			{
				"account": account,
				"cost_center": cost_center,
				"amount": amount,
				"description": description if isinstance(description, str) and description else None,
			}
		)
	return checked


def skipped_invoices(doc):
	"""The ticked bills a Payment Entry skipped, or None for a payment that named none (no `skipped`
	in the result). A fresh booking answers from its own flags; only an entry loaded again (a resend)
	reads them back from the remarks' last part."""
	ticked = doc.flags.get("crenya_ticked")
	if ticked is not None:
		return list(doc.flags.get("crenya_skipped") or []) if ticked else None
	_head, mark, tail = (doc.get("remarks") or "").rpartition(f"; {SKIPPED_REMARK}")
	if not mark or "; " in tail:
		return None
	return [name for name in tail.split(", ") if name]


def payment_result(doc):
	"""{allocated, unallocated, invoices} of a booked Payment Entry, as decimal strings."""
	precision = money_precision()
	allocated = sum((as_decimal(row.allocated_amount) for row in doc.get("references") or []), as_decimal(0))
	result = {
		"allocated": format_money(allocated, precision),
		"unallocated": format_money(as_decimal(doc.get("unallocated_amount") or 0), precision),
		"invoices": [row.reference_name for row in doc.get("references") or [] if row.reference_name],
	}
	# a payment that named bills (0.14.0) says which it skipped; a payment without `invoices` answers
	# exactly as before
	skipped = skipped_invoices(doc)
	if skipped is not None:
		result["skipped"] = skipped
	return result


class CustomerPaymentHandler(AggregateHandler):
	"""A customer's payment taken at the till: one submitted Payment Entry."""

	aggregate_type = AGGREGATE_CUSTOMER_PAYMENT
	doctype = PAYMENT_ENTRY
	operations = ("submit",)
	local_id_field = LOCAL_ID_FIELD

	def validate(self, operation, payload):
		return validate_customer_payment_payload(payload)

	def apply(self, ctx, operation, data, notes):
		# before anything is booked: a resend (under a new event_id) answers the entry it made
		existing = payment_for_local_id(data["local_id"], ctx.device_id)
		if existing:
			notes.append(f"Matched existing Payment Entry {existing} by {LOCAL_ID_FIELD}")
			return frappe.get_doc(PAYMENT_ENTRY, existing)
		return book_payment(ctx, data, notes)

	def find_by_local_id(self, local_id):
		return payment_for_local_id(local_id)

	def find_for_device(self, ctx, local_id):
		return payment_for_local_id(local_id, ctx.device_id)

	def lock_key(self, payload):
		"""Receipts of one customer run one after another: each allocates to the invoices the
		previous one left. The customer by name, or by the till's local id for one it made (resolved
		to the name once it exists, so both ways of naming it share a lock)."""
		customer = payload.get("customer")
		local_id = payload.get("customer_local_id")
		if local_id and isinstance(local_id, str):
			customer = frappe.db.get_value("Customer", {"crenya_local_id": local_id}, "name") or customer
			if not customer:
				return f"customer-local:{local_id}"
		return customer_lock_key(customer) if customer and isinstance(customer, str) else None

	def result_fields(self, doc):
		return {
			"doctype": doc.doctype,
			"name": doc.name,
			"docstatus": cint(doc.get("docstatus")),
			"modified": format_db_datetime(doc.get("modified")),
			"totals": None,
			"fawtara_status": None,
			"payment": payment_result(doc),
		}
