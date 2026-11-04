"""What a shift's Z page on app.crenya.com adds to the counts (0.17.0, plan Ruling A3): the
discounts and price overrides on the shift's submitted invoices and the shift's refunds, read from
the invoices themselves (`crenya_shift_id`), and the store-control events the till sent with it
(`sync.control_log`), each as the web's rows. Money is a decimal string at the outlet currency's
precision.

A discount is an invoice's additional discount (`discount_amount`) or a line sold below its price
list rate (`rate < price_list_rate`: a line discount or a price override). Who approved it comes from
the till's `approval_used` events of the shift (`discount_over_limit`, `price_change`), matched by
item code (an invoice discount by the event without one); one within the cashier's own limit has none.
"""

from decimal import Decimal

import frappe

from crenya_pos_api.utils.decimal import as_decimal, format_money

MAX_ROWS = 200
APPROVAL_ACTIONS = ("discount_over_limit", "price_change")


def _money(value, precision):
	return format_money(value, precision)


def _approvers(control_events) -> dict:
	"""{item code or None: approver's name} of the shift's `approval_used` discount and price events
	(the latest wins)."""
	out = {}
	for event in control_events or []:
		if event.get("event_type") == "approval_used" and event.get("action") in APPROVAL_ACTIONS:
			out[event.get("item_code") or None] = event.get("approver_name") or event.get("approver")
	return out


def discount_rows(invoices, items, precision, control_events=None) -> dict:
	"""The `discounts` block (pure) of `invoices` ([{name, discount_amount}]) and their `items`
	([{parent, item_code, qty, price_list_rate, rate}]): {count, amount, rows (at most MAX_ROWS)}."""
	approvers = _approvers(control_events)
	rows = []
	total = Decimal(0)
	by_invoice = {}
	for item in items:
		by_invoice.setdefault(item["parent"], []).append(item)
	for invoice in invoices:
		discount = as_decimal(invoice.get("discount_amount"))
		if discount:
			total += discount
			rows.append(
				{
					"invoice": invoice["name"],
					"item_code": None,
					"qty": None,
					"list_rate": None,
					"rate": None,
					"discount": _money(discount, precision),
					"approver": approvers.get(None),
				}
			)
		for item in by_invoice.get(invoice["name"], []):
			list_rate = as_decimal(item.get("price_list_rate"))
			rate = as_decimal(item.get("rate"))
			if not list_rate or rate >= list_rate:
				continue
			qty = as_decimal(item.get("qty"))
			line = (list_rate - rate) * qty
			total += line
			rows.append(
				{
					"invoice": invoice["name"],
					"item_code": item.get("item_code"),
					"qty": f"{qty.normalize():f}",
					"list_rate": _money(list_rate, precision),
					"rate": _money(rate, precision),
					"discount": _money(line, precision),
					"approver": approvers.get(item.get("item_code")),
				}
			)
	return {"count": len(rows), "amount": _money(total, precision), "rows": rows[:MAX_ROWS]}


def refund_rows(returns, names, precision) -> dict:
	"""The `refunds` block (pure) of the shift's returns ([{name, return_against, grand_total,
	cashier}]), `names` {user: full name}: {count, amount (positive), rows (at most MAX_ROWS)}."""
	rows = []
	total = Decimal(0)
	for row in returns:
		amount = abs(as_decimal(row.get("grand_total")))
		total += amount
		cashier = row.get("cashier")
		rows.append(
			{
				"invoice": row["name"],
				"return_against": row.get("return_against") or None,
				"amount": _money(amount, precision),
				"cashier_name": names.get(cashier) or cashier or None,
			}
		)
	return {"count": len(rows), "amount": _money(total, precision), "rows": rows[:MAX_ROWS]}


def _invoices(shift_local_id, is_return, pos_profile=None):
	"""The shift's submitted sales (or returns), of `pos_profile` when given (no other outlet's)."""
	invoice = frappe.qb.DocType("Sales Invoice")
	condition = (
		(invoice.crenya_shift_id == shift_local_id)
		& (invoice.docstatus == 1)
		& (invoice.is_return == (1 if is_return else 0))
	)
	if pos_profile:
		condition = condition & (invoice.pos_profile == pos_profile)
	return (
		frappe.qb.from_(invoice)
		.select(
			invoice.name,
			invoice.discount_amount,
			invoice.return_against,
			invoice.grand_total,
			invoice.crenya_cashier.as_("crenya_cashier"),
			invoice.owner,
		)
		.where(condition)
		.orderby(invoice.posting_date)
		.orderby(invoice.posting_time)
		.orderby(invoice.name)
		.run(as_dict=True)
	)


def shift_discounts(shift_local_id, precision, control_events=None, pos_profile=None) -> dict:
	"""The discounts and price overrides on the shift's submitted sales of `pos_profile` (`discount_rows`)."""
	invoices = _invoices(shift_local_id, False, pos_profile)
	items = []
	if invoices:
		item = frappe.qb.DocType("Sales Invoice Item")
		items = (
			frappe.qb.from_(item)
			.select(item.parent, item.item_code, item.qty, item.price_list_rate, item.rate)
			.where(item.parent.isin([row.name for row in invoices]) & (item.parenttype == "Sales Invoice"))
			.orderby(item.parent)
			.orderby(item.idx)
			.run(as_dict=True)
		)
	return discount_rows(invoices, items, precision, control_events)


def shift_refunds(shift_local_id, precision, pos_profile=None) -> dict:
	"""The shift's submitted returns of `pos_profile` (`refund_rows`)."""
	from crenya_pos_api.web.devices import full_names

	returns = [
		{**row, "cashier": row.get("crenya_cashier") or row.get("owner")}
		for row in _invoices(shift_local_id, True, pos_profile)
	]
	return refund_rows(returns, full_names(row["cashier"] for row in returns), precision)
