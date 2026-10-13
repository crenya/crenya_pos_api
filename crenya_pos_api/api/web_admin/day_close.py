"""A shop's Z reports on app.crenya.com: the tills' closed shifts (Crenya POS Shift) as the
restaurant's `DayCloses` list and `DayClose` report, for the shop's managers (`scope.manage`).

A shop has no business-day start: a shift's business date is the date it opened. Money is a
decimal string at the outlet currency's precision; every Z row has `kind` "money" or "count";
the Arabic labels come from `Z_LABELS` (the tax row says the company's own tax name).
"""

from decimal import Decimal

import frappe
from frappe.utils import cint, flt, formatdate, getdate

from crenya_pos_api.api.web_admin.scope import manage
from crenya_pos_api.sync.context import money_precision
from crenya_pos_api.sync.shift import count_shift_invoices
from crenya_pos_api.utils.decimal import as_decimal, format_money
from crenya_pos_api.web.devices import SHIFT_DOCTYPE, full_names, wire
from crenya_pos_api.web.reports import common, date_range

PAYMENT_DOCTYPE = "Crenya POS Shift Payment"
KIND_MONEY = "money"
KIND_COUNT = "count"
# {key: (English, Arabic)} of the Z report's section titles and row labels
Z_LABELS = {
	"sales": ("Sales", "المبيعات"),
	"payments": ("Payments", "المدفوعات"),
	"returns": ("Returns", "المرتجعات"),
	"store_controls": ("Store controls", "ضوابط المتجر"),
	"paid_out": ("Paid out", "المدفوع من الدرج"),
	"bills": ("Bills", "الفواتير"),
	"sales_total": ("Sales", "المبيعات"),
	"tax_total": ("Tax", "الضريبة"),
	"net_total": ("Net total", "الصافي"),
	"opening_float": ("Opening float", "الرصيد الافتتاحي"),
	"expected": ("expected", "المتوقع"),
	"counted": ("counted", "المعدود"),
	"difference": ("difference", "الفرق"),
	"returns_count": ("Returns", "المرتجعات"),
	"returns_total": ("Returns amount", "مبلغ المرتجعات"),
	"line_voids_count": ("Lines removed", "الأصناف المحذوفة"),
	"line_voids_amount": ("Lines removed amount", "مبلغ الأصناف المحذوفة"),
	"bill_clears_count": ("Bills cleared", "الفواتير الممسوحة"),
	"bill_clears_amount": ("Bills cleared amount", "مبلغ الفواتير الممسوحة"),
	"no_sales_count": ("Drawer opened (no sale)", "فتح الدرج بدون بيع"),
	"approvals_count": ("Manager approvals", "موافقات المدير"),
	"expense_count": ("Expenses", "المصروفات"),
	"paid_out_mode": ("paid out", "مدفوع"),
}
# the shift fields of each section: (row key, kind)
SALES_ROWS = (
	("sales_count", "bills", KIND_COUNT),
	("sales_total", "sales_total", KIND_MONEY),
	("tax_total", "tax_total", KIND_MONEY),
	("net_total", "net_total", KIND_MONEY),
)
RETURNS_ROWS = (
	("returns_count", "returns_count", KIND_COUNT),
	("returns_total", "returns_total", KIND_MONEY),
)
STORE_CONTROL_ROWS = (
	("line_voids_count", "line_voids_count", KIND_COUNT),
	("line_voids_amount", "line_voids_amount", KIND_MONEY),
	("bill_clears_count", "bill_clears_count", KIND_COUNT),
	("bill_clears_amount", "bill_clears_amount", KIND_MONEY),
	("no_sales_count", "no_sales_count", KIND_COUNT),
	("approvals_count", "approvals_count", KIND_COUNT),
)
SHIFT_FIELDS = (
	"name",
	"shift_number",
	"pos_profile",
	"company",
	"device",
	"cashier",
	"opened_at",
	"closed_at",
	"sales_count",
	"returns_count",
	"sales_total",
	"returns_total",
	"net_total",
)


def _row(key, value, kind, precision, label_en=None, label_ar=None):
	en, ar = Z_LABELS[key]
	return {
		"label_en": label_en or en,
		"label_ar": label_ar or ar,
		"qty": None,
		"value": format_money(flt(value), precision) if kind == KIND_MONEY else str(cint(value)),
		"kind": kind,
	}


def _section(key, rows):
	en, ar = Z_LABELS[key]
	return {"key": key, "title_en": en, "title_ar": ar, "rows": rows}


def z_sections(shift, precision, tax_label_en, tax_label_ar=None) -> list[dict]:
	"""The Z report's sections of a shift document (or dict with its `payments` rows)."""

	def rows(fields):
		return [
			_row(
				key,
				shift.get(field),
				kind,
				precision,
				*((tax_label_en, tax_label_ar) if key == "tax_total" else ()),
			)
			for field, key, kind in fields
		]

	payments = shift.get("payments") or []
	payment_rows = [_row("opening_float", shift.get("opening_float"), KIND_MONEY, precision)]
	for payment in payments:
		mode = payment.get("mode_of_payment")
		for key in ("expected", "counted", "difference"):
			en, ar = Z_LABELS[key]
			payment_rows.append(
				_row(key, payment.get(key), KIND_MONEY, precision, f"{mode} {en}", f"{ar} ({mode})")
			)
	paid_out_rows = [_row("expense_count", shift.get("expense_count"), KIND_COUNT, precision)]
	for payment in payments:
		if flt(payment.get("paid_out")):
			mode = payment.get("mode_of_payment")
			en, ar = Z_LABELS["paid_out_mode"]
			paid_out_rows.append(
				_row(
					"paid_out_mode",
					payment.get("paid_out"),
					KIND_MONEY,
					precision,
					f"{mode} {en}",
					f"{ar} ({mode})",
				)
			)
	return [
		_section("sales", rows(SALES_ROWS)),
		_section("payments", payment_rows),
		_section("returns", rows(RETURNS_ROWS)),
		_section("store_controls", rows(STORE_CONTROL_ROWS)),
		_section("paid_out", paid_out_rows),
	]


def _business_day(opened_at, closed_at) -> tuple:
	first = getdate(opened_at)
	last = getdate(closed_at) if closed_at else first
	label = formatdate(first) if last == first else f"{formatdate(first)} - {formatdate(last)}"
	return first, label


def _device_names(devices) -> dict:
	devices = sorted({device for device in devices if device})
	return dict(
		frappe.get_all(
			"Crenya POS Device",
			filters={"name": ["in", devices or [""]]},
			fields=["name", "device_name"],
			as_list=True,
		)
	)


def _payments(shifts) -> dict:
	grouped = {}
	for row in frappe.get_all(
		PAYMENT_DOCTYPE,
		filters={"parenttype": SHIFT_DOCTYPE, "parent": ["in", list(shifts) or [""]]},
		fields=["parent", "mode_of_payment", "expected", "counted", "difference", "paid_out"],
		order_by="idx asc",
	):
		grouped.setdefault(row.parent, []).append(row)
	return grouped


def _total(payments, field) -> Decimal:
	"""The sum of a payment field, in Decimal (no float drift before it is formatted)."""
	return sum((as_decimal(p.get(field)) for p in payments), Decimal(0))


def day_close_rows(shifts, payments, cash_modes, devices, names, precision) -> list[dict]:
	"""The list rows of `shifts` (pure but for date formatting), latest first: the restaurant's
	DayCloseRow; a shop has no service providers and sends no day close email."""
	out = []
	for shift in shifts:
		first, label = _business_day(shift["opened_at"], shift.get("closed_at"))
		rows = payments.get(shift["name"], [])
		cash = [p for p in rows if p.get("mode_of_payment") in cash_modes]
		card = [p for p in rows if p.get("mode_of_payment") not in cash_modes]
		out.append(
			{
				"business_date": first.isoformat(),
				"business_day": label,
				"shift": shift["name"],
				"shift_number": shift.get("shift_number"),
				"outlet": shift.get("pos_profile"),
				"device_name": devices.get(shift.get("device")) or shift.get("device"),
				"cashier": shift.get("cashier"),
				"cashier_name": names.get(shift.get("cashier")) or shift.get("cashier"),
				"opened_at": wire(shift.get("opened_at")),
				"closed_at": wire(shift.get("closed_at")),
				"bills": cint(shift.get("sales_count")),
				"sales": format_money(flt(shift.get("sales_total")), precision),
				"refunds_count": cint(shift.get("returns_count")),
				"refunds": format_money(flt(shift.get("returns_total")), precision),
				"net_total": format_money(flt(shift.get("net_total")), precision),
				"cash_expected": format_money(_total(cash, "expected"), precision),
				"cash_counted": format_money(_total(cash, "counted"), precision),
				"cash_difference": format_money(_total(cash, "difference"), precision),
				"card_difference": format_money(_total(card, "difference"), precision),
				"provider_sales": format_money(0, precision),
				"email_state": "",
			}
		)
	return sorted(out, key=lambda r: (r["business_date"], str(r["opened_at"])), reverse=True)


@frappe.whitelist(methods=["GET", "POST"])
def day_closes(from_date: str, to_date: str, outlet: str) -> dict:
	"""The shop's closed and open shifts that opened between the dates, latest first."""
	ctx = manage(outlet)
	first, last = date_range(from_date, to_date)
	shifts = frappe.get_all(
		SHIFT_DOCTYPE,
		filters={
			"pos_profile": ctx.outlet,
			"opened_at": ["between", [f"{first} 00:00:00", f"{last} 23:59:59.999999"]],
		},
		fields=list(SHIFT_FIELDS),
		order_by="opened_at desc",
	)
	cash_modes = set(frappe.get_all("Mode of Payment", filters={"type": "Cash"}, pluck="name"))
	return {
		**common(ctx.company, ctx.currency),
		"rows": day_close_rows(
			shifts,
			_payments(row.name for row in shifts),
			cash_modes,
			_device_names(row.device for row in shifts),
			full_names(row.cashier for row in shifts),
			money_precision(ctx.currency),
		),
	}


@frappe.whitelist(methods=["GET", "POST"])
def day_close(shift: str) -> dict:
	"""The Z report of one shift of a shop the user manages; frappe.PermissionError for any other
	(or no) shift."""
	row = (
		frappe.db.get_value(SHIFT_DOCTYPE, shift, ["name", "pos_profile"], as_dict=True)
		if isinstance(shift, str) and shift
		else None
	)
	if not row:
		raise frappe.PermissionError
	ctx = manage(row.pos_profile)
	doc = frappe.get_doc(SHIFT_DOCTYPE, row.name)
	first, _label = _business_day(doc.opened_at, doc.closed_at)
	answer = common(ctx.company, ctx.currency)
	tax_ar = frappe.get_cached_value("Company", ctx.company, "crenya_tax_name_ar") or None
	return {
		**answer,
		"shift": doc.name,
		"outlet": doc.pos_profile,
		"device_name": _device_names([doc.device]).get(doc.device) or doc.device,
		"shift_number": doc.shift_number,
		"business_day": first.isoformat(),
		"opened_at": wire(doc.opened_at),
		"closed_at": wire(doc.closed_at) if doc.closed_at else None,
		# bills the till counted that have not reached ERPNext yet, read now
		"missing_bills": max(
			0, cint(doc.sales_count) + cint(doc.returns_count) - count_shift_invoices(doc.local_id)
		),
		"sections": z_sections(doc.as_dict(), money_precision(ctx.currency), answer["tax_label"], tax_ar),
	}
