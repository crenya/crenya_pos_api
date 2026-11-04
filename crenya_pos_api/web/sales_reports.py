"""A shop's Reports tab on app.crenya.com (0.17.0, plan Ruling A5): sales by item and by cashier over
a date range, in the restaurant's row keys so the web's `salesColumns` reads both products, plus the
shop's own `discounts`, `line_voids_count` and `no_sales_count`.

The sums are made in the database (one grouped query a tab, nothing per invoice in Python): the
outlet's (POS Profile, of its company) submitted Sales Invoices by posting date, returns netted
(a return's quantities and amounts are negative). The cashier is the invoice's `crenya_cashier`,
else its owner. Voids and no-sales are the till's control events (`sync.control_log`) by the time
they happened, credited to the event's actor. Money is a decimal string at the currency's precision.
"""

import frappe

from crenya_pos_api.sync.control_log import EVENT_DOCTYPE, LOG_DOCTYPE
from crenya_pos_api.utils.decimal import as_decimal, format_money, format_number

TABS = ("items", "cashiers")
MAX_ITEM_ROWS = 5000


# a line sold below its price list rate: (list - rate) x qty (a return's is negative, so it nets)
LINE_DISCOUNT_SQL = (
	"case when item.price_list_rate > 0 and item.rate < item.price_list_rate "
	"then (item.price_list_rate - item.rate) * item.qty else 0 end"
)
# the outlet's submitted (not draft, not cancelled) invoices of the company, by posting date
INVOICE_FILTER_SQL = (
	"invoice.docstatus = 1 and invoice.company = %(company)s and invoice.pos_profile = %(outlet)s "
	"and invoice.posting_date between %(first)s and %(last)s"
)


# the per-item sums (one query, grouped in the database)
ITEM_SUMS_SQL = (
	"select item.item_code, max(item.item_name) as item_name, max(item.item_group) as item_group, sum(item.qty) as qty, sum(item.amount) as amount, sum("
	+ LINE_DISCOUNT_SQL
	+ ") as discounts from `tabSales Invoice Item` item join `tabSales Invoice` invoice on invoice.name = item.parent and item.parenttype = 'Sales Invoice' where "
	+ INVOICE_FILTER_SQL
	+ " group by item.item_code order by amount desc, item.item_code limit "
	+ str(MAX_ITEM_ROWS)
	+ ""
)
# the per-cashier sums: each invoice with its lines' discounts (grouped by invoice), then by cashier
CASHIER_SUMS_SQL = (
	"select cashier, sum(case when is_return = 0 then 1 else 0 end) as bills, sum(case when is_return = 1 then 1 else 0 end) as refunds_count, sum(case when is_return = 1 then -grand_total else 0 end) as refunds, sum(case when is_return = 0 then grand_total else 0 end) as sales, sum(discount_amount) + sum(line_discounts) as discounts from ( select coalesce(nullif(invoice.crenya_cashier, ''), invoice.owner) as cashier, invoice.is_return, invoice.grand_total, invoice.discount_amount, coalesce(line_sums.discounts, 0) as line_discounts from `tabSales Invoice` invoice left join ( select item.parent, sum("
	+ LINE_DISCOUNT_SQL
	+ ") as discounts from `tabSales Invoice Item` item join `tabSales Invoice` invoice on invoice.name = item.parent and item.parenttype = 'Sales Invoice' where "
	+ INVOICE_FILTER_SQL
	+ " group by item.parent ) line_sums on line_sums.parent = invoice.name where "
	+ INVOICE_FILTER_SQL
	+ " ) bills group by cashier"
)


def item_rows(raw, precision) -> list[dict]:
	"""The By item rows (pure) of the per-item sums `raw` ([{item_code, item_name, item_group, qty,
	amount, discounts}], already netted and ordered): quantities as plain numbers, money at
	`precision`."""
	return [
		{
			"item_code": row["item_code"],
			"item_name": row.get("item_name") or row["item_code"],
			"item_group": row.get("item_group"),
			"qty": format_number(as_decimal(row.get("qty"))),
			"amount": format_money(as_decimal(row.get("amount")), precision),
			"discounts": format_money(as_decimal(row.get("discounts")), precision),
		}
		for row in raw
	]


def cashier_rows(raw, controls, names, precision) -> list[dict]:
	"""The By cashier rows (pure) of the per-cashier sums `raw` ([{cashier, bills, refunds_count,
	refunds (positive), sales, discounts}]), the till's `controls` ({user: {line_voids_count,
	no_sales_count}}) and `names` ({user: full name}), by cashier name; a cashier with only voids or
	no-sales is listed too. `net_total` is sales less refunds."""
	sums = {row["cashier"]: row for row in raw}
	out = []
	for user in list(sums) + [user for user in controls if user not in sums]:
		row = sums.get(user) or {}
		counts = controls.get(user) or {}
		sales, refunds = as_decimal(row.get("sales")), as_decimal(row.get("refunds"))
		out.append(
			{
				"cashier": user,
				"cashier_name": names.get(user) or user,
				"bills": int(row.get("bills") or 0),
				"refunds_count": int(row.get("refunds_count") or 0),
				"refunds": format_money(refunds, precision),
				"sales": format_money(sales, precision),
				"net_total": format_money(sales - refunds, precision),
				"discounts": format_money(as_decimal(row.get("discounts")), precision),
				"line_voids_count": int(counts.get("line_voids_count") or 0),
				"no_sales_count": int(counts.get("no_sales_count") or 0),
			}
		)
	return sorted(out, key=lambda r: (str(r["cashier_name"]).lower(), r["cashier"]))


def _params(company, outlet, first, last):
	return {"company": company, "outlet": outlet, "first": first, "last": last}


def item_sums(company, outlet, first, last) -> list[dict]:
	"""Per item, in SQL: quantity, amount and line discounts of the outlet's submitted invoices in the
	range (returns netted), by amount descending, at most MAX_ITEM_ROWS rows."""
	return frappe.db.sql(
		ITEM_SUMS_SQL,
		_params(company, outlet, first, last),
		as_dict=True,
	)


def cashier_sums(company, outlet, first, last) -> list[dict]:
	"""Per cashier (`crenya_cashier`, else the owner), in SQL: sales and returns of the outlet's
	submitted invoices in the range, refunds positive, and the discounts given (the invoices'
	additional discounts plus their lines below the price list rate)."""
	return frappe.db.sql(
		CASHIER_SUMS_SQL,
		_params(company, outlet, first, last),
		as_dict=True,
	)


def control_counts(outlet, first, last) -> dict:
	"""{actor: {line_voids_count, no_sales_count}} of the outlet's control events in the range (by the
	event's time, site time)."""
	if not frappe.db.table_exists(LOG_DOCTYPE):
		return {}
	log = frappe.qb.DocType(LOG_DOCTYPE)
	event = frappe.qb.DocType(EVENT_DOCTYPE)
	rows = (
		frappe.qb.from_(event)
		.join(log)
		.on((event.parent == log.name) & (event.parenttype == LOG_DOCTYPE))
		.select(event.actor, event.event_type)
		.where(
			(log.pos_profile == outlet)
			& event.event_type.isin(["line_voided", "no_sale"])
			& (event.at >= f"{first} 00:00:00")
			& (event.at <= f"{last} 23:59:59.999999")
			& event.actor.isnotnull()
		)
		.run(as_dict=True)
	)
	out = {}
	for row in rows:
		counts = out.setdefault(row.actor, {"line_voids_count": 0, "no_sales_count": 0})
		counts["line_voids_count" if row.event_type == "line_voided" else "no_sales_count"] += 1
	return out
