"""Role and custom fields required by the sync protocol. Idempotent: runs after install and every migrate."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from crenya_pos_api.expenses.fields import EXPENSE_PROFILE_FIELDS
from crenya_pos_api.sync.approvals import (
	APPROVAL_ACTIONS,
	APPROVAL_DEFAULT_OFF,
	APPROVER_ROLE,
	MAX_DISCOUNT_FIELD,
)
from crenya_pos_api.sync.credit import CREDIT_CHANGED_AT_FIELD, CREDIT_ENABLED_FIELD, CREDIT_SALES_FIELD
from crenya_pos_api.sync.tax_wording import (
	DEFAULT_CREDIT_NOTE_TITLE,
	DEFAULT_INVOICE_TITLE,
	DEFAULT_RECEIPT_QR_OPTION,
	RECEIPT_QR_OPTIONS,
)

POS_USER_ROLE = "Crenya POS User"
DEVICE_ROLE = "Crenya POS Device"

# Read rights a device's user needs for ERPNext to accept a pushed sale (0.9.3). The push posts
# for the authorised device (sync/invoice_builder.py, ignore_permissions), but some ERPNext checks
# test the session user's rights whatever the document's flags say (found by recording every
# check a device-role-only user's push fails, 2026-10-12, v15 and v16):
# - `erpnext.accounts.party.get_party_account` checks the party account. It runs from
#   `_get_party_details` -> `set_account_and_due_date` on every Sales Invoice, even when debit_to
#   is already set, so setting debit_to first does not avoid it;
# - `erpnext.stock.get_item_details.get_item_details` checks each line's Item;
# - `erpnext.accounts.doctype.loyalty_program.loyalty_program.get_loyalty_program_details_with_points`
#   checks `has_permission("Customer", doc=..., throw=True)` when a sale of a customer in a Loyalty
#   Program is submitted (found by retail P1 Task 0, v15 and v16; 0.12.1);
# - `erpnext.controllers.accounts_controller.get_taxes_and_charges` checks `select` on the Sales
#   Taxes and Charges Template: a sale wholly on account is a plain (not POS) invoice, whose tax rows
#   the builder reads with it (0.13.0).
# {DocType: right}, read-only, never a write; granted to the roles of devices that post sales only
# (`ensure_push_reads`).
PUSH_READS = {
	"Account": "select",
	"Item": "read",
	"Customer": "read",
	"Sales Taxes and Charges Template": "select",
}


def _store_control_fields():
	"""POS Profile's *Store controls* section: the cashier's discount limit and one Check per
	`APPROVAL_ACTIONS` row (on by default, except `APPROVAL_DEFAULT_OFF`), in that order."""
	fields = [
		{
			"fieldname": "crenya_store_controls_section",
			"fieldtype": "Section Break",
			"label": "Store controls",
			"collapsible": 1,
			"insert_after": "crenya_scale_barcode_rules",
		},
		{
			"fieldname": MAX_DISCOUNT_FIELD,
			"fieldtype": "Percent",
			"label": "Most discount without a manager (%)",
			"description": "Blank or 0: no limit. Above it, a Crenya POS Manager approves at the till.",
			"insert_after": "crenya_store_controls_section",
		},
		{
			"fieldname": "crenya_store_controls_column",
			"fieldtype": "Column Break",
			"insert_after": MAX_DISCOUNT_FIELD,
		},
	]
	previous = "crenya_store_controls_column"
	for key, fieldname, label, _label_ar in APPROVAL_ACTIONS:
		fields.append(
			{
				"fieldname": fieldname,
				"fieldtype": "Check",
				"label": label,
				"description": "Needs a Crenya POS Manager's approval at the till",
				"default": "0" if key in APPROVAL_DEFAULT_OFF else "1",
				"insert_after": previous,
			}
		)
		previous = fieldname
	return fields


STORE_CONTROL_FIELDS = _store_control_fields()

# POS Profile's *Credit sales* section (0.13.0), after the expense settings: one switch per outlet
CREDIT_PROFILE_FIELDS = [
	{
		"fieldname": "crenya_credit_section",
		"fieldtype": "Section Break",
		"label": "Credit sales",
		"collapsible": 1,
		"insert_after": EXPENSE_PROFILE_FIELDS[-1]["fieldname"],
	},
	{
		"fieldname": CREDIT_SALES_FIELD,
		"fieldtype": "Check",
		"label": "Sell on Account",
		"default": "0",
		"insert_after": "crenya_credit_section",
		"description": "Cashiers sell on account to a named customer (never the default customer). "
		"A customer's credit limit is the customer's own, else its customer group's, else the company's "
		"(a System Manager or an Accounts Manager sets them); a customer with none has no limit.",
	},
]

# Customer's House Account facility (0.14.0, restaurant spec D6/RC14), after ERPNext's credit limits:
# off for every customer until a System Manager or an Accounts Manager enables one on app.crenya.com.
# `crenya_credit_changed_at` is written by the Customer validate hook (sync.credit.stamp_credit_change)
CREDIT_CUSTOMER_FIELDS = [
	{
		"fieldname": CREDIT_ENABLED_FIELD,
		"fieldtype": "Check",
		"label": "House Account (credit facility)",
		"default": "0",
		"insert_after": "credit_limits",
		"description": "The customer may be put on a House Account at a restaurant till (an active "
		"customer only). Switched on or off by a System Manager or an Accounts Manager.",
	},
	{
		"fieldname": CREDIT_CHANGED_AT_FIELD,
		"fieldtype": "Datetime",
		"label": "House Account changed at",
		"insert_after": CREDIT_ENABLED_FIELD,
		"read_only": 1,
		"no_copy": 1,
		"description": "When the House Account facility or Disabled last changed (server time).",
	},
]

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
		# a device user (0.12.0): the user of one till, phone or display, not a person
		{
			"fieldname": "crenya_device_id",
			"fieldtype": "Data",
			"label": "POS Device ID",
			"description": "Set on the user a POS device signs in with; never a person.",
			"insert_after": "crenya_pos_pin_hash",
			"read_only": 1,
			"no_copy": 1,
			"search_index": 1,
			# System Manager only (permlevel 1): a device user cannot rewrite who it is
			"permlevel": 1,
		},
		{
			"fieldname": "crenya_paired_by",
			"fieldtype": "Link",
			"label": "POS Device Paired By",
			"options": "User",
			"insert_after": "crenya_device_id",
			"read_only": 1,
			"no_copy": 1,
			"permlevel": 1,
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
		*CREDIT_CUSTOMER_FIELDS,
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
	"Item Group": [
		{
			"fieldname": "crenya_item_group_name_ar",
			"fieldtype": "Data",
			"label": "Item Group Name (Arabic)",
			"insert_after": "item_group_name",
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
		*STORE_CONTROL_FIELDS,
		# the outlet's expense settings, shared with the restaurant (expenses/fields.py)
		*EXPENSE_PROFILE_FIELDS,
		*CREDIT_PROFILE_FIELDS,
	],
	"Payment Entry": [
		# the receipt of a customer's payment taken at the till (Customer Payment aggregate): its
		# idempotency key
		{
			"fieldname": "crenya_local_id",
			"fieldtype": "Data",
			"label": "Crenya Local ID",
			"insert_after": "reference_no",
			"unique": 1,
			"read_only": 1,
			"no_copy": 1,
			"print_hide": 1,
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


# the till roles and their desk access: a cashier never signs in to the desk; the store controls'
# approver manages shops on app.crenya.com, which signs in System Users only (as Restaurant
# Manager); a device user's role (0.12.0) never opens the desk. Desk access gives no permission by
# itself: the role has no DocPerm of its own. Crenya POS User already exists with desk access when
# this runs (the DocType sync makes the roles its DocPerms name); it is left as it is, since
# turning desk access off would make every cashier a Website User on their next save.
ROLE_DESK_ACCESS = {POS_USER_ROLE: 0, APPROVER_ROLE: 1, DEVICE_ROLE: 0}


def make_role():
	"""The till roles: Crenya POS User, the store controls' approver and the device user's
	(ROLE_DESK_ACCESS)."""
	for role, desk_access in ROLE_DESK_ACCESS.items():
		if frappe.db.exists("Role", role):
			continue
		frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": desk_access}).insert(
			ignore_permissions=True
		)


def make_custom_fields():
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True, update=True)


def ensure_push_reads(roles=None):
	"""Give `roles` (default: the roles of this app's retail till, `device_roles(DEFAULT_DEVICE_TYPE)`:
	Crenya POS User and the roles hooked for every device type) the PUSH_READS rights, as a Custom
	DocPerm rule (level 0), once: a rule that exists (an administrator may have changed it) is left
	as it is. An app whose own device types post sales calls it with the roles of those devices
	(never with roles of devices that take no payment). Returns {DocType: [roles given]}."""
	from frappe.permissions import add_permission

	from crenya_pos_api.sync.context import DEFAULT_DEVICE_TYPE, device_roles

	roles = device_roles(DEFAULT_DEVICE_TYPE) if roles is None else tuple(roles)
	given = {}
	for doctype, right in PUSH_READS.items():
		for role in roles:
			rule = {"parent": doctype, "role": role, "permlevel": 0, "if_owner": 0}
			if not frappe.db.exists("Role", role) or frappe.db.exists("Custom DocPerm", rule):
				continue
			add_permission(doctype, role, 0, ptype=right)
			given.setdefault(doctype, []).append(role)
		frappe.clear_cache(doctype=doctype)
	return given


def ensure_setup():
	"""Role, custom fields and the device roles' push reads (idempotent); returns what the app
	ensures."""
	make_role()
	make_custom_fields()
	ensure_push_reads()
	return {
		"role": POS_USER_ROLE,
		"approver_role": APPROVER_ROLE,
		"push_reads": PUSH_READS,
		"custom_fields": {
			doctype: [field["fieldname"] for field in fields] for doctype, fields in CUSTOM_FIELDS.items()
		},
	}


def after_install():
	ensure_setup()


def after_migrate():
	ensure_setup()


def before_tests():
	"""Prepare ERPNext and the app for tests (the wizard step is skipped when a company exists)."""
	from crenya_pos_api.compat import erpnext_before_tests

	erpnext_before_tests()
	make_role()
	make_custom_fields()
	# test setup must survive the per-test rollbacks of the test runner
	frappe.db.commit()  # nosemgrep
