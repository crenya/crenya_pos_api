"""Setup checks of a shop (a retail POS Profile): what its tills need to sell and book, read live
and shown on the shop's settings page of app.crenya.com beside the money checks. Rows are
`setup_checks.check` rows; `label` and `fix` are fixed English sentences (the web app has their
Arabic, keyed by `key`), `detail` the values read. Nothing here changes a setting.

`retail_checks` is pure (unit tested); `retail_setup_checks` reads the facts from the site.
"""

import frappe
from frappe.utils import cint

from crenya_pos_api.sync.approvals import APPROVER_ROLE, required_actions
from crenya_pos_api.sync.cashier import PIN_HASH_FIELD, staff_roles
from crenya_pos_api.sync.context import DEFAULT_DEVICE_TYPE, DeviceContext
from crenya_pos_api.sync.invoice_builder import mode_of_payment_account
from crenya_pos_api.web.setup_checks import MISSING, OK, WARN, check

CHECK_KEYS = (
	"price_list",
	"tax_template",
	"warehouse",
	"default_customer",
	"payment_modes",
	"write_off",
	"cashiers",
	"approver",
)


def _price_list_check(facts):
	row = facts.get("price_list")
	label = "A selling price list in the outlet's currency"
	fix = "Settings → Selling price list → a selling price list in the outlet's currency"
	if not row:
		return check("price_list", MISSING, label, None, fix)
	detail = f"{row['name']} ({row.get('currency') or '-'})"
	if not cint(row.get("selling")) or row.get("currency") != facts["currency"]:
		return check("price_list", MISSING, label, detail, fix)
	return check("price_list", OK, label, detail)


def _tax_template_check(facts):
	row = facts.get("tax_template")
	label = "A sales tax template of the outlet's company"
	fix = "Settings → Sales taxes template → a template of the outlet's company"
	if not row:
		return check("tax_template", WARN, label, "None: bills carry no tax", fix)
	if row.get("company") != facts["company"]:
		return check("tax_template", MISSING, label, f"{row['name']} ({row.get('company')})", fix)
	return check("tax_template", OK, label, row["name"])


def _warehouse_check(facts):
	row = facts.get("warehouse")
	label = "A stock warehouse (not a group)"
	fix = "Settings → Warehouse → a warehouse that is not a group"
	if not row:
		return check("warehouse", MISSING, label, None, fix)
	if cint(row.get("is_group")):
		return check("warehouse", MISSING, label, row["name"], fix)
	return check("warehouse", OK, label, row["name"])


def _customer_check(facts):
	customer = facts.get("customer")
	return check(
		"default_customer",
		OK if customer else MISSING,
		"A walk-in customer for bills without a customer",
		customer,
		"Settings → Customer → the walk-in customer",
	)


def _payment_modes_check(facts):
	modes = facts.get("payment_modes") or []
	label = "Payment methods, each with an account of the company"
	fix = "Give the outlet at least one payment method, and each one an account of the outlet's company"
	if not modes:
		return check("payment_modes", MISSING, label, None, fix)
	without = [row["mode"] for row in modes if not row.get("account")]
	detail = ", ".join(row["mode"] for row in modes)
	if without:
		return check("payment_modes", MISSING, label, "No account: " + ", ".join(without), fix)
	return check("payment_modes", OK, label, detail)


def _write_off_check(facts):
	label = "A write-off account for rounding"
	if cint(facts.get("disable_rounded_total")):
		return check("write_off", OK, label, "Rounded total is off")
	account = facts.get("write_off_account")
	return check(
		"write_off",
		OK if account else MISSING,
		label,
		account,
		"Settings → Write off account → an account of the company (rounded total is on)",
	)


def _cashiers_check(facts):
	count = cint(facts.get("cashiers"))
	return check(
		"cashiers",
		OK if count else MISSING,
		"At least one cashier with a till PIN",
		str(count),
		"People → a user → Shop till → a privilege, a 6-digit PIN and this outlet",
	)


def _approver_check(facts):
	count = cint(facts.get("approvers"))
	if count:
		return check("approver", OK, "A manager who approves at the till", str(count))
	# without a required approval nothing at the till waits for a manager
	return check(
		"approver",
		MISSING if facts.get("approvals_required") else WARN,
		"A manager who approves at the till",
		str(count),
		"People → a user → Shop till → Manager, a 6-digit PIN and this outlet",
	)


def retail_checks(facts: dict) -> list[dict]:
	"""The shop's checks of `retail_facts` (pure), in CHECK_KEYS order."""
	return [
		_price_list_check(facts),
		_tax_template_check(facts),
		_warehouse_check(facts),
		_customer_check(facts),
		_payment_modes_check(facts),
		_write_off_check(facts),
		_cashiers_check(facts),
		_approver_check(facts),
	]


def _row(doctype, name, fields):
	if not name:
		return None
	row = frappe.db.get_value(doctype, name, ["name", *fields], as_dict=True)
	return dict(row) if row else {"name": name}


def retail_facts(profile) -> dict:
	"""What `retail_checks` reads of a POS Profile, live."""
	company = profile.company
	staff = staff_roles(profile, DEFAULT_DEVICE_TYPE)
	with_pin = set(
		frappe.get_all(
			"User",
			filters={"name": ["in", list(staff) or [""]], PIN_HASH_FIELD: ["is", "set"]},
			pluck="name",
		)
	)
	return {
		"company": company,
		"currency": DeviceContext(device={}, profile=profile).currency,
		"price_list": _row("Price List", profile.get("selling_price_list"), ["selling", "currency"]),
		"tax_template": _row(
			"Sales Taxes and Charges Template", profile.get("taxes_and_charges"), ["company"]
		),
		"warehouse": _row("Warehouse", profile.get("warehouse"), ["is_group"]),
		"customer": profile.get("customer") or None,
		"payment_modes": [
			{"mode": row.mode_of_payment, "account": mode_of_payment_account(row.mode_of_payment, company)}
			for row in profile.get("payments") or []
			if row.mode_of_payment
		],
		"disable_rounded_total": cint(profile.get("disable_rounded_total")),
		"write_off_account": profile.get("write_off_account") or None,
		"cashiers": len(with_pin),
		"approvers": sum(1 for user in with_pin if APPROVER_ROLE in staff[user]),
		"approvals_required": bool(required_actions(profile)),
	}


def retail_setup_checks(profile) -> list[dict]:
	"""The shop's setup checks, read live."""
	return retail_checks(retail_facts(profile))
