"""Online lookup of an original invoice for a return made at another till."""

import frappe
from frappe.utils import cint, flt

from crenya_pos_api.sync.batches import batch_expiry_dates, row_batches, single_batch
from crenya_pos_api.sync.context import money_precision, qty_precision
from crenya_pos_api.sync.errors import InvalidRequestError, raise_api_error
from crenya_pos_api.utils.dates import format_date
from crenya_pos_api.utils.decimal import format_money, format_number

INVOICE_FIELDS = [
	"name",
	"company",
	"customer",
	"posting_date",
	"currency",
	"grand_total",
	"pos_profile",
	"docstatus",
	"is_return",
	"crenya_local_id",
	"crenya_offline_number",
	"taxes_and_charges",
	"loyalty_amount",
]


def _find_invoice(ctx, invoice):
	for filters in ({"name": invoice}, {"crenya_offline_number": invoice}):
		filters.update({"docstatus": 1, "is_return": 0, "company": ctx.company})
		row = frappe.db.get_value("Sales Invoice", filters, INVOICE_FIELDS, as_dict=True)
		if row:
			return row
	return None


def returned_quantities(invoice_name, rows):
	"""Already returned qty per original row (in the row's UOM).

	Sums submitted returns whose rows reference the original row through
	`sales_invoice_item`; legacy return rows without that link are matched by
	item_code, filling original rows in order.
	"""
	child = frappe.qb.DocType("Sales Invoice Item")
	parent = frappe.qb.DocType("Sales Invoice")
	returned = (
		frappe.qb.from_(child)
		.join(parent)
		.on(parent.name == child.parent)
		.select(child.sales_invoice_item, child.item_code, child.stock_qty)
		.where(
			(parent.return_against == invoice_name)
			& (parent.docstatus == 1)
			& (parent.is_return == 1)
			& (child.parenttype == "Sales Invoice")
		)
		.run(as_dict=True)
	)

	row_names = {row.name for row in rows}
	stock_by_row = {row.name: 0.0 for row in rows}
	unlinked_by_item = {}
	for ret in returned:
		stock_qty = -flt(ret.stock_qty)
		if ret.sales_invoice_item and ret.sales_invoice_item in row_names:
			stock_by_row[ret.sales_invoice_item] += stock_qty
		else:
			unlinked_by_item[ret.item_code] = unlinked_by_item.get(ret.item_code, 0.0) + stock_qty

	for row in rows:
		remaining = unlinked_by_item.get(row.item_code)
		if not remaining:
			continue
		capacity = max(flt(row.stock_qty) - stock_by_row[row.name], 0.0)
		take = min(capacity, remaining)
		stock_by_row[row.name] += take
		unlinked_by_item[row.item_code] = remaining - take

	return {row.name: stock_by_row[row.name] / (flt(row.conversion_factor) or 1.0) for row in rows}


def get_invoice_for_return(ctx, invoice):
	if not invoice or not isinstance(invoice, str):
		raise_api_error(InvalidRequestError, "invoice is required")

	header = _find_invoice(ctx, invoice.strip())
	if not header:
		raise frappe.DoesNotExistError(f"No submitted invoice {invoice} found for {ctx.company}")
	if not frappe.has_permission("Sales Invoice", "read", header.name):
		raise frappe.PermissionError(f"Not permitted to read {header.name}")

	precision = money_precision()
	q_precision = qty_precision()
	rows = frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": header.name, "parenttype": "Sales Invoice"},
		fields=[
			"name",
			"idx",
			"item_code",
			"item_name",
			"qty",
			"stock_qty",
			"uom",
			"conversion_factor",
			"rate",
			"amount",
			"item_tax_template",
			"batch_no",
			"serial_and_batch_bundle",
		],
		order_by="idx asc",
	)
	returned = returned_quantities(header.name, rows)
	batch_of_row = {name: single_batch(batches) for name, batches in row_batches(rows).items()}
	expiry_dates = batch_expiry_dates(batch_of_row.values())
	payments = frappe.get_all(
		"Sales Invoice Payment",
		filters={"parent": header.name, "parenttype": "Sales Invoice"},
		fields=["mode_of_payment", "amount"],
		order_by="idx asc",
	)

	items = []
	for row in rows:
		returned_qty = flt(returned.get(row.name), q_precision)
		batch_no = batch_of_row.get(row.name)
		items.append(
			{
				"idx": cint(row.idx),
				"row_name": row.name,
				"item_code": row.item_code,
				"item_name": row.item_name,
				"qty": format_number(row.qty, q_precision),
				"uom": row.uom,
				"conversion_factor": format_number(row.conversion_factor),
				"rate": format_money(row.rate, precision),
				"amount": format_money(row.amount, precision),
				"item_tax_template": row.item_tax_template or None,
				"returned_qty": format_number(returned_qty, q_precision),
				"returnable_qty": format_number(max(flt(row.qty) - returned_qty, 0), q_precision),
				# null when the row has no batch or consumed several (the return is split on the server)
				"batch_no": batch_no,
				"expiry_date": format_date(expiry_dates.get(batch_no)) if batch_no else None,
			}
		)

	return {
		"name": header.name,
		"crenya_local_id": header.crenya_local_id or None,
		"crenya_offline_number": header.crenya_offline_number or None,
		"customer": header.customer,
		"posting_date": format_date(header.posting_date),
		"currency": header.currency,
		"grand_total": format_money(header.grand_total, precision),
		"pos_profile": header.pos_profile,
		"taxes_and_charges": header.taxes_and_charges or None,
		# tills refuse to return an invoice partly paid with points (so does push_batch)
		"loyalty_amount": format_money(header.loyalty_amount or 0, precision),
		"items": items,
		"payments": [
			{"mode_of_payment": row.mode_of_payment, "amount": format_money(row.amount, precision)}
			for row in payments
		],
	}
