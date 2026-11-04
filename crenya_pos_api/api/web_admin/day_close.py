"""A shop's Z reports on app.crenya.com: the tills' closed shifts (Crenya POS Shift) as the
restaurant's `DayCloses` list and `DayClose` report, for the shop's managers (`scope.manage`).

A shop has no business-day start: a shift's business date is the date it opened. Money is a
decimal string at the outlet currency's precision; every Z row has `kind` "money" or "count";
the Arabic labels come from `Z_LABELS` (the tax row says the company's own tax name).
"""

from decimal import Decimal

import frappe
from frappe.utils import cint, flt, formatdate, getdate

from crenya_pos_api import cash_difference
from crenya_pos_api.api.web_admin.scope import manage_company
from crenya_pos_api.expenses.reports import expense_label, shift_expenses
from crenya_pos_api.sync.context import money_precision
from crenya_pos_api.sync.control_log import shift_control_events, table_ready
from crenya_pos_api.sync.shift import count_shift_invoices
from crenya_pos_api.utils.decimal import as_decimal, format_money
from crenya_pos_api.web import shift_audit
from crenya_pos_api.web.context import require_web_session
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
	# selling on account and the payments received on it (retail P1)
	"on_account": ("On account", "بالآجل"),
	"on_account_sales_count": ("Sales on account", "مبيعات بالآجل"),
	"on_account_sales_amount": ("Sold on account", "المباع بالآجل"),
	"on_account_returned_amount": ("Returned to the account", "المرتجع إلى الحساب"),
	"on_account_received_count": ("Payments received", "الدفعات المستلمة"),
	"on_account_received_amount": ("Received on account", "المستلم على الحساب"),
	"received_mode": ("received", "مستلم"),
	# the shift's expenses, one row each
	"expenses": ("Expenses", "المصروفات"),
	"expenses_total": ("Total", "الإجمالي"),
}
# {event type: (English, Arabic)} of the till's store-control events on the Z (0.17.0); a type not
# listed shows as its own name
CONTROL_EVENT_LABELS = {
	"line_voided": ("Line removed", "حذف صنف"),
	"bill_cleared": ("Bill cleared", "مسح الفاتورة"),
	"tax_template_changed": ("Tax changed", "تغيير الضريبة"),
	"no_sale": ("Drawer opened (no sale)", "فتح الدرج بدون بيع"),
	"approval_granted": ("Manager approved", "موافقة المدير"),
	"approval_refused": ("Manager PIN refused", "رفض رمز المدير"),
	"approval_used": ("Approval used", "استخدام الموافقة"),
}
MAX_CONTROL_EVENTS = 2000
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


ON_ACCOUNT_ROWS = (
	("on_account_sales_count", "on_account_sales_count", KIND_COUNT),
	("on_account_sales_amount", "on_account_sales_amount", KIND_MONEY),
	("on_account_returned_amount", "on_account_returned_amount", KIND_MONEY),
	("on_account_received_count", "on_account_received_count", KIND_COUNT),
	("on_account_received_amount", "on_account_received_amount", KIND_MONEY),
)


def on_account_section(shift, precision):
	"""The ON ACCOUNT section of a shift (pure), or None when it sold nothing on account and received
	nothing: the counts and amounts the till sent, then each mode's receipts. A shift of a till
	without credit has none, so its Z is what it was."""
	payments = shift.get("payments") or []
	received = [p for p in payments if flt(p.get("received"))]
	if not received and not any(flt(shift.get(field)) for field, _key, _kind in ON_ACCOUNT_ROWS):
		return None
	rows = [_row(key, shift.get(field), kind, precision) for field, key, kind in ON_ACCOUNT_ROWS]
	for payment in received:
		mode = payment.get("mode_of_payment")
		en, ar = Z_LABELS["received_mode"]
		rows.append(
			_row(
				"received_mode",
				payment.get("received"),
				KIND_MONEY,
				precision,
				f"{mode} {en}",
				f"{ar} ({mode})",
			)
		)
	return _section("on_account", rows)


def expenses_section(expenses, precision):
	"""The EXPENSES section (pure) from `reports.shift_expenses` rows, one row each and their total,
	or None without expenses. The mode shows after the name of an expense not paid in cash."""
	if not expenses:
		return None
	rows = []
	total = Decimal(0)
	for expense in expenses:
		label = expense_label(expense) or Z_LABELS["expenses"][0]
		amount = as_decimal(expense["amount"])
		total += amount
		rows.append(
			{
				"label_en": label,
				"label_ar": label,
				"qty": None,
				"value": format_money(amount, precision),
				"kind": KIND_MONEY,
			}
		)
	rows.append(_row("expenses_total", total, KIND_MONEY, precision))
	return _section("expenses", rows)


def z_sections(shift, precision, tax_label_en, tax_label_ar=None, expenses=None) -> list[dict]:
	"""The Z report's sections of a shift document (or dict with its `payments` rows). ON ACCOUNT
	follows when the shift sold or received on account, and EXPENSES (the shift's `expenses`, from
	`reports.shift_expenses`) at the end."""

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
	sections = [
		_section("sales", rows(SALES_ROWS)),
		_section("payments", payment_rows),
		_section("returns", rows(RETURNS_ROWS)),
		_section("store_controls", rows(STORE_CONTROL_ROWS)),
		_section("paid_out", paid_out_rows),
	]
	for extra in (on_account_section(shift, precision), expenses_section(expenses, precision)):
		if extra:
			sections.append(extra)
	return sections


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
	"""The shop's closed and open shifts that opened between the dates, latest first. For a manager
	of the shop permitted on its company (`manage_company`, T12 F7)."""
	ctx = manage_company(outlet)
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


CONTROL_EVENT_KEYS = (
	"at",
	"event_type",
	"label",
	"label_ar",
	"actor_name",
	"approver_name",
	"amount",
	"item_code",
	"subject",
	"outside_shift",
)


def control_event_rows(events, names, precision) -> list[dict]:
	"""The Z's control events (pure) from `control_log.shift_control_events` rows: label in English
	and Arabic, the actor's and approver's names, the amount at `precision`. Each row keeps `action`
	and `approver` too, for matching discounts to their approval (the answer sends CONTROL_EVENT_KEYS)."""
	out = []
	for event in events:
		en, ar = CONTROL_EVENT_LABELS.get(event["event_type"], (event["event_type"], None))
		amount = event.get("amount")
		out.append(
			{
				"at": wire(event.get("at")),
				"event_type": event["event_type"],
				"label": en,
				"label_ar": ar,
				"actor_name": names.get(event.get("actor")) or event.get("actor") or None,
				"approver_name": names.get(event.get("approver")) or event.get("approver") or None,
				"amount": None if amount is None else format_money(as_decimal(amount), precision),
				"item_code": event.get("item_code") or None,
				"subject": event.get("subject") or None,
				"outside_shift": bool(event.get("outside_shift")),
				"action": event.get("action"),
				"approver": event.get("approver"),
			}
		)
	return out


def _shift_events(doc, precision):
	"""The shift's control events as `control_event_rows` (none on a site not migrated yet)."""
	if not table_ready():
		return [], False
	events, truncated = shift_control_events(doc.local_id, doc.device, MAX_CONTROL_EVENTS)
	names = full_names([e.get("actor") for e in events] + [e.get("approver") for e in events])
	return control_event_rows(events, names, precision), truncated


@frappe.whitelist(methods=["GET", "POST"])
def day_close(shift: str) -> dict:
	"""The Z report of one shift of a shop the user manages and whose company they are permitted on
	(`manage_company`, T12 F7: the Z carries discounts, refunds and control events); frappe.PermissionError
	for any other (or no) shift."""
	row = (
		frappe.db.get_value(SHIFT_DOCTYPE, shift, ["name", "pos_profile"], as_dict=True)
		if isinstance(shift, str) and shift
		else None
	)
	if not row:
		raise frappe.PermissionError
	ctx = manage_company(row.pos_profile)
	doc = frappe.get_doc(SHIFT_DOCTYPE, row.name)
	first, _label = _business_day(doc.opened_at, doc.closed_at)
	answer = common(ctx.company, ctx.currency)
	tax_ar = frappe.get_cached_value("Company", ctx.company, "crenya_tax_name_ar") or None
	cash_modes = set(frappe.get_all("Mode of Payment", filters={"type": "Cash"}, pluck="name"))
	precision = money_precision(ctx.currency)
	events, truncated = _shift_events(doc, precision)
	return {
		**answer,
		# 0.17.0: the till's control events, the invoices' discounts and refunds, the over/short
		"control_events": [{key: event[key] for key in CONTROL_EVENT_KEYS} for event in events],
		"control_events_truncated": truncated,
		"discounts": shift_audit.shift_discounts(doc.local_id, precision, events, doc.pos_profile),
		"refunds": shift_audit.shift_refunds(doc.local_id, precision, doc.pos_profile),
		"cash_difference": cash_difference.cash_difference_block(doc),
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
		"sections": z_sections(
			doc.as_dict(),
			money_precision(ctx.currency),
			answer["tax_label"],
			tax_ar,
			expenses=shift_expenses(doc, cash_modes),
		),
	}


@frappe.whitelist(methods=["POST"])
def post_cash_difference(shift: str) -> dict:
	"""Retry the cash over/short Journal Entry of a shift left "Not posted" (0.17.0), for a manager of
	its shop permitted on its company, who needs no accounts role: the entry is posted as
	Administrator after that check (`cash_difference.post_for_shift`, under the shift's lock, so two
	presses post one entry). Answers the Z's `cash_difference` block; frappe.PermissionError for any
	other (or no) shift."""
	require_web_session()
	row = (
		frappe.db.get_value(
			SHIFT_DOCTYPE, shift, ["name", "pos_profile", "cash_difference_state"], as_dict=True
		)
		if isinstance(shift, str) and shift
		else None
	)
	if not row:
		raise frappe.PermissionError
	ctx = manage_company(row.pos_profile)
	if row.cash_difference_state != cash_difference.NOT_POSTED:
		# nothing to retry: answered as it stands
		return cash_difference.cash_difference_block(frappe.get_doc(SHIFT_DOCTYPE, row.name))
	block = cash_difference.post_for_shift(row.name)
	ctx.log(frappe.get_doc(SHIFT_DOCTYPE, row.name))
	return block
