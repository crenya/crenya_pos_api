"""The expense settings of a POS Profile: the fields (made on install and migrate) and the check
that runs when the profile is saved."""

import frappe
from frappe import _

from crenya_pos_api.sync.approvals import APPROVAL_ACTIONS

# the Expenses section follows the last field of the Store controls section (setup/install.py:
# one Check per approval action, in order)
EXPENSES_AFTER = APPROVAL_ACTIONS[-1][1]

# the outlet's expense settings (moved down from crenya_restaurant, 0.13.0; the restaurant till's
# expenses are 0.14.0), shared by both products. crenya_restaurant defines the same fields after the
# owner's Opening Hours until it imports this list; the later app's definition wins on a site that
# has both.
EXPENSE_PROFILE_FIELDS = [
	{
		"fieldname": "crenya_expenses_section",
		"fieldtype": "Section Break",
		"label": "Expenses",
		"insert_after": EXPENSES_AFTER,
		"collapsible": 1,
	},
	{
		"fieldname": "crenya_expenses_enabled",
		"fieldtype": "Check",
		"label": "Expenses on the Till",
		"default": "0",
		"insert_after": "crenya_expenses_section",
		"description": (
			"Cashiers record what they pay out of the drawer; each expense is booked as a paid "
			"Purchase Invoice and the day close expects the drawer net of cash expenses."
		),
	},
	{
		"fieldname": "crenya_expense_cash_supplier",
		"fieldtype": "Link",
		"label": "Cash Supplier",
		"options": "Supplier",
		"insert_after": "crenya_expenses_enabled",
		"description": "The supplier of an expense paid to someone who is not a supplier (the payee's name is kept).",
	},
	{
		"fieldname": "crenya_expense_supplier_group",
		"fieldtype": "Link",
		"label": "Expense Suppliers",
		"options": "Supplier Group",
		"insert_after": "crenya_expense_cash_supplier",
		"description": "The suppliers of this group are offered on the till.",
	},
	{
		"fieldname": "crenya_expenses_column",
		"fieldtype": "Column Break",
		"insert_after": "crenya_expense_supplier_group",
	},
	{
		"fieldname": "crenya_expense_tax_template",
		"fieldtype": "Link",
		"label": "Expense VAT",
		"options": "Purchase Taxes and Charges Template",
		"insert_after": "crenya_expenses_column",
		"description": "Amounts paid include this VAT. Empty: expenses are booked without VAT.",
	},
	{
		"fieldname": "crenya_expense_add_needs_pin",
		"fieldtype": "Check",
		"label": "Manager PIN to Add an Expense",
		"default": "0",
		"insert_after": "crenya_expense_tax_template",
		"description": "Editing or deleting an expense always needs a manager PIN.",
	},
]


def validate_expenses(doc):
	"""The expense VAT template is one of the outlet's company (its rows decide the till's VAT)."""
	template = doc.get("crenya_expense_tax_template")
	if template:
		company = frappe.db.get_value("Purchase Taxes and Charges Template", template, "company")
		if company != doc.company:
			frappe.throw(
				_("Expense VAT {0} is a template of {1}, not of {2}").format(template, company, doc.company)
			)
