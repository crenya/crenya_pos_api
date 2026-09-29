"""Role and custom fields required by the sync protocol. Idempotent: runs after install and every migrate."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

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


def after_install():
	make_role()
	make_custom_fields()


def after_migrate():
	make_role()
	make_custom_fields()


def before_tests():
	"""Complete the ERPNext setup wizard on a fresh test site (no-op when a company exists)."""
	if not frappe.db.a_row_exists("Company"):
		from erpnext.setup.utils import before_tests as erpnext_before_tests

		erpnext_before_tests()
	make_role()
	make_custom_fields()
	# test setup must survive the per-test rollbacks of the test runner
	frappe.db.commit()  # nosemgrep
