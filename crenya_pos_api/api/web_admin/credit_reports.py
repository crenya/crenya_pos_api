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
passed, the report runs as Administrator (`elevated_as_administrator`) and the session (user, sid, session data, form dict) is restored in a
`finally`, always. The report is asked for exactly the listed customers.
"""

from datetime import date
from decimal import Decimal

import frappe
from frappe import _
from frappe.query_builder import Case
from frappe.query_builder.functions import Min, Sum
from frappe.utils import cint, getdate, nowdate

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
from crenya_pos_api.utils.session import elevated_as_administrator
from crenya_pos_api.web.context import refuse
from crenya_pos_api.web.reports import date_range, money_view, parse_date

# the most customers one report lists
MAX_REPORT_ROWS = 1000
# the customers asked of ERPNext's Accounts Receivable report at a time
AR_CHUNK = 200
RANGE_KEYS = ("range_0_30", "range_31_60", "range_61_90", "range_90_plus")
# ERPNext's Accounts Receivable columns of the ageing, in the order of RANGE_KEYS
AR_RANGES = ("range1", "range2", "range3", "range4")
# the most rows one statement lists
MAX_STATEMENT_ROWS = 2000
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


def statement_description(voucher_type, is_return=False) -> str:
	"""What a statement line is called: "sale", "credit_note" (a return Sales Invoice), "payment",
	"journal"; any other voucher shows its raw voucher type (the web translates what it knows)."""
	if voucher_type == "Sales Invoice":
		return "credit_note" if is_return else "sale"
	if voucher_type == "Payment Entry":
		return "payment"
	if voucher_type == "Journal Entry":
		return "journal"
	return voucher_type


def statement_rows(opening, entries, precision) -> tuple[list[dict], str]:
	"""(rows, closing) of a customer statement: `entries` are dicts {posting_date, voucher_type,
	voucher_no, debit, credit, bill_no, is_return} in order, one per voucher; each row carries the
	running `balance` (opening + debits - credits so far), the closing is the last balance, or the
	opening with no entry. Money as decimal strings at `precision`."""
	balance = as_decimal(opening)
	rows = []
	for entry in entries:
		debit, credit = as_decimal(entry.get("debit")), as_decimal(entry.get("credit"))
		balance += debit - credit
		rows.append(
			{
				"posting_date": str(entry["posting_date"]),
				"voucher_type": entry["voucher_type"],
				"voucher_no": entry["voucher_no"],
				"bill_no": entry.get("bill_no") or None,
				"description": statement_description(entry["voucher_type"], entry.get("is_return")),
				"debit": format_money(debit, precision),
				"credit": format_money(credit, precision),
				"balance": format_money(balance, precision),
			}
		)
	return rows, format_money(balance, precision)


def is_zero_row(row) -> bool:
	return all(as_decimal(row[key]) == 0 for key in MONEY_KEYS)


# reads


def _month_start(day: date) -> date:
	return day.replace(day=1)


def on_account_sql(invoice, precision):
	"""The SQL form of `credit_sale_part` before its floor at 0: the payable total (the rounded total
	unless rounding is off) less the paid amount net of change, the write-off and redeemed points."""
	payable = (
		Case()
		.when(invoice.disable_rounded_total == 1, invoice.grand_total)
		.else_(Case().when(invoice.rounded_total != 0, invoice.rounded_total).else_(invoice.grand_total))
	)
	return (
		payable
		- (invoice.paid_amount - invoice.change_amount)
		- invoice.write_off_amount
		- invoice.loyalty_amount
	)


def _candidates(ctx, first, last, precision) -> tuple[list[str], set, bool]:
	"""(customer names, the enabled live ones, more): the enabled customers of the outlet and those with a
	till sale on account posted in `first..last`, inside the outlet's customer groups, without the
	walk-in and the excluded."""
	base = _outlet_customer_filter(ctx)
	# a restaurant keeps a disabled customer who still owes (`_outlet_customer_filter`'s rule): listed
	# here, dropped below when all its figures are zero; a shop's filter already says disabled = 0
	enabled_rows = frappe.get_all(
		CUSTOMER,
		filters={**base, CREDIT_ENABLED_FIELD: 1},
		fields=["name", "disabled"],
		order_by="name asc",
		limit=MAX_REPORT_ROWS + 1,
	)
	enabled = [row.name for row in enabled_rows]
	live = {row.name for row in enabled_rows if not cint(row.disabled)}
	invoice = frappe.qb.DocType("Sales Invoice")
	in_company = invoice.company == ctx.company
	left_out = excluded_customers(ctx) | {ctx.profile.get("customer")}
	left_out.discard(None)
	if left_out:
		in_company = in_company & invoice.customer.notin(sorted(left_out))
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
				# an actual on-account part (`credit_sale_part`): payable less paid net of change,
				# write-off and redeemed points, above half the smallest unit
				& (on_account_sql(invoice, precision) > Decimal(1).scaleb(-precision) / 2)
				& (invoice.posting_date >= first)
				& (invoice.posting_date <= last)
			)
			.groupby(invoice.customer)
			.orderby(invoice.customer)
			.limit(MAX_REPORT_ROWS + 1)
			.run()
		)
	]
	on_account = {
		name for name, part in credit_sales_of(sold, ctx.company, first, last, precision).items() if part > 0
	}
	pool = (set(enabled) | on_account) - left_out
	if on_account - set(enabled):
		# the sold-on-account ones must also lie inside the outlet's groups and exist
		pool = set(
			frappe.get_all(CUSTOMER, filters={**base, "name": ["in", sorted(pool) or [""]]}, pluck="name")
		)
	more = len(enabled) > MAX_REPORT_ROWS or len(sold) > MAX_REPORT_ROWS
	return sorted(pool), live, more


def _gl_where(entry, names, company):
	return (
		(entry.party_type == CUSTOMER)
		& entry.party.isin(names)
		& (entry.company == company)
		& (entry.is_cancelled == 0)
	)


def _gl_opening(names, company, first) -> dict:
	"""{customer: opening}: debit less credit of the party's General Ledger before `first`."""
	entry = frappe.qb.DocType("GL Entry")
	return {
		row[0]: as_decimal(row[1]) - as_decimal(row[2])
		for row in (
			frappe.qb.from_(entry)
			.select(entry.party, Sum(entry.debit), Sum(entry.credit))
			.where(_gl_where(entry, names, company) & (entry.posting_date < first))
			.groupby(entry.party)
			.run()
		)
	}


def _gl_figures(names, company, first, last):
	"""({customer: opening}, {customer: (charged, paid)}) from the General Ledger: two grouped
	queries."""
	entry = frappe.qb.DocType("GL Entry")
	moved = {
		row[0]: (as_decimal(row[1]), as_decimal(row[2]))
		for row in (
			frappe.qb.from_(entry)
			.select(entry.party, Sum(entry.debit), Sum(entry.credit))
			.where(
				_gl_where(entry, names, company)
				& (entry.posting_date >= first)
				& (entry.posting_date <= last)
			)
			.groupby(entry.party)
			.run()
		)
	}
	return _gl_opening(names, company, first), moved


def _receivable_rows(names, company, as_on) -> list[dict]:
	"""The rows of ERPNext's Accounts Receivable report for exactly `names` (chunks of AR_CHUNK) as on
	`as_on`, by due date, 30/60/90. Runs as Administrator (see the module docstring: call it only
	after `credit_view` passed) and puts the session back whatever happens (`elevated_as_administrator`: user, sid, session data, form dict)."""
	from erpnext.accounts.report.accounts_receivable.accounts_receivable import execute

	rows = []
	with elevated_as_administrator():
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
		overdue, oldest = overdue_figures(bills.get(name), closing, precision, today=end)
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
			"oldest_unpaid": None if oldest is None else oldest.isoformat(),
		}
		if keep_zero or name in enabled or not is_zero_row(row):
			rows.append(row)
	return rows


def credit_statement_report(
	outlet: str, customer: str, from_date: str | None, to_date: str | None
) -> dict:
	"""A customer's House Account statement, see the module docstring of `credit_outstanding`: the
	General Ledger of the customer and the outlet's company, one line per voucher (a cash sale to a
	named customer posts both a debit and a credit in one voucher, so both columns sit on its line),
	with the running balance. Both dates are required (YYYY-MM-DD, `to_date` not before `from_date`,
	at most a year). The customer must be one of the outlet (not its walk-in or an excluded one:
	PermissionError). At most MAX_STATEMENT_ROWS lines (`more`)."""
	ctx = credit_view(outlet)
	if not from_date or not to_date:
		raise frappe.ValidationError(_("From Date and To Date are required"))
	first, last = date_range(from_date, to_date)
	refusal = outlet_customer_refusal(ctx, customer)
	if not refusal and customer in excluded_customers(ctx):
		refusal = _("{0} is not on this page").format(customer)
	if refusal:
		refuse(refusal)
	precision = money_precision(ctx.currency)
	opening = _gl_opening([customer], ctx.company, first).get(customer, Decimal(0))
	entry = frappe.qb.DocType("GL Entry")
	found = (
		frappe.qb.from_(entry)
		.select(
			entry.voucher_type,
			entry.voucher_no,
			entry.posting_date,
			Sum(entry.debit),
			Sum(entry.credit),
			Min(entry.creation),
		)
		.where(
			_gl_where(entry, [customer], ctx.company)
			& (entry.posting_date >= first)
			& (entry.posting_date <= last)
		)
		.groupby(entry.voucher_type, entry.voucher_no, entry.posting_date)
		.orderby(entry.posting_date)
		.orderby(Min(entry.creation))
		.limit(MAX_STATEMENT_ROWS + 1)
		.run()
	)
	more = len(found) > MAX_STATEMENT_ROWS
	found = found[:MAX_STATEMENT_ROWS]
	invoices = [row[1] for row in found if row[0] == "Sales Invoice"]
	payments = [row[1] for row in found if row[0] == "Payment Entry"]
	bills = {
		row.name: row
		for row in (
			frappe.get_all(
				"Sales Invoice",
				filters={"name": ["in", invoices]},
				fields=["name", "is_return", "crenya_offline_number"],
			)
			if invoices
			else []
		)
	}
	references = (
		dict(
			frappe.get_all(
				"Payment Entry",
				filters={"name": ["in", payments]},
				fields=["name", "reference_no"],
				as_list=True,
			)
		)
		if payments
		else {}
	)
	entries = []
	for voucher_type, voucher_no, posted, debit, credit, _created in found:
		bill = bills.get(voucher_no) if voucher_type == "Sales Invoice" else None
		entries.append(
			{
				"posting_date": posted,
				"voucher_type": voucher_type,
				"voucher_no": voucher_no,
				"debit": debit,
				"credit": credit,
				"is_return": bool(bill and cint(bill.is_return)),
				"bill_no": (bill.crenya_offline_number if bill else references.get(voucher_no)) or None,
			}
		)
	rows, closing = statement_rows(opening, entries, precision)
	if more:
		# the listed lines stop short of to_date: the closing is still the ledger's
		moved = _gl_figures([customer], ctx.company, first, last)[1].get(customer, (Decimal(0), Decimal(0)))
		closing = format_money(opening + moved[0] - moved[1], precision)
	return {
		"customer": customer,
		"name": frappe.db.get_value(CUSTOMER, customer, "customer_name") or customer,
		"company": ctx.company,
		"currency": money_view(ctx.currency),
		"from_date": first.isoformat(),
		"to_date": last.isoformat(),
		"opening": format_money(opening, precision),
		"closing": closing,
		"more": more,
		"rows": rows,
	}
