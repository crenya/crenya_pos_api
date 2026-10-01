"""Returns without an invoice: a till credit note that names no original Sales Invoice.

ERPNext books it as a standalone credit note (`is_return = 1`, no `return_against`).
Only POS Profiles with `crenya_allow_return_without_invoice` accept them.

Stock comes back at ERPNext's own valuation. ERPNext v15 up to 15.119 computed
the incoming rate of a standalone credit note itself (`get_incoming_rate`); from
15.120 (and in v16) it no longer does for FIFO / LIFO items and falls back to the
row's selling rate. The server therefore sets each row's `incoming_rate` from
`get_incoming_rate` before insert: the batch's average rate in the warehouse for a
row with a batch (the warehouse valuation when the batch holds nothing), else the
FIFO / LIFO / Moving Average rate of the warehouse, else the Item's valuation
rate. A zero result is left to ERPNext. ERPNext's own rules still apply on top
(Selling Settings "Set Incoming Rate as Zero for Expired Batch"; v15 re-values
Moving Average items itself).
"""

import frappe
from frappe.utils import cint, flt

from crenya_pos_api.sync.errors import VALIDATION, SyncError

ALLOW_FIELD = "crenya_allow_return_without_invoice"


def allows_return_without_invoice(profile):
	return bool(cint(profile.get(ALLOW_FIELD)))


def assert_allowed(profile):
	if not allows_return_without_invoice(profile):
		raise SyncError(
			VALIDATION,
			f"POS Profile {profile.name} does not allow returns without an invoice; "
			"return against the original invoice",
		)


def _rate_function():
	"""ERPNext's valuation lookup as its controllers call it: v16 moved it to `_get_incoming_rate`
	(the public `get_incoming_rate` became a whitelisted wrapper with permission checks)."""
	from erpnext.stock import utils

	return getattr(utils, "_get_incoming_rate", None) or utils.get_incoming_rate


def _incoming_rate(doc, row, batch_no):
	return flt(
		_rate_function()(
			{
				"item_code": row.item_code,
				"warehouse": row.warehouse,
				"posting_date": doc.posting_date,
				"posting_time": doc.posting_time,
				# negative on a return, as ERPNext passes it
				"qty": flt(row.qty) * flt(row.conversion_factor or 1),
				"company": doc.company,
				"voucher_type": doc.doctype,
				"voucher_no": doc.name or "",
				"batch_no": batch_no,
			},
			raise_error_if_no_rate=False,
		)
	)


def set_valuation_incoming_rates(doc):
	"""Incoming rate (per stock unit) of every stock row of a credit note without invoice."""
	if not cint(doc.update_stock):
		return
	precision = frappe.get_precision("Sales Invoice Item", "incoming_rate")
	for row in doc.get("items") or []:
		if not cint(frappe.get_cached_value("Item", row.item_code, "is_stock_item")):
			continue
		rate = _incoming_rate(doc, row, row.get("batch_no") or None)
		if not rate and row.get("batch_no"):
			# a batch with no stock left in the warehouse has no average of its own
			rate = _incoming_rate(doc, row, None)
		if rate:
			row.incoming_rate = flt(rate, precision)
