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
from frappe.utils import cint

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


LIMIT_SOURCES = ("customer", "group", "company")


def customer_credit_limit_sources(rows, company):
	"""{customer name: (credit limit as a Decimal or None, where it comes from)} for the customer
	`rows` (name, customer_group), in three queries for the page: ERPNext's `get_credit_limit`
	order, the customer's own row ("customer"), else its group's ("group"), else the company's
	("company"); 0 or none is no limit (None, None)."""
	names = [row.name for row in rows]
	groups = list({row.customer_group for row in rows if row.customer_group})

	def limits(parenttype, parents):
		if not parents:
			return {}
		found = frappe.get_all(
			"Customer Credit Limit",
			filters={"parent": ["in", parents], "parenttype": parenttype, "company": company},
			fields=["parent", "credit_limit", "bypass_credit_limit_check"],
			order_by="idx asc",
		)
		result = {}
		for row in found:
			if row.parent in result:
				continue
			# ERPNext's `get_credit_limit` ignores a Customer Group row that bypasses the check
			# (v15 and v16) and falls to the company's limit
			bypass = parenttype == "Customer Group" and row.bypass_credit_limit_check
			result[row.parent] = None if bypass else row.credit_limit
		return result

	own = limits("Customer", names)
	by_group = limits("Customer Group", groups)
	company_limit = frappe.get_cached_value("Company", company, "credit_limit")
	out = {}
	for row in rows:
		for source, value in (
			("customer", own.get(row.name)),
			("group", by_group.get(row.customer_group)),
			("company", company_limit),
		):
			if value:
				limit = as_decimal(value)
				out[row.name] = (limit, source) if limit > 0 else (None, None)
				break
		else:
			out[row.name] = (None, None)
	return out


def customer_credit_limits(rows, company):
	"""{customer name: credit limit as a Decimal or None} for the pull's customer `rows`
	(`customer_credit_limit_sources` without the source); 0 or none is no limit."""
	return {name: limit for name, (limit, _source) in customer_credit_limit_sources(rows, company).items()}


def payable_of(row, precision):
	"""What an invoice row (grand_total, rounded_total, disable_rounded_total) is payable for, as
	ERPNext decides it: the grand total when rounding is off, else the rounded total (Decimal)."""
	if cint(row.get("disable_rounded_total")):
		return quantize(row.get("grand_total") or 0, precision)
	return quantize(row.get("rounded_total") or row.get("grand_total") or 0, precision)


def on_account_part(invoice, payments, precision):
	"""What a sale put on the customer's account when it was sold (pure, Decimal >= 0): its payable
	total less the payment rows and any loyalty points it redeemed. A sale paid in full has none."""
	paid = sum((as_decimal(p.get("amount")) for p in payments or []), Decimal(0))
	loyalty = as_decimal(invoice.get("loyalty_amount") or 0)
	return max(
		payable_of(invoice, precision) - quantize(paid, precision) - quantize(loyalty, precision), Decimal(0)
	)


def credited_to_account(returns, payments_by_return, precision):
	"""What returns of an invoice credited to the customer's account (pure, Decimal >= 0): for each
	return that does not reduce its own outstanding (`update_outstanding_for_self` 0, the shape a
	credited return posts), its total less the money refunded in its payment rows. A plain refund
	credits nothing."""
	total = Decimal(0)
	for ret in returns or []:
		if cint(ret.get("update_outstanding_for_self", 1)):
			continue
		refunded = sum(
			(abs(as_decimal(p.get("amount"))) for p in (payments_by_return or {}).get(ret.get("name"), [])),
			Decimal(0),
		)
		total += max(abs(payable_of(ret, precision)) - quantize(refunded, precision), Decimal(0))
	return quantize(total, precision)


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


NOT_FOUND = "Not found"


SWITCHED_OFF = "Credit sales are switched off for this outlet"


def _credit_on(ctx):
	return bool(cint(ctx.profile.get(CREDIT_SALES_FIELD)))


def customer_in_outlet(ctx, customer):
	"""Whether `customer` exists and lies inside the outlet's customer groups (any, when it limits
	none)."""
	if not customer or not isinstance(customer, str) or not frappe.db.exists("Customer", customer):
		return False
	roots = [row.customer_group for row in ctx.profile.get("customer_groups") or []]
	if roots:
		from crenya_pos_api.sync.pull import _descendants

		group = frappe.db.get_value("Customer", customer, "customer_group")
		return group in _descendants("Customer Group", roots)
	return True


def status_for(ctx, customer):
	"""`credit_status` for a customer of the device's outlet (an existing customer inside its customer
	groups, when it limits them). One answer for a customer that is not there and one that is not the
	outlet's, so the call tells nothing about other customers. An outlet that no longer sells on
	account still answers for a customer who owes (they can pay, and an old on-account invoice can be
	returned); for any other it answers that credit is switched off."""
	_require_till(ctx)
	if not customer_in_outlet(ctx, customer):
		raise_api_error(InvalidRequestError, NOT_FOUND)
	status = credit_status(customer, ctx.company)
	if not _credit_on(ctx) and quantize(status["outstanding"], money_precision()) <= 0:
		raise_api_error(InvalidRequestError, SWITCHED_OFF)
	return status


def invoice_outstanding_for(ctx, invoice):
	"""{name, grand_total, outstanding, as_of} of a submitted sale of this outlet (by name or by the
	till's local id): what a return of it can credit to the account. Answered whatever the outlet's
	credit switch says: invoices sold on account while it was on are returned after it is off."""
	_require_till(ctx)
	if not invoice or not isinstance(invoice, str):
		raise_api_error(InvalidRequestError, NOT_FOUND)
	fields = ["name", "docstatus", "is_return", "company", "pos_profile", "grand_total", "outstanding_amount"]
	row = frappe.db.get_value("Sales Invoice", invoice, fields, as_dict=True) or frappe.db.get_value(
		"Sales Invoice", {"crenya_local_id": invoice}, fields, as_dict=True
	)
	if not row or row.docstatus != 1 or row.is_return:
		raise_api_error(InvalidRequestError, NOT_FOUND)
	if row.company != ctx.company or row.pos_profile != ctx.profile.name:
		raise_api_error(InvalidRequestError, NOT_FOUND)
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
