"""The House Account reports of app.crenya.com's credit pages (Phase 2, 0.16.0): the bodies of the
whitelisted methods in `credit.py`, which re-exports each (the web path stays
`crenya_pos_api.api.web_admin.credit.<name>`).

`credit_outstanding` (Task 6): who owes the outlet's company what, opening to closing for a date
range, with ERPNext's own ageing.

**Who is listed** (F9). The outlet's House Account (credit) enabled customers, plus the customers
with a till sale on account in the range (a House Account sale at a till of the company:
`credit_sales_of` above zero), less the walk-in customer and the customers the product leaves out
(`excluded_customers`), inside the outlet's customer groups. The company's desk debtors are NOT
listed: a restaurant manager does not see them. A customer that is not enabled and has nothing in any
column is dropped. At most MAX_REPORT_ROWS rows (`more`), the biggest closing first.

**Figures.** Per customer and the outlet's company, from the General Ledger (receivable rows,
`party_type` Customer, not cancelled): `opening` = debit less credit before `from_date`, `charged` =
debits and `paid` = credits in `from_date..as_on` (a cash sale to a named customer posts both, in one
voucher, so it is in both), `closing` = opening + charged - paid. The ageing (`range_*`) is
ERPNext's Accounts Receivable report as on `as_on`, by due date, 30/60/90. `overdue` and
`oldest_unpaid` follow the one rule of the credit page (`sync.credit.overdue_figures`, with `today`
= `as_on`, capped at the closing balance), so on the day the report equals the credit page's row.

**Permission.** Reading is `credit_view` (the outlet's manager, or an Accounts role, for a company
the user may use). ERPNext's Accounts Receivable report raises PermissionError for a user without
Accounts roles and `frappe.flags.ignore_permissions` does not help, so, only AFTER `credit_view`
passed, the report runs as Administrator (`frappe.set_user`) and the session user is restored in a
`finally`, always. The report is asked for exactly the listed customers.
"""

from datetime import date
from decimal import Decimal

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import getdate, nowdate

from crenya_pos_api.api.web_admin.credit import (
	CREDIT_ENABLED_FIELD,
	CUSTOMER,
	_outlet_customer_filter,
	credit_sales_of,
	credit_view,
	excluded_customers,
	outlet_customer_refusal,
)
from crenya_pos_api.sync.context import money_precision
from crenya_pos_api.sync.credit import overdue_figures
from crenya_pos_api.sync.credit_terms import customer_terms
from crenya_pos_api.utils.decimal import as_decimal, format_money
from crenya_pos_api.web.context import refuse
from crenya_pos_api.web.reports import money_view, parse_date

# the most customers one report lists
MAX_REPORT_ROWS = 1000
# the customers asked of ERPNext's Accounts Receivable report at a time
AR_CHUNK = 200
RANGE_KEYS = ("range_0_30", "range_31_60", "range_61_90", "range_90_plus")
# ERPNext's Accounts Receivable columns of the ageing, in the order of RANGE_KEYS
AR_RANGES = ("range1", "range2", "range3", "range4")
MONEY_KEYS = ("opening", "charged", "paid", "closing", *RANGE_KEYS, "overdue")
TOTAL_KEYS = ("opening", "charged", "paid", "closing", *RANGE_KEYS, "overdue")


# pure


def ageing_buckets(ar_rows, precision) -> dict:
	"""{party: {range_0_30, range_31_60, range_61_90, range_90_plus (decimal strings), oldest_unpaid
	("YYYY-MM-DD" | None)}} from the rows of ERPNext's Accounts Receivable report: the ranges sum
	`range1..range4` of every row of the party (an unallocated payment is a negative row, as in
	ERPNext's own total), `oldest_unpaid` is the earliest `posting_date` among the rows with
	something outstanding."""
	sums, oldest = {}, {}
	for row in ar_rows or []:
		party = row.get("party")
		if not party:
			continue
		totals = sums.setdefault(party, [Decimal(0)] * len(AR_RANGES))
		for index, key in enumerate(AR_RANGES):
			totals[index] += as_decimal(row.get(key))
		posted = row.get("posting_date")
		if posted and as_decimal(row.get("outstanding")) > 0:
			posted = getdate(posted)
			if party not in oldest or posted < oldest[party]:
				oldest[party] = posted
	answer = {}
	for party, totals in sums.items():
		answer[party] = {
			**{key: format_money(value, precision) for key, value in zip(RANGE_KEYS, totals, strict=True)},
			"oldest_unpaid": oldest[party].isoformat() if party in oldest else None,
		}
	return answer


def sum_rows(rows, keys, precision) -> dict:
	"""{key: decimal string} sums of the rows' money strings."""
	return {
		key: format_money(sum((as_decimal(row[key]) for row in rows), Decimal(0)), precision) for key in keys
	}


def is_zero_row(row) -> bool:
	return all(as_decimal(row[key]) == 0 for key in MONEY_KEYS)


# reads


def _month_start(day: date) -> date:
	return day.replace(day=1)


def _candidates(ctx, first, last, precision) -> tuple[list[str], set, bool]:
	"""(customer names, the enabled ones, more): the enabled customers of the outlet and those with a
	till sale on account posted in `first..last`, inside the outlet's customer groups, without the
	walk-in and the excluded."""
	base = _outlet_customer_filter(ctx)
	enabled = frappe.get_all(
		CUSTOMER,
		filters={**base, CREDIT_ENABLED_FIELD: 1, "disabled": 0},
		pluck="name",
		order_by="name asc",
		limit=MAX_REPORT_ROWS + 1,
	)
	invoice = frappe.qb.DocType("Sales Invoice")
	in_company = invoice.company == ctx.company
	if base.get("customer_group"):
		# cut the pool in SQL by the customers' groups now, not the group an invoice carried
		customer = frappe.qb.DocType(CUSTOMER)
		in_company = in_company & invoice.customer.isin(
			frappe.qb.from_(customer)
			.select(customer.name)
			.where(customer.customer_group.isin(base["customer_group"][1]))
		)
	sold = [
		row[0]
		for row in (
			frappe.qb.from_(invoice)
			.select(invoice.customer)
			.where(
				in_company
				& (invoice.docstatus == 1)
				& (invoice.is_return == 0)
				& invoice.pos_profile.isnotnull()
				& (invoice.pos_profile != "")
				# not the sales paid in full at the sale (the wide cut of `credit_sales_of`)
				& (
					invoice.paid_amount + invoice.write_off_amount + invoice.loyalty_amount
					< invoice.grand_total + 1
				)
				& (invoice.posting_date >= first)
				& (invoice.posting_date <= last)
			)
			.groupby(invoice.customer)
			.limit(MAX_REPORT_ROWS + 1)
			.run()
		)
	]
	on_account = {
		name for name, part in credit_sales_of(sold, ctx.company, first, last, precision).items() if part > 0
	}
	pool = (set(enabled) | on_account) - excluded_customers(ctx) - {ctx.profile.get("customer")}
	if on_account - set(enabled):
		# the sold-on-account ones must also lie inside the outlet's groups and exist
		pool = set(
			frappe.get_all(CUSTOMER, filters={**base, "name": ["in", sorted(pool) or [""]]}, pluck="name")
		)
	more = len(enabled) > MAX_REPORT_ROWS or len(sold) > MAX_REPORT_ROWS
	return sorted(pool), set(enabled), more


def _gl_figures(names, company, first, last):
	"""({customer: opening}, {customer: (charged, paid)}) from the General Ledger: two grouped
	queries."""
	entry = frappe.qb.DocType("GL Entry")
	where = (
		(entry.party_type == CUSTOMER)
		& entry.party.isin(names)
		& (entry.company == company)
		& (entry.is_cancelled == 0)
	)
	opening = {
		row[0]: as_decimal(row[1]) - as_decimal(row[2])
		for row in (
			frappe.qb.from_(entry)
			.select(entry.party, Sum(entry.debit), Sum(entry.credit))
			.where(where & (entry.posting_date < first))
			.groupby(entry.party)
			.run()
		)
	}
	moved = {
		row[0]: (as_decimal(row[1]), as_decimal(row[2]))
		for row in (
			frappe.qb.from_(entry)
			.select(entry.party, Sum(entry.debit), Sum(entry.credit))
			.where(where & (entry.posting_date >= first) & (entry.posting_date <= last))
			.groupby(entry.party)
			.run()
		)
	}
	return opening, moved


def _receivable_rows(names, company, as_on) -> list[dict]:
	"""The rows of ERPNext's Accounts Receivable report for exactly `names` (chunks of AR_CHUNK) as on
	`as_on`, by due date, 30/60/90. Runs as Administrator (see the module docstring: call it only
	after `credit_view` passed) and puts the session user back whatever happens."""
	from erpnext.accounts.report.accounts_receivable.accounts_receivable import execute

	user = frappe.session.user
	rows = []
	try:
		frappe.set_user("Administrator")
		for start in range(0, len(names), AR_CHUNK):
			filters = frappe._dict(
				company=company,
				report_date=str(as_on),
				ageing_based_on="Due Date",
				range="30, 60, 90",
				party_type=CUSTOMER,
				party=names[start : start + AR_CHUNK],
			)
			result = execute(filters)
			rows.extend(row for row in (result[1] or []) if isinstance(row, dict))
	finally:
		frappe.set_user(user)
	return rows


def credit_outstanding_report(
	outlet: str, as_on: str | None = None, from_date: str | None = None, customer: str | None = None
) -> dict:
	"""The House Account Outstanding report, see the module docstring. `as_on` defaults to the site's
	today, `from_date` to the first of `as_on`'s month; `from_date` after `as_on` is a
	ValidationError. `customer` limits the report to that one customer, who must be a customer of the
	outlet (not its walk-in or an excluded one: PermissionError)."""
	ctx = credit_view(outlet)
	end = parse_date(as_on) if as_on else getdate(nowdate())
	first = parse_date(from_date) if from_date else _month_start(end)
	if first > end:
		raise frappe.ValidationError(_("From Date must be before As On"))
	precision = money_precision(ctx.currency)
	if customer:
		refusal = outlet_customer_refusal(ctx, customer)
		if not refusal and customer in excluded_customers(ctx):
			refusal = _("{0} is not on this page").format(customer)
		if refusal:
			refuse(refusal)
		names, enabled, more = [customer], set(), False
	else:
		names, enabled, more = _candidates(ctx, first, end, precision)
	rows = []
	if names:
		rows = _rows(ctx, names, enabled, first, end, precision, keep_zero=bool(customer))
	rows.sort(key=lambda row: (-as_decimal(row["closing"]), row["name"]))
	if len(rows) > MAX_REPORT_ROWS:
		more = True
		rows = rows[:MAX_REPORT_ROWS]
	return {
		"outlet": ctx.outlet,
		"company": ctx.company,
		"currency": money_view(ctx.currency),
		"as_on": end.isoformat(),
		"from_date": first.isoformat(),
		"more": more,
		"rows": rows,
		"totals": sum_rows(rows, TOTAL_KEYS, precision),
	}


def _rows(ctx, names, enabled, first, end, precision, keep_zero) -> list[dict]:
	labels = {
		row.name: row.customer_name or row.name
		for row in frappe.get_all(CUSTOMER, filters={"name": ["in", names]}, fields=["name", "customer_name"])
	}
	opening, moved = _gl_figures(names, ctx.company, first, end)
	receivable = _receivable_rows(names, ctx.company, end)
	ageing = ageing_buckets(receivable, precision)
	bills = {}
	for row in receivable:
		if row.get("voucher_type") == "Sales Invoice" and row.get("party"):
			bills.setdefault(row["party"], []).append(
				{
					"outstanding_amount": row.get("outstanding"),
					"due_date": row.get("due_date"),
					"posting_date": row.get("posting_date"),
				}
			)
	rows = []
	for name in names:
		charged, paid = moved.get(name, (Decimal(0), Decimal(0)))
		start = opening.get(name, Decimal(0))
		closing = start + charged - paid
		buckets = ageing.get(name) or {key: format_money(0, precision) for key in RANGE_KEYS}
		overdue = overdue_figures(bills.get(name), closing, precision, today=end)[0]
		row = {
			"customer": name,
			"name": labels.get(name, name),
			"credit_days": customer_terms(name, ctx.company)["credit_days"],
			"opening": format_money(start, precision),
			"charged": format_money(charged, precision),
			"paid": format_money(paid, precision),
			"closing": format_money(closing, precision),
			**{key: buckets[key] for key in RANGE_KEYS},
			"overdue": format_money(overdue, precision),
			"oldest_unpaid": buckets.get("oldest_unpaid"),
		}
		if keep_zero or name in enabled or not is_zero_row(row):
			rows.append(row)
	return rows
