"""A shop's Reports tab on app.crenya.com (0.17.0, plan Ruling A5): sales by item and by cashier over
a date range, in the restaurant's row keys so the web's `salesColumns` reads both products, plus the
shop's own `discounts`, `line_voids_count` and `no_sales_count`.

The rows are the outlet's (POS Profile) submitted Sales Invoices by posting date, returns netted
(a return's quantities and amounts are negative). The cashier is the invoice's `crenya_cashier`,
else its owner. Voids and no-sales are the till's control events (`sync.control_log`) by the time
they happened, credited to the event's actor. Money is a decimal string at the currency's precision.
"""

from decimal import Decimal

import frappe

from crenya_pos_api.sync.control_log import EVENT_DOCTYPE, LOG_DOCTYPE
from crenya_pos_api.utils.decimal import as_decimal, format_money, format_number

TABS = ("items", "cashiers")
MAX_ITEM_ROWS = 5000


def _discount(list_rate, rate, qty) -> Decimal:
	list_rate, rate = as_decimal(list_rate), as_decimal(rate)
	return (list_rate - rate) * as_decimal(qty) if list_rate and rate < list_rate else Decimal(0)


def item_rows(raw, precision) -> list[dict]:
	"""The By item rows (pure) of invoice lines `raw` ([{item_code, item_name, item_group, qty,
	amount, price_list_rate, rate}], returns negative): one row per item, netted, by amount
	descending, at most MAX_ITEM_ROWS."""
	items = {}
	for line in raw:
		row = items.setdefault(
			line["item_code"],
			{
				"item_code": line["item_code"],
				"item_name": line.get("item_name") or line["item_code"],
				"item_group": line.get("item_group"),
				"qty": Decimal(0),
				"amount": Decimal(0),
				"discounts": Decimal(0),
			},
		)
		row["qty"] += as_decimal(line.get("qty"))
		row["amount"] += as_decimal(line.get("amount"))
		row["discounts"] += _discount(line.get("price_list_rate"), line.get("rate"), line.get("qty"))
	rows = sorted(items.values(), key=lambda r: (-r["amount"], r["item_code"]))[:MAX_ITEM_ROWS]
	return [
		{
			**row,
			"qty": format_number(row["qty"]),
			"amount": format_money(row["amount"], precision),
			"discounts": format_money(row["discounts"], precision),
		}
		for row in rows
	]


def cashier_rows(raw, controls, names, precision) -> list[dict]:
	"""The By cashier rows (pure) of invoices `raw` ([{cashier, is_return, grand_total, discounts}]),
	the till's `controls` ({user: {line_voids_count, no_sales_count}}) and `names` ({user: full
	name}), by cashier name. `sales` are the sales' totals, `refunds` the returns' (positive),
	`net_total` the difference."""
	cashiers = {}

	def row_of(user):
		return cashiers.setdefault(
			user,
			{
				"bills": 0,
				"refunds_count": 0,
				"refunds": Decimal(0),
				"sales": Decimal(0),
				"discounts": Decimal(0),
			},
		)

	for invoice in raw:
		row = row_of(invoice["cashier"])
		total = as_decimal(invoice.get("grand_total"))
		row["discounts"] += as_decimal(invoice.get("discounts"))
		if invoice.get("is_return"):
			row["refunds_count"] += 1
			row["refunds"] += -total
		else:
			row["bills"] += 1
			row["sales"] += total
	for user in controls:
		row_of(user)
	out = []
	for user, row in cashiers.items():
		counts = controls.get(user) or {}
		out.append(
			{
				"cashier": user,
				"cashier_name": names.get(user) or user,
				"bills": row["bills"],
				"refunds_count": row["refunds_count"],
				"refunds": format_money(row["refunds"], precision),
				"sales": format_money(row["sales"], precision),
				"net_total": format_money(row["sales"] - row["refunds"], precision),
				"discounts": format_money(row["discounts"], precision),
				"line_voids_count": int(counts.get("line_voids_count") or 0),
				"no_sales_count": int(counts.get("no_sales_count") or 0),
			}
		)
	return sorted(out, key=lambda r: (str(r["cashier_name"]).lower(), r["cashier"]))


def _invoice_filter(invoice, outlet, first, last):
	return (
		(invoice.pos_profile == outlet)
		& (invoice.docstatus == 1)
		& (invoice.posting_date >= first)
		& (invoice.posting_date <= last)
	)


def item_lines(outlet, first, last) -> list[dict]:
	invoice = frappe.qb.DocType("Sales Invoice")
	item = frappe.qb.DocType("Sales Invoice Item")
	return (
		frappe.qb.from_(item)
		.join(invoice)
		.on((item.parent == invoice.name) & (item.parenttype == "Sales Invoice"))
		.select(
			item.item_code,
			item.item_name,
			item.item_group,
			item.qty,
			item.amount,
			item.price_list_rate,
			item.rate,
		)
		.where(_invoice_filter(invoice, outlet, first, last))
		.run(as_dict=True)
	)


def cashier_invoices(outlet, first, last) -> list[dict]:
	"""[{cashier, is_return, grand_total, discounts}]: an invoice's discounts are its additional
	discount plus its lines below their price list rate."""
	invoice = frappe.qb.DocType("Sales Invoice")
	invoices = (
		frappe.qb.from_(invoice)
		.select(
			invoice.name,
			invoice.crenya_cashier.as_("crenya_cashier"),
			invoice.owner,
			invoice.is_return,
			invoice.grand_total,
			invoice.discount_amount,
		)
		.where(_invoice_filter(invoice, outlet, first, last))
		.run(as_dict=True)
	)
	line_discounts = {}
	if invoices:
		item = frappe.qb.DocType("Sales Invoice Item")
		for line in (
			frappe.qb.from_(item)
			.select(item.parent, item.qty, item.price_list_rate, item.rate)
			.where(item.parent.isin([row.name for row in invoices]) & (item.parenttype == "Sales Invoice"))
			.run(as_dict=True)
		):
			line_discounts[line.parent] = line_discounts.get(line.parent, Decimal(0)) + _discount(
				line.price_list_rate, line.rate, line.qty
			)
	return [
		{
			"cashier": row.crenya_cashier or row.owner,
			"is_return": bool(row.is_return),
			"grand_total": row.grand_total,
			"discounts": as_decimal(row.discount_amount) + line_discounts.get(row.name, Decimal(0)),
		}
		for row in invoices
	]


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
