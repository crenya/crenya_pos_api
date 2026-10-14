"""Credit sales: a customer's credit limit and balance as ERPNext reads them, and the request-local
waiver of ERPNext's limit check for the invoices a till pushes.

ERPNext stays the authority. The limit is `get_credit_limit` (the Customer's *Credit Limit* row for
the company, else its Customer Group's, else the Company's) and the balance
`get_customer_outstanding`, so the answer equals what a desk invoice of the same customer is checked
against. **A limit of 0 or none means no limit**: ERPNext's `check_credit_limit` returns when the limit
is 0, and so does everything here (`credit_limit` and `available` are null, `over_limit` false).

The server never refuses a sale the customer already took (spec C5): ERPNext's check would turn a
done credit sale, or a cash sale to an already over-limit customer, into a refused one. The invoice
is therefore posted inside `credit_limit_waived()`, and a note says when the customer ended above the
limit. The till's manager PIN is the approval; the web lists such customers.
"""

from contextlib import contextmanager
from decimal import Decimal

import frappe

from crenya_pos_api.sync.context import DEFAULT_DEVICE_TYPE, money_precision
from crenya_pos_api.sync.errors import InvalidRequestError, raise_api_error
from crenya_pos_api.utils.dates import utc_now_iso
from crenya_pos_api.utils.decimal import as_decimal, format_money, quantize

# POS Profile Check: the outlet may sell on account (Credit sales section, setup/install.py)
CREDIT_SALES_FIELD = "crenya_credit_sales_enabled"
# the request-local flag `credit_limit_waived` sets and the wrapper reads
WAIVER_FLAG = "crenya_credit_waived"
WRAPPED_MARK = "_crenya_waiver"


def customer_credit_limit(customer, company):
	"""The customer's credit limit for `company` as ERPNext reads it (Decimal), or None when it has
	none (a limit of 0 is no limit)."""
	from erpnext.selling.doctype.customer.customer import get_credit_limit

	limit = as_decimal(get_credit_limit(customer, company))
	return limit if limit > 0 else None


def credit_status(customer, company):
	"""{customer, credit_limit, outstanding, available, over_limit, as_of} (money as decimal strings
	at the currency's precision). `credit_limit` and `available` are null for a customer with no
	limit; `available` is never negative; `over_limit` is the balance above the limit."""
	from erpnext.selling.doctype.customer.customer import get_customer_outstanding

	precision = money_precision()
	limit = customer_credit_limit(customer, company)
	outstanding = quantize(get_customer_outstanding(customer, company), precision)
	over_limit = limit is not None and outstanding > quantize(limit, precision)
	available = None if limit is None else max(quantize(limit, precision) - outstanding, Decimal(0))
	return {
		"customer": customer,
		"credit_limit": None if limit is None else format_money(limit, precision),
		"outstanding": format_money(outstanding, precision),
		"available": None if available is None else format_money(available, precision),
		"over_limit": over_limit,
		"as_of": utc_now_iso(),
	}


def over_limit_note(status):
	"""The Sync Event note for a customer who ended above the limit (from a `credit_status`), else
	None."""
	if not status["over_limit"]:
		return None
	precision = money_precision()
	over = quantize(status["outstanding"], precision) - quantize(status["credit_limit"], precision)
	return f"Customer {status['customer']} is over the credit limit by {format_money(over, precision)}"


def _require_till(ctx):
	if ctx.device_type != DEFAULT_DEVICE_TYPE:
		raise_api_error(InvalidRequestError, f"Credit is for tills, not for {ctx.device_type} devices")


def status_for(ctx, customer, company=None):
	"""`credit_status` for the device's customer: the device's company only (another company is
	refused), an existing customer of the outlet (inside its customer groups, when it limits them)."""
	_require_till(ctx)
	if company and company != ctx.company:
		raise_api_error(InvalidRequestError, f"Company {company!r} is not the company of this device")
	if not customer or not isinstance(customer, str) or not frappe.db.exists("Customer", customer):
		raise_api_error(InvalidRequestError, f"Customer {customer!r} does not exist")
	roots = [row.customer_group for row in ctx.profile.get("customer_groups") or []]
	if roots:
		from crenya_pos_api.sync.pull import _descendants

		group = frappe.db.get_value("Customer", customer, "customer_group")
		if group not in _descendants("Customer Group", roots):
			raise_api_error(InvalidRequestError, f"Customer {customer} is not a customer of this outlet")
	return credit_status(customer, ctx.company)


def invoice_outstanding_for(ctx, invoice):
	"""{name, grand_total, outstanding, as_of} of a submitted sale of this outlet (by name or by the
	till's local id): what a return of it can credit to the account."""
	_require_till(ctx)
	if not invoice or not isinstance(invoice, str):
		raise_api_error(InvalidRequestError, "invoice is required")
	fields = ["name", "docstatus", "is_return", "company", "pos_profile", "grand_total", "outstanding_amount"]
	row = frappe.db.get_value("Sales Invoice", invoice, fields, as_dict=True) or frappe.db.get_value(
		"Sales Invoice", {"crenya_local_id": invoice}, fields, as_dict=True
	)
	if not row or row.docstatus != 1 or row.is_return:
		raise_api_error(InvalidRequestError, f"Invoice {invoice!r} is not a submitted sale")
	if row.company != ctx.company or row.pos_profile != ctx.profile.name:
		raise_api_error(InvalidRequestError, f"Invoice {row.name} was not sold at this outlet")
	precision = money_precision()
	return {
		"name": row.name,
		"grand_total": format_money(row.grand_total, precision),
		"outstanding": format_money(row.outstanding_amount, precision),
		"as_of": utc_now_iso(),
	}


def _install_waiver():
	"""Wrap ERPNext's `check_credit_limit` once. Sales Invoice imports it inside its own
	`check_credit_limit` at call time (v15 and v16), so the module attribute is the one it calls."""
	import erpnext.selling.doctype.customer.customer as customer_module

	original = customer_module.check_credit_limit
	if getattr(original, WRAPPED_MARK, False):
		return

	def check_credit_limit(*args, **kwargs):
		if getattr(frappe.local, WAIVER_FLAG, False):
			return None
		return original(*args, **kwargs)

	setattr(check_credit_limit, WRAPPED_MARK, True)
	customer_module.check_credit_limit = check_credit_limit


@contextmanager
def credit_limit_waived():
	"""ERPNext's credit limit check is skipped while the block runs, for this request only: the flag
	lives in `frappe.local` (one request, one thread) and is cleared when the block ends, however it
	ends. A desk user submitting the same invoice is still refused."""
	_install_waiver()
	before = getattr(frappe.local, WAIVER_FLAG, False)
	setattr(frappe.local, WAIVER_FLAG, True)
	try:
		yield
	finally:
		setattr(frappe.local, WAIVER_FLAG, before)
