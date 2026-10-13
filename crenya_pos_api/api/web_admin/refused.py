"""A shop's refused sales on app.crenya.com: what its tills pushed and ERPNext refused (a Crenya
Sync Event with status `error`), with a hint in the reader's language, and Retry, which asks the
till to send the sale again at its next sync (`sync/retry.py`). For the shop's managers
(`scope.manage`).

The hint is chosen by the event's error code and, for a validation error, by what the message
names (stock, a price list, an account): ERPNext raises all of those as validation errors.
"""

import re

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from crenya_pos_api.api.web_admin.scope import manage
from crenya_pos_api.sync.context import DEVICE_DOCTYPE, EVENT_DOCTYPE
from crenya_pos_api.sync.errors import (
	DEPENDENCY_MISSING,
	PAYLOAD_CONFLICT,
	PERMISSION,
	TOTAL_MISMATCH,
	UNSUPPORTED_TAX,
	VALIDATION,
)
from crenya_pos_api.sync.events import STATUS_ERROR
from crenya_pos_api.sync.retry import RETRY_FIELD
from crenya_pos_api.utils.dates import format_db_datetime
from crenya_pos_api.web.devices import wire

MAX_REFUSED = 200
# {hint key: (English, Arabic)}: what the owner can do about a refused sale
HINTS = {
	"stock": (
		"Not enough stock in the outlet's warehouse. Receive the stock, then retry.",
		"لا يوجد مخزون كافٍ في مستودع المتجر. استلم المخزون ثم أعد المحاولة.",
	),
	"price_list": (
		"A price list of the sale is missing or not in the outlet's currency. Fix the outlet's price list, then retry.",
		"قائمة أسعار البيع مفقودة أو بعملة أخرى. صحّح قائمة أسعار المتجر ثم أعد المحاولة.",
	),
	"account": (
		"An account is missing for the company (a payment method, write-off or tax account). Add it, then retry.",
		"حساب مفقود للشركة (طريقة دفع أو شطب أو ضريبة). أضفه ثم أعد المحاولة.",
	),
	PERMISSION: (
		"The cashier or till may not do this. Check the cashier's privilege and outlets in People, then retry.",
		"لا يُسمح للكاشير أو الجهاز بذلك. تحقق من صلاحية الكاشير ومتاجره في الأشخاص ثم أعد المحاولة.",
	),
	TOTAL_MISMATCH: (
		"The till's total differs from ERPNext's (a price, tax or rounding setting changed). Check the outlet's settings, then retry.",
		"إجمالي الجهاز يختلف عن إجمالي النظام (تغيّر سعر أو ضريبة أو تقريب). راجع إعدادات المتجر ثم أعد المحاولة.",
	),
	DEPENDENCY_MISSING: (
		"Something the sale needs has not reached the server yet (a customer or the original bill). The till retries by itself.",
		"شيء تحتاجه العملية لم يصل إلى الخادم بعد (عميل أو الفاتورة الأصلية). سيعيد الجهاز المحاولة تلقائياً.",
	),
	UNSUPPORTED_TAX: (
		"The outlet's tax template has a tax a till cannot compute. Choose a template of 'On Net Total' taxes, then retry.",
		"قالب الضريبة يحتوي ضريبة لا يحسبها الجهاز. اختر قالباً بضرائب 'على صافي الإجمالي' ثم أعد المحاولة.",
	),
	PAYLOAD_CONFLICT: (
		"The till sent this sale twice with different contents. Contact support before retrying.",
		"أرسل الجهاز هذه العملية مرتين بمحتوى مختلف. تواصل مع الدعم قبل إعادة المحاولة.",
	),
	"generic": (
		"Read the reason below, fix it, then retry.",
		"اقرأ السبب أدناه وصحّحه ثم أعد المحاولة.",
	),
}
# what a validation message names: (hint key, pattern), the first match wins
VALIDATION_HINTS = (
	("stock", re.compile(r"stock|warehouse|qty|quantity", re.I)),
	("price_list", re.compile(r"price list", re.I)),
	("account", re.compile(r"account", re.I)),
)


def hint_key(error_code, error_message) -> str:
	"""The HINTS key of a refused event (pure)."""
	if error_code == VALIDATION:
		for key, pattern in VALIDATION_HINTS:
			if pattern.search(error_message or ""):
				return key
		return "generic"
	return error_code if error_code in HINTS else "generic"


def _refused(ctx) -> dict:
	devices = dict(
		frappe.get_all(
			DEVICE_DOCTYPE,
			filters={"pos_profile": ctx.outlet},
			fields=["name", "device_name"],
			as_list=True,
		)
	)
	rows = frappe.get_all(
		EVENT_DOCTYPE,
		filters={"status": STATUS_ERROR, "device": ["in", list(devices) or [""]]},
		fields=[
			"name",
			"device",
			"aggregate_type",
			"local_id",
			"received_at",
			"attempts",
			"error_code",
			"error_message",
			RETRY_FIELD,
		],
		order_by="received_at desc",
		limit=MAX_REFUSED,
	)
	out = []
	for row in rows:
		hint, hint_ar = HINTS[hint_key(row.error_code, row.error_message)]
		out.append(
			{
				"name": row.name,
				"device_name": devices.get(row.device) or row.device,
				"aggregate_type": row.aggregate_type,
				"local_id": row.local_id,
				"received_at": wire(row.received_at),
				"attempts": cint(row.attempts),
				"error_code": row.error_code,
				"error_message": row.error_message,
				"hint": hint,
				"hint_ar": hint_ar,
				"retry_requested_at": wire(row.get(RETRY_FIELD)),
			}
		)
	return {"outlet": ctx.outlet, "rows": out}


@frappe.whitelist(methods=["GET", "POST"])
def refused_sales(outlet: str) -> dict:
	"""The sales the shop's tills pushed that are still refused, newest first (at most
	MAX_REFUSED)."""
	return _refused(manage(outlet))


@frappe.whitelist(methods=["POST"])
def retry_sale(name: str) -> dict:
	"""Ask the till that pushed refused event `name` to send it again at its next sync (it pulls
	`sync_retry`); answers `refused_sales` of its shop. An event of a shop the user does not
	manage is frappe.PermissionError; one no longer refused is a validation error."""
	row = (
		frappe.db.get_value(EVENT_DOCTYPE, name, ["name", "device", "status"], as_dict=True)
		if isinstance(name, str) and name
		else None
	)
	outlet = frappe.db.get_value(DEVICE_DOCTYPE, row.device, "pos_profile") if row and row.device else None
	if not outlet:
		raise frappe.PermissionError
	ctx = manage(outlet)
	if row.status != STATUS_ERROR:
		frappe.throw(_("This sale is no longer refused"), frappe.ValidationError)
	doc = frappe.get_doc(EVENT_DOCTYPE, row.name)
	doc.set(RETRY_FIELD, format_db_datetime(now_datetime()))
	doc.save(ignore_permissions=True)
	ctx.log(doc)
	return _refused(ctx)
