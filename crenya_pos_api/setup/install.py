"""Role and custom fields required by the sync protocol. Idempotent: runs after install and every migrate."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from crenya_pos_api.sync.tax_wording import (
	DEFAULT_CREDIT_NOTE_TITLE,
	DEFAULT_INVOICE_TITLE,
	DEFAULT_RECEIPT_QR_OPTION,
	RECEIPT_QR_OPTIONS,
)

POS_USER_ROLE = "Crenya POS User"

CUSTOM_FIELDS = {
	"Sales Invoice": [
		{
			"fieldname": "crenya_local_id",
			"fieldtype": "Data",
			"label": "Crenya Local ID",
			"insert_after": "pos_profile",
			"unique": 1,
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "crenya_offline_number",
			"fieldtype": "Data",
			"label": "Offline Number",
			"insert_after": "crenya_local_id",
			"read_only": 1,
			"no_copy": 1,
			"in_standard_filter": 1,
			"search_index": 1,
		},
		{
			"fieldname": "crenya_device",
			"fieldtype": "Link",
			"label": "POS Device",
			"options": "Crenya POS Device",
			"insert_after": "crenya_offline_number",
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "crenya_shift_id",
			"fieldtype": "Data",
			"label": "POS Shift ID",
			"insert_after": "crenya_device",
			"read_only": 1,
			"no_copy": 1,
			"in_standard_filter": 1,
			"search_index": 1,
			"print_hide": 1,
		},
		{
			"fieldname": "crenya_cashier",
			"fieldtype": "Link",
			"label": "POS Cashier",
			"options": "User",
			"insert_after": "crenya_shift_id",
			"read_only": 1,
			"no_copy": 1,
			"in_standard_filter": 1,
			"print_hide": 1,
		},
	],
	"User": [
		# own section on the Roles & Permissions tab, right after the roles (where
		# the Crenya POS User role is granted)
		{
			"fieldname": "crenya_pos_section",
			"fieldtype": "Section Break",
			"label": "Crenya POS",
			"insert_after": "roles",
		},
		{
			"fieldname": "crenya_pos_pin",
			"fieldtype": "Password",
			"label": "POS PIN",
			"description": "4–6 digits. Used to unlock Crenya POS tills.",  # noqa: RUF001
			"insert_after": "crenya_pos_section",
			"no_copy": 1,
		},
		{
			"fieldname": "crenya_pos_pin_hash",
			"fieldtype": "Data",
			"label": "POS PIN Hash",
			"insert_after": "crenya_pos_pin",
			"hidden": 1,
			"read_only": 1,
			"no_copy": 1,
		},
	],
	"Customer": [
		{
			"fieldname": "crenya_local_id",
			"fieldtype": "Data",
			"label": "Crenya Local ID",
			"insert_after": "tax_id",
			"unique": 1,
			"read_only": 1,
			"no_copy": 1,
		},
	],
	"Item": [
		{
			"fieldname": "crenya_item_name_ar",
			"fieldtype": "Data",
			"label": "Item Name (Arabic)",
			"insert_after": "item_name",
			"translatable": 0,
		},
	],
	"POS Profile": [
		{
			"fieldname": "crenya_allow_return_without_invoice",
			"fieldtype": "Check",
			"label": "Allow returns without invoice (Crenya POS)",
			"description": "Crenya POS tills may refund items without the original invoice "
			"(a credit note with a reason; batch tracked items name their batch).",
			"default": "0",
			"insert_after": "allow_discount_change",
		},
		# own collapsible section after the Filters section (customer_groups is its last field)
		{
			"fieldname": "crenya_scale_section",
			"fieldtype": "Section Break",
			"label": "Crenya POS Scale Barcodes",
			"collapsible": 1,
			"insert_after": "customer_groups",
		},
		{
			"fieldname": "crenya_scale_barcode_rules",
			"fieldtype": "Table",
			"label": "Scale Barcode Rules",
			"options": "Crenya POS Scale Barcode Rule",
			"description": "EAN-13 labels printed by scales: prefix, PLU, then the weight or price. "
			"When rules are set they replace the scale settings of the tills.",
			"insert_after": "crenya_scale_section",
		},
	],
	"Company": [
		{
			"fieldname": "crenya_company_name_ar",
			"fieldtype": "Data",
			"label": "Company Name (Arabic)",
			"insert_after": "company_name",
		},
		{
			"fieldname": "crenya_cr_number",
			"fieldtype": "Data",
			"label": "CR Number",
			"insert_after": "tax_id",
		},
		# receipt wording, own collapsible section at the end of the address & contact section
		# (address_html is its last field in ERPNext v15 and v16)
		{
			"fieldname": "crenya_pos_section",
			"fieldtype": "Section Break",
			"label": "Crenya POS",
			"collapsible": 1,
			"insert_after": "address_html",
		},
		{
			"fieldname": "crenya_tax_name",
			"fieldtype": "Data",
			"label": "Tax Name",
			"description": "Name of the tax on receipts (for example VAT or GST). "
			"Blank: taken from the POS Profile's taxes template.",
			"insert_after": "crenya_pos_section",
			"translatable": 0,
		},
		{
			"fieldname": "crenya_tax_id_label",
			"fieldtype": "Data",
			"label": "Tax ID Label",
			"description": "Label of the company tax ID on receipts (for example VATIN, TRN or GSTIN). "
			"Blank: the label of the Tax ID field.",
			"insert_after": "crenya_tax_name",
			"translatable": 0,
		},
		{
			"fieldname": "crenya_invoice_title",
			"fieldtype": "Data",
			"label": "Invoice Title",
			"description": f"Title of full (A4) sales invoices. Blank: {DEFAULT_INVOICE_TITLE}.",
			"insert_after": "crenya_tax_id_label",
			"translatable": 0,
		},
		{
			"fieldname": "crenya_credit_note_title",
			"fieldtype": "Data",
			"label": "Credit Note Title",
			"description": f"Title of full (A4) credit notes. Blank: {DEFAULT_CREDIT_NOTE_TITLE}.",
			"insert_after": "crenya_invoice_title",
			"translatable": 0,
		},
		{
			"fieldname": "crenya_receipt_title",
			"fieldtype": "Data",
			"label": "Receipt Title",
			"description": "Title of thermal sales receipts (for example Simplified Tax Invoice). "
			"Blank: the Invoice Title.",
			"insert_after": "crenya_credit_note_title",
			"translatable": 0,
		},
		{
			"fieldname": "crenya_receipt_credit_note_title",
			"fieldtype": "Data",
			"label": "Receipt Credit Note Title",
			"description": "Title of thermal return receipts. Blank: the Credit Note Title.",
			"insert_after": "crenya_receipt_title",
			"translatable": 0,
		},
		{
			"fieldname": "crenya_receipt_qr",
			"fieldtype": "Select",
			"label": "Receipt QR Code",
			"options": RECEIPT_QR_OPTIONS,
			"default": DEFAULT_RECEIPT_QR_OPTION,
			"description": "Verification link: the invoice's public verification page. "
			"ZATCA (KSA): the phase 1 QR code (seller, VAT number, time, totals). None: no QR code.",
			"insert_after": "crenya_receipt_credit_note_title",
		},
		{
			"fieldname": "crenya_pos_column_break",
			"fieldtype": "Column Break",
			"insert_after": "crenya_receipt_qr",
		},
		{
			"fieldname": "crenya_tax_name_ar",
			"fieldtype": "Data",
			"label": "Tax Name (Arabic)",
			"insert_after": "crenya_pos_column_break",
			"translatable": 0,
		},
		{
			"fieldname": "crenya_tax_id_label_ar",
			"fieldtype": "Data",
			"label": "Tax ID Label (Arabic)",
			"insert_after": "crenya_tax_name_ar",
			"translatable": 0,
		},
		{
			"fieldname": "crenya_invoice_title_ar",
			"fieldtype": "Data",
			"label": "Invoice Title (Arabic)",
			"insert_after": "crenya_tax_id_label_ar",
			"translatable": 0,
		},
		{
			"fieldname": "crenya_credit_note_title_ar",
			"fieldtype": "Data",
			"label": "Credit Note Title (Arabic)",
			"insert_after": "crenya_invoice_title_ar",
			"translatable": 0,
		},
		{
			"fieldname": "crenya_receipt_title_ar",
			"fieldtype": "Data",
			"label": "Receipt Title (Arabic)",
			"description": "Blank: the Invoice Title (Arabic).",
			"insert_after": "crenya_credit_note_title_ar",
			"translatable": 0,
		},
		{
			"fieldname": "crenya_receipt_credit_note_title_ar",
			"fieldtype": "Data",
			"label": "Receipt Credit Note Title (Arabic)",
			"description": "Blank: the Credit Note Title (Arabic).",
			"insert_after": "crenya_receipt_title_ar",
			"translatable": 0,
		},
	],
}


def make_role():
	if frappe.db.exists("Role", POS_USER_ROLE):
		return
	frappe.get_doc(
		{
			"doctype": "Role",
			"role_name": POS_USER_ROLE,
			"desk_access": 0,
		}
	).insert(ignore_permissions=True)


def make_custom_fields():
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True, update=True)


def ensure_setup():
	"""Role and custom fields (idempotent); returns what the app ensures, per doctype."""
	make_role()
	make_custom_fields()
	return {
		"role": POS_USER_ROLE,
		"custom_fields": {
			doctype: [field["fieldname"] for field in fields] for doctype, fields in CUSTOM_FIELDS.items()
		},
	}


def after_install():
	ensure_setup()


def after_migrate():
	ensure_setup()


def before_tests():
	"""Complete the ERPNext setup wizard on a fresh test site (no-op when a company exists)."""
	if not frappe.db.a_row_exists("Company"):
		from erpnext.setup.utils import before_tests as erpnext_before_tests

		erpnext_before_tests()
	make_role()
	make_custom_fields()
	# test setup must survive the per-test rollbacks of the test runner
	frappe.db.commit()  # nosemgrep
