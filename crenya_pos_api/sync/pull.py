"""pull_changes: keyset-paginated change feed ordered by (modified, name).

No OFFSET paging: each page continues strictly after the last (modified, name)
delivered. Records newer than `now - lag` are held back to the next pull so a
long-running transaction that commits late cannot slip behind the cursor.

Master data is read without per-user permission checks (like ERPNext's own
POS item search); access is governed by the authorized device and its POS
Profile.
"""

import hashlib
from dataclasses import replace
from datetime import timedelta

import frappe
from frappe.utils import cint, get_datetime, getdate, now_datetime, nowdate

from crenya_pos_api.sync.batches import batch_quantities, batches_of_bundles, build_batch_records
from crenya_pos_api.sync.cashier import PIN_HASH_FIELD, device_roles_of
from crenya_pos_api.sync.context import device_roles, get_pull_lag_seconds, money_precision, profile_users
from crenya_pos_api.sync.cursor import Cursor, InvalidCursor, decode_cursor, encode_cursor
from crenya_pos_api.sync.errors import InvalidRequestError, raise_api_error
from crenya_pos_api.sync.promotions import rule_is_active
from crenya_pos_api.sync.tax_wording import GSTIN_FIELD, item_tax_code_field, tax_id_value
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
	"""One pull entity: which DocType rows a device receives, and how they are serialized.

	Register a subclass with the `crenya_pos_pull_entities` hook. `pull_changes` pages the
	DocType by (modified, name) (keyset, never OFFSET), adds tombstones from Deleted Document,
	and calls the methods below for one page at a time:

	- `select(table)`: the pypika columns to read (default: `fields`); `modified` is added.
	- `filters(table, ctx)`: extra pypika conditions for the device (`ctx` is DeviceContext).
	- `build(rows, ctx)`: the records sent to the device (default: the rows as they are).
	- `cursor_scope(ctx)`: a short string the records depend on besides the rows themselves
	  (default None). A cursor issued under another scope restarts the records from the
	  beginning (tombstones keep their position), so the device gets every record again.
	"""

	doctype = None
	fields = ()
	# entities whose records also change with stock movements (see BatchSpec)
	follows_stock = False

	def select(self, table):
		return [getattr(table, field) for field in self.fields]

	def filters(self, table, ctx):
		return []

	def build(self, rows, ctx):
		return rows

	def cursor_scope(self, ctx):
		return None


class ItemSpec(EntitySpec):
	doctype = "Item"
	fields = (
		"name",
		"item_code",
		"item_name",
		"item_group",
		"brand",
		"stock_uom",
		"is_stock_item",
		"has_batch_no",
		"has_expiry_date",
		"disabled",
	)

	def filters(self, table, ctx):
		return _profile_item_conditions(table, ctx)

	def select(self, table):
		columns = super().select(table)
		meta = frappe.get_meta("Item")
		if meta.has_field("crenya_item_name_ar"):
			columns.append(table.crenya_item_name_ar)
		# India Compliance's HSN/SAC code, when the site has it
		code_field = item_tax_code_field(meta)
		if code_field:
			columns.append(getattr(table, code_field))
		return columns

	def build(self, rows, ctx):
		names = [row.name for row in rows]
		barcodes = _group_children("Item Barcode", names, ["parent", "barcode", "uom"])
		uoms = _group_children("UOM Conversion Detail", names, ["parent", "uom", "conversion_factor"])
		tax_templates = _item_tax_templates(names, ctx.company)
		code_field = item_tax_code_field(frappe.get_meta("Item"))
		records = []
		for row in rows:
			records.append(
				{
					"item_code": row.item_code or row.name,
					"item_name": row.item_name,
					"item_name_ar": row.get("crenya_item_name_ar") or None,
					"item_group": row.item_group,
					"brand": row.brand or None,
					"stock_uom": row.stock_uom,
					"is_stock_item": cint(row.is_stock_item),
					"has_batch_no": cint(row.has_batch_no),
					"has_expiry_date": cint(row.has_expiry_date),
					"disabled": cint(row.disabled),
					"item_tax_template": tax_templates.get(row.name),
					"tax_code": (row.get(code_field) or None) if code_field else None,
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


class BatchSpec(EntitySpec):
	"""Batches of the profile's batch tracked items with their stock in the profile warehouse.

	A batch is (re)sent when the Batch changes (keyset on Batch `modified`) and when
	its stock in the warehouse moves: a second keyset position (`sm`/`sn` in the
	cursor) walks the warehouse's Stock Ledger Entries of those items by
	(modified, name), which catches new entries as well as cancellations (ERPNext
	sets `is_cancelled` and `modified` on the cancelled entries and adds reversing
	ones). Expired and disabled batches are sent like any other.
	"""

	doctype = "Batch"
	fields = ("name", "item", "expiry_date", "manufacturing_date", "disabled")
	follows_stock = True

	def _items(self, ctx):
		item = frappe.qb.DocType("Item")
		query = frappe.qb.from_(item).select(item.name).where(item.has_batch_no == 1)
		for condition in _profile_item_conditions(item, ctx):
			query = query.where(condition)
		return query

	def filters(self, table, ctx):
		return [table.item.isin(self._items(ctx))]

	def fetch_stock_changes(self, ctx, position, upper, limit):
		"""Batches touched by the next `limit` Stock Ledger Entries after `position` (modified, name).

		Returns (batch rows, more entries pending, new position or None).
		"""
		warehouse = ctx.profile.warehouse
		if not warehouse:
			return [], False, None
		sle = frappe.qb.DocType("Stock Ledger Entry")
		start_modified, start_name = position
		entries = (
			frappe.qb.from_(sle)
			.select(sle.name, sle.modified, sle.serial_and_batch_bundle, sle.batch_no)
			.where(
				(sle.warehouse == warehouse)
				& (sle.modified <= upper)
				& sle.item_code.isin(self._items(ctx))
				& (
					(sle.modified > start_modified)
					| ((sle.modified == start_modified) & (sle.name > (start_name or "")))
				)
			)
			.orderby(sle.modified)
			.orderby(sle.name)
			.limit(limit + 1)
			.run(as_dict=True)
		)
		more = len(entries) > limit
		entries = entries[:limit]
		if not entries:
			return [], False, None

		names = {entry.batch_no for entry in entries if entry.batch_no}
		for batches in batches_of_bundles(entry.serial_and_batch_bundle for entry in entries).values():
			names.update(batches)
		rows = []
		if names:
			table = frappe.qb.DocType(self.doctype)
			query = (
				frappe.qb.from_(table)
				.select(*self.select(table), table.modified)
				.where(table.name.isin(sorted(names)))
			)
			for condition in self.filters(table, ctx):
				query = query.where(condition)
			rows = query.orderby(table.modified).orderby(table.name).run(as_dict=True)
		last = entries[-1]
		return rows, more, (format_db_datetime(get_datetime(last.modified)), last.name)

	def build(self, rows, ctx):
		quantities = batch_quantities([row.name for row in rows], ctx.profile.warehouse)
		return build_batch_records(rows, quantities, frappe.get_precision("Stock Ledger Entry", "actual_qty"))


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
		meta = frappe.get_meta("Customer")
		if meta.has_field("crenya_local_id"):
			columns.append(table.crenya_local_id)
		if meta.has_field(GSTIN_FIELD):
			columns.append(getattr(table, GSTIN_FIELD))
		return columns

	def build(self, rows, ctx):
		has_gstin = frappe.get_meta("Customer").has_field(GSTIN_FIELD)
		return [
			{
				"name": row.name,
				"customer_name": row.customer_name,
				"customer_group": row.customer_group,
				"mobile_no": row.mobile_no,
				"email_id": row.email_id,
				# empty tax_id falls back to India Compliance's GSTIN
				"tax_id": tax_id_value(row, has_gstin) or row.tax_id,
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


class CashierSpec(EntitySpec):
	"""Till cashiers: Users holding a role that opens the pulling device's type (Crenya POS User,
	or a role another app registers with `crenya_pos_device_roles` for every device type or for
	this one, e.g. a restaurant manager on a `restaurant_pos` device). On a retail till (type
	"till") a role scoped to other device types counts for nothing: such a user is not a
	cashier there (with a PIN: `enabled: 0`, `roles: []`).

	Users with a PIN hash are always included, so a cashier who loses every device role, is
	disabled, or is removed from the profile's Applicable for Users comes back with
	`enabled: 0` (profile changes bump `modified` of the affected users, see
	`pos_profile_on_update`). `pin_hash` is only sent for enabled cashiers.

	`roles` lists the roles of the device's type the user holds (in `device_roles()` order)
	and nothing else: a device decides with it who may approve a manager-only action. It is []
	when `enabled` is 0. Adding or removing a role saves the User, so the change is pulled.

	The cursor is scoped to the device type's role set (`cursor_scope`): when it changes (the
	device registers again with another type, or an app adds a role for the type) the next
	pull sends every cashier again with the new `enabled` / `roles`.
	"""

	doctype = "User"
	fields = ("name", "full_name", "enabled")

	def _has_pin_hash(self):
		return frappe.get_meta("User").has_field(PIN_HASH_FIELD)

	def select(self, table):
		columns = super().select(table)
		if self._has_pin_hash():
			columns.append(getattr(table, PIN_HASH_FIELD))
		return columns

	def cursor_scope(self, ctx):
		roles = "\n".join(device_roles(ctx.device_type))
		return hashlib.sha256(roles.encode("utf-8")).hexdigest()[:16]

	def filters(self, table, ctx):
		has_role = frappe.qb.DocType("Has Role")
		holders = (
			frappe.qb.from_(has_role)
			.select(has_role.parent)
			.where((has_role.parenttype == "User") & has_role.role.isin(list(device_roles(ctx.device_type))))
		)
		candidate = table.name.isin(holders)
		allowed = profile_users(ctx.profile)
		if allowed:
			candidate = candidate & table.name.isin(allowed)
		if self._has_pin_hash():
			pin_hash = getattr(table, PIN_HASH_FIELD)
			candidate = candidate | (pin_hash.notnull() & (pin_hash != ""))
		return [table.name != "Guest", candidate]

	def build(self, rows, ctx):
		held = device_roles_of([row.name for row in rows], ctx.device_type)
		allowed = set(profile_users(ctx.profile))

		records = []
		for row in rows:
			roles = held.get(row.name) or []
			enabled = cint(row.enabled) and roles and (not allowed or row.name in allowed)
			records.append(
				{
					"name": row.name,
					"full_name": row.full_name,
					"enabled": 1 if enabled else 0,
					"pin_hash": (row.get(PIN_HASH_FIELD) or None) if enabled else None,
					"roles": list(roles) if enabled else [],
					"modified": format_db_datetime(row.modified),
				}
			)
		return records


class ItemGroupSpec(EntitySpec):
	"""The whole Item Group tree (a small table): tills resolve item group promotions with it."""

	doctype = "Item Group"
	fields = ("name", "parent_item_group", "lft", "rgt")

	def select(self, table):
		columns = super().select(table)
		if frappe.get_meta("Item Group").has_field("crenya_item_group_name_ar"):
			columns.append(table.crenya_item_group_name_ar)
		return columns

	def build(self, rows, ctx):
		return [
			{
				"name": row.name,
				"item_group_name_ar": row.get("crenya_item_group_name_ar") or None,
				"parent_item_group": row.parent_item_group or None,
				"lft": cint(row.lft),
				"rgt": cint(row.rgt),
				"modified": format_db_datetime(row.modified),
			}
			for row in rows
		]


class PricingRuleSpec(EntitySpec):
	"""Promotions: Pricing Rules the till evaluates offline.

	Every changed rule is sent; rules the till cannot evaluate (see
	`promotions.rule_qualifies`), disabled rules and rules whose `valid_upto` has
	passed arrive with `disabled: 1`, so a till drops a rule that stops qualifying.
	"""

	doctype = "Pricing Rule"
	# qualification inputs, read but not sent as such
	check_fields = (
		"disable",
		"selling",
		"company",
		"currency",
		"condition",
		"coupon_code_based",
		"apply_rule_on_other",
		"margin_type",
		"margin_rate_or_amount",
		"validate_applied_rule",
	)
	fields = (
		"name",
		"title",
		"priority",
		"apply_on",
		"mixed_conditions",
		"is_cumulative",
		"applicable_for",
		"customer",
		"customer_group",
		"min_qty",
		"max_qty",
		"min_amt",
		"max_amt",
		"valid_from",
		"valid_upto",
		"price_or_product_discount",
		"rate_or_discount",
		"rate",
		"discount_percentage",
		"discount_amount",
		"apply_discount_on_rate",
		"apply_multiple_pricing_rules",
		"for_price_list",
		"warehouse",
		"threshold_percentage",
		"same_item",
		"free_item",
		"free_qty",
		"free_item_uom",
		"free_item_rate",
		"round_free_qty",
		"is_recursive",
		"recurse_for",
		"apply_recursion_over",
		"promotional_scheme",
		"rule_description",
		*check_fields,
	)
	flag_fields = (
		"mixed_conditions",
		"is_cumulative",
		"apply_discount_on_rate",
		"apply_multiple_pricing_rules",
		"same_item",
		"round_free_qty",
		"is_recursive",
	)
	money_fields = ("min_amt", "max_amt", "rate", "discount_amount", "free_item_rate")
	number_fields = (
		"min_qty",
		"max_qty",
		"discount_percentage",
		"threshold_percentage",
		"free_qty",
		"recurse_for",
		"apply_recursion_over",
	)
	link_fields = (
		"customer",
		"customer_group",
		"for_price_list",
		"warehouse",
		"free_item",
		"free_item_uom",
		"promotional_scheme",
		"rule_description",
	)

	def select(self, table):
		# only columns this ERPNext version has (v15 / v16)
		meta = frappe.get_meta(self.doctype)
		return [getattr(table, field) for field in self.fields if field == "name" or meta.has_field(field)]

	def build(self, rows, ctx):
		names = [row.name for row in rows]
		items = _group_children(
			"Pricing Rule Item Code", names, ["parent", "item_code", "uom"], "Pricing Rule"
		)
		groups = _group_children("Pricing Rule Item Group", names, ["parent", "item_group"], "Pricing Rule")
		brands = _group_children("Pricing Rule Brand", names, ["parent", "brand"], "Pricing Rule")
		today = getdate(nowdate())
		money = money_precision()
		meta = frappe.get_meta(self.doctype)
		precisions = {
			field: frappe.get_precision(self.doctype, field) if meta.has_field(field) else None
			for field in self.number_fields
		}

		records = []
		for row in rows:
			record = {
				"name": row.name,
				"title": row.get("title") or row.name,
				"disabled": 0 if rule_is_active(row, ctx.company, today, ctx.currency) else 1,
				"priority": cint(row.get("priority")),
				"apply_on": row.get("apply_on"),
				"items": [
					{"item_code": child.item_code, "uom": child.uom or None}
					for child in items.get(row.name, [])
					if child.item_code
				],
				"item_groups": [child.item_group for child in groups.get(row.name, []) if child.item_group],
				"brands": [child.brand for child in brands.get(row.name, []) if child.brand],
				"applicable_for": row.get("applicable_for") or "",
				"valid_from": format_date(row.get("valid_from")),
				"valid_upto": format_date(row.get("valid_upto")),
				"price_or_product_discount": row.get("price_or_product_discount"),
				"rate_or_discount": row.get("rate_or_discount") or None,
				"modified": format_db_datetime(row.modified),
			}
			for field in self.flag_fields:
				record[field] = cint(row.get(field))
			for field in self.money_fields:
				record[field] = format_money(row.get(field) or 0, money)
			for field in self.number_fields:
				record[field] = format_number(row.get(field) or 0, precisions[field])
			for field in self.link_fields:
				record[field] = row.get(field) or None
			records.append(record)
		return records


class UomSpec(EntitySpec):
	"""Every UOM with ERPNext's whole-number flag: tills refuse fractional quantities in such a
	UOM, as ERPNext does on submit ("Quantity cannot be a fraction")."""

	doctype = "UOM"
	fields = ("name", "must_be_whole_number")

	def build(self, rows, ctx):
		return build_uom_records(rows)


def build_uom_records(rows):
	return [
		{
			"name": row.name,
			"must_be_whole_number": bool(cint(row.must_be_whole_number)),
			"modified": format_db_datetime(row.modified),
		}
		for row in rows
	]


# built-in entities; crenya_pos_api.sync.registry.entities() adds those of other apps
ENTITIES = {
	"item": ItemSpec(),
	"item_price": ItemPriceSpec(),
	"customer": CustomerSpec(),
	"stock": StockSpec(),
	"cashier": CashierSpec(),
	"item_group": ItemGroupSpec(),
	"pricing_rule": PricingRuleSpec(),
	"batch": BatchSpec(),
	"uom": UomSpec(),
}


def _profile_item_conditions(table, ctx):
	"""Items a till of the profile sells: sales items of the profile's item groups (and sub-groups)."""
	conditions = [table.is_sales_item == 1]
	roots = [row.item_group for row in ctx.profile.get("item_groups") or []]
	if roots:
		groups = _descendants("Item Group", roots)
		conditions.append(table.item_group.isin(groups or [""]))
	return conditions


def _group_children(doctype, parents, fields, parenttype="Item"):
	"""One query for all child rows of the page, grouped by parent."""
	grouped = {}
	if not parents:
		return grouped
	rows = frappe.get_all(
		doctype,
		filters={"parent": ["in", parents], "parenttype": parenttype},
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
	# built-in ENTITIES first, then other apps' crenya_pos_pull_entities
	from crenya_pos_api.sync.registry import entities

	known = entities()
	spec = known.get(entity) if isinstance(entity, str) else None
	if not spec:
		raise_api_error(InvalidRequestError, f"Unknown entity {entity!r}; expected one of {', '.join(known)}")
	limit = normalize_limit(limit)

	try:
		cursor = decode_cursor(cursor_token, entity)
	except InvalidCursor as e:
		raise_api_error(InvalidRequestError, f"Invalid cursor: {e}")

	upper = format_db_datetime(now_datetime() - timedelta(seconds=get_pull_lag_seconds()))
	scope = spec.cursor_scope(ctx)
	if cursor is not None and cursor.scope != scope:
		# the records depend on something that changed since this cursor (e.g. the device's
		# type for `cashier`): send them all again, keep the tombstone / stock positions
		cursor = replace(cursor, modified=None, name=None)
	if cursor is None:
		# first pull: the snapshot replaces local data, only later deletions / stock movements matter
		cursor = Cursor(
			entity=entity,
			tomb_creation=upper,
			tomb_name="",
			stock_modified=upper if spec.follows_stock else None,
			stock_name="" if spec.follows_stock else None,
		)

	rows, records_more = _fetch_records(spec, ctx, cursor, upper, limit)
	tomb_rows, tombs_more = _fetch_tombstones(spec.doctype, cursor, upper, limit)

	stock_modified, stock_name, stock_rows, stock_more = None, None, [], False
	if spec.follows_stock:
		# a cursor without a stock position follows stock from its record position on
		stock_modified = cursor.stock_modified or cursor.modified or upper
		stock_name = cursor.stock_name if cursor.stock_modified else ""
		stock_rows, stock_more, position = spec.fetch_stock_changes(
			ctx, (stock_modified, stock_name), upper, limit
		)
		if position:
			stock_modified, stock_name = position

	modified, name = cursor.modified, cursor.name
	if rows:
		modified, name = format_db_datetime(get_datetime(rows[-1].modified)), rows[-1].name

	tomb_creation, tomb_name = cursor.tomb_creation, cursor.tomb_name
	if tomb_rows:
		tomb_creation = format_db_datetime(get_datetime(tomb_rows[-1].creation))
		tomb_name = tomb_rows[-1].name

	next_cursor = Cursor(
		entity=entity,
		scope=scope,
		modified=modified,
		name=name,
		tomb_creation=tomb_creation,
		tomb_name=tomb_name,
		stock_modified=stock_modified,
		stock_name=stock_name,
	)

	if spec.follows_stock:
		# records whose stock moved, sent again with their current quantities
		seen = {row.name for row in rows}
		rows = rows + [row for row in stock_rows if row.name not in seen]

	return {
		"entity": entity,
		"records": spec.build(rows, ctx),
		"tombstones": _still_deleted(spec.doctype, [row.deleted_name for row in tomb_rows]),
		"next_cursor": encode_cursor(next_cursor),
		"has_more": bool(records_more or tombs_more or stock_more),
		"server_time": utc_now_iso(),
	}
