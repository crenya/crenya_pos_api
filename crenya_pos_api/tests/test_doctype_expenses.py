"""The expense doctypes this app ships on the erpnext branch (taken from erptrue_app): the names
and fields the expenses engine reaches by name, the module, the permissions, and no link to a
doctype a plain ERPNext site lacks.

Pure python, no site needed: `python -m unittest discover -s <app>/tests -p 'test_doctype_*.py'`.
"""

import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DOCTYPES = os.path.join(HERE, "..", "crenya_pos_api", "doctype")
MODULE = "Crenya Pos Api"

# the Link / Table targets a plain ERPNext site (or this app) has
KNOWN_TARGETS = {
	"User",
	"Company",
	"POS Profile",
	"Purchase Invoice",
	"Supplier",
	"Mode of Payment",
	"Currency",
	"Purchase Taxes and Charges Template",
	"Account",
	"POS Expense Category",
	"POS Expense Line",
	"POS Expense Log",
}

TILL_FIELDS = {
	"local_id": ("Data", None),
	"company": ("Link", "Company"),
	"outlet": ("Link", "POS Profile"),
	"device": ("Data", None),
	"supplier": ("Link", "Supplier"),
	"payee_name": ("Data", None),
	"bill_no": ("Data", None),
	"bill_date": ("Date", None),
	"mode_of_payment": ("Link", "Mode of Payment"),
	"currency": ("Link", "Currency"),
	"net_amount": ("Currency", "currency"),
	"tax_amount": ("Currency", "currency"),
	"taxes_and_charges": ("Link", "Purchase Taxes and Charges Template"),
	"lines": ("Table", "POS Expense Line"),
	"status": ("Select", "\nBooked\nNot booked\nCancelled"),
	"purchase_invoice": ("Link", "Purchase Invoice"),
	"photo": ("Attach", None),
	"approved_by": ("Link", "User"),
	"log": ("Table", "POS Expense Log"),
}


def _load(name):
	with open(os.path.join(DOCTYPES, name, f"{name}.json")) as f:
		return json.load(f)


def _perms(j):
	return {
		p["role"]: {k for k in ("read", "write", "create", "delete") if p.get(k)} for p in j["permissions"]
	}


NAMES = {
	"pos_expense": "POS Expense",
	"pos_expense_line": "POS Expense Line",
	"pos_expense_log": "POS Expense Log",
	"pos_expense_category": "POS Expense Category",
}


class TestExpenseDoctypes(unittest.TestCase):
	def test_names_module_and_links(self):
		for folder, name in NAMES.items():
			j = _load(folder)
			self.assertEqual(j["name"], name)
			self.assertEqual(j["module"], MODULE, name)
			for f in j["fields"]:
				if f["fieldtype"] in ("Link", "Table", "Table MultiSelect"):
					self.assertIn(f["options"], KNOWN_TARGETS, f"{name}.{f['fieldname']}")
			self.assertEqual(set(j.get("field_order", [])), {f["fieldname"] for f in j["fields"]}, name)

	def test_no_erptrue_expense(self):
		j = _load("pos_expense")
		self.assertNotIn("erptrue_expense", {f["fieldname"] for f in j["fields"]})
		self.assertNotIn("erptrue_expense", j["field_order"])

	def test_pos_expense_till_fields(self):
		j = _load("pos_expense")
		by = {f["fieldname"]: f for f in j["fields"]}
		self.assertEqual(by["category"]["reqd"], 1)
		self.assertEqual(by["amount"]["reqd"], 1)
		self.assertIn("posting_error", by)
		for n, (ft, opt) in TILL_FIELDS.items():
			self.assertIn(n, by, n)
			self.assertEqual(by[n]["fieldtype"], ft, n)
			if opt is not None:
				self.assertEqual(by[n].get("options"), opt, n)
			self.assertFalse(by[n].get("reqd"), f"{n} must be optional")
			self.assertEqual(by[n].get("read_only"), 1, f"{n} must be read-only in the desk")
		self.assertEqual(by["local_id"].get("unique"), 1)
		self.assertFalse(j.get("make_attachment_public"), "the bill photo stays private")

	def test_children_and_category(self):
		for folder in ("pos_expense_line", "pos_expense_log"):
			self.assertEqual(_load(folder)["istable"], 1)
		log = {f["fieldname"]: f for f in _load("pos_expense_log")["fields"]}
		self.assertEqual(log["action"]["options"], "add\nedit\ndelete\nbooked\nbooking failed")
		category = {f["fieldname"]: f for f in _load("pos_expense_category")["fields"]}
		for n in ("category_name", "category_name_ar", "is_active", "expense_account"):
			self.assertIn(n, category)

	def test_permissions(self):
		p = _perms(_load("pos_expense"))
		self.assertEqual(p["System Manager"], {"read", "write", "create", "delete"})
		self.assertEqual(p["Accounts Manager"], {"read"})
		self.assertEqual(p["Accounts User"], {"read"})
		c = _perms(_load("pos_expense_category"))
		self.assertEqual(c["Accounts Manager"], {"read", "write", "create", "delete"})
		self.assertEqual(c["Accounts User"], {"read"})


if __name__ == "__main__":
	unittest.main()
