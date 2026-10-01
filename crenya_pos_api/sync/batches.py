"""Pharmacy batches: batch stock for the pull feed, batch checks for till sales and
the batches a return row goes back into.

Quantities follow ERPNext v15 (Serial and Batch Bundle era): a batch's stock in a
warehouse is the sum of the Serial and Batch Entries of the non-cancelled Stock
Ledger Entries of that warehouse, plus the `batch_no` of ledger entries without
a bundle (data from before bundles), exactly as
`serial_and_batch_bundle.get_available_batches` + `get_stock_ledgers_batches`
count it, but without dropping expired or disabled batches (tills judge those
by date) and without subtracting reservations.

The helpers at the top are pure (no site needed); the rest reads the database.
"""

from decimal import ROUND_FLOOR, Decimal

import frappe
from frappe.query_builder.functions import Min, Sum
from frappe.utils import cint

from crenya_pos_api.sync.errors import VALIDATION, SyncError
from crenya_pos_api.utils.dates import format_date, format_db_datetime
from crenya_pos_api.utils.decimal import as_decimal, format_number

SALES_INVOICE_ITEM = "Sales Invoice Item"
RETURN_REFERENCE_FIELD = "sales_invoice_item"


class BatchSplitError(ValueError):
	"""The batches of an original row cannot take back the requested quantity."""

	def __init__(self, available):
		super().__init__(f"only {format_number(available)} can still be returned into its batches")
		self.available = available


# pure helpers


def build_batch_record(row, qty, precision):
	"""Pull record of one Batch row (`name, item, expiry_date, manufacturing_date, disabled, modified`)."""
	return {
		"name": row["name"],
		"item_code": row["item"],
		"expiry_date": format_date(row.get("expiry_date")),
		"manufacturing_date": format_date(row.get("manufacturing_date")),
		"disabled": cint(row.get("disabled")),
		"qty": format_number(qty or 0, precision),
		"modified": format_db_datetime(row.get("modified")),
	}


def lines_missing_batch(lines, tracked):
	"""Lines of batch tracked items (`tracked`: item codes) that name no `batch_no`."""
	return [line for line in lines if line["item_code"] in tracked and not line.get("batch_no")]


def build_batch_records(rows, quantities, precision):
	return [build_batch_record(row, quantities.get(row["name"]), precision) for row in rows]


def single_batch(batches):
	"""The batch of a row that consumed exactly one batch, else None (no batch or several)."""
	names = list(dict.fromkeys(batch_no for batch_no, _qty in batches or [] if batch_no))
	return names[0] if len(names) == 1 else None


def floor_to_step(value, step):
	value, step = as_decimal(value), as_decimal(step)
	return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def split_return_qty(qty, batches, step):
	"""Split a returned quantity across the batches the original row consumed.

	`batches` is `[(batch_no, sold, returnable)]` in the original bundle's order,
	all in the return row's UOM; `returnable` is what the batch sold less what
	was already returned into it. Each batch gets a share proportional to what it
	sold, capped by its returnable quantity; what a capped batch cannot take goes
	to the other batches by the same rule. Shares are multiples of `step` (the
	rounding remainder goes to the largest fractions, earlier batches first on
	ties) and add up to `qty`. Returns `[(batch_no, qty)]` without empty shares,
	in batch order; raises BatchSplitError when the batches cannot take `qty`.
	"""
	qty, step = as_decimal(qty), as_decimal(step)
	order, sold, spare = [], {}, {}
	for batch_no, sold_qty, returnable in batches:
		if batch_no in sold:
			sold[batch_no] += as_decimal(sold_qty)
			spare[batch_no] += as_decimal(returnable)
			continue
		order.append(batch_no)
		sold[batch_no] = as_decimal(sold_qty)
		spare[batch_no] = as_decimal(returnable)
	for batch_no in order:
		spare[batch_no] = max(floor_to_step(spare[batch_no], step), Decimal(0))

	eligible = [batch_no for batch_no in order if sold[batch_no] > 0 and spare[batch_no] > 0]
	available = sum((spare[batch_no] for batch_no in eligible), Decimal(0))
	if qty > available:
		raise BatchSplitError(available)

	allocated = dict.fromkeys(order, Decimal(0))
	left = qty
	while left > 0:
		active = [batch_no for batch_no in eligible if allocated[batch_no] < spare[batch_no]]
		weight = sum((sold[batch_no] for batch_no in active), Decimal(0))
		ideal = {batch_no: left * sold[batch_no] / weight for batch_no in active}
		floors = {batch_no: floor_to_step(ideal[batch_no], step) for batch_no in active}
		shares = {
			batch_no: min(floors[batch_no], spare[batch_no] - allocated[batch_no]) for batch_no in active
		}
		rounding = left - sum(floors.values(), Decimal(0))
		by_fraction = sorted(active, key=lambda b: (-(ideal[b] - floors[b]), order.index(b)))
		for batch_no in by_fraction:
			if rounding <= 0:
				break
			room = spare[batch_no] - allocated[batch_no] - shares[batch_no]
			if shares[batch_no] < floors[batch_no] or room <= 0:
				continue
			take = min(step, room, rounding)
			shares[batch_no] += take
			rounding -= take
		for batch_no, share in shares.items():
			allocated[batch_no] += share
			left -= share

	return [(batch_no, allocated[batch_no]) for batch_no in order if allocated[batch_no] > 0]


# database


def batch_quantities(batch_names, warehouse):
	"""Stock of each batch in the warehouse, in stock UOM ({batch: qty}; batches without stock left out)."""
	names = list(dict.fromkeys(name for name in batch_names or [] if name))
	if not names or not warehouse:
		return {}
	sle = frappe.qb.DocType("Stock Ledger Entry")
	entry = frappe.qb.DocType("Serial and Batch Entry")
	bundled = (
		frappe.qb.from_(sle)
		.inner_join(entry)
		.on(entry.parent == sle.serial_and_batch_bundle)
		.select(entry.batch_no, Sum(entry.qty).as_("qty"))
		.where((sle.is_cancelled == 0) & (sle.warehouse == warehouse) & entry.batch_no.isin(names))
		.groupby(entry.batch_no)
		.run(as_dict=True)
	)
	# ledger entries from before bundles carry the batch on the entry itself
	legacy = (
		frappe.qb.from_(sle)
		.select(sle.batch_no, Sum(sle.actual_qty).as_("qty"))
		.where((sle.is_cancelled == 0) & (sle.warehouse == warehouse) & sle.batch_no.isin(names))
		.groupby(sle.batch_no)
		.run(as_dict=True)
	)
	quantities = {}
	for row in (*bundled, *legacy):
		quantities[row.batch_no] = quantities.get(row.batch_no, 0.0) + (row.qty or 0.0)
	return quantities


def batches_of_bundles(bundles):
	"""Distinct batches of Serial and Batch Bundles: {bundle: [batch_no, ...]}."""
	bundles = list(dict.fromkeys(name for name in bundles or [] if name))
	if not bundles:
		return {}
	entry = frappe.qb.DocType("Serial and Batch Entry")
	rows = (
		frappe.qb.from_(entry)
		.select(entry.parent, entry.batch_no)
		.distinct()
		.where(entry.parent.isin(bundles) & entry.batch_no.isnotnull() & (entry.batch_no != ""))
		.run(as_dict=True)
	)
	result = {}
	for row in rows:
		result.setdefault(row.parent, []).append(row.batch_no)
	return result


def row_batches(rows):
	"""Batches each (submitted) Sales Invoice Item consumed: {row name: [(batch_no, stock qty)]}.

	From the row's Serial and Batch Bundle in bundle order; a row without a bundle
	but with a `batch_no` consumed its whole stock qty from that batch. Rows
	without batch information map to [].
	"""
	result = {row.name: [] for row in rows}
	bundles = {row.serial_and_batch_bundle: row.name for row in rows if row.get("serial_and_batch_bundle")}
	if bundles:
		entry = frappe.qb.DocType("Serial and Batch Entry")
		entries = (
			frappe.qb.from_(entry)
			.select(entry.parent, entry.batch_no, Sum(entry.qty).as_("qty"), Min(entry.idx).as_("idx"))
			.where(entry.parent.isin(list(bundles)) & entry.batch_no.isnotnull() & (entry.batch_no != ""))
			.groupby(entry.parent, entry.batch_no)
			.run(as_dict=True)
		)
		for row in sorted(entries, key=lambda e: (e.parent, e.idx or 0)):
			result[bundles[row.parent]].append((row.batch_no, abs(row.qty or 0.0)))
	for row in rows:
		if not result[row.name] and not row.get("serial_and_batch_bundle") and row.get("batch_no"):
			result[row.name] = [(row.batch_no, abs(row.get("stock_qty") or 0.0))]
	return result


def batch_expiry_dates(batch_names):
	names = list(dict.fromkeys(name for name in batch_names or [] if name))
	if not names:
		return {}
	return {
		row.name: row.expiry_date
		for row in frappe.get_all("Batch", filters={"name": ["in", names]}, fields=["name", "expiry_date"])
	}


def _batch_tracked_items(item_codes):
	"""Items tracked by batch only (serial numbered items keep ERPNext's own handling)."""
	codes = list(dict.fromkeys(item_codes))
	if not codes:
		return set()
	return set(
		frappe.get_all(
			"Item",
			filters={"name": ["in", codes], "has_batch_no": 1, "has_serial_no": 0},
			pluck="name",
		)
	)


def check_line_batches(lines):
	"""Every `batch_no` the till sends must be a batch of the line's (batch tracked) item."""
	wanted = {line["batch_no"] for line in lines if line.get("batch_no")}
	if not wanted:
		return
	batches = {
		row.name: row
		for row in frappe.get_all(
			"Batch", filters={"name": ["in", list(wanted)]}, fields=["name", "item", "disabled"]
		)
	}
	tracked = _batch_tracked_items(line["item_code"] for line in lines if line.get("batch_no"))
	for line in lines:
		batch_no = line.get("batch_no")
		if not batch_no:
			continue
		label = f"Line {line['line_no']} ({line['item_code']})"
		if line["item_code"] not in tracked:
			raise SyncError(
				VALIDATION, f"{label}: item is not tracked by batch, batch {batch_no} not allowed"
			)
		batch = batches.get(batch_no)
		if not batch:
			raise SyncError(VALIDATION, f"{label}: batch {batch_no} does not exist")
		if batch.item != line["item_code"]:
			raise SyncError(VALIDATION, f"{label}: batch {batch_no} belongs to item {batch.item}")


def require_line_batches(lines):
	"""A return without an invoice has no original row to take the batch from: every line of a
	batch tracked item must name the batch it goes back into."""
	tracked = _batch_tracked_items(line["item_code"] for line in lines)
	missing = lines_missing_batch(lines, tracked)
	if missing:
		line = missing[0]
		raise SyncError(
			VALIDATION,
			f"Line {line['line_no']} ({line['item_code']}): batch_no is required for a batch tracked "
			"item on a return without an invoice",
		)


def _returnable_batches(row_names):
	"""What each original row's batches can still take back ({row: {batch: stock qty}}), per ERPNext."""
	from erpnext.controllers.sales_and_purchase_return import get_available_serial_batches

	available = get_available_serial_batches(RETURN_REFERENCE_FIELD, SALES_INVOICE_ITEM, list(row_names))
	return {name: dict(data.get("batches") or {}) for name, data in (available or {}).items()}


def _qty_step(uom, precision):
	if uom and cint(frappe.get_cached_value("UOM", uom, "must_be_whole_number")):
		return Decimal(1)
	return Decimal(1).scaleb(-int(precision))


def plan_return_batches(lines, return_rows, precision):
	"""Batches of the return rows: {line_no: [(batch_no, qty)]} with positive quantities in the line's UOM.

	A line whose original row consumed one batch goes back into that batch; one
	whose original row consumed several is split across them (`split_return_qty`,
	capped by what ERPNext's `get_available_serial_batches` says each batch can
	still take back). Lines of items not tracked by batch, or whose original row
	has no batch, are left out (ERPNext's own return handling applies).
	"""
	names = list(dict.fromkeys(return_rows[line["line_no"]] for line in lines))
	rows = frappe.get_all(
		SALES_INVOICE_ITEM,
		filters={"name": ["in", names]},
		fields=["name", "idx", "item_code", "uom", "batch_no", "serial_and_batch_bundle", "stock_qty"],
	)
	tracked = _batch_tracked_items(row.item_code for row in rows)
	rows = [row for row in rows if row.item_code in tracked]
	by_name = {row.name: row for row in rows}
	consumed = row_batches(rows)
	several = [name for name, batches in consumed.items() if len({b for b, _q in batches}) > 1]
	returnable = _returnable_batches(several) if several else {}

	plan = {}
	for line in lines:
		row = by_name.get(return_rows[line["line_no"]])
		batches = consumed.get(row.name) if row else None
		if not batches:
			continue
		qty = abs(as_decimal(line["qty"]))
		batch_no = single_batch(batches)
		if batch_no:
			plan[line["line_no"]] = [(batch_no, qty)]
			continue

		factor = as_decimal(line.get("conversion_factor") or 1)
		# stock qty each batch can still take back, shared by all lines against this row
		left = returnable.setdefault(row.name, {batch_no: sold for batch_no, sold in batches})
		candidates = [
			(batch_no, as_decimal(sold) / factor, as_decimal(left.get(batch_no) or 0) / factor)
			for batch_no, sold in batches
		]
		try:
			parts = split_return_qty(qty, candidates, _qty_step(line.get("uom") or row.uom, precision))
		except BatchSplitError as e:
			raise SyncError(
				VALIDATION,
				f"Line {line['line_no']} ({line['item_code']}): original line {row.idx} {e}",
			)
		for batch_no, part in parts:
			left[batch_no] = float(as_decimal(left.get(batch_no) or 0) - part * factor)
		plan[line["line_no"]] = parts
	return plan
