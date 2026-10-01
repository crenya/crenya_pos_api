"""Build, check and submit a Sales Invoice (sale or return) from a till payload.

The invoice goes through the normal ERPNext controller (insert + submit, no
ignore_permissions); the till's numbers are only trusted after the server has
recomputed them.
"""

import json

import frappe
from frappe.utils import cint, flt

from crenya_pos_api.sync.batches import check_line_batches, plan_return_batches, require_line_batches
from crenya_pos_api.sync.cashier import enabled_user
from crenya_pos_api.sync.context import (
	get_total_tolerance,
	get_update_stock,
	is_rounded_total_disabled,
	money_precision,
	qty_precision,
)
from crenya_pos_api.sync.errors import DEPENDENCY_MISSING, TOTAL_MISMATCH, VALIDATION, SyncError
from crenya_pos_api.sync.loyalty import apply_redemption, customer_program
from crenya_pos_api.sync.open_returns import assert_allowed, set_valuation_incoming_rates
from crenya_pos_api.sync.taxes import allowed_template_names, resolve_invoice_template
from crenya_pos_api.sync.validation import is_open_return
from crenya_pos_api.utils.dates import format_db_datetime
from crenya_pos_api.utils.decimal import as_decimal, format_money, quantize, within_tolerance

ORIGINAL_FIELDS = [
	"name",
	"customer",
	"company",
	"docstatus",
	"is_return",
	"update_stock",
	"currency",
	"loyalty_program",
	"loyalty_amount",
]

LOYALTY_RETURN_MESSAGE = "Return this invoice from ERPNext: it was partly paid with loyalty points"

# Till lines keep the names of the promotions applied to them in the row flags until submit.
PRICING_RULES_FLAG = "crenya_pricing_rules"


def resolve_customer(ctx, data):
	local_id = data.get("customer_local_id")
	if local_id:
		name = frappe.db.get_value("Customer", {"crenya_local_id": local_id}, "name")
		if not name:
			raise SyncError(DEPENDENCY_MISSING, f"Customer with local id {local_id} is not on the server yet")
		return name

	customer = data.get("customer")
	if customer:
		if not frappe.db.exists("Customer", customer):
			raise SyncError(VALIDATION, f"Customer {customer} does not exist")
		return customer

	if ctx.profile.customer:
		return ctx.profile.customer

	raise SyncError(VALIDATION, "Invoice has no customer and the POS Profile has no default customer")


def assert_returnable_at_till(loyalty_amount, precision):
	"""Invoices partly paid with loyalty points are returned from ERPNext, not from the till."""
	if quantize(loyalty_amount or 0, precision) > 0:
		raise SyncError(VALIDATION, LOYALTY_RETURN_MESSAGE)


def resolve_return_against(ctx, data):
	"""Original invoice of a return, or None for a return without reference."""
	if not data.get("is_return"):
		return None

	if data.get("return_against"):
		original = frappe.db.get_value("Sales Invoice", data["return_against"], ORIGINAL_FIELDS, as_dict=True)
		if not original:
			raise SyncError(VALIDATION, f"Original invoice {data['return_against']} does not exist")
	elif data.get("return_against_local_id"):
		local_id = data["return_against_local_id"]
		original = frappe.db.get_value(
			"Sales Invoice", {"crenya_local_id": local_id}, ORIGINAL_FIELDS, as_dict=True
		)
		if not original or original.docstatus == 0:
			raise SyncError(
				DEPENDENCY_MISSING, f"Original invoice with local id {local_id} is not on the server yet"
			)
	else:
		return None

	if original.docstatus != 1:
		raise SyncError(VALIDATION, f"Original invoice {original.name} is not submitted")
	if cint(original.is_return):
		raise SyncError(VALIDATION, f"{original.name} is itself a return")
	if original.company != ctx.company:
		raise SyncError(VALIDATION, f"Original invoice {original.name} belongs to another company")
	assert_returnable_at_till(original.loyalty_amount, money_precision())
	return original


def map_return_rows(original_name, lines):
	"""Map each return line to the original Sales Invoice Item (line_no -> row name)."""
	rows = frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": original_name, "parenttype": "Sales Invoice"},
		fields=["name", "idx", "item_code"],
		order_by="idx asc",
	)
	by_name = {row.name: row for row in rows}
	by_idx = {cint(row.idx): row for row in rows}

	mapping = {}
	for line in lines:
		label = f"Line {line['line_no']} ({line['item_code']})"
		if line.get("against_row_name"):
			row = by_name.get(line["against_row_name"])
			if not row:
				raise SyncError(
					VALIDATION, f"{label}: row {line['against_row_name']} is not on invoice {original_name}"
				)
		elif line.get("against_line_no"):
			row = by_idx.get(line["against_line_no"])
			if not row:
				raise SyncError(
					VALIDATION, f"{label}: invoice {original_name} has no line {line['against_line_no']}"
				)
		else:
			candidates = [row for row in rows if row.item_code == line["item_code"]]
			if not candidates:
				raise SyncError(VALIDATION, f"{label}: item was not sold on invoice {original_name}")
			if len(candidates) > 1:
				raise SyncError(
					VALIDATION,
					f"{label}: item appears on several lines of {original_name}; send against_line_no",
				)
			row = candidates[0]

		if row.item_code != line["item_code"]:
			raise SyncError(
				VALIDATION,
				f"{label}: original line {row.idx} is item {row.item_code}, not {line['item_code']}",
			)
		mapping[line["line_no"]] = row.name
	return mapping


def _profile_payment_modes(profile):
	rows = [row for row in profile.get("payments") or [] if row.mode_of_payment]
	modes = [row.mode_of_payment for row in rows]
	default = next((row.mode_of_payment for row in rows if cint(row.default)), modes[0] if modes else None)
	return modes, default


def _append_payments(doc, ctx, payments):
	from erpnext.accounts.doctype.sales_invoice.sales_invoice import get_mode_of_payments_info

	precision = money_precision()
	modes, default_mode = _profile_payment_modes(ctx.profile)
	info = get_mode_of_payments_info(modes, ctx.company) if modes else {}

	for payment in payments:
		mode = payment["mode_of_payment"]
		if mode not in modes:
			raise SyncError(
				VALIDATION, f"Mode of payment {mode} is not enabled on POS Profile {ctx.profile.name}"
			)
		details = info.get(mode)
		if not details:
			raise SyncError(
				VALIDATION, f"Mode of payment {mode} has no default account for company {ctx.company}"
			)
		amount = flt(f"{payment['amount']:f}", precision)
		if not amount:
			continue
		doc.append(
			"payments",
			{
				"mode_of_payment": mode,
				"amount": amount,
				"account": details.default_account,
				"type": details.type,
				"default": 1 if mode == default_mode else 0,
			},
		)
	return default_mode


def _set_batch(row, batch_no):
	"""ERPNext v15 builds the row's Serial and Batch Bundle from these fields on submit."""
	row["batch_no"] = batch_no
	row["use_serial_batch_fields"] = 1


def _append_items(doc, ctx, lines, return_rows, batch_plan):
	q_precision = qty_precision()
	rate_precision = frappe.get_precision("Sales Invoice Item", "rate")
	cf_precision = frappe.get_precision("Sales Invoice Item", "conversion_factor")
	profile = ctx.profile

	for line in lines:
		rate = flt(f"{line['rate']:f}", rate_precision)
		row = {
			"item_code": line["item_code"],
			"qty": flt(f"{line['qty']:f}", q_precision),
			"conversion_factor": flt(f"{line['conversion_factor']:f}", cf_precision),
			"rate": rate,
			"warehouse": profile.warehouse,
		}
		if line.get("uom"):
			row["uom"] = line["uom"]
		if profile.get("cost_center"):
			row["cost_center"] = profile.cost_center
		if line.get("item_tax_template"):
			row["item_tax_template"] = line["item_tax_template"]
		if line.get("is_free_item"):
			row["is_free_item"] = 1

		if not rate:
			# a zero rate with a price list rate would be re-priced by ERPNext
			row["price_list_rate"] = 0
			row["discount_percentage"] = 0
		else:
			if line.get("price_list_rate") is not None:
				row["price_list_rate"] = flt(f"{line['price_list_rate']:f}", rate_precision)
			if line.get("discount_percentage") is not None:
				row["discount_percentage"] = flt(f"{line['discount_percentage']:f}")

		if return_rows:
			row["sales_invoice_item"] = return_rows[line["line_no"]]

		parts = batch_plan.get(line["line_no"])
		if parts:
			# a return goes back into the original row's batches, one row per batch
			rows = []
			for batch_no, qty in parts:
				part = dict(row, qty=-flt(f"{qty:f}", q_precision))
				_set_batch(part, batch_no)
				rows.append(part)
		else:
			if line.get("batch_no"):
				_set_batch(row, line["batch_no"])
			rows = [row]

		for values in rows:
			child = doc.append("items", values)
			if line.get("pricing_rules"):
				child.flags[PRICING_RULES_FLAG] = json.dumps(line["pricing_rules"])


def restore_pricing_rules(doc, method=None):
	"""Sales Invoice `before_submit`: store the till's promotion names on the item rows.

	They are kept out of the rows while ERPNext validates: with `ignore_pricing_rule`
	set, validating a saved invoice whose rows name pricing rules undoes those rules
	(clears `pricing_rules`, resets a discount percentage rule's rate to the price
	list rate), and a row with `pricing_rules` and a discount percentage is re-priced
	from the price list rate. `before_submit` runs after the last validation, so the
	till's rates stay as sent and the names are saved with the submitted invoice.
	"""
	for row in doc.get("items") or []:
		value = row.flags.get(PRICING_RULES_FLAG)
		if value:
			row.pricing_rules = value


def _payable_total(doc):
	if doc.is_rounded_total_disabled():
		return doc.grand_total
	return doc.rounded_total or doc.grand_total


def _payments_due(doc):
	"""What the payment rows must cover: ERPNext counts a redeemed loyalty amount as paid."""
	due = _payable_total(doc)
	if cint(doc.get("redeem_loyalty_points")):
		due = flt(due) - flt(doc.get("loyalty_amount"))
	return due


def check_grand_total(doc, data):
	precision = money_precision(doc.currency)
	tolerance = get_total_tolerance(precision)
	server_total = quantize(doc.grand_total, precision)
	client_total = data["client_totals"]["grand_total"]
	if not within_tolerance(server_total, client_total, tolerance):
		raise SyncError(
			TOTAL_MISMATCH,
			f"Server grand total {format_money(server_total, precision)} differs from till grand total "
			f"{format_money(client_total, precision)} by more than {format_money(tolerance, precision)}",
		)


def _absorb_row(doc, default_mode):
	rows = doc.get("payments") or []
	for row in rows:
		if cint(row.default):
			return row
	for row in rows:
		if row.type == "Cash":
			return row
	if rows:
		return rows[0]
	if default_mode:
		return doc.append("payments", {"mode_of_payment": default_mode, "amount": 0, "default": 1})
	return None


def _ensure_account(doc, row):
	if not row.account:
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import get_bank_cash_account

		row.account = get_bank_cash_account(row.mode_of_payment, doc.company).get("account")


def reconcile_payments(doc, default_mode, notes):
	"""Payments must equal the payable total (less any loyalty redemption); a difference within
	tolerance goes to the default/cash row."""
	precision = money_precision(doc.currency)
	tolerance = get_total_tolerance(precision)
	payable = quantize(_payments_due(doc), precision)
	paid = sum((quantize(row.amount, precision) for row in doc.get("payments") or []), as_decimal(0))
	difference = payable - paid

	if difference:
		if abs(difference) > as_decimal(tolerance):
			raise SyncError(
				TOTAL_MISMATCH,
				f"Payments {format_money(paid, precision)} do not match payable total "
				f"{format_money(payable, precision)}",
			)
		row = _absorb_row(doc, default_mode)
		if row is None:
			raise SyncError(
				VALIDATION, "POS Profile has no mode of payment to absorb the rounding difference"
			)
		_ensure_account(doc, row)
		row.amount = flt(flt(row.amount) + float(difference), precision)
		notes.append(
			f"Payment difference {format_money(difference, precision)} absorbed into {row.mode_of_payment}"
		)

	keep = [row for row in doc.get("payments") or [] if flt(row.amount, precision)]
	if not keep and cint(doc.get("redeem_loyalty_points")):
		# paid in full with points: ERPNext still wants one mode of payment on a POS invoice
		row = _absorb_row(doc, default_mode)
		if row is not None:
			_ensure_account(doc, row)
			keep = [row]
	kept = {id(row) for row in keep}
	for row in list(doc.get("payments") or []):
		if id(row) not in kept:
			doc.remove(row)


def build_invoice(ctx, data, notes):
	"""Create the unsaved Sales Invoice with server-computed totals."""
	profile = ctx.profile

	if data["pos_profile"] != profile.name:
		raise SyncError(
			VALIDATION,
			f"Invoice POS Profile {data['pos_profile']} does not match device profile {profile.name}",
		)
	if data["company"] != ctx.company:
		raise SyncError(VALIDATION, f"Invoice company {data['company']} does not match {ctx.company}")
	if data.get("currency") and data["currency"] != ctx.currency:
		raise SyncError(VALIDATION, f"Invoice currency {data['currency']} does not match {ctx.currency}")
	default_template = profile.get("taxes_and_charges") or None
	requested_template = data.get("taxes_and_charges")
	taxes_and_charges = resolve_invoice_template(
		requested_template,
		allowed_template_names(profile)
		if requested_template and requested_template != default_template
		else {default_template},
		default_template,
	)
	cashier = enabled_user(data.get("cashier"))
	if data.get("cashier") and not cashier:
		notes.append(f"Till cashier {data['cashier']} is not an enabled user; POS Cashier left empty")

	customer = resolve_customer(ctx, data)
	original = resolve_return_against(ctx, data)
	open_return = is_open_return(data)
	if open_return:
		# a till may have been allowed when it sold offline: the profile decides at push time
		assert_allowed(profile)
	return_rows = None
	batch_plan = {}
	if original:
		return_rows = map_return_rows(original.name, data["items"])
		batch_plan = plan_return_batches(data["items"], return_rows, qty_precision())
		if original.customer != customer:
			notes.append(f"Customer {customer} replaced by original invoice customer {original.customer}")
			customer = original.customer

	doc = frappe.new_doc("Sales Invoice")
	doc.update(
		{
			"company": ctx.company,
			"is_pos": 1,
			"pos_profile": profile.name,
			"customer": customer,
			"posting_date": data["posting_date"],
			"posting_time": data["posting_time"],
			"set_posting_time": 1,
			"currency": ctx.currency,
			"is_return": data["is_return"],
			"return_against": original.name if original else None,
			"update_stock": cint(original.update_stock) if original else get_update_stock(profile),
			"ignore_pricing_rule": 1,
			"set_warehouse": profile.warehouse,
			"selling_price_list": profile.selling_price_list,
			"taxes_and_charges": taxes_and_charges,
			"tax_category": profile.get("tax_category"),
			"disable_rounded_total": is_rounded_total_disabled(profile),
			"remarks": data.get("remarks"),
			"crenya_local_id": data["local_id"],
			"crenya_offline_number": data.get("offline_number"),
			"crenya_device": ctx.device_id,
			"crenya_shift_id": data.get("shift_local_id"),
			"crenya_cashier": cashier,
		}
	)
	if profile.get("cost_center"):
		doc.cost_center = profile.cost_center

	check_line_batches([line for line in data["items"] if line["line_no"] not in batch_plan])
	if open_return:
		require_line_batches(data["items"])
	_append_items(doc, ctx, data["items"], return_rows, batch_plan)

	doc.set_missing_values(for_validate=True)
	# POS Profile values must not re-enable pricing rules: the till's rate is final
	doc.ignore_pricing_rule = 1
	if open_return:
		set_valuation_incoming_rates(doc)
	if doc.taxes_and_charges and not doc.get("taxes"):
		from erpnext.controllers.accounts_controller import get_taxes_and_charges

		for tax in get_taxes_and_charges("Sales Taxes and Charges Template", doc.taxes_and_charges) or []:
			doc.append("taxes", tax)

	if doc.is_return:
		# a return carrying the original's program makes ERPNext re-book the original invoice's
		# earned points on submit (and again on cancel); points are never redeemed on a return
		doc.loyalty_program = original.loyalty_program if original else None
	else:
		# earn points like a desk invoice of an enrolled customer
		program = customer_program(customer, ctx.company)
		doc.loyalty_program = program.name if program else None

	default_mode = _append_payments(doc, ctx, data["payments"])
	doc.calculate_taxes_and_totals()
	if data.get("loyalty"):
		apply_redemption(doc, ctx, data["loyalty"], _payable_total(doc))
		doc.calculate_taxes_and_totals()

	check_grand_total(doc, data)
	reconcile_payments(doc, default_mode, notes)
	doc.calculate_taxes_and_totals()
	return doc


def submit_invoice(ctx, data, notes):
	doc = build_invoice(ctx, data, notes)
	doc.insert()
	# validate() recomputed everything; the till total must still hold before posting
	check_grand_total(doc, data)
	doc.submit()
	return doc


def invoice_result_fields(doc):
	precision = money_precision()
	fawtara_status = doc.get("fawtara_status") if doc.meta.has_field("fawtara_status") else None
	return {
		"doctype": doc.doctype,
		"name": doc.name,
		"docstatus": cint(doc.docstatus),
		"modified": format_db_datetime(doc.modified),
		"totals": {
			"grand_total": format_money(doc.grand_total, precision),
			"rounded_total": format_money(_payable_total(doc), precision),
			"outstanding_amount": format_money(doc.outstanding_amount, precision),
		},
		"fawtara_status": fawtara_status or None,
	}
