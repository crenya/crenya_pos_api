"""pull_changes: keyset-paginated change feed ordered by (modified, name).

No OFFSET paging: each page continues strictly after the last (modified, name)
delivered. Records newer than `now - lag` are held back to the next pull so a
long-running transaction that commits late cannot slip behind the cursor.

Master data is read without per-user permission checks (like ERPNext's own
POS item search); access is governed by the authorized device and its POS
Profile.
"""

from datetime import timedelta

import frappe
from frappe.utils import cint, get_datetime, now_datetime

from crenya_pos_api.sync.context import get_pull_lag_seconds, money_precision
from crenya_pos_api.sync.cursor import Cursor, InvalidCursor, decode_cursor, encode_cursor
from crenya_pos_api.sync.errors import InvalidRequestError, raise_api_error
from crenya_pos_api.utils.dates import format_date, format_db_datetime, utc_now_iso
from crenya_pos_api.utils.decimal import format_money, format_number

DEFAULT_LIMIT = 500
MAX_LIMIT = 1000


def _descendants(doctype, roots):
	"""Names of all nodes under the given tree roots (inclusive), via lft/rgt in one query."""
	roots = [root for root in roots if root]
	if not roots:
		return []
	table = frappe.qb.DocType(doctype)
	bounds = (
		frappe.qb.from_(table).select(table.lft, table.rgt).where(table.name.isin(roots)).run(as_dict=True)
	)
	if not bounds:
		return []
	condition = None
	for bound in bounds:
		part = (table.lft >= bound.lft) & (table.rgt <= bound.rgt)
		condition = part if condition is None else condition | part
	return frappe.qb.from_(table).select(table.name).where(condition).run(pluck=True)


def _keyset_condition(table, cursor):
	if not cursor or cursor.modified is None:
		return None
	return (table.modified > cursor.modified) | (
		(table.modified == cursor.modified) & (table.name > cursor.name)
	)


class EntitySpec:
	doctype = None
	fields = ()

	def select(self, table):
		return [getattr(table, field) for field in self.fields]

	def filters(self, table, ctx):
		return []

	def build(self, rows, ctx):
		return rows


class ItemSpec(EntitySpec):
	doctype = "Item"
	fields = ("name", "item_code", "item_name", "item_group", "stock_uom", "is_stock_item", "disabled")

	def filters(self, table, ctx):
		conditions = [table.is_sales_item == 1]
		roots = [row.item_group for row in ctx.profile.get("item_groups") or []]
		if roots:
			groups = _descendants("Item Group", roots)
			conditions.append(table.item_group.isin(groups or [""]))
		return conditions

	def select(self, table):
		columns = super().select(table)
		if frappe.get_meta("Item").has_field("crenya_item_name_ar"):
			columns.append(table.crenya_item_name_ar)
		return columns

	def build(self, rows, ctx):
		names = [row.name for row in rows]
		barcodes = _group_children("Item Barcode", names, ["parent", "barcode", "uom"])
		uoms = _group_children("UOM Conversion Detail", names, ["parent", "uom", "conversion_factor"])
		tax_templates = _item_tax_templates(names, ctx.company)
		records = []
		for row in rows:
			records.append(
				{
					"item_code": row.item_code or row.name,
					"item_name": row.item_name,
					"item_name_ar": row.get("crenya_item_name_ar") or None,
					"item_group": row.item_group,
					"stock_uom": row.stock_uom,
					"is_stock_item": cint(row.is_stock_item),
					"disabled": cint(row.disabled),
					"item_tax_template": tax_templates.get(row.name),
					"barcodes": [
						{"barcode": child.barcode, "uom": child.uom} for child in barcodes.get(row.name, [])
					],
					"uoms": [
						{"uom": child.uom, "conversion_factor": format_number(child.conversion_factor)}
						for child in uoms.get(row.name, [])
					],
					"modified": format_db_datetime(row.modified),
				}
			)
		return records


class ItemPriceSpec(EntitySpec):
	doctype = "Item Price"
	fields = ("name", "item_code", "uom", "price_list_rate", "currency", "valid_from", "valid_upto")

	def filters(self, table, ctx):
		return [table.price_list == (ctx.profile.selling_price_list or "")]

	def build(self, rows, ctx):
		precision = money_precision()
		return [
			{
				"name": row.name,
				"item_code": row.item_code,
				"uom": row.uom,
				"price_list_rate": format_money(row.price_list_rate, precision),
				"currency": row.currency,
				"valid_from": format_date(row.valid_from),
				"valid_upto": format_date(row.valid_upto),
				"modified": format_db_datetime(row.modified),
			}
			for row in rows
		]


class CustomerSpec(EntitySpec):
	doctype = "Customer"
	fields = ("name", "customer_name", "customer_group", "mobile_no", "email_id", "tax_id", "disabled")

	def filters(self, table, ctx):
		roots = [row.customer_group for row in ctx.profile.get("customer_groups") or []]
		if not roots:
			return []
		groups = _descendants("Customer Group", roots)
		return [table.customer_group.isin(groups or [""])]

	def select(self, table):
		columns = super().select(table)
		if frappe.get_meta("Customer").has_field("crenya_local_id"):
			columns.append(table.crenya_local_id)
		return columns

	def build(self, rows, ctx):
		return [
			{
				"name": row.name,
				"customer_name": row.customer_name,
				"customer_group": row.customer_group,
				"mobile_no": row.mobile_no,
				"email_id": row.email_id,
				"tax_id": row.tax_id,
				"disabled": cint(row.disabled),
				"crenya_local_id": row.get("crenya_local_id") or None,
				"modified": format_db_datetime(row.modified),
			}
			for row in rows
		]


class StockSpec(EntitySpec):
	doctype = "Bin"
	fields = ("name", "item_code", "warehouse", "actual_qty", "reserved_qty")

	def filters(self, table, ctx):
		return [table.warehouse == (ctx.profile.warehouse or "")]

	def build(self, rows, ctx):
		precision = frappe.get_precision("Bin", "actual_qty")
		return [
			{
				"name": row.name,
				"item_code": row.item_code,
				"warehouse": row.warehouse,
				"actual_qty": format_number(row.actual_qty, precision),
				"reserved_qty": format_number(row.reserved_qty, precision),
				"modified": format_db_datetime(row.modified),
			}
			for row in rows
		]


ENTITIES = {
	"item": ItemSpec(),
	"item_price": ItemPriceSpec(),
	"customer": CustomerSpec(),
	"stock": StockSpec(),
}


def _group_children(doctype, parents, fields):
	"""One query for all child rows of the page, grouped by parent."""
	grouped = {}
	if not parents:
		return grouped
	rows = frappe.get_all(
		doctype,
		filters={"parent": ["in", parents], "parenttype": "Item"},
		fields=fields,
		order_by="parent asc, idx asc",
	)
	for row in rows:
		grouped.setdefault(row.parent, []).append(row)
	return grouped


def _item_tax_templates(items, company):
	"""First Item Tax row per item whose template belongs to the company (or to no company)."""
	if not items:
		return {}
	item_tax = frappe.qb.DocType("Item Tax")
	template = frappe.qb.DocType("Item Tax Template")
	rows = (
		frappe.qb.from_(item_tax)
		.join(template)
		.on(template.name == item_tax.item_tax_template)
		.select(item_tax.parent, item_tax.item_tax_template)
		.where(
			item_tax.parent.isin(items)
			& (item_tax.parenttype == "Item")
			& (template.disabled == 0)
			& ((template.company == company) | template.company.isnull() | (template.company == ""))
		)
		.orderby(item_tax.parent)
		.orderby(item_tax.idx)
		.run(as_dict=True)
	)
	result = {}
	for row in rows:
		result.setdefault(row.parent, row.item_tax_template)
	return result


def _fetch_records(spec, ctx, cursor, upper, limit):
	table = frappe.qb.DocType(spec.doctype)
	query = frappe.qb.from_(table).select(*spec.select(table), table.modified).where(table.modified <= upper)
	for condition in spec.filters(table, ctx):
		query = query.where(condition)
	keyset = _keyset_condition(table, cursor)
	if keyset is not None:
		query = query.where(keyset)
	rows = query.orderby(table.modified).orderby(table.name).limit(limit + 1).run(as_dict=True)
	return rows[:limit], len(rows) > limit


def _fetch_tombstones(doctype, cursor, upper, limit):
	deleted = frappe.qb.DocType("Deleted Document")
	query = (
		frappe.qb.from_(deleted)
		.select(deleted.name, deleted.deleted_name, deleted.creation)
		.where((deleted.deleted_doctype == doctype) & (deleted.creation <= upper) & (deleted.restored == 0))
	)
	if cursor.tomb_creation is not None:
		query = query.where(
			(deleted.creation > cursor.tomb_creation)
			| ((deleted.creation == cursor.tomb_creation) & (deleted.name > (cursor.tomb_name or "")))
		)
	rows = query.orderby(deleted.creation).orderby(deleted.name).limit(limit + 1).run(as_dict=True)
	return rows[:limit], len(rows) > limit


def _still_deleted(doctype, names):
	"""Drop tombstones for names that exist again (deleted, then re-created)."""
	if not names:
		return []
	alive = set(frappe.get_all(doctype, filters={"name": ["in", list(set(names))]}, pluck="name"))
	return [name for name in names if name not in alive]


def normalize_limit(limit):
	if limit in (None, ""):
		return DEFAULT_LIMIT
	try:
		value = int(limit)
	except (TypeError, ValueError):
		raise_api_error(InvalidRequestError, "limit must be an integer")
	return min(max(value, 1), MAX_LIMIT)


def pull_changes(ctx, entity, cursor_token=None, limit=None):
	spec = ENTITIES.get(entity)
	if not spec:
		raise_api_error(
			InvalidRequestError, f"Unknown entity {entity!r}; expected one of {', '.join(ENTITIES)}"
		)
	limit = normalize_limit(limit)

	try:
		cursor = decode_cursor(cursor_token, entity)
	except InvalidCursor as e:
		raise_api_error(InvalidRequestError, f"Invalid cursor: {e}")

	upper = format_db_datetime(now_datetime() - timedelta(seconds=get_pull_lag_seconds()))
	if cursor is None:
		# first pull: the snapshot replaces local data, only later deletions matter
		cursor = Cursor(entity=entity, tomb_creation=upper, tomb_name="")

	rows, records_more = _fetch_records(spec, ctx, cursor, upper, limit)
	tomb_rows, tombs_more = _fetch_tombstones(spec.doctype, cursor, upper, limit)

	modified, name = cursor.modified, cursor.name
	if rows:
		modified, name = format_db_datetime(get_datetime(rows[-1].modified)), rows[-1].name

	tomb_creation, tomb_name = cursor.tomb_creation, cursor.tomb_name
	if tomb_rows:
		tomb_creation = format_db_datetime(get_datetime(tomb_rows[-1].creation))
		tomb_name = tomb_rows[-1].name

	next_cursor = Cursor(
		entity=entity,
		modified=modified,
		name=name,
		tomb_creation=tomb_creation,
		tomb_name=tomb_name,
	)

	return {
		"entity": entity,
		"records": spec.build(rows, ctx),
		"tombstones": _still_deleted(spec.doctype, [row.deleted_name for row in tomb_rows]),
		"next_cursor": encode_cursor(next_cursor),
		"has_more": bool(records_more or tombs_more),
		"server_time": utc_now_iso(),
	}
