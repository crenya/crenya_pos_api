"""An outlet's credit customers on app.crenya.com, for both products: a shop's Credit page and a
restaurant's House Account page. Who has a credit limit, a House Account or a balance, who is over the
limit, the limit's edit, the customer's open bills and a non-cash Settle, the page's totals, the
customer's payments and a payment's receipt PDF. From a browser session only (`require_web_session`,
inside `scope`), for a user who may use the outlet's company (`credit_view`).

**Products.** Which outlets a page opens and who manages one is the product's (`CreditProduct`): the
shop product is this app's (`SHOP`: a retail POS Profile, its Crenya POS Managers); crenya_restaurant
registers "restaurant" (`register_credit_product`: its outlets and Restaurant Managers) and wraps
every method. `scope.outlet_product` names the product of an outlet; a product nobody registered
opens nothing. A restaurant outlet lists the customers whose House Account is on **or** who owe
(RC16); a shop those with a credit limit of their own or a balance, as before.

ERPNext stays the authority: the limit is the Customer's own *Credit Limit* row for the outlet's
company, else its Customer Group's, else the Company's (`sync.credit`), and the balance
`get_customer_outstanding`. A limit of 0 or none, wherever it is read from, means no limit and reads
"No limit" (`credit_limit` null). Every row says where its limit comes from (`limit_source`):
clearing a customer's own limit does not mean "no limit" when the group or the company has one.

Reading is for the outlet's managers and for a System Manager or an Accounts Manager, who need not
be a manager of the outlet (P5): for them the boundary is the company (a User Permission on Company
is honoured; none means every company). **Changing a limit is for a System Manager or an Accounts
Manager only** (founder decisions 2026-10-14, P4): the manager who approves an over-limit sale at the
till does not set the limit that approval is measured against. The same two roles alone switch a
customer's House Account facility on or off (`save_credit_facility`, restaurant spec D6 and P4).
Recording a payment (`settle_customer`) is for whoever reads the page (Q2), in a non-cash mode of
the outlet only (D5, D7: cash is taken at the till, where a shift counts it), whatever the outlet's
credit switch says (a customer who still owes can pay).
"""

import json
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any, NamedTuple

import frappe
from frappe import _
from frappe.query_builder import Order
from frappe.query_builder.functions import Count, Sum
from frappe.utils import cint, get_fullname, get_system_timezone, getdate, now_datetime, nowdate

from crenya_pos_api.api.web_admin.scope import manages, outlet_product, shop_profiles, user_may_use_company
from crenya_pos_api.sync.context import money_precision, profile_users
from crenya_pos_api.sync.credit import (
	CREDIT_CHANGED_AT_FIELD,
	CREDIT_EDITOR_ROLES,
	CREDIT_ENABLED_FIELD,
	CREDIT_SALES_FIELD,
	GRANT_FLAG,
	WEB_GRANT,
	credited_to_account,
	customer_advance,
	customer_credit_limit_sources,
	customer_in_outlet,
	facility_editor_refusal,
	open_invoices_answer,
	payable_of,
)
from crenya_pos_api.sync.customer import LOCATION_FIELD, _default_customer_group, _default_territory
from crenya_pos_api.sync.customer_payment import (
	LOCAL_ID_FIELD,
	PAYMENT_ENTRY,
	PaymentBooking,
	payment_result,
	post_payment_entry,
)
from crenya_pos_api.sync.customer_payment import _invoices as ticked_invoice_names
from crenya_pos_api.sync.errors import SyncError
from crenya_pos_api.sync.events import customer_lock_key, named_lock
from crenya_pos_api.sync.validation import clean_location
from crenya_pos_api.utils.dates import format_db_datetime, site_naive_to_utc_iso
from crenya_pos_api.utils.decimal import (
	DecimalParseError,
	as_decimal,
	format_money,
	parse_decimal,
	quantize,
)
from crenya_pos_api.web.context import (
	WEB_SOURCE,
	WebAdminContext,
	check_modified,
	checked,
	invalid,
	parse_flag,
	refuse,
	require_web_session,
)
from crenya_pos_api.web.reports import money_view, parse_date

CUSTOMER = "Customer"
CREDIT_LIMIT = "Customer Credit Limit"
# the most customers listed, and the most candidates read before the list is cut
MAX_CREDIT_ROWS = 200
MAX_CANDIDATES = 1000
# the largest limit a Currency field holds
MAX_CREDIT_LIMIT = Decimal("999999999999")
# the most payments `customer_payments` lists, newest first
MAX_PAYMENT_ROWS = 200
# a Mode of Payment of this type is never taken on the web (D5, D7)
CASH_MODE_TYPE = "Cash"
# the remarks of every web settlement open with this and WEB_SOURCE
WEB_REMARKS_PREFIX = "Settled on "
# the print format of the web payment receipt (CC9; print_format/crenya_payment_receipt)
RECEIPT_PRINT_FORMAT = "crenya Payment Receipt"
RECEIPT_LANGUAGES = ("en", "ar")


class CreditProduct(NamedTuple):
	"""What differs between the products on the credit pages (the expenses seam's shape,
	`expenses.product.ExpenseProduct`)."""

	key: str  # "shop" | "restaurant"
	# [{name, company}] of this product's outlets, by name (of `outlet` alone when given)
	outlets: Callable[[str | None], list]
	# (roles, user, POS Profile) -> whether the user manages the outlet (reads its credit page)
	manages: Callable[[set, str, Any], bool]
	# (outlet context) -> the customers this product's credit page leaves out: never listed, never
	# making the company "owe", never settled here (PS8: a restaurant's service providers, settled on
	# their own page). None: nobody is left out (the shop)
	excluded_customers: Callable[[WebAdminContext], set[str]] | None = None
	# the refusal of a Settle of an excluded customer, with "{name}" for the customer's name, in
	# English (it goes through frappe._), e.g. "{name} is a service provider: record its payment on
	# Service providers → Settle"
	excluded_refusal: str | None = None
	# (roles, user, POS Profile) -> whether the user may switch the House Account on for a customer
	# they create on the page (NC5: a restaurant's outlet manager). None: P4 roles only
	# (CREDIT_EDITOR_ROLES), who always may. An existing customer's switch stays P4 only
	may_enable_new: Callable[[set, str, Any], bool] | None = None


def _manages_shop(roles, user, profile) -> bool:
	return manages(roles, user, set(profile_users(profile)))


SHOP = CreditProduct(key="shop", outlets=shop_profiles, manages=_manages_shop)
RESTAURANT = "restaurant"
_PRODUCTS = {SHOP.key: SHOP}


def register_credit_product(product: CreditProduct) -> None:
	"""Add `product`, or replace the one with its key (crenya_restaurant registers "restaurant" at
	import). Until a product is registered its outlets open no credit page."""
	_PRODUCTS[product.key] = product


def credit_product(key) -> CreditProduct | None:
	"""The registered product of `key`, or None."""
	return _PRODUCTS.get(key) if isinstance(key, str) else None


def outlet_credit_product(outlet) -> CreditProduct | None:
	"""The product whose credit page opens `outlet` (`scope.outlet_product`, confirmed by the
	product's own outlet list), or None: no such outlet, or its product is not registered."""
	product = credit_product(outlet_product(outlet))
	if product is None or not product.outlets(outlet):
		return None
	return product


def is_restaurant(ctx) -> bool:
	return getattr(ctx, "credit_product", SHOP.key) == RESTAURANT


def _ctx_product(ctx) -> CreditProduct:
	return credit_product(getattr(ctx, "credit_product", None)) or SHOP


def excluded_customers(ctx) -> set[str]:
	"""The customers the outlet's credit product leaves out of its page (`CreditProduct.
	excluded_customers`; PS8), read once per call: an empty set for a shop or a product without the
	seam."""
	seam = _ctx_product(ctx).excluded_customers
	if seam is None:
		return set()
	return {name for name in (seam(ctx) or ()) if isinstance(name, str)}


def excluded_refusal(ctx, customer) -> str | None:
	"""Why `customer` cannot be settled on the outlet's page (the product's words, `{name}` its
	customer name), or None when the product does not leave it out."""
	if customer not in excluded_customers(ctx):
		return None
	text = _ctx_product(ctx).excluded_refusal or "{name} cannot be settled on this page"
	name = frappe.db.get_value(CUSTOMER, customer, "customer_name") or customer
	# a refusal is shown as HTML (msgprint): the name is the user's text
	return _(text).format(name=frappe.utils.escape_html(name))


def _refuse_excluded(ctx, customer):
	refusal = excluded_refusal(ctx, customer)
	if refusal:
		refuse(refusal)


def _outlet_customer_filter(ctx):
	"""Filters for the customers the outlet serves: inside its customer groups, and a shop's not
	disabled. A restaurant's list keeps a disabled customer who still owes (F7: their bills can still
	be settled and they stay in the Grand total); the candidates of the list already leave out a
	disabled customer who owes nothing."""
	filters = {} if is_restaurant(ctx) else {"disabled": 0}
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
	("customer", "group", "company", or null), `over_limit` the balance above the limit,
	`credit_enabled` the customer's stored House Account flag (what the toggle shows) and
	`mobile_no` its phone (the page's search reads it), `location` the one line typed with it (null
	when none)."""
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
		"credit_enabled": bool(cint(customer.get(CREDIT_ENABLED_FIELD))),
		"mobile_no": customer.get("mobile_no") or None,
		"location": customer.get(LOCATION_FIELD) or None,
	}


def _sort_key(row):
	return (not row["over_limit"], -Decimal(row["outstanding"]), row["name"])


def _own_candidates(ctx) -> list:
	"""The customers listed whatever they owe: a shop's with a credit limit of their own for its
	company; a restaurant's with the House Account on (RC16). At most MAX_CANDIDATES + 1 names."""
	if is_restaurant(ctx):
		return frappe.get_all(
			CUSTOMER,
			filters={CREDIT_ENABLED_FIELD: 1, "disabled": 0},
			pluck="name",
			order_by="name asc",
			limit=MAX_CANDIDATES + 1,
		)
	return frappe.get_all(
		CREDIT_LIMIT,
		filters={"parenttype": CUSTOMER, "company": ctx.company, "credit_limit": [">", 0]},
		pluck="parent",
		limit=MAX_CANDIDATES + 1,
	)


def _location_fields() -> list[str]:
	return [LOCATION_FIELD] if frappe.get_meta(CUSTOMER).has_field(LOCATION_FIELD) else []


def credit_rows(ctx, only=None) -> tuple[list[dict], bool]:
	"""(rows, more): the outlet's credit customers (`only`: that one customer alone), over the limit
	first, then the biggest balance. A shop lists those with a limit of their own for its company or
	a balance, a restaurant those with the House Account on or a balance (`_own_candidates`), less
	the customers the product leaves out (`excluded_customers`; `only` is answered whatever).
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
		own = _own_candidates(ctx)
		more = more or len(own) > MAX_CANDIDATES
		names = sorted((set(own[:MAX_CANDIDATES]) | set(counts) | set(balances)) - excluded_customers(ctx))
	if not names:
		return [], False
	filters = {"name": ["in", names]}
	customers = frappe.get_all(
		CUSTOMER,
		# the one customer just saved is answered whatever its state; a list is the outlet's live ones
		filters=filters if only else {**_outlet_customer_filter(ctx), **filters},
		fields=[
			"name",
			"customer_name",
			"customer_group",
			"modified",
			"mobile_no",
			"disabled",
			CREDIT_ENABLED_FIELD,
			*_location_fields(),
		],
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
	if is_restaurant(ctx):
		# a disabled customer who still owes stays listed, marked so the page can say so
		for row in exact:
			if cint(by_name[row["customer"]].get("disabled")):
				row["disabled"] = True
	return exact, more


def parse_range(from_date, to_date):
	"""(from, to) as dates or None each (blank: open on that side; both blank: all time).
	frappe.ValidationError when not YYYY-MM-DD or reversed."""
	first = parse_date(from_date) if from_date else None
	last = parse_date(to_date) if to_date else None
	if first and last and last < first:
		raise frappe.ValidationError(_("From Date must be before To Date"))
	return first, last


def _date_filter(first, last):
	"""A `posting_date` filter for the range, or None for all time."""
	if first and last:
		return ["between", [first, last]]
	if first:
		return [">=", first]
	if last:
		return ["<=", last]
	return None


def credit_sale_part(invoice, precision) -> Decimal:
	"""What a submitted sale put on the customer's account (pure, Decimal >= 0): its payable total
	(`payable_of`) less what was paid at the sale (`paid_amount` less change), the write-off and
	redeemed loyalty. A sale paid in full has none; a desk invoice with no payment is all of it."""
	paid = as_decimal(invoice.get("paid_amount")) - as_decimal(invoice.get("change_amount"))
	part = (
		payable_of(invoice, precision)
		- quantize(paid, precision)
		- quantize(invoice.get("write_off_amount") or 0, precision)
		- quantize(invoice.get("loyalty_amount") or 0, precision)
	)
	return max(part, Decimal(0))


def credit_sales_of(customers, company, first, last, precision) -> dict:
	"""{customer: Decimal} (CC8): over each customer's submitted sales of `company` posted in the
	range, what they put on the account (`credit_sale_part`) less what returns of them credited back
	to it (`credited_to_account`, a House Account cancel included; never below 0 per sale). A
	cancelled sale is not submitted and counts nothing.

	Left out: a credit note that is no return of a sale of the range (a standalone one, or one
	against a sale posted before it) and anything booked outside Sales Invoices afterwards (a
	Journal Entry write-off, a reconciliation): those move `outstanding`, never `credit_sales`."""
	if not customers:
		return {}
	invoice = frappe.qb.DocType("Sales Invoice")
	where = (
		invoice.customer.isin(list(customers))
		& (invoice.company == company)
		& (invoice.docstatus == 1)
		& (invoice.is_return == 0)
		# not the sales paid in full at the sale: a wide cut (the rounded total may pass the grand
		# total by less than 1), the exact part is `credit_sale_part` below
		& (invoice.paid_amount + invoice.write_off_amount + invoice.loyalty_amount < invoice.grand_total + 1)
	)
	if first:
		where = where & (invoice.posting_date >= first)
	if last:
		where = where & (invoice.posting_date <= last)
	sales = (
		frappe.qb.from_(invoice)
		.select(
			invoice.name,
			invoice.customer,
			invoice.grand_total,
			invoice.rounded_total,
			invoice.disable_rounded_total,
			invoice.paid_amount,
			invoice.change_amount,
			invoice.write_off_amount,
			invoice.loyalty_amount,
		)
		.where(where)
		.run(as_dict=True)
	)
	parts = {row.name: credit_sale_part(row, precision) for row in sales}
	parts = {name: part for name, part in parts.items() if part > 0}
	returns = {}
	if parts:
		for ret in frappe.get_all(
			"Sales Invoice",
			filters={"return_against": ["in", list(parts)], "docstatus": 1, "is_return": 1},
			fields=[
				"name",
				"return_against",
				"grand_total",
				"rounded_total",
				"disable_rounded_total",
				"update_outstanding_for_self",
			],
		):
			returns.setdefault(ret.return_against, []).append(ret)
	refunds = {}
	names = [ret.name for rows in returns.values() for ret in rows]
	if names:
		for payment in frappe.get_all(
			"Sales Invoice Payment",
			filters={"parent": ["in", names], "parenttype": "Sales Invoice"},
			fields=["parent", "amount"],
		):
			refunds.setdefault(payment.parent, []).append(payment)
	totals = {}
	for row in sales:
		part = parts.get(row.name)
		if not part:
			continue
		credited = credited_to_account(returns.get(row.name), refunds, precision)
		totals[row.customer] = totals.get(row.customer, Decimal(0)) + max(part - credited, Decimal(0))
	return totals


def payments_of(customers, company, first, last) -> dict:
	"""{customer: Decimal} (CC8): the paid amounts of each customer's submitted Receive Payment
	Entries of `company` posted in the range."""
	if not customers:
		return {}
	entry = frappe.qb.DocType(PAYMENT_ENTRY)
	where = (
		(entry.party_type == CUSTOMER)
		& entry.party.isin(list(customers))
		& (entry.company == company)
		& (entry.docstatus == 1)
		& (entry.payment_type == "Receive")
	)
	if first:
		where = where & (entry.posting_date >= first)
	if last:
		where = where & (entry.posting_date <= last)
	rows = (
		frappe.qb.from_(entry)
		.select(entry.party, Sum(entry.paid_amount).as_("paid"))
		.where(where)
		.groupby(entry.party)
		.run(as_dict=True)
	)
	return {row.party: as_decimal(row.paid) for row in rows}


TOTAL_KEYS = ("credit_sales", "payments", "outstanding")


def range_totals(rows, precision) -> dict:
	"""The Grand total row (pure): each of TOTAL_KEYS summed over `rows`, as decimal strings."""
	return {
		key: format_money(sum((as_decimal(row[key]) for row in rows), Decimal(0)), precision)
		for key in TOTAL_KEYS
	}


@frappe.whitelist(methods=["GET", "POST"])
def credit_customers(outlet: str, from_date: str | None = None, to_date: str | None = None) -> dict:
	"""The outlet's credit customers (`credit_rows`), over the limit first: {"outlet", "currency",
	"rows": [{customer, name, modified, credit_limit, limit_source, outstanding, available,
	over_limit, open_invoices, credit_enabled, mobile_no, credit_sales, payments}], "more",
	"can_edit", "totals"}. `credit_limit` and `available` are null for a customer with no limit ("No
	limit"), `limit_source` says whether the limit is the customer's own, its group's or the
	company's. At most MAX_CREDIT_ROWS rows; `more` says the list was cut. `can_edit`: the user may
	change limits (System Manager or Accounts Manager).

	CC8: `credit_sales` and `payments` are the customer's for `from_date`..`to_date` (YYYY-MM-DD,
	either open; default all time, `credit_sales_of`, `payments_of`); `outstanding` stays ERPNext's
	balance today, whatever the range. `totals` {credit_sales, payments, outstanding} sums the rows
	listed (the Grand total row)."""
	ctx = credit_view(outlet)
	first, last = parse_range(from_date, to_date)
	rows, more = credit_rows(ctx)
	precision = money_precision(ctx.currency)
	names = [row["customer"] for row in rows]
	sales = credit_sales_of(names, ctx.company, first, last, precision)
	paid = payments_of(names, ctx.company, first, last)
	for row in rows:
		row["credit_sales"] = format_money(sales.get(row["customer"], Decimal(0)), precision)
		row["payments"] = format_money(paid.get(row["customer"], Decimal(0)), precision)
	return {
		"outlet": ctx.outlet,
		"currency": money_view(ctx.currency),
		"rows": rows,
		"more": more,
		"can_edit": _is_credit_editor(),
		"totals": range_totals(rows, precision),
	}


def _is_credit_editor() -> bool:
	return bool(set(frappe.get_roles()) & set(CREDIT_EDITOR_ROLES))


def credit_view(outlet) -> WebAdminContext:
	"""The context of an outlet whose credit page the session user may open, for the outlet's
	product (`outlet_credit_product`): a manager of the outlet by the product's rule, or a System
	Manager or an Accounts Manager (who need not be its manager, P5), in every case only for a
	company they may use (User Permission on Company). From a browser session only. The context
	carries `credit_product` (the product's key). frappe.PermissionError otherwise, for an unknown
	outlet and one whose product is not registered too."""
	require_web_session()
	product = outlet_credit_product(outlet)
	if product is None:
		raise frappe.PermissionError
	user = frappe.session.user
	ctx = WebAdminContext(user, outlet)
	roles = set(frappe.get_roles(user))
	if not (roles & set(CREDIT_EDITOR_ROLES)) and not product.manages(roles, user, ctx.profile):
		raise frappe.PermissionError
	if not user_may_use_company(ctx.company):
		raise frappe.PermissionError
	ctx.credit_product = product.key
	return ctx


def credit_editor(outlet, refusal=None) -> WebAdminContext:
	"""`credit_view` for a user who may change the outlet's limits (or a customer's House Account
	facility): a System Manager or an Accounts Manager (an outlet manager is not enough, P4).
	frappe.PermissionError otherwise, with `refusal` (default: the credit limits' words)."""
	require_web_session()
	if not _is_credit_editor():
		refuse(refusal or _("Credit limits are changed by a System Manager or an Accounts Manager"))
	return credit_view(outlet)


def owing_companies(companies, excluded=None) -> set:
	"""The companies of `companies` where a House Account is in use: a submitted sale made at a till
	(it has a POS Profile) that is still outstanding, or any customer with the House Account facility
	on. An ordinary unpaid desk invoice does not count, so an outlet that never switched credit on
	does not grow a House Account tab because the company has a B2B receivable. `excluded`
	({company: customers}, PS8) leaves those customers of a company out of both rules; a company
	not in it (a shop's, always) is read as before."""
	if not companies:
		return set()
	excluded = {
		company: names for company, names in (excluded or {}).items() if names and company in companies
	}
	plain = set(companies) - set(excluded)
	owing = set()
	if frappe.db.exists(CUSTOMER, {CREDIT_ENABLED_FIELD: 1, "disabled": 0}):
		owing |= plain
	elif plain:
		owing |= _till_sale_owing(plain)
	for company, names in excluded.items():
		enabled = frappe.get_all(
			CUSTOMER,
			filters=[[CREDIT_ENABLED_FIELD, "=", 1], ["disabled", "=", 0], ["name", "not in", list(names)]],
			pluck="name",
			limit=1,
		)
		if enabled:
			owing.add(company)
		elif _till_sale_owing({company}, names):
			owing.add(company)
	return owing


def _till_sale_owing(companies, excluded=()) -> set:
	"""The companies of `companies` with a submitted till sale still outstanding, of a customer not
	in `excluded`."""
	invoice = frappe.qb.DocType("Sales Invoice")
	where = (
		invoice.company.isin(list(companies))
		& (invoice.docstatus == 1)
		& (invoice.is_return == 0)
		& (invoice.outstanding_amount > 0)
		& invoice.pos_profile.isnotnull()
		& (invoice.pos_profile != "")
	)
	if excluded:
		where = where & invoice.customer.notin(list(excluded))
	rows = frappe.qb.from_(invoice).select(invoice.company).where(where).groupby(invoice.company).run()
	return {row[0] for row in rows}


def _excluded_by_company(product, user, allowed) -> dict | None:
	"""{company: customers the product leaves out} for `credit_shops`, asked once per company (of
	its first outlet listed), or None for a product without the seam (a shop)."""
	if product.excluded_customers is None:
		return None
	excluded = {}
	for row, _profile in allowed:
		if row.company in excluded:
			continue
		ctx = WebAdminContext(user, row.name)
		ctx.credit_product = product.key
		excluded[row.company] = excluded_customers(ctx)
	return excluded


@frappe.whitelist(methods=["GET", "POST"])
def credit_shops(product: str = "shop") -> dict:
	"""The outlets of `product` ("shop", or "restaurant" once registered) the session user may open
	the credit page of: {"shops": [{name, outlet_name, company, currency, credit_view: true,
	credit_enabled, can_edit, credit_on, owing}]}, by name. A System Manager or an Accounts Manager
	gets every outlet of the companies they may use (a User Permission on Company is honoured; none
	means all), an outlet's manager the outlets they manage (read only: `can_edit` false), a cashier
	none. Unlike `outlets`, an outlet is listed without the user managing it, so the web shows the
	tab to an accountant who cannot open the outlet's other pages.

	`credit_on` (= `credit_enabled`) is the outlet's sell-on-account switch, `owing` whether a
	House Account is in use in its company (`owing_companies`: a till sale still unpaid, or a customer with the facility on). A shop is listed either way; a restaurant outlet only when
	one of the two is true (RC16: switched off, the page stays while somebody owes). A product that
	is not registered lists nothing."""
	require_web_session()
	chosen = credit_product(product or SHOP.key)
	if chosen is None:
		return {"shops": []}
	user = frappe.session.user
	roles = set(frappe.get_roles(user))
	editor = bool(roles & set(CREDIT_EDITOR_ROLES))
	allowed = []
	for row in chosen.outlets(None):
		profile = frappe.get_cached_doc("POS Profile", row.name)
		if not (editor or chosen.manages(roles, user, profile)):
			continue
		if not user_may_use_company(row.company):
			continue
		allowed.append((row, profile))
	owing = owing_companies(
		{row.company for row, _profile in allowed}, _excluded_by_company(chosen, user, allowed)
	)
	shops = []
	for row, profile in allowed:
		credit_on = bool(cint(profile.get(CREDIT_SALES_FIELD)))
		owes = row.company in owing
		if chosen.key != SHOP.key and not (credit_on or owes):
			continue
		shops.append(
			{
				"name": row.name,
				"outlet_name": row.name,
				"company": row.company,
				"currency": frappe.get_cached_value("Company", row.company, "default_currency"),
				"credit_view": True,
				"credit_enabled": credit_on,
				"can_edit": editor,
				"credit_on": credit_on,
				"owing": owes,
			}
		)
	return {"shops": shops}


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
	_refuse_excluded(ctx, customer)
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


def facility_customer_refusal(ctx, customer):
	"""Why `customer` cannot have its House Account facility changed for the outlet (a refusal in
	the user's words), or None.

	ERPNext's Customer has no company: one customer may buy from every company of the site. "A
	customer of the outlet's company" is therefore a customer the outlet's tills may sell to, the
	same set the till's customer pull sends and the Credit page lists (`customer_in_outlet`: an
	existing customer inside the outlet's customer groups, any when the POS Profile limits none),
	less two that are never a House Account of this outlet: the outlet's default (walk-in)
	customer, and an internal customer that stands for another company (`represents_company`)."""
	if not customer_in_outlet(ctx, customer):
		return _("Customer {0} is not a customer of {1}").format(customer, ctx.outlet)
	if customer == ctx.profile.get("customer"):
		return _("{0} is the outlet's default customer and cannot have a House Account").format(customer)
	internal, company = frappe.db.get_value(
		CUSTOMER, customer, ["is_internal_customer", "represents_company"]
	)
	if cint(internal) and company and company != ctx.company:
		return _("Customer {0} is a customer of {1}, not of {2}").format(customer, company, ctx.company)
	return None


def facility_audit_text(enabled, source, user) -> str:
	"""The comment a facility change leaves on the Customer (pure)."""
	state = "switched on" if enabled else "switched off"
	return f"House Account (credit facility) {state} from {source} by {user}"


@frappe.whitelist(methods=["POST"])
def save_credit_facility(outlet: str, customer: str, enabled: bool | int | str) -> dict:
	"""Switch the customer's House Account facility (`crenya_credit_enabled`) on or off, a System
	Manager's or an Accounts Manager's only (P4), from a browser session: {customer, credit_enabled,
	changed_at}. `enabled` is 1 / 0 (true / false). The customer must be one of the outlet's
	(`facility_customer_refusal`); a disabled customer can be switched off, not on. The save is a
	Version of the Customer (it tracks changes) and leaves a Comment naming the web user; saving the
	state it already has changes nothing. `changed_at` (UTC ISO-8601, null when never changed) is
	when the facility or `disabled` last changed, stamped by the Customer validate hook."""
	ctx = credit_editor(outlet, facility_editor_refusal())
	flag = checked(parse_flag, enabled, "enabled")
	if flag is None:
		invalid(_("enabled is required"))
	refusal = facility_customer_refusal(ctx, customer)
	if refusal:
		refuse(refusal)
	_refuse_excluded(ctx, customer)
	doc = frappe.get_doc(CUSTOMER, customer)
	if flag and cint(doc.disabled):
		invalid(_("Customer {0} is disabled: enable the customer first").format(customer))
	if bool(cint(doc.get(CREDIT_ENABLED_FIELD))) != flag:
		doc.set(CREDIT_ENABLED_FIELD, 1 if flag else 0)
		# always a Version (Frappe skips it under tests unless asked): the audit of D6 / K
		doc.save(ignore_permissions=True, ignore_version=False)
		doc.add_comment("Info", facility_audit_text(flag, ctx.source, ctx.approver))
	changed_at = doc.get(CREDIT_CHANGED_AT_FIELD)
	return {
		"customer": doc.name,
		"credit_enabled": bool(cint(doc.get(CREDIT_ENABLED_FIELD))),
		"changed_at": site_naive_to_utc_iso(changed_at, get_system_timezone()) if changed_at else None,
	}


# New customer (provider settlement NC5, 7.5)

MAX_NAME_LENGTH = 140
MAX_MOBILE_LENGTH = 20  # pos-core's
MAX_EMAIL_LENGTH = 140
MOBILE_MIN_DIGITS = 6
MOBILE_CHARACTERS = frozenset("0123456789 +-()")


def mobile_digits(value) -> str:
	"""The digits of a phone number (pure): what two numbers are compared by."""
	return "".join(char for char in value or "" if char.isascii() and char.isdigit())


def clean_new_customer(value) -> dict:
	"""The New customer form (pure but for translation; a dict, or its JSON from a form post):
	{customer_name, mobile_no, email_id, location}, stripped. The till's rules (pos-core
	`customers::validate`): the name required and at most 140; the mobile required, at most 20, at
	least 6 digits and nothing but digits, spaces and + - ( ); the email optional, at most 140,
	something@domain.tld; the location one line of at most 140 (`clean_location`). Any other key is
	dropped. ValueError otherwise."""
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			value = None
	if not isinstance(value, dict):
		raise ValueError(_("customer must be an object"))

	def text(key):
		field = value.get(key)
		if field is None:
			return ""
		if not isinstance(field, str):
			raise ValueError(_("{0} must be text").format(key))
		return field.strip()

	name = text("customer_name")
	if not name:
		raise ValueError(_("Enter the customer's name"))
	if len(name) > MAX_NAME_LENGTH:
		raise ValueError(_("The name is longer than {0} characters").format(MAX_NAME_LENGTH))
	mobile = text("mobile_no")
	if not mobile:
		raise ValueError(_("Enter the mobile number"))
	if (
		len(mobile) > MAX_MOBILE_LENGTH
		or len(mobile_digits(mobile)) < MOBILE_MIN_DIGITS
		or not set(mobile) <= MOBILE_CHARACTERS
	):
		raise ValueError(_("Mobile number is not valid"))
	email = text("email_id") or None
	if email:
		user, at, domain = email.partition("@")
		if len(email) > MAX_EMAIL_LENGTH or not (user and at and "." in domain) or " " in email:
			raise ValueError(_("Email is not valid"))
	try:
		location = clean_location(value.get("location"))
	except SyncError as e:
		raise ValueError(e.message)
	return {"customer_name": name, "mobile_no": mobile, "email_id": email, "location": location}


def _may_enable_new(ctx, roles, user) -> bool:
	"""Whether the user may switch the House Account on for a customer they create here: P4 roles
	always, else the product's `may_enable_new` (none: nobody else)."""
	if roles & set(CREDIT_EDITOR_ROLES):
		return True
	seam = _ctx_product(ctx).may_enable_new
	return bool(seam and seam(roles, user, ctx.profile))


def _outlet_customer_with_mobile(ctx, mobile) -> dict | None:
	"""A customer of the outlet (inside its customer groups, disabled ones too) whose mobile has the
	same digits as `mobile`, or None."""
	filters = {}
	roots = [row.customer_group for row in ctx.profile.get("customer_groups") or []]
	if roots:
		from crenya_pos_api.sync.pull import _descendants

		filters["customer_group"] = ["in", _descendants("Customer Group", roots) or [""]]
	digits = mobile_digits(mobile)
	# narrowed in SQL first: a number with the same digits holds its last MOBILE_MIN_DIGITS digits in
	# order, whatever separators lie between them; the digits are then compared exactly
	tail = digits[-MOBILE_MIN_DIGITS:]
	filters["mobile_no"] = ["like", "%" + "%".join(tail) + "%"]
	for row in frappe.get_all(
		CUSTOMER, filters=filters, fields=["name", "customer_name", "mobile_no"], order_by="creation asc"
	):
		if mobile_digits(row.mobile_no) == digits:
			return row
	return None


def new_customer_group(ctx) -> str:
	"""The Customer Group of a customer created on the outlet's page: one inside the outlet's
	customer groups, so it is listed and found again there. The till's default
	(`sync.customer._default_customer_group`) when it lies inside them (or the outlet limits none),
	else the first leaf under them by name. frappe.ValidationError when they hold no leaf."""
	chosen = _default_customer_group(ctx, None)
	roots = [row.customer_group for row in ctx.profile.get("customer_groups") or []]
	if not roots:
		return chosen
	from crenya_pos_api.sync.pull import _descendants

	inside = _descendants("Customer Group", roots)
	if chosen in inside and not cint(frappe.db.get_value("Customer Group", chosen, "is_group")):
		return chosen
	leaves = frappe.get_all(
		"Customer Group",
		filters={"name": ["in", inside or [""]], "is_group": 0},
		pluck="name",
		order_by="name asc",
		limit=1,
	)
	if not leaves:
		invalid(
			_("{0}'s customer groups have no group a customer can be in: add one on the outlet").format(
				ctx.outlet
			)
		)
	return leaves[0]


def _created_before(ctx, request_id, user) -> str | None:
	"""The customer this request already created, or None. One another user created (or no customer
	of the outlet) is refused: a request id reads back only what the same user made with it."""
	name = frappe.db.get_value(CUSTOMER, {LOCAL_ID_FIELD: request_id}, "name")
	if not name:
		return None
	if frappe.db.get_value(CUSTOMER, name, "owner") != user or not customer_in_outlet(ctx, name):
		refuse(_("This request was used for another customer: close the dialog and open it again"))
	return name


def new_customer_comment(source, user, enabled) -> str:
	"""The comment a web New customer leaves on the Customer (pure; shown as HTML, so escaped)."""
	from frappe.utils import escape_html

	text = f"Created on {escape_html(source)} by {escape_html(user)}"
	return f"{text}; House Account on" if enabled else text


@frappe.whitelist(methods=["POST"])
def create_credit_customer(
	outlet: str,
	customer: dict | str,
	enable_credit: bool | int | str,
	credit_limit: str | None = None,
	request_id: str | None = None,
) -> dict:
	"""Create a customer from the House Account page (NC5): `customer` {customer_name, mobile_no,
	email_id, location} (`clean_new_customer`), for whoever reads the outlet's credit page
	(`credit_view`). Answers the new customer's `credit_rows` row.

	- `enable_credit` (1 / 0, true / false) switches its House Account on, for a P4 role or whom the
	  product's `may_enable_new` names (a restaurant's outlet manager), through the server-only
	  grant (`sync.credit.GRANT_FLAG` = "web"); anyone else asking is refused and nothing is made.
	- `credit_limit` (optional; blank or 0: none) is the Customer's own Credit Limit row for the
	  outlet's company, set by a P4 role only (`parse_limit`).
	- A mobile with the same digits as one of the outlet's customers' is refused: "Use {name}
	  instead".
	- `request_id` (a UUID, required) is kept in the Customer's `crenya_local_id`: the same request
	  again answers the customer it created, whatever else it now says.

	The customer is put in a Customer Group inside the outlet's (`new_customer_group`), so it is
	listed there at once; an outlet whose groups hold no leaf group is refused.

	Runs under one lock per company, so two requests at once cannot make the same customer twice.
	The Customer is owned by the web user; a Comment names them ("Created on app.crenya.com by
	{user}", "; House Account on" when switched on)."""
	ctx = credit_view(outlet)
	user = frappe.session.user
	roles = set(frappe.get_roles(user))
	data = checked(clean_new_customer, customer)
	enabled = checked(parse_flag, enable_credit, "enable_credit")
	if enabled is None:
		invalid(_("enable_credit is required"))
	request = checked(parse_request_id, request_id)
	precision = money_precision(ctx.currency)
	limit = checked(parse_limit, credit_limit, precision)
	editor = bool(roles & set(CREDIT_EDITOR_ROLES))
	if limit is not None and not editor:
		refuse(_("Credit limits are changed by a System Manager or an Accounts Manager"))
	if enabled and not _may_enable_new(ctx, roles, user):
		refuse(facility_editor_refusal())
	group = new_customer_group(ctx)
	entered = False
	try:
		# the lock commits the open transaction first: nothing has been written before it
		with named_lock(f"credit-new-customer:{ctx.company}"):
			entered = True
			name = _created_before(ctx, request, user)
			if name is None:
				name = _insert_credit_customer(ctx, data, enabled, limit, request, precision, group)
	except frappe.ValidationError:
		if not entered:
			frappe.throw(
				_("Another customer is being created right now: try again in a moment"),
				frappe.ValidationError,
				title=_("Try again"),
			)
		raise
	rows, _more = credit_rows(ctx, only=name)
	return rows[0]


def _insert_credit_customer(ctx, data, enabled, limit, request, precision, group) -> str:
	same = _outlet_customer_with_mobile(ctx, data["mobile_no"])
	if same:
		invalid(_("Use {0} instead").format(same.customer_name or same.name))
	doc = frappe.new_doc(CUSTOMER)
	doc.update(
		{
			"customer_name": data["customer_name"],
			"customer_type": "Individual",
			"customer_group": group,
			"territory": _default_territory(ctx, None),
			"mobile_no": data["mobile_no"],
			"email_id": data["email_id"],
			LOCAL_ID_FIELD: request,
		}
	)
	if data["location"] and doc.meta.has_field(LOCATION_FIELD):
		doc.set(LOCATION_FIELD, data["location"])
	if enabled:
		doc.set(CREDIT_ENABLED_FIELD, 1)
		# counted only inside this insert of a new customer (sync.credit._granted_on_create)
		doc.flags[GRANT_FLAG] = WEB_GRANT
	if limit is not None:
		doc.append("credit_limits", {"company": ctx.company, "credit_limit": float(limit)})
	# who may create and enable was checked above, by the product's rule: a restaurant manager has
	# no desk right on Customer
	doc.insert(ignore_permissions=True)
	doc.add_comment("Info", new_customer_comment(ctx.source, ctx.approver, enabled))
	if limit is not None:
		doc.add_comment("Info", audit_text(ctx.company, None, limit, ctx.source, ctx.approver, precision))
	return doc.name


# open bills and Settle (RC10, D5, D7, Q2)


def outlet_customer_refusal(ctx, customer):
	"""Why `customer` has no open bills or payments on the outlet's page (the user's words), or None:
	a customer the outlet's tills may sell to (`customer_in_outlet`), never its default (walk-in)
	customer."""
	if not customer_in_outlet(ctx, customer):
		return _("Customer {0} is not a customer of {1}").format(str(customer)[:140], ctx.outlet)
	if customer == ctx.profile.get("customer"):
		return _("{0} is the outlet's default customer and has no account").format(customer)
	return None


def _require_outlet_customer(ctx, customer):
	refusal = outlet_customer_refusal(ctx, customer)
	if refusal:
		refuse(refusal)


def outlet_modes(profile) -> list[dict]:
	"""[{mode_of_payment, type}] of the POS Profile's modes of payment, in its order (each once);
	`type` is the Mode of Payment's own (Cash, Bank, General, Phone; "" when it has none)."""
	names = []
	for row in profile.get("payments") or []:
		if row.mode_of_payment and row.mode_of_payment not in names:
			names.append(row.mode_of_payment)
	if not names:
		return []
	types = {
		row.name: row.type
		for row in frappe.get_all("Mode of Payment", filters={"name": ["in", names]}, fields=["name", "type"])
	}
	return [{"mode_of_payment": name, "type": types.get(name) or ""} for name in names]


def is_settle_mode(mode) -> bool:
	"""Whether a mode ({mode_of_payment, type}) may record a payment on the web (pure): it has a type
	and the type is not Cash (D5, D7)."""
	kind = (mode.get("type") or "").strip()
	return bool(kind) and kind.lower() != CASH_MODE_TYPE.lower()


@frappe.whitelist(methods=["GET", "POST"])
def customer_open_invoices(outlet: str, customer: str) -> dict:
	"""The customer's unpaid and part-paid bills for the Settle dialog: the till's `get_open_invoices`
	answer (`open_invoices_answer`: {customer, outstanding, advance, as_of, more, invoices}, the
	company's bills of every outlet, oldest first; `outstanding` is ERPNext's net balance, shown
	beside `advance`, never less it again) plus `modes`, the outlet's non-cash modes [{mode_of_payment,
	type}] (D5, D7). Answered whatever the outlet's credit switch says (RC2, Review Focus 1)."""
	ctx = credit_view(outlet)
	_require_outlet_customer(ctx, customer)
	answer = open_invoices_answer(customer, ctx.company, money_precision(ctx.currency))
	answer["modes"] = [mode for mode in outlet_modes(ctx.profile) if is_settle_mode(mode)]
	return answer


def parse_settle_amount(value, precision) -> Decimal:
	"""The amount typed (pure): a decimal string above 0 with at most `precision` decimals and at
	most MAX_CREDIT_LIMIT; ValueError otherwise."""
	try:
		amount = parse_decimal(value, "amount")
	except DecimalParseError as e:
		raise ValueError(str(e))
	if amount <= 0:
		raise ValueError("The amount must be more than 0")
	if quantize(amount, precision) != amount:
		raise ValueError(f"The amount has more than {precision} decimals")
	if amount > MAX_CREDIT_LIMIT:
		raise ValueError(f"The amount is more than {MAX_CREDIT_LIMIT:,}")
	return amount


def parse_request_id(value) -> str:
	"""The dialog's request id (pure): a UUID, answered in its canonical form; ValueError otherwise."""
	if not isinstance(value, str) or not value.strip():
		raise ValueError("request_id is required")
	try:
		return str(uuid.UUID(value.strip()))
	except ValueError:
		raise ValueError("request_id must be a UUID")


def parse_ticked(value) -> list[str] | None:
	"""The ticked bills in the order shown (a list, or its JSON from a form post), checked as a till's
	`invoices` are; None (oldest first) for none."""
	if value is None or value == "":
		return None
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			raise ValueError("invoices must be a list of invoice names")
	try:
		names = ticked_invoice_names({"invoices": value})
	except SyncError as e:
		raise ValueError(e.message)
	return names or None


def frozen_till(company):
	"""The date accounts are frozen up to, or None: the Company's `accounts_frozen_till_date`
	(ERPNext v16), else Accounts Settings `acc_frozen_upto` (v15)."""
	if frappe.get_meta("Company").has_field("accounts_frozen_till_date"):
		value = frappe.db.get_value("Company", company, "accounts_frozen_till_date")
	elif frappe.get_meta("Accounts Settings").has_field("acc_frozen_upto"):
		value = frappe.db.get_single_value("Accounts Settings", "acc_frozen_upto")
	else:
		value = None
	return getdate(value) if value else None


def parse_posting_date(value, company):
	"""The date the customer paid: YYYY-MM-DD (blank: today), not after today and after the date
	the company's accounts are frozen up to. frappe.ValidationError otherwise."""
	today = getdate(nowdate())
	day = parse_date(value) if value else today
	if day > today:
		invalid(_("The date cannot be after today"))
	frozen = frozen_till(company)
	if frozen and day <= frozen:
		invalid(_("The accounts of {0} are frozen up to {1}: choose a later date").format(company, frozen))
	return day


# the part of a web settlement's remarks that names the bills ticked, in order
TICKED_REMARK = "; ticked "


def settle_remarks(user, reference, ticked=None) -> str:
	"""The remarks of a web settlement (pure): where it was made and by whom, the reference, and the
	bills ticked in order (`ticked_of` reads them back to check a resent request)."""
	remarks = f"{WEB_REMARKS_PREFIX}{WEB_SOURCE} by {user}; reference {reference}"
	if ticked:
		remarks += TICKED_REMARK + ", ".join(ticked)
	return remarks


def ticked_of(remarks) -> list[str]:
	"""The bills a web settlement ticked, from its remarks (pure): [] for none (oldest first)."""
	from crenya_pos_api.sync.customer_payment import SKIPPED_REMARK

	head = (remarks or "").rpartition(f"; {SKIPPED_REMARK}")
	text = head[0] if head[1] else (remarks or "")
	_before, mark, tail = text.rpartition(TICKED_REMARK)
	return [name for name in tail.split(", ") if name] if mark else []


def settle_comment(entry, amount, mode, reference, user) -> str:
	"""The comment a web settlement leaves on the Customer (pure)."""
	return f"Payment {entry} of {amount} ({mode}, reference {reference}) recorded from {WEB_SOURCE} by {user}"


def _settled_before(ctx, customer, request_id, booking, precision):
	"""The submitted web settlement this request already booked, or None. An id another payment
	holds (a till's, another customer's, a cancelled one) is refused; so is the same request sent
	again with another amount, mode or ticked bills (the dialog is opened again for a new one)."""
	name = frappe.db.get_value(PAYMENT_ENTRY, {LOCAL_ID_FIELD: request_id}, "name")
	if not name:
		return None
	doc = frappe.get_doc(PAYMENT_ENTRY, name)
	if (
		doc.docstatus != 1
		or doc.payment_type != "Receive"
		or doc.party != customer
		or doc.company != ctx.company
		or not (doc.remarks or "").startswith(WEB_REMARKS_PREFIX)
	):
		refuse(_("This request was used for another payment: close the dialog and open it again"))
	if (
		quantize(doc.paid_amount, precision) != booking.amount
		or doc.mode_of_payment != booking.mode_of_payment
		or ticked_of(doc.remarks) != list(booking.invoices or [])
	):
		invalid(
			_(
				"This request was already recorded as {0} with another amount, mode or bills: close the "
				"dialog and open it again"
			).format(doc.name)
		)
	return doc


def _busy(customer):
	frappe.throw(
		_("Another payment of {0} is being recorded right now: try again in a moment").format(customer),
		frappe.ValidationError,
		title=_("Try again"),
	)


@frappe.whitelist(methods=["POST"])
def settle_customer(
	outlet: str,
	customer: str,
	mode_of_payment: str,
	amount: str,
	invoices: list | str | None = None,
	reference: str | None = None,
	posting_date: str | None = None,
	request_id: str | None = None,
) -> dict:
	"""Record a customer's payment from app.crenya.com (RC10): one submitted Payment Entry (Receive)
	booked by the till's own booking function (`post_payment_entry`): allocated to the ticked
	`invoices` in the order given (a bill paid meanwhile is skipped, the rest stays an advance), or
	oldest first when none are ticked. No shift and no device: it is in no Day Close.

	For whoever reads the outlet's credit page (`credit_view`: its manager, an accountant; Q2), from
	a browser session, whatever the outlet's credit switch says (a customer who still owes can pay).
	`mode_of_payment` must be one of the outlet's and not a cash one (D5, D7); `reference` (the slip,
	transfer or cheque number) is required; `amount` above 0 at the currency's decimals;
	`posting_date` (default today) not after today nor in frozen accounts; `request_id` a UUID, kept
	in the entry's `crenya_local_id`: the same request again answers the entry it booked. A customer
	the outlet's product leaves out (`excluded_customers`, PS8: a service provider) is refused in
	the product's words.

	Runs under the customer's lock (`named_lock`), the one a till's receipt of the customer takes, so
	the two never allocate at once; a lock not had in time answers "try again". The entry is owned by
	the web user, its remarks and a Comment on the customer name them and app.crenya.com.

	{payment_entry, allocated, unallocated, invoices, skipped, outstanding, advance}: the allocation
	(`skipped`: ticked bills it could not pay) and the customer's balance and advance after it."""
	from erpnext.selling.doctype.customer.customer import get_customer_outstanding

	ctx = credit_view(outlet)
	_require_outlet_customer(ctx, customer)
	_refuse_excluded(ctx, customer)
	precision = money_precision(ctx.currency)
	modes = {mode["mode_of_payment"]: mode for mode in outlet_modes(ctx.profile)}
	mode = modes.get(mode_of_payment) if isinstance(mode_of_payment, str) else None
	if mode is None:
		invalid(_("Mode of payment {0} is not one of {1}'s").format(str(mode_of_payment)[:140], ctx.outlet))
	if not is_settle_mode(mode):
		invalid(
			_("Cash is taken at the till, so it is counted in a shift: choose a bank, card or cheque mode")
		)
	value = checked(parse_settle_amount, amount, precision)
	if not isinstance(reference, str) or not reference.strip():
		invalid(_("Enter the reference (slip, transfer or cheque number)"))
	reference = reference.strip()
	if len(reference) > 140:
		invalid(_("The reference is longer than 140 characters"))
	day = parse_posting_date(posting_date, ctx.company)
	request = checked(parse_request_id, request_id)
	ticked = checked(parse_ticked, invoices)
	user = frappe.session.user
	booking = PaymentBooking(
		company=ctx.company,
		customer=customer,
		mode_of_payment=mode_of_payment,
		amount=value,
		currency=ctx.currency,
		posting_date=str(day),
		local_id=request,
		reference=reference,
		remarks=settle_remarks(user, reference, ticked),
		invoices=ticked,
	)
	entered = False
	try:
		# the lock commits the open transaction first: nothing has been written before it
		with named_lock(customer_lock_key(customer)):
			entered = True
			doc = _settled_before(ctx, customer, request, booking, precision)
			if doc is None:
				doc = post_payment_entry(booking, [])
				# with the entry, in the lock's one commit
				frappe.get_doc(CUSTOMER, customer).add_comment(
					"Info",
					settle_comment(
						doc.name, format_money(value, precision), mode_of_payment, reference, user
					),
				)
	except SyncError as e:
		# the booking's refusal (a mode without an account, a receivable in another currency)
		invalid(e.message)
	except frappe.ValidationError:
		if not entered:
			_busy(customer)
		raise
	result = payment_result(doc)
	result.setdefault("skipped", [])
	return {
		"payment_entry": doc.name,
		**result,
		"outstanding": format_money(get_customer_outstanding(customer, ctx.company), precision),
		"advance": format_money(customer_advance(customer, ctx.company), precision),
	}


# payment history and receipt (CC9)


def _reference_shown(entry):
	"""The reference typed for a payment, or None: a till's entry without one keeps the till's id."""
	reference = entry.get("reference_no")
	if not reference or reference == entry.get(LOCAL_ID_FIELD):
		return None
	return reference


@frappe.whitelist(methods=["GET", "POST"])
def customer_payments(
	outlet: str, customer: str, from_date: str | None = None, to_date: str | None = None
) -> list[dict]:
	"""The customer's submitted Receive Payment Entries of the outlet's company (CC9), newest first,
	at most MAX_PAYMENT_ROWS, posted in `from_date`..`to_date` (YYYY-MM-DD, either open; default all
	time): [{payment_entry, payment_type, posting_date, amount, mode_of_payment, reference, invoices,
	recorded_by}], whoever recorded them (a till, the web, the desk). `invoices` are the bills the
	entry pays, `recorded_by` the entry's owner by name."""
	ctx = credit_view(outlet)
	_require_outlet_customer(ctx, customer)
	first, last = parse_range(from_date, to_date)
	filters = {
		"party_type": CUSTOMER,
		"party": customer,
		"company": ctx.company,
		"docstatus": 1,
		"payment_type": "Receive",
	}
	dates = _date_filter(first, last)
	if dates:
		filters["posting_date"] = dates
	rows = frappe.get_all(
		PAYMENT_ENTRY,
		filters=filters,
		fields=[
			"name",
			"posting_date",
			"paid_amount",
			"mode_of_payment",
			"reference_no",
			LOCAL_ID_FIELD,
			"owner",
		],
		order_by="posting_date desc, creation desc, name desc",
		limit=MAX_PAYMENT_ROWS,
	)
	bills = {}
	if rows:
		for ref in frappe.get_all(
			"Payment Entry Reference",
			filters={"parenttype": PAYMENT_ENTRY, "parent": ["in", [row.name for row in rows]]},
			fields=["parent", "reference_name"],
			order_by="idx asc",
		):
			if ref.reference_name:
				bills.setdefault(ref.parent, []).append(ref.reference_name)
	precision = money_precision(ctx.currency)
	return [
		{
			"payment_entry": row.name,
			"payment_type": "Receive",
			"posting_date": str(row.posting_date),
			"amount": format_money(row.paid_amount, precision),
			"mode_of_payment": row.mode_of_payment,
			"reference": _reference_shown(row),
			"invoices": bills.get(row.name, []),
			"recorded_by": get_fullname(row.owner) if row.owner else None,
		}
		for row in rows
	]


RECEIPT_REFUSAL = "This receipt is not on this outlet's House Account"

# the receipt's words (spec 11.2 where it has them)
RECEIPT_LABELS = {
	"en": {
		"title": "PAYMENT RECEIVED",
		"receipt_no": "Receipt no.",
		"date": "Date",
		"customer": "Customer",
		"mode": "Paid by",
		"reference": "Reference",
		"received": "Received",
		"bill": "Bill",
		"paid": "Paid",
		"left": "Still owed",
		"cleared": "Bills cleared",
		"part_paid": "Part paid",
		"advance": "Kept as advance",
		"balance": "Remaining balance (as of {0})",
		"recorded_by": "Recorded by",
	},
	"ar": {
		"title": "سند قبض",
		"receipt_no": "رقم السند",
		"date": "التاريخ",
		"customer": "العميل",
		"mode": "طريقة الدفع",
		"reference": "المرجع",
		"received": "المبلغ المستلم",
		"bill": "الفاتورة",
		"paid": "المدفوع",
		"left": "المتبقي",
		"cleared": "الفواتير المسددة",
		"part_paid": "مسددة جزئياً",
		"advance": "محفوظ كدفعة مقدمة",
		"balance": "الرصيد المتبقي (حتى {0})",
		"recorded_by": "سجّلها",
	},
}


def _receipt_entry(ctx, payment_entry):
	"""The Payment Entry of a receipt the outlet's page may print (CC9): a submitted Receive entry of
	the outlet's company whose party is a customer of the outlet; refused with one answer otherwise
	(no such entry included)."""
	doc = None
	if isinstance(payment_entry, str) and payment_entry and frappe.db.exists(PAYMENT_ENTRY, payment_entry):
		doc = frappe.get_doc(PAYMENT_ENTRY, payment_entry)
	if (
		doc is None
		or doc.docstatus != 1
		or doc.payment_type != "Receive"
		or doc.party_type != CUSTOMER
		or doc.company != ctx.company
		or outlet_customer_refusal(ctx, doc.party)
	):
		refuse(_(RECEIPT_REFUSAL))
	return doc


def receipt_data(doc, ctx, lang) -> dict:
	"""What the receipt prints (CC9): the entry's customer, date, mode, reference and amount, the
	bills it cleared and part paid (from its references, with what each still owed after it), the
	advance it kept, the customer's balance now (ERPNext's) and who recorded it, with the words of
	`lang`."""
	from erpnext.selling.doctype.customer.customer import get_customer_outstanding

	precision = money_precision(ctx.currency)
	cleared, part_paid = [], []
	for row in doc.get("references") or []:
		paid = quantize(row.allocated_amount, precision)
		# ERPNext writes each reference's outstanding again when the entry is submitted
		# (`update_outstanding_amounts`, v15 and v16): what the bill still owed right after this payment
		left = max(quantize(row.outstanding_amount, precision), Decimal(0))
		line = {
			"name": row.reference_name,
			"paid": format_money(paid, precision),
			"left": format_money(left, precision),
		}
		(part_paid if left > 0 else cleared).append(line)
	labels = dict(RECEIPT_LABELS[lang])
	as_of = now_datetime().strftime("%Y-%m-%d %H:%M")
	labels["balance"] = labels["balance"].format(as_of)
	return {
		"lang": lang,
		"dir": "rtl" if lang == "ar" else "ltr",
		"labels": labels,
		"company": doc.company,
		"receipt_no": doc.name,
		"customer": doc.party_name or doc.party,
		"date": str(doc.posting_date),
		"mode": doc.mode_of_payment,
		"reference": _reference_shown(doc),
		"currency": ctx.currency,
		"received": format_money(doc.paid_amount, precision),
		"cleared": cleared,
		"part_paid": part_paid,
		"advance": format_money(doc.unallocated_amount or 0, precision),
		"has_advance": quantize(doc.unallocated_amount or 0, precision) > 0,
		"balance": format_money(get_customer_outstanding(doc.party, doc.company), precision),
		"as_of": as_of,
		"recorded_by": get_fullname(doc.owner) if doc.owner else None,
	}


def receipt_html(doc, ctx, lang) -> str:
	"""The receipt as one HTML page: the print format RECEIPT_PRINT_FORMAT rendered with `doc` and
	`receipt_data`."""
	from frappe.www.printview import get_print_format

	template = get_print_format(PAYMENT_ENTRY, frappe.get_cached_doc("Print Format", RECEIPT_PRINT_FORMAT))
	data = receipt_data(doc, ctx, lang)
	# the template is the Print Format's: the app's own file, or what a System Manager saved
	body = frappe.render_template(template, {"doc": doc, "receipt": data})  # nosemgrep
	return (
		f'<!DOCTYPE html><html lang="{lang}" dir="{data["dir"]}"><head><meta charset="utf-8">'
		f"<title>{frappe.utils.escape_html(doc.name)}</title></head><body>{body}</body></html>"
	)


@frappe.whitelist(methods=["GET", "POST"])
def payment_receipt_pdf(outlet: str, payment_entry: str, lang: str = "en") -> None:
	"""The payment receipt PDF of a customer's Receive Payment Entry (CC9: "PAYMENT RECEIVED / سند
	قبض", the bills cleared and part paid, the advance kept, the balance at print time, who recorded
	it), in English or Arabic (`lang` en | ar), from the print format "crenya Payment Receipt". For
	whoever reads the outlet's credit page (`credit_view`), and only for a submitted Receive entry of
	the outlet's company whose party is a customer of the outlet: anything else is refused ("This
	receipt is not on this outlet's House Account"). Answers the PDF as a download
	(application/pdf); a refusal is a normal Frappe error."""
	from frappe.utils.pdf import get_pdf

	ctx = credit_view(outlet)
	lang = lang.strip().lower() if isinstance(lang, str) and lang.strip() else "en"
	if lang not in RECEIPT_LANGUAGES:
		invalid(_("lang must be en or ar"))
	doc = _receipt_entry(ctx, payment_entry)
	pdf = get_pdf(receipt_html(doc, ctx, lang))
	frappe.local.response.filename = f"{doc.name}.pdf"
	frappe.local.response.filecontent = pdf
	frappe.local.response.type = "pdf"
