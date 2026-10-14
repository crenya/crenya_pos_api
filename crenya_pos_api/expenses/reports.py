"""The expenses of a shift as the Z report lists them (moved down from crenya_restaurant's
day_close, 0.13.0): the rows and their label."""

import frappe
from frappe.utils import flt

from crenya_pos_api.expenses import booking


def expense_label(row):
	"""An expense's Z row: "{supplier or payee} · {bill no} · {categories}", plus its mode when it is
	not cash (pure)."""
	label = " · ".join(part for part in (row.get("payee"), row.get("bill_no"), row.get("categories")) if part)
	return label if row.get("is_cash") else f"{label} ({row['mode_of_payment']})"


def shift_expenses(shift, cash_modes):
	"""The till expenses of a shift not deleted (Cancelled, or a till delete still to finish), in
	the order they were made: [{payee, bill_no, categories, mode_of_payment, is_cash, amount}];
	none on a site without POS Expense."""
	if not booking.expenses_supported():
		return []
	rows = frappe.get_all(
		booking.EXPENSE,
		filters={
			"shift": shift.local_id,
			"outlet": shift.pos_profile,
			"local_id": ["is", "set"],
			"status": ["!=", booking.CANCELLED],
		},
		fields=["name", "supplier", "payee_name", "bill_no", "mode_of_payment", "amount"],
		order_by="creation asc, name asc",
	)
	if not rows:
		return []
	# a delete the server could not finish leaves the row `Not booked`; the till counted it out at
	# the delete, so the Z does too
	deleted = booking.deleted_at_till(r.name for r in rows)
	rows = [r for r in rows if r.name not in deleted]
	if not rows:
		return []
	categories = {}
	for line in frappe.get_all(
		"POS Expense Line",
		filters={"parent": ["in", [r.name for r in rows]], "parenttype": booking.EXPENSE},
		fields=["parent", "category"],
		order_by="idx asc",
	):
		names = categories.setdefault(line.parent, [])
		if line.category not in names:
			names.append(line.category)
	suppliers = dict(
		frappe.get_all(
			"Supplier",
			filters={"name": ["in", [r.supplier for r in rows if r.supplier] or [""]]},
			fields=["name", "supplier_name"],
			as_list=True,
		)
	)
	return [
		{
			"payee": r.payee_name or suppliers.get(r.supplier) or r.supplier,
			"bill_no": r.bill_no,
			"categories": ", ".join(categories.get(r.name, [])),
			"mode_of_payment": r.mode_of_payment,
			"is_cash": r.mode_of_payment in cash_modes,
			"amount": flt(r.amount),
		}
		for r in rows
	]
