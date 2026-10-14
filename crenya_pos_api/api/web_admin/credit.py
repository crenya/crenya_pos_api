"""A shop's credit customers on app.crenya.com: who has a credit limit or a balance, who is over the
limit, and the limit's edit. From a browser session only (`require_web_session`, inside `scope`), for
a user who may use the shop's company (`scope.manage_company`).

ERPNext stays the authority: the limit is the Customer's own *Credit Limit* row for the shop's
company, else its Customer Group's, else the Company's (`sync.credit`), and the balance
`get_customer_outstanding`. A limit of 0 or none, wherever it is read from, means no limit and reads
"No limit" (`credit_limit` null). Every row says where its limit comes from (`limit_source`):
clearing a customer's own limit does not mean "no limit" when the group or the company has one.

Reading is for the shop's managers. **Changing a limit is for a System Manager or an Accounts
Manager only** (founder decision 2026-10-14): the manager who approves an over-limit sale at the
till does not set the limit that approval is measured against.
"""

from decimal import Decimal

import frappe
from frappe import _
from frappe.query_builder import Order
from frappe.query_builder.functions import Count, Sum
from frappe.utils import cint

from crenya_pos_api.api.web_admin.scope import (
	manage_company,
	shop_profiles,
	user_may_use_company,
)
from crenya_pos_api.sync.context import money_precision, profile_users
from crenya_pos_api.sync.credit import customer_credit_limit_sources, customer_in_outlet
from crenya_pos_api.utils.dates import format_db_datetime
from crenya_pos_api.utils.decimal import (
	DecimalParseError,
	as_decimal,
	format_money,
	parse_decimal,
	quantize,
)
from crenya_pos_api.web.context import (
	WebAdminContext,
	check_modified,
	invalid,
	refuse,
	require_web_session,
)
from crenya_pos_api.web.reports import money_view

CUSTOMER = "Customer"
CREDIT_LIMIT = "Customer Credit Limit"
# who may change a limit
CREDIT_EDITOR_ROLES = ("System Manager", "Accounts Manager")
# the most customers listed, and the most candidates read before the list is cut
MAX_CREDIT_ROWS = 200
MAX_CANDIDATES = 1000
# the largest limit a Currency field holds
MAX_CREDIT_LIMIT = Decimal("999999999999")


def _outlet_customer_filter(ctx):
	"""Filters for the customers the outlet serves: not disabled, inside its customer groups."""
	filters = {"disabled": 0}
	roots = [row.customer_group for row in ctx.profile.get("customer_groups") or []]
	if roots:
		from crenya_pos_api.sync.pull import _descendants

		filters["customer_group"] = ["in", _descendants("Customer Group", roots) or [""]]
	return filters


def _balances(ctx):
	"""({customer: open invoices}, {customer: sum of outstanding amounts}, more) of the company's
	submitted sales, two grouped queries cut at MAX_CANDIDATES by the biggest balance: the
	candidates of the list, read without a call per customer."""
	invoice = frappe.qb.DocType("Sales Invoice")
	company = (invoice.company == ctx.company) & (invoice.docstatus == 1)
	balance_sum = Sum(invoice.outstanding_amount)
	balances = (
		frappe.qb.from_(invoice)
		.select(invoice.customer, balance_sum.as_("balance"))
		.where(company & (invoice.outstanding_amount != 0))
		.groupby(invoice.customer)
		.orderby(balance_sum, order=Order.desc)
		.limit(MAX_CANDIDATES + 1)
		.run(as_dict=True)
	)
	counts = (
		frappe.qb.from_(invoice)
		.select(invoice.customer, Count(invoice.name).as_("invoices"))
		.where(company & (invoice.is_return == 0) & (invoice.outstanding_amount > 0))
		.groupby(invoice.customer)
		.limit(MAX_CANDIDATES + 1)
		.run(as_dict=True)
	)
	more = len(balances) > MAX_CANDIDATES or len(counts) > MAX_CANDIDATES
	return (
		{row.customer: cint(row.invoices) for row in counts[:MAX_CANDIDATES]},
		{row.customer: as_decimal(row.balance) for row in balances[:MAX_CANDIDATES]},
		more,
	)


def _row(customer, limit, source, outstanding, open_invoices, precision) -> dict:
	"""One credit row (pure but for formatting): money as decimal strings, `credit_limit` and
	`available` null for a customer with no limit, `limit_source` where the limit is read from
	("customer", "group", "company", or null), `over_limit` the balance above the limit."""
	outstanding = quantize(outstanding, precision)
	over_limit = limit is not None and outstanding > quantize(limit, precision)
	available = None if limit is None else max(quantize(limit, precision) - outstanding, Decimal(0))
	return {
		"customer": customer.name,
		"name": customer.customer_name or customer.name,
		"modified": format_db_datetime(customer.modified),
		"credit_limit": None if limit is None else format_money(limit, precision),
		"limit_source": source if limit is not None else None,
		"outstanding": format_money(outstanding, precision),
		"available": None if available is None else format_money(available, precision),
		"over_limit": over_limit,
		"open_invoices": open_invoices,
	}


def _sort_key(row):
	return (not row["over_limit"], -Decimal(row["outstanding"]), row["name"])


def credit_rows(ctx, only=None) -> tuple[list[dict], bool]:
	"""(rows, more): the shop's credit customers, those with a limit of their own for its company
	or a balance (`only`: that one customer alone), over the limit first, then the biggest balance.
	Candidates and the first ordering come from grouped queries; ERPNext's exact balance is read for
	the MAX_CREDIT_ROWS listed only, and they are ordered again."""
	from erpnext.selling.doctype.customer.customer import get_customer_outstanding

	precision = money_precision(ctx.currency)
	more = False
	if only:
		names = [only]
		counts = {
			only: cint(
				frappe.db.count(
					"Sales Invoice",
					{
						"customer": only,
						"company": ctx.company,
						"docstatus": 1,
						"is_return": 0,
						"outstanding_amount": [">", 0],
					},
				)
			)
		}
		balances = {}
	else:
		counts, balances, more = _balances(ctx)
		own = frappe.get_all(
			CREDIT_LIMIT,
			filters={"parenttype": CUSTOMER, "company": ctx.company, "credit_limit": [">", 0]},
			pluck="parent",
			limit=MAX_CANDIDATES + 1,
		)
		more = more or len(own) > MAX_CANDIDATES
		names = sorted(set(own[:MAX_CANDIDATES]) | set(counts) | set(balances))
	if not names:
		return [], False
	filters = {"name": ["in", names]}
	customers = frappe.get_all(
		CUSTOMER,
		# the one customer just saved is answered whatever its state; a list is the shop's live ones
		filters=filters if only else {**_outlet_customer_filter(ctx), **filters},
		fields=["name", "customer_name", "customer_group", "modified"],
	)
	limits = customer_credit_limit_sources(customers, ctx.company)
	rows = [
		_row(
			customer,
			*limits[customer.name],
			balances.get(customer.name, Decimal(0)),
			counts.get(customer.name, 0),
			precision,
		)
		for customer in customers
	]
	rows.sort(key=_sort_key)
	if len(rows) > MAX_CREDIT_ROWS:
		more = True
		rows = rows[:MAX_CREDIT_ROWS]
	# the exact balance (ERPNext's, with advances and credit notes) of what is listed
	by_name = {customer.name: customer for customer in customers}
	exact = [
		_row(
			by_name[row["customer"]],
			*limits[row["customer"]],
			as_decimal(get_customer_outstanding(row["customer"], ctx.company)),
			row["open_invoices"],
			precision,
		)
		for row in rows
	]
	exact.sort(key=_sort_key)
	return exact, more


@frappe.whitelist(methods=["GET", "POST"])
def credit_customers(outlet: str) -> dict:
	"""The customers of the shop that have a credit limit or a balance, over the limit first:
	{"outlet", "currency", "rows": [{customer, name, modified, credit_limit, limit_source,
	outstanding, available, over_limit, open_invoices}], "more", "can_edit"}. `credit_limit` and
	`available` are null for a customer with no limit ("No limit"), `limit_source` says whether the
	limit is the customer's own, its group's or the company's. At most MAX_CREDIT_ROWS rows;
	`more` says the list was cut. `can_edit`: the user may change limits (System Manager or Accounts
	Manager)."""
	ctx = manage_company(outlet)
	rows, more = credit_rows(ctx)
	return {
		"outlet": ctx.outlet,
		"currency": money_view(ctx.currency),
		"rows": rows,
		"more": more,
		"can_edit": _is_credit_editor(),
	}


def _is_credit_editor() -> bool:
	return bool(set(frappe.get_roles()) & set(CREDIT_EDITOR_ROLES))


def credit_editor(outlet) -> WebAdminContext:
	"""The context of a shop whose credit limits the session user may change: a System Manager or an
	Accounts Manager (Crenya POS Manager is not enough), who works at the shop (a System Manager at
	every one) and may use its company. frappe.PermissionError otherwise."""
	require_web_session()
	if not _is_credit_editor():
		refuse(_("Credit limits are changed by a System Manager or an Accounts Manager"))
	if not outlet or not isinstance(outlet, str) or not shop_profiles(outlet):
		raise frappe.PermissionError
	user = frappe.session.user
	ctx = WebAdminContext(user, outlet)
	users = set(profile_users(ctx.profile))
	if "System Manager" not in frappe.get_roles(user) and users and user not in users:
		raise frappe.PermissionError
	if not user_may_use_company(ctx.company):
		raise frappe.PermissionError
	return ctx


def parse_limit(value, precision) -> Decimal | None:
	"""The limit typed (pure): a decimal string with at most `precision` decimals and at most
	MAX_CREDIT_LIMIT; 0 or blank clears the customer's own limit (None); ValueError for anything
	else, a negative included."""
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
	if limit > MAX_CREDIT_LIMIT:
		raise ValueError(f"The credit limit is more than {MAX_CREDIT_LIMIT:,}")
	return limit if limit > 0 else None


def audit_text(company, old, new, source, user, precision) -> str:
	"""The comment a limit change leaves on the Customer (pure): the old and the new figure."""

	def shown(value):
		return "no limit of its own" if value is None else format_money(value, precision)

	return f"Credit limit for {company} changed from {shown(old)} to {shown(new)} from {source} by {user}"


@frappe.whitelist(methods=["POST"])
def save_credit_limit(outlet: str, customer: str, credit_limit: str | None, modified: str) -> dict:
	"""Set the customer's own credit limit for the shop's company, a System Manager's or an Accounts
	Manager's: the Customer's *Credit Limit* row, written, or removed for 0 or blank (a bypass flag
	on the row is kept: the row then stays at 0). `modified` is the one `credit_customers` answered
	for the customer (changed since is refused). The customer must be one of the shop's. Clearing
	the customer's own limit is not "no limit" when its group or the company has one: the answer is
	the row as ERPNext now reads it, with `limit_source`."""
	ctx = credit_editor(outlet)
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
	old = as_decimal(rows[0].credit_limit) if rows and as_decimal(rows[0].credit_limit) > 0 else None
	if limit is None:
		for row in rows:
			if row.get("bypass_credit_limit_check"):
				row.credit_limit = 0
			else:
				doc.remove(row)
	elif rows:
		rows[0].credit_limit = float(limit)
		for extra in rows[1:]:
			doc.remove(extra)
	else:
		doc.append("credit_limits", {"company": ctx.company, "credit_limit": float(limit)})
	doc.save(ignore_permissions=True)
	doc.add_comment("Info", audit_text(ctx.company, old, limit, ctx.source, ctx.approver, precision))
	rows, _more = credit_rows(ctx, only=customer)
	return rows[0]
