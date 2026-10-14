"""A shop's credit customers on app.crenya.com: who has a credit limit or a balance, who is over the
limit, and the limit's edit. For the shop's managers (`scope.manage`), from a browser session only
(`require_web_session`).

ERPNext stays the authority: the limit is the Customer's *Credit Limit* row for the shop's company
(else its Customer Group's, else the Company's: `sync.credit`), the balance `get_customer_outstanding`.
A limit of 0 or none means no limit and reads "No limit": the web shows `credit_limit` null so.
"""

from collections import Counter
from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cint

from crenya_pos_api.api.web_admin.scope import manage
from crenya_pos_api.sync.context import money_precision
from crenya_pos_api.sync.credit import customer_credit_limits, customer_in_outlet
from crenya_pos_api.utils.dates import format_db_datetime
from crenya_pos_api.utils.decimal import DecimalParseError, as_decimal, format_money, parse_decimal, quantize
from crenya_pos_api.web.context import check_modified, invalid, refuse, require_web_session
from crenya_pos_api.web.reports import money_view

CUSTOMER = "Customer"
CREDIT_LIMIT = "Customer Credit Limit"
# the most customers listed, and the most candidates read before the list is cut
MAX_CREDIT_ROWS = 200
MAX_CANDIDATES = 1000
# the most open invoices read to count them per customer
MAX_INVOICE_ROWS = 20000


def _outlet_customer_filter(ctx):
	"""Filters for the customers the outlet serves: not disabled, inside its customer groups."""
	filters = {"disabled": 0}
	roots = [row.customer_group for row in ctx.profile.get("customer_groups") or []]
	if roots:
		from crenya_pos_api.sync.pull import _descendants

		filters["customer_group"] = ["in", _descendants("Customer Group", roots) or [""]]
	return filters


def _open_invoices(ctx) -> dict:
	"""{customer: number of open invoices} of the company's submitted sales (counted here, not by a
	SQL function, which Frappe's list API restricts by version)."""
	customers = frappe.get_all(
		"Sales Invoice",
		filters={
			"company": ctx.company,
			"docstatus": 1,
			"is_return": 0,
			"outstanding_amount": [">", 0],
		},
		pluck="customer",
		limit=MAX_INVOICE_ROWS,
	)
	return dict(Counter(customers))


def _row(customer, limit, outstanding, open_invoices, precision) -> dict:
	"""One credit row (pure but for formatting): money as decimal strings, `credit_limit` and
	`available` null for a customer with no limit, `over_limit` the balance above the limit."""
	outstanding = quantize(outstanding, precision)
	over_limit = limit is not None and outstanding > quantize(limit, precision)
	available = None if limit is None else max(quantize(limit, precision) - outstanding, Decimal(0))
	return {
		"customer": customer.name,
		"name": customer.customer_name or customer.name,
		"modified": format_db_datetime(customer.modified),
		"credit_limit": None if limit is None else format_money(limit, precision),
		"outstanding": format_money(outstanding, precision),
		"available": None if available is None else format_money(available, precision),
		"over_limit": over_limit,
		"open_invoices": open_invoices,
	}


def credit_rows(ctx, only=None) -> list[dict]:
	"""The shop's credit customers: those with a limit of their own for its company or a balance
	(`only`: that one customer alone), over the limit first, then the biggest balance."""
	from erpnext.selling.doctype.customer.customer import get_customer_outstanding

	precision = money_precision(ctx.currency)
	invoices = _open_invoices(ctx)
	if only:
		names = [only]
	else:
		own = frappe.get_all(
			CREDIT_LIMIT,
			filters={"parenttype": CUSTOMER, "company": ctx.company, "credit_limit": [">", 0]},
			pluck="parent",
			limit=MAX_CANDIDATES,
		)
		names = sorted(set(own) | set(invoices))
	if not names:
		return []
	customers = frappe.get_all(
		CUSTOMER,
		# the one customer just saved is answered whatever its state; a list is the shop's live ones
		filters={"name": ["in", names]} if only else {**_outlet_customer_filter(ctx), "name": ["in", names]},
		fields=["name", "customer_name", "customer_group", "modified"],
	)
	limits = customer_credit_limits(customers, ctx.company)
	rows = [
		_row(
			customer,
			limits[customer.name],
			as_decimal(get_customer_outstanding(customer.name, ctx.company)),
			invoices.get(customer.name, 0),
			precision,
		)
		for customer in customers
	]
	rows.sort(key=lambda row: (not row["over_limit"], -Decimal(row["outstanding"]), row["name"]))
	return rows


@frappe.whitelist(methods=["GET", "POST"])
def credit_customers(outlet: str) -> dict:
	"""The customers of the shop that have a credit limit or a balance, over the limit first:
	{"outlet", "currency", "rows": [{customer, name, modified, credit_limit, outstanding, available,
	over_limit, open_invoices}], "more"}. `credit_limit` and `available` are null for a customer
	with no limit ("No limit"); at most MAX_CREDIT_ROWS rows."""
	require_web_session()
	ctx = manage(outlet)
	rows = credit_rows(ctx)
	return {
		"outlet": ctx.outlet,
		"currency": money_view(ctx.currency),
		"rows": rows[:MAX_CREDIT_ROWS],
		"more": len(rows) > MAX_CREDIT_ROWS,
	}


def parse_limit(value, precision) -> Decimal | None:
	"""The limit a manager typed (pure): a decimal string with at most `precision` decimals, 0 or
	blank meaning no limit (None); ValueError for anything else, a negative included."""
	if value is None or (isinstance(value, str) and not value.strip()):
		return None
	try:
		limit = parse_decimal(value, "credit_limit")
	except DecimalParseError as e:
		raise ValueError(str(e))
	if limit < 0:
		raise ValueError("The credit limit cannot be negative")
	if quantize(limit, precision) != limit:
		raise ValueError(f"The credit limit has more than {precision} decimals")
	return limit if limit > 0 else None


@frappe.whitelist(methods=["POST"])
def save_credit_limit(outlet: str, customer: str, credit_limit: str | None, modified: str) -> dict:
	"""Set the credit limit of `customer` for the shop's company: the Customer's own *Credit Limit*
	row (written, or removed for 0 or blank: no limit). `modified` is the one `credit_customers`
	answered for the customer (changed since is refused). The customer must be one of the shop's.
	Answers the customer's row like `credit_customers`, with the limit ERPNext now reads."""
	require_web_session()
	ctx = manage(outlet)
	if not customer_in_outlet(ctx, customer):
		refuse(_("Customer {0} is not a customer of shop {1}").format(customer, ctx.outlet))
	precision = money_precision(ctx.currency)
	try:
		limit = parse_limit(credit_limit, precision)
	except ValueError as e:
		invalid(str(e))
	check_modified(CUSTOMER, customer, modified)
	doc = frappe.get_doc(CUSTOMER, customer)
	rows = [row for row in doc.get("credit_limits") or [] if row.company == ctx.company]
	if limit is None:
		for row in rows:
			doc.remove(row)
	elif rows:
		rows[0].credit_limit = float(limit)
		for extra in rows[1:]:
			doc.remove(extra)
	else:
		doc.append("credit_limits", {"company": ctx.company, "credit_limit": float(limit)})
	doc.save(ignore_permissions=True)
	doc.add_comment(
		"Info",
		f"Credit limit for {ctx.company} set to "
		f"{'no limit' if limit is None else format_money(limit, precision)} from {ctx.source} by {ctx.approver}",
	)
	frappe.clear_document_cache(CUSTOMER, customer)
	row = credit_rows(ctx, only=customer)
	return row[0]
