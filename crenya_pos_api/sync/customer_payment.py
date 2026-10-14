"""A customer's payment taken at the till (aggregate `Customer Payment`, retail P1 task 8), booked as
one ERPNext Payment Entry (Receive).

Payload (money as a decimal string at the currency's decimals; all keys the till sends):
local_id, pos_profile, company, shift_local_id, business_date (YYYY-MM-DD), cashier,
customer_local_id (a customer made at the till) or customer (a name), mode_of_payment, amount,
currency, reference, note, received_at (ISO-8601 UTC). The outlet and company are the device's.

The Payment Entry is allocated to the customer's submitted, outstanding Sales Invoices of the company
(not returns), `posting_date` then `creation` oldest first, each up to its outstanding amount; what is
left stays unallocated, an advance on the customer's account. The result adds
`payment` {allocated, unallocated, invoices}.

A resend never books twice: the Payment Entry keeps the till's `local_id` in `crenya_local_id` (NULL on
every entry that did not come from a till, never an empty string: the field is unique), looked up
before anything is booked. A payment ERPNext cannot book (a mode of payment without an account for the
company, say) is a refused sync event with a hint on the Refused page; the cash is already in the
drawer and the till's Z report counts it, and Retry books it once the account exists.
"""

import frappe
from frappe.utils import cint, flt

from crenya_pos_api.sync.aggregates import AggregateHandler
from crenya_pos_api.sync.cashier import enabled_user
from crenya_pos_api.sync.context import DEFAULT_DEVICE_TYPE, money_precision
from crenya_pos_api.sync.credit import customer_in_outlet
from crenya_pos_api.sync.errors import PERMISSION, VALIDATION, SyncError
from crenya_pos_api.sync.invoice_builder import mode_of_payment_account, resolve_customer
from crenya_pos_api.sync.validation import (
	_decimal,
	_fail,
	_optional_str,
	_required_str,
	_validate_date,
	_validate_utc_datetime,
)
from crenya_pos_api.utils.dates import format_db_datetime
from crenya_pos_api.utils.decimal import as_decimal, format_money, quantize

AGGREGATE_CUSTOMER_PAYMENT = "Customer Payment"
PAYMENT_ENTRY = "Payment Entry"
LOCAL_ID_FIELD = "crenya_local_id"
MAX_NOTE_LENGTH = 500


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


def open_invoices(customer, company):
	"""The customer's submitted Sales Invoices of the company with something outstanding, oldest
	first (rows locked until the entry is booked, so two receipts never take the same debt)."""
	rows = frappe.get_all(
		"Sales Invoice",
		filters={
			"customer": customer,
			"company": company,
			"docstatus": 1,
			"is_return": 0,
			"outstanding_amount": [">", 0],
		},
		fields=["name", "grand_total", "outstanding_amount", "due_date"],
		order_by="posting_date asc, creation asc, name asc",
		for_update=True,
	)
	return [
		{
			"name": row.name,
			"total": row.grand_total,
			"outstanding": row.outstanding_amount,
			"due_date": row.due_date,
		}
		for row in rows
	]


def payment_for_local_id(local_id):
	"""The name of the Payment Entry that holds a till's `local_id`, or None (an empty id matches
	nothing: entries not made at a till keep NULL)."""
	if not local_id:
		return None
	return frappe.db.get_value(PAYMENT_ENTRY, {LOCAL_ID_FIELD: local_id}, "name")


def _remarks(ctx, data, cashier):
	parts = [f"Received at the till {ctx.device_id} by {cashier}"]
	if data["shift_local_id"]:
		parts.append(f"shift {data['shift_local_id']}")
	if data["reference"]:
		parts.append(f"reference {data['reference']}")
	if data["note"]:
		parts.append(data["note"])
	return "; ".join(parts)


def book_payment(ctx, data, notes):
	"""The submitted Payment Entry of a till's payment."""
	if ctx.device_type != DEFAULT_DEVICE_TYPE:
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
	if not customer_in_outlet(ctx, customer):
		raise SyncError(VALIDATION, f"Customer {customer} is not a customer of outlet {ctx.profile.name}")

	account = mode_of_payment_account(data["mode_of_payment"], ctx.company)
	if not account:
		raise SyncError(
			VALIDATION,
			f"Mode of payment {data['mode_of_payment']} has no account for company {ctx.company}: "
			"add the account to the mode of payment",
		)
	from erpnext.accounts.party import get_party_account

	party_account = get_party_account("Customer", customer, ctx.company)
	if not party_account:
		raise SyncError(VALIDATION, f"Customer {customer} has no receivable account for {ctx.company}")
	account_currency = frappe.db.get_value("Account", party_account, "account_currency")
	if account_currency and account_currency != ctx.currency:
		raise SyncError(
			VALIDATION,
			f"The receivable account {party_account} is in {account_currency}, not in the outlet's "
			f"{ctx.currency}: a payment in the outlet's currency cannot be booked to it",
		)

	cashier = enabled_user(data["cashier"]) or frappe.session.user
	rows, _left = allocate(data["amount"], open_invoices(customer, ctx.company), precision)
	doc = frappe.new_doc(PAYMENT_ENTRY)
	doc.update(
		{
			"payment_type": "Receive",
			"party_type": "Customer",
			"party": customer,
			"company": ctx.company,
			"posting_date": data["business_date"],
			"mode_of_payment": data["mode_of_payment"],
			"paid_from": party_account,
			"paid_to": account,
			"paid_amount": flt(data["amount"], precision),
			"received_amount": flt(data["amount"], precision),
			"source_exchange_rate": 1,
			"target_exchange_rate": 1,
			# the till's id: the idempotency key, and the reference a bank account's entry requires
			LOCAL_ID_FIELD: data["local_id"],
			"reference_no": data["local_id"],
			"reference_date": data["business_date"],
			"remarks": _remarks(ctx, data, cashier),
		}
	)
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
	# the device was authorised by get_device_context; a till posts only through this handler
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc


def payment_result(doc):
	"""{allocated, unallocated, invoices} of a booked Payment Entry, as decimal strings."""
	precision = money_precision()
	allocated = sum((as_decimal(row.allocated_amount) for row in doc.get("references") or []), as_decimal(0))
	return {
		"allocated": format_money(allocated, precision),
		"unallocated": format_money(as_decimal(doc.get("unallocated_amount") or 0), precision),
		"invoices": [row.reference_name for row in doc.get("references") or [] if row.reference_name],
	}


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
		existing = payment_for_local_id(data["local_id"])
		if existing:
			notes.append(f"Matched existing Payment Entry {existing} by {LOCAL_ID_FIELD}")
			return frappe.get_doc(PAYMENT_ENTRY, existing)
		return book_payment(ctx, data, notes)

	def find_by_local_id(self, local_id):
		return payment_for_local_id(local_id)

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
