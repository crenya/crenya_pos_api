"""Tax and invoice wording of a company for the till's receipts (India, KSA, UAE, Oman, …).

Nothing country specific is hardcoded in the till: the bootstrap `company` block carries the tax
name, the tax ID label, the invoice / credit note titles and the receipt QR kind, from Company
custom fields with defaults read from the site (the POS Profile's tax template, field labels).
With India Compliance installed its `gstin` (Company, Customer) and `gst_hsn_code` (Item) fields
are used where ERPNext's own fields are empty or missing.
"""

import frappe

GSTIN_FIELD = "gstin"
TAX_CODE_FIELD = "gst_hsn_code"

DEFAULT_INVOICE_TITLE = "Tax Invoice"
DEFAULT_CREDIT_NOTE_TITLE = "Credit Note"

# Company.crenya_receipt_qr option -> protocol value
RECEIPT_QR_MODES = {
	"Verification link": "verify_link",
	"ZATCA (KSA)": "zatca_tlv",
	"None": "none",
}
DEFAULT_RECEIPT_QR = "verify_link"
RECEIPT_QR_OPTIONS = "\n".join(RECEIPT_QR_MODES)
DEFAULT_RECEIPT_QR_OPTION = "Verification link"

# protocol key -> Company custom field; `<key>_ar` reads `<field>_ar`
WORDING_FIELDS = {
	"tax_name": "crenya_tax_name",
	"tax_id_label": "crenya_tax_id_label",
	"invoice_title": "crenya_invoice_title",
	"credit_note_title": "crenya_credit_note_title",
}
RECEIPT_QR_FIELD = "crenya_receipt_qr"
COMPANY_WORDING_FIELDS = (
	*(field + suffix for field in WORDING_FIELDS.values() for suffix in ("", "_ar")),
	RECEIPT_QR_FIELD,
)


def _text(value):
	if value is None:
		return None
	return str(value).strip() or None


def tax_words(text):
	"""Leading words of a tax description or account up to the rate or the company abbreviation.

	"VAT 5% - CR" -> "VAT", "CGST @ 9.0" -> "CGST". Same rule as the till's `tax_words`.
	"""
	words = []
	for word in str(text or "").split():
		if word == "-" or any(char.isascii() and (char.isdigit() or char in "%@") for char in word):
			break
		words.append(word)
	return " ".join(words)


def default_tax_name(tax_rows):
	"""Name of a set of tax rows as configured in ERPNext: the first row's description without its
	rate, else its account head without the company abbreviation (then the next row); None when no
	row has one.
	"""
	for row in tax_rows or []:
		for value in (row.get("description"), row.get("account_head")):
			words = tax_words(value)
			if words:
				return words
	return None


def receipt_qr_mode(value):
	"""Protocol `receipt_qr` for a Company `crenya_receipt_qr` value (blank / unknown: verify link)."""
	return RECEIPT_QR_MODES.get(_text(value) or "", DEFAULT_RECEIPT_QR)


def field_label(meta, fieldname):
	"""Label of a field as the site shows it (property setters included); None when missing."""
	if meta is None or not meta.has_field(fieldname):
		return None
	return _text(meta.get_field(fieldname).label)


def tax_id_value(row, has_gstin):
	"""ERPNext `tax_id`, else India Compliance's `gstin` when the site has that field."""
	return _text(row.get("tax_id")) or (_text(row.get(GSTIN_FIELD)) if has_gstin else None)


def tax_id_label(company_meta):
	"""Label of the company tax ID: India Compliance's `gstin` field when present, else `tax_id`."""
	if company_meta is not None and company_meta.has_field(GSTIN_FIELD):
		return field_label(company_meta, GSTIN_FIELD)
	return field_label(company_meta, "tax_id")


def item_tax_code_field(item_meta):
	"""Item field holding the tax code (India Compliance's HSN/SAC), None when the site has none."""
	if item_meta is not None and item_meta.has_field(TAX_CODE_FIELD):
		return TAX_CODE_FIELD
	return None


def company_wording(row, company_meta, item_meta, tax_rows):
	"""Wording keys of the bootstrap `company` block (pure).

	`row`: Company values (custom fields only when the site has them); `tax_rows`: rows of the POS
	Profile's default Sales Taxes and Charges Template in idx order.
	"""
	defaults = {
		"tax_name": default_tax_name(tax_rows),
		"tax_id_label": tax_id_label(company_meta),
		"invoice_title": DEFAULT_INVOICE_TITLE,
		"credit_note_title": DEFAULT_CREDIT_NOTE_TITLE,
	}
	result = {"country": _text(row.get("country"))}
	for key, field in WORDING_FIELDS.items():
		result[key] = _text(row.get(field)) or defaults[key]
		result[f"{key}_ar"] = _text(row.get(f"{field}_ar"))
	result["receipt_qr"] = receipt_qr_mode(row.get(RECEIPT_QR_FIELD))
	code_field = item_tax_code_field(item_meta)
	result["tax_code_label"] = field_label(item_meta, code_field) if code_field else None
	has_gstin = company_meta is not None and company_meta.has_field(GSTIN_FIELD)
	result["tax_id"] = tax_id_value(row, has_gstin)
	return result


def company_wording_fields(company_meta):
	"""Company columns to read for `company_wording` that this site has."""
	fields = [field for field in COMPANY_WORDING_FIELDS if company_meta.has_field(field)]
	if company_meta.has_field(GSTIN_FIELD):
		fields.append(GSTIN_FIELD)
	return fields


def bootstrap_company_wording(row, tax_rows):
	return company_wording(row, frappe.get_meta("Company"), frappe.get_meta("Item"), tax_rows)
