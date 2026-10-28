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
from frappe.utils import cint, get_fullname, getdate, nowdate

from crenya_pos_api.api.web_admin.credit import (
	CREDIT_ENABLED_FIELD,
	CUSTOMER,
	_outlet_customer_filter,
	credit_sale_part,
	credit_sales_of,
	credit_view,
	excluded_customers,
	outlet_customer_refusal,
)
from crenya_pos_api.sync.context import money_precision
from crenya_pos_api.sync.credit import credited_to_account, overdue_figures
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
# the longest range and the most rows of the transactions report
MAX_TRANSACTION_DAYS = 92
MAX_TRANSACTION_ROWS = 2000
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


def transaction_subtotals(rows, precision) -> tuple[list[dict], list[dict], dict]:
	"""(days, shifts, totals) of the transaction rows (pure). `sales` = the sale rows less the cancel
	rows (a cancel's amount is negative), `collections` = the collection rows. `days` are by
	posting_date, `shifts` by shift_id (a row without a shift is in no shift), each in the order
	first met in a date sorted list: days by date, shifts by shift number (then id)."""
	days, shifts = {}, {}
	sales_total, collected_total = Decimal(0), Decimal(0)
	for row in rows:
		amount = as_decimal(row["amount"])
		sale, collected = (amount, Decimal(0)) if row["kind"] != "collection" else (Decimal(0), amount)
		sales_total += sale
		collected_total += collected
		day = days.setdefault(row["posting_date"], [Decimal(0), Decimal(0)])
		day[0] += sale
		day[1] += collected
		if row.get("shift_id"):
			entry = shifts.setdefault(
				row["shift_id"],
				{
					"shift_id": row["shift_id"],
					"shift_number": row.get("shift_number"),
					"cashier": row.get("cashier"),
					"cashier_name": row.get("cashier_name"),
					"sales": Decimal(0),
					"collections": Decimal(0),
				},
			)
			entry["sales"] += sale
			entry["collections"] += collected
	return (
		[
			{
				"posting_date": day,
				"sales": format_money(v[0], precision),
				"collections": format_money(v[1], precision),
			}
			for day, v in sorted(days.items())
		],
		[
			{
				**v,
				"sales": format_money(v["sales"], precision),
				"collections": format_money(v["collections"], precision),
			}
			for v in sorted(
				shifts.values(),
				key=lambda v: (v["shift_number"] is None, v["shift_number"] or "", v["shift_id"]),
			)
		],
		{
			"sales": format_money(sales_total, precision),
			"collections": format_money(collected_total, precision),
		},
	)


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


def credit_statement_report(outlet: str, customer: str, from_date: str | None, to_date: str | None) -> dict:
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
		.orderby(entry.voucher_no)
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


# the House Account transactions (Task 8)


def _transaction_row(day, kind, voucher_no, bill_no, customer, amount, precision, mode=None, **who):
	return {
		"posting_date": str(day),
		"kind": kind,
		"voucher_no": voucher_no,
		"bill_no": bill_no or None,
		"customer": customer,
		"amount": format_money(amount, precision),
		"mode_of_payment": mode or None,
		"shift_id": who.get("shift_id") or None,
		"cashier": who.get("cashier") or None,
		"source": who.get("source", "till"),
	}


def _sale_rows(ctx, first, last, precision, shift, cashier, cap) -> list[dict]:
	"""The outlet's till sales that put something on a House Account in the range (the part is
	`credit_sale_part`; the SQL cut is `on_account_sql`) and the credit notes against them that
	credited it back (`credited_to_account`): "sale" rows, then "cancel" rows (negative)."""
	invoice = frappe.qb.DocType("Sales Invoice")
	own = (
		(invoice.company == ctx.company)
		& (invoice.docstatus == 1)
		& (invoice.pos_profile == ctx.outlet)
		& (invoice.posting_date >= first)
		& (invoice.posting_date <= last)
	)
	if shift:
		own = own & (invoice.crenya_shift_id == shift)
	if cashier:
		own = own & (invoice.crenya_cashier == cashier)
	fields = (
		invoice.name,
		invoice.customer,
		invoice.posting_date,
		invoice.grand_total,
		invoice.rounded_total,
		invoice.disable_rounded_total,
		invoice.paid_amount,
		invoice.change_amount,
		invoice.write_off_amount,
		invoice.loyalty_amount,
		invoice.update_outstanding_for_self,
		invoice.return_against,
		invoice.crenya_offline_number,
		invoice.crenya_shift_id,
		invoice.crenya_cashier,
	)
	half = Decimal(1).scaleb(-precision) / 2
	sales = (
		frappe.qb.from_(invoice)
		.select(*fields)
		.where(own & (invoice.is_return == 0) & (on_account_sql(invoice, precision) > half))
		.orderby(invoice.posting_date)
		.orderby(invoice.name)
		.limit(cap)
		.run(as_dict=True)
	)
	rows = []
	for sale in sales:
		part = credit_sale_part(sale, precision)
		if part > 0:
			rows.append(
				_transaction_row(
					sale.posting_date,
					"sale",
					sale.name,
					sale.crenya_offline_number,
					sale.customer,
					part,
					precision,
					shift_id=sale.crenya_shift_id,
					cashier=sale.crenya_cashier,
				)
			)
	original = frappe.qb.DocType("Sales Invoice", alias="original")
	of_outlet = (
		frappe.qb.from_(original)
		.select(original.name)
		.where(
			(original.company == ctx.company)
			& (original.docstatus == 1)
			& (original.is_return == 0)
			& (original.pos_profile == ctx.outlet)
			& (on_account_sql(original, precision) > half)
		)
	)
	returns = (
		frappe.qb.from_(invoice)
		.select(*fields)
		.where(own & (invoice.is_return == 1) & invoice.return_against.isin(of_outlet))
		.orderby(invoice.posting_date)
		.orderby(invoice.name)
		.limit(cap)
		.run(as_dict=True)
	)
	if returns:
		parts = {
			row.name: credit_sale_part(row, precision)
			for row in frappe.get_all(
				"Sales Invoice",
				filters={"name": ["in", sorted({r.return_against for r in returns})]},
				fields=[
					"name",
					"grand_total",
					"rounded_total",
					"disable_rounded_total",
					"paid_amount",
					"change_amount",
					"write_off_amount",
					"loyalty_amount",
				],
			)
		}
		refunds = {}
		for payment in frappe.get_all(
			"Sales Invoice Payment",
			filters={"parent": ["in", [r.name for r in returns]], "parenttype": "Sales Invoice"},
			fields=["parent", "amount"],
		):
			refunds.setdefault(payment.parent, []).append(payment)
		for ret in returns:
			credited = min(credited_to_account([ret], refunds, precision), parts.get(ret.return_against, 0))
			if credited > 0:
				rows.append(
					_transaction_row(
						ret.posting_date,
						"cancel",
						ret.name,
						ret.crenya_offline_number,
						ret.customer,
						-credited,
						precision,
						shift_id=ret.crenya_shift_id,
						cashier=ret.crenya_cashier,
					)
				)
	return rows


def _collection_rows(ctx, first, last, precision, shift, cashier, cap) -> tuple[list[dict], bool]:
	"""(rows, more): the company's submitted Receive Payment Entries of customers in the range, a
	till's (its `crenya_device` belongs to the outlet; "till") and, when no shift or cashier is
	asked for, the desk's and the web's (no device, a customer of the outlet's House Account set;
	"web_desk")."""
	entry = frappe.qb.DocType("Payment Entry")
	base = (
		(entry.docstatus == 1)
		& (entry.payment_type == "Receive")
		& (entry.party_type == CUSTOMER)
		& (entry.company == ctx.company)
		& (entry.posting_date >= first)
		& (entry.posting_date <= last)
	)
	if shift:
		base = base & (entry.crenya_shift_id == shift)
	if cashier:
		base = base & (entry.crenya_cashier == cashier)
	devices = frappe.get_all("Crenya POS Device", filters={"pos_profile": ctx.outlet}, pluck="name")
	more = False
	where = [("till", base & entry.crenya_device.isin(devices or [""]))]
	if not shift and not cashier:
		names, _enabled, more = _candidates(ctx, first, last, precision)
		where.append(
			(
				"web_desk",
				base
				& entry.party.isin(names or [""])
				& (entry.crenya_device.isnull() | (entry.crenya_device == "")),
			)
		)
	rows = []
	for source, condition in where:
		found = (
			frappe.qb.from_(entry)
			.select(
				entry.name,
				entry.party,
				entry.posting_date,
				entry.paid_amount,
				entry.mode_of_payment,
				entry.reference_no,
				entry.crenya_shift_id,
				entry.crenya_cashier,
			)
			.where(condition)
			.orderby(entry.posting_date)
			.orderby(entry.name)
			.limit(cap)
			.run(as_dict=True)
		)
		more = more or len(found) >= cap
		for pay in found:
			rows.append(
				_transaction_row(
					pay.posting_date,
					"collection",
					pay.name,
					pay.reference_no,
					pay.party,
					pay.paid_amount,
					precision,
					mode=pay.mode_of_payment,
					shift_id=pay.crenya_shift_id,
					cashier=pay.crenya_cashier,
					source=source,
				)
			)
	return rows, more


def credit_transactions_report(
	outlet: str,
	from_date: str | None,
	to_date: str | None,
	shift: str | None = None,
	cashier: str | None = None,
) -> dict:
	"""The House Account transactions of the outlet per day and shift: what was put on an account
	("sale": the on-account part of the outlet's till sales; "cancel": minus what a return credited
	back), and what was collected ("collection": a till's payments, the desk's and the web's
	settlements of the outlet's House Account customers). Both dates are required (YYYY-MM-DD, at
	most MAX_TRANSACTION_DAYS days). `shift` (a shift's local id) and `cashier` (a user) limit the
	rows; a "web_desk" row has neither, so it drops out under either. At most MAX_TRANSACTION_ROWS
	rows (`more`) ordered by date, shift number and voucher; `days`, `shifts` and `totals` sum every
	row the queries found, so they match the rows unless `more`."""
	ctx = credit_view(outlet)
	if not from_date or not to_date:
		raise frappe.ValidationError(_("From Date and To Date are required"))
	first, last = date_range(from_date, to_date, MAX_TRANSACTION_DAYS)
	precision = money_precision(ctx.currency)
	shift = (shift or "").strip() or None
	cashier = (cashier or "").strip() or None
	cap = MAX_TRANSACTION_ROWS + 1
	rows = _sale_rows(ctx, first, last, precision, shift, cashier, cap)
	more = len(rows) > MAX_TRANSACTION_ROWS
	collected, more_collected = _collection_rows(ctx, first, last, precision, shift, cashier, cap)
	rows += collected
	more = more or more_collected
	numbers = {}
	shift_ids = sorted({row["shift_id"] for row in rows if row["shift_id"]})
	if shift_ids:
		numbers = dict(
			frappe.get_all(
				"Crenya POS Shift",
				filters={"local_id": ["in", shift_ids]},
				fields=["local_id", "shift_number"],
				as_list=True,
			)
		)
	customers = sorted({row["customer"] for row in rows})
	labels = (
		{
			row.name: row.customer_name or row.name
			for row in frappe.get_all(
				CUSTOMER, filters={"name": ["in", customers]}, fields=["name", "customer_name"]
			)
		}
		if customers
		else {}
	)
	full_names = {user: get_fullname(user) for user in {row["cashier"] for row in rows if row["cashier"]}}
	for row in rows:
		row["name"] = labels.get(row["customer"], row["customer"])
		row["shift_number"] = numbers.get(row["shift_id"]) if row["shift_id"] else None
		row["cashier_name"] = full_names.get(row["cashier"]) if row["cashier"] else None
	rows.sort(key=lambda row: (row["posting_date"], row["shift_number"] or "", row["voucher_no"]))
	days, shifts, totals = transaction_subtotals(rows, precision)
	if len(rows) > MAX_TRANSACTION_ROWS:
		more = True
		rows = rows[:MAX_TRANSACTION_ROWS]
	return {
		"outlet": ctx.outlet,
		"currency": money_view(ctx.currency),
		"from_date": first.isoformat(),
		"to_date": last.isoformat(),
		"more": more,
		"rows": rows,
		"days": days,
		"shifts": shifts,
		"totals": totals,
	}
