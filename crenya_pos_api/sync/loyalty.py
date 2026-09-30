"""Loyalty points: balance lookup for the till and redemption on pushed Sales Invoices.

ERPNext stays the authority: the balance comes from its Loyalty Point
Entries, and the invoice controller checks the redeemed points against it on
insert (`validate_loyalty_points`) and books the redemption on submit.
"""

from decimal import ROUND_FLOOR

import frappe
from frappe.utils import flt, getdate, today

from crenya_pos_api.sync.context import get_total_tolerance, money_precision
from crenya_pos_api.sync.errors import VALIDATION, InvalidRequestError, SyncError, raise_api_error
from crenya_pos_api.utils.decimal import as_decimal, format_money, format_number, quantize

PROGRAM_FIELDS = ["name", "company", "conversion_factor", "expense_account", "from_date", "to_date"]


def _is_active(program, on_date):
	on_date = getdate(on_date)
	if program.from_date and getdate(program.from_date) > on_date:
		return False
	return not program.to_date or getdate(program.to_date) >= on_date


def loyalty_enabled(company):
	"""An active Loyalty Program exists for the company."""
	return any(
		_is_active(program, today())
		for program in frappe.get_all(
			"Loyalty Program", filters={"company": company}, fields=["from_date", "to_date"]
		)
	)


def customer_program(customer, company):
	"""The customer's Loyalty Program when it belongs to `company`, else None."""
	name = frappe.db.get_value("Customer", customer, "loyalty_program")
	if not name:
		return None
	program = frappe.db.get_value("Loyalty Program", name, PROGRAM_FIELDS, as_dict=True)
	if not program or program.company != company:
		return None
	return program


def max_redeemable_amount(points, conversion_factor, precision):
	"""points x conversion factor, rounded down to the currency precision (never negative)."""
	exponent = as_decimal(1).scaleb(-int(precision))
	value = as_decimal(max(int(points or 0), 0)) * as_decimal(conversion_factor or 0)
	return max(value.quantize(exponent, rounding=ROUND_FLOOR), as_decimal(0))


def get_details(ctx, customer):
	from erpnext.accounts.doctype.loyalty_program.loyalty_program import (
		get_loyalty_program_details_with_points,
	)

	if not customer or not isinstance(customer, str) or not frappe.db.exists("Customer", customer):
		raise_api_error(InvalidRequestError, f"Customer {customer!r} does not exist")

	precision = money_precision()
	result = {
		"customer": customer,
		"loyalty_program": None,
		"loyalty_points": 0,
		"conversion_factor": "0",
		"max_redeemable_amount": format_money(0, precision),
		"currency": ctx.currency,
	}
	program = customer_program(customer, ctx.company)
	if not program:
		return result

	details = get_loyalty_program_details_with_points(
		customer, loyalty_program=program.name, company=ctx.company
	)
	points = max(int(flt(details.get("loyalty_points"))), 0)
	conversion_factor = details.get("conversion_factor") or 0
	result.update(
		{
			"loyalty_program": program.name,
			"loyalty_points": points,
			"conversion_factor": format_number(conversion_factor),
			"max_redeemable_amount": format_money(
				max_redeemable_amount(points, conversion_factor, precision), precision
			),
		}
	)
	return result


def check_redemption(loyalty, conversion_factor, payable, tolerance, precision):
	"""Pure checks of a redemption against the program and the invoice (Decimals in, raises SyncError).

	The amount may be at most points x conversion factor and at most `tolerance`
	below it (tills round the value down), and may not exceed the payable total.
	"""
	value = as_decimal(loyalty["points"]) * as_decimal(conversion_factor or 0)
	amount = loyalty["amount"]
	if not value:
		raise SyncError(VALIDATION, "Loyalty Program has no conversion factor")
	if amount > value or value - amount > as_decimal(tolerance):
		raise SyncError(
			VALIDATION,
			f"Loyalty amount {format_money(amount, precision)} does not match {loyalty['points']} points "
			f"x {format_number(conversion_factor)} = {format_money(value, precision)}",
		)
	if amount > quantize(payable, precision):
		raise SyncError(
			VALIDATION,
			f"Loyalty amount {format_money(amount, precision)} is more than the invoice total "
			f"{format_money(payable, precision)}",
		)


def apply_redemption(doc, ctx, loyalty, payable):
	"""Set the redemption fields; ERPNext validates the points balance when the invoice is inserted."""
	if doc.customer == ctx.profile.customer:
		raise SyncError(
			VALIDATION, "Loyalty points cannot be redeemed for the POS Profile's default customer"
		)
	program = customer_program(doc.customer, ctx.company)
	if not program:
		raise SyncError(VALIDATION, "Customer has no loyalty program")
	if not _is_active(program, doc.posting_date):
		raise SyncError(VALIDATION, f"Loyalty Program {program.name} is not active on {doc.posting_date}")
	if not program.expense_account:
		raise SyncError(VALIDATION, f"Loyalty Program {program.name} has no expense account")

	precision = money_precision(doc.currency)
	check_redemption(loyalty, program.conversion_factor, payable, get_total_tolerance(precision), precision)
	doc.update(
		{
			"loyalty_program": program.name,
			"redeem_loyalty_points": 1,
			"loyalty_points": loyalty["points"],
			"loyalty_amount": flt(f"{loyalty['amount']:f}", precision),
		}
	)
