"""Fawtara (Oman e-invoicing) status for tills and the public receipt verification page.

E-invoicing itself lives in the `oman_compliance` app. Its fields are read only
when that app is installed and only if they exist, so everything here also works
(with null statuses) on sites without e-invoicing.
"""

import re

import frappe
from frappe.utils import cint

from crenya_pos_api.sync.context import money_precision
from crenya_pos_api.sync.errors import InvalidRequestError, raise_api_error
from crenya_pos_api.utils.dates import format_date
from crenya_pos_api.utils.decimal import format_money

COMPLIANCE_APP = "oman_compliance"
RECORD_DOCTYPE = "Fawtara Sales Invoice Additional Fields"
MAX_STATUS_IDS = 50
MAX_LOCAL_ID_LENGTH = 140
FINAL_STATUSES = ("Accepted", "Accepted with warnings", "Rejected")

VERIFY_RATE_LIMIT = 60
VERIFY_RATE_WINDOW_SECONDS = 60

_LOCAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,139}$")

STATUS_LABELS_AR = {
	"Ready": "جاهزة للإرسال",
	"Submitted": "مُرسلة",
	"Accepted": "مقبولة",
	"Accepted with warnings": "مقبولة مع ملاحظات",
	"Rejected": "مرفوضة",
	"Resend": "بانتظار إعادة الإرسال",
	"Failed": "تعذر الإرسال",
}


def compliance_installed():
	return COMPLIANCE_APP in frappe.get_installed_apps()


def _invoice_fawtara_fields():
	"""Fawtara columns of Sales Invoice on this site (none without oman_compliance)."""
	if not compliance_installed():
		return []
	meta = frappe.get_meta("Sales Invoice")
	return [
		field
		for field in ("fawtara_status", "fawtara_document_id", "fawtara_record")
		if meta.has_field(field)
	]


def _record_details(names):
	"""(integration_status, document_id) of Fawtara records by name."""
	names = [name for name in set(names) if name]
	if not names or not frappe.db.exists("DocType", RECORD_DOCTYPE):
		return {}
	meta = frappe.get_meta(RECORD_DOCTYPE)
	fields = ["name"] + [field for field in ("integration_status", "document_id") if meta.has_field(field)]
	rows = frappe.get_all(RECORD_DOCTYPE, filters={"name": ["in", names]}, fields=fields)
	return {row.name: row for row in rows}


def fawtara_details(rows):
	"""{invoice name: {"fawtara_status", "document_id"}} for Sales Invoice rows read with
	`_invoice_fawtara_fields()`; values are None when e-invoicing is not installed or not
	done yet. The invoice's own fields win; the linked Fawtara record fills the gaps."""
	records = _record_details(
		row.get("fawtara_record")
		for row in rows
		if row.get("fawtara_record") and not (row.get("fawtara_status") and row.get("fawtara_document_id"))
	)
	details = {}
	for row in rows:
		record = records.get(row.get("fawtara_record")) or frappe._dict()
		details[row.name] = {
			"fawtara_status": row.get("fawtara_status") or record.get("integration_status") or None,
			"document_id": row.get("fawtara_document_id") or record.get("document_id") or None,
		}
	return details


def _clean_local_ids(local_ids):
	if isinstance(local_ids, str):
		try:
			local_ids = frappe.parse_json(local_ids)
		except ValueError:
			raise_api_error(InvalidRequestError, "local_ids must be a JSON list")
	if not isinstance(local_ids, list):
		raise_api_error(InvalidRequestError, "local_ids must be a list")
	if len(local_ids) > MAX_STATUS_IDS:
		raise_api_error(InvalidRequestError, f"At most {MAX_STATUS_IDS} local_ids per call")
	cleaned = []
	for index, local_id in enumerate(local_ids):
		if not isinstance(local_id, str) or not local_id.strip():
			raise_api_error(InvalidRequestError, f"local_ids[{index}] must be a non-empty string")
		local_id = local_id.strip()
		if len(local_id) > MAX_LOCAL_ID_LENGTH:
			raise_api_error(
				InvalidRequestError, f"local_ids[{index}] is longer than {MAX_LOCAL_ID_LENGTH} characters"
			)
		cleaned.append(local_id)
	return list(dict.fromkeys(cleaned))


def get_status(ctx, local_ids):
	"""Fawtara status of the device company's submitted invoices with these till local ids.

	Returns one entry per invoice found, in request order; unknown ids are left out.
	"""
	local_ids = _clean_local_ids(local_ids)
	if not local_ids:
		return []
	fields = ["name", "crenya_local_id", *_invoice_fawtara_fields()]
	rows = frappe.get_all(
		"Sales Invoice",
		filters={"crenya_local_id": ["in", local_ids], "company": ctx.company, "docstatus": 1},
		fields=fields,
	)
	details = fawtara_details(rows)
	by_local_id = {row.crenya_local_id: row for row in rows}
	result = []
	for local_id in local_ids:
		row = by_local_id.get(local_id)
		if row:
			result.append({"local_id": local_id, "name": row.name, **details[row.name]})
	return result


# public verification page


def verify_rate_limited():
	"""Simple per-IP counter for the guest verification page (requests without an IP pass)."""
	ip = getattr(frappe.local, "request_ip", None)
	if not ip:
		return False
	limit = cint(frappe.conf.get("crenya_pos_verify_rate_limit")) or VERIFY_RATE_LIMIT
	# site-prefixed key; atomic increment, the first request of a window starts its expiry
	key = frappe.cache.make_key(f"crenya_pos_verify:{ip}")
	count = frappe.cache.incrby(key, 1)
	if count == 1 or frappe.cache.ttl(key) < 0:
		frappe.cache.expire(key, VERIFY_RATE_WINDOW_SECONDS)
	return count > limit


def is_valid_local_id(local_id):
	return isinstance(local_id, str) and bool(_LOCAL_ID_RE.match(local_id))


def _format_time(value):
	if value in (None, ""):
		return None
	text = str(value)
	hours, _sep, rest = text.partition(":")
	return f"{int(hours):02d}:{rest[:5]}" if rest else text


def verification_facts(local_id):
	"""Seller-side facts of a submitted till invoice for the public page, or None.

	Deliberately no customer data: the page is reachable by anyone holding the receipt.
	"""
	if not is_valid_local_id(local_id):
		return None
	fields = [
		"name",
		"company",
		"company_tax_id",
		"posting_date",
		"posting_time",
		"currency",
		"grand_total",
		"total_taxes_and_charges",
		"is_return",
		"crenya_offline_number",
		*_invoice_fawtara_fields(),
	]
	meta = frappe.get_meta("Sales Invoice")
	fields = [field for field in fields if field == "name" or meta.has_field(field)]
	invoice = frappe.db.get_value(
		"Sales Invoice", {"crenya_local_id": local_id, "docstatus": 1}, fields, as_dict=True
	)
	if not invoice:
		return None

	company_meta = frappe.get_meta("Company")
	company_fields = ["company_name", "tax_id"] + [
		field for field in ("crenya_company_name_ar", "crenya_cr_number") if company_meta.has_field(field)
	]
	company = frappe.db.get_value("Company", invoice.company, company_fields, as_dict=True) or frappe._dict()
	precision = money_precision()
	status = fawtara_details([invoice])[invoice.name] if compliance_installed() else None

	return {
		"seller_name": company.get("company_name") or invoice.company,
		"seller_name_ar": company.get("crenya_company_name_ar") or None,
		"seller_vatin": invoice.get("company_tax_id") or company.get("tax_id") or None,
		"cr_number": company.get("crenya_cr_number") or None,
		"invoice_number": invoice.name,
		"offline_number": invoice.get("crenya_offline_number") or None,
		"posting_date": format_date(invoice.posting_date),
		"posting_time": _format_time(invoice.posting_time),
		"is_credit_note": bool(cint(invoice.is_return)),
		"currency": invoice.currency,
		"grand_total": format_money(invoice.grand_total, precision),
		"vat_total": format_money(invoice.total_taxes_and_charges, precision),
		"einvoicing": status is not None,
		"fawtara_status": status["fawtara_status"] if status else None,
		"fawtara_status_ar": STATUS_LABELS_AR.get(status["fawtara_status"]) if status else None,
		"document_id": status["document_id"] if status else None,
	}
