"""The expenses booking engine (moved down from crenya_restaurant, 0.13.0; the restaurant till's
expenses are 0.14.0): what a cashier paid out of the drawer, booked in ERPNext as ONE submitted
Purchase Invoice with `is_paid=1`, paid from the mode's account (Rulings 1-8 of the outlet expenses
plan). Product-neutral: the retail and the restaurant till share it, and every sentence that names
a settings page takes it from the product (`product.ExpenseProduct.settings_hint`).

The record is erptrue_app's **POS Expense** (with its lines and log) and the categories are its
**POS Expense Category**. This app reaches them only by DocType name: erptrue_app is not in
`required_apps` and is never imported (`expenses_supported` gates everything). Without them the
till hides Expenses (no capability of the product, `product.ExpenseProduct.capability`), the bootstrap says why and the push
handler refuses with the same words.

The Purchase Invoice:
- supplier: the expense's, else the outlet's Cash Supplier; bill_no / bill_date from the till
  (a bill number the supplier already used, under Accounts Settings' uniqueness check, books
  without it and says so in `remarks`);
- posting_date: the business date (`set_posting_time`); company and cost centre: the outlet's;
- is_paid with the expense's mode of payment, `cash_bank_account` its account for the company and
  `paid_amount` the total; rounding off, no stock;
- one item-less row per line: item_name = the category, qty 1, rate = the net amount, the expense
  account of the category (of this company), else the company's default expense account;
- the outlet's Purchase Taxes and Charges Template, its rows rebuilt as charge type Actual with the
  till's tax amounts, so `grand_total` is the cash that left the drawer to the minor unit.

A booking runs in its own savepoint: when it fails, the POS Expense stays `Not booked` with its
`posting_error` (the cash has left the drawer whatever the books say) and a manager retries it
from app.crenya.com (`api.web_admin.retry_expense_booking`). The log is append-only: add, edit and
delete from the till, booked and booking failed from here.
"""

import json
import re
from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, now_datetime

from crenya_pos_api.expenses.product import ExpenseProduct, product_of_expense
from crenya_pos_api.sync.context import money_precision
from crenya_pos_api.utils.decimal import as_decimal, format_money, format_number, quantize
from crenya_pos_api.web.setup_checks import check

EXPENSE = "POS Expense"
CATEGORY = "POS Expense Category"
PURCHASE_INVOICE = "Purchase Invoice"
TAX_TEMPLATE = "Purchase Taxes and Charges Template"

BOOKED = "Booked"
NOT_BOOKED = "Not booked"
CANCELLED = "Cancelled"

# the log's actions (erptrue_app POS Expense Log `action`)
ADD, EDIT, DELETE = "add", "edit", "delete"
LOG_BOOKED, LOG_FAILED = "booked", "booking failed"
CHANGES = (ADD, EDIT, DELETE)

# outlet settings (POS Profile, expenses/fields.py EXPENSE_PROFILE_FIELDS)
ENABLED_FIELD = "crenya_expenses_enabled"
CASH_SUPPLIER_FIELD = "crenya_expense_cash_supplier"
SUPPLIER_GROUP_FIELD = "crenya_expense_supplier_group"
TAX_TEMPLATE_FIELD = "crenya_expense_tax_template"
ADD_PIN_FIELD = "crenya_expense_add_needs_pin"

# the bill photo: the till scales it to this longest side; the server takes a JPEG of at most the
# item-image size
PHOTO_MAX_SIDE = 1600

UNSUPPORTED = "Expenses need crenya erp's Expenses module on this workspace"
BOOKING_SAVEPOINT = "crenya_expense_booking"
MAX_ERROR_LENGTH = 1000


class BookingError(Exception):
	"""Why an expense cannot be booked (kept as its `posting_error`)."""


def expenses_supported():
	"""Whether this site has erptrue_app's POS Expense with the till's fields (erptrue_app 0.0.3,
	migrated)."""
	return bool(frappe.db.exists("DocType", EXPENSE)) and frappe.get_meta(EXPENSE).has_field("local_id")


def currency_precision(currency=None):
	"""Decimals of the currency's money (ERPNext's rule for a Purchase Invoice total)."""
	return cint(money_precision(currency or None))


def off_reason(product: ExpenseProduct):
	"""Why expenses are off for an outlet whose switch is off, in English like the sync
	protocol's messages."""
	return f"Expenses are switched off for this outlet ({product.settings_hint})"


def no_cash_supplier_reason(product: ExpenseProduct):
	"""The sentence that asks for the outlet's Cash Supplier (translated like the message the
	booking raises)."""
	return _("Set this outlet's Cash Supplier ({0})").format(product.settings_hint)


# settings


def template_rows(template, company):
	"""The rows of an enabled Purchase Taxes and Charges Template of `company`, in order:
	[{account_head, description, rate}], or None for no (or another company's) template."""
	if not template:
		return None
	values = frappe.db.get_value(TAX_TEMPLATE, template, ["company", "disabled"], as_dict=True)
	if not values or values.company != company or cint(values.disabled):
		return None
	return frappe.get_all(
		"Purchase Taxes and Charges",
		filters={"parent": template, "parenttype": TAX_TEMPLATE},
		fields=["account_head", "description", "rate"],
		order_by="idx asc",
	)


def supplier_allowed(profile, supplier):
	"""Whether `supplier` is one the till is sent for this outlet (sync/expenses.py
	ExpenseSupplierSpec): an enabled Supplier that is the outlet's Cash Supplier or in its
	supplier group."""
	values = frappe.db.get_value("Supplier", supplier, ["disabled", "supplier_group"], as_dict=True)
	if not values or cint(values.disabled):
		return False
	group = profile.get(SUPPLIER_GROUP_FIELD)
	return supplier == profile.get(CASH_SUPPLIER_FIELD) or bool(group and values.supplier_group == group)


def cash_supplier(profile):
	"""The outlet's Cash Supplier when it is an enabled Supplier, else None."""
	name = profile.get(CASH_SUPPLIER_FIELD)
	if name and frappe.db.get_value("Supplier", name, "disabled") == 0:
		return name
	return None


# the till's amounts (pure)


def _decimals_ok(value, precision):
	return quantize(value, precision) == value


def split_check(data, template, precision):
	"""Why the till's amounts do not hold together, or None (pure): every amount at most
	`precision` decimals; the lines sum to `net`; the tax rows are the template's account heads in
	its order (`template` the list of heads, None when VAT is off: then no rows and no tax) and sum
	to `tax`; net + tax = total. Exact, never with a tolerance."""
	amounts = [("net", data["net"]), ("tax", data["tax"]), ("total", data["total"])]
	amounts += [(f"lines[{i}].amount", line["amount"]) for i, line in enumerate(data["lines"])]
	amounts += [(f"taxes[{i}].amount", row["amount"]) for i, row in enumerate(data["taxes"])]
	for field, value in amounts:
		if not _decimals_ok(value, precision):
			return f"{field} {value} has more decimals than the currency's {precision}"
	if sum((line["amount"] for line in data["lines"]), Decimal(0)) != data["net"]:
		return f"The lines do not add up to net {data['net']}"
	heads = [row["account_head"] for row in data["taxes"]]
	if template is None:
		if heads or data["tax"]:
			return "No tax template: an expense without VAT has no tax rows and no tax"
	elif heads != list(template):
		return f"The tax rows must be the tax template's account heads ({', '.join(template)})"
	if sum((row["amount"] for row in data["taxes"]), Decimal(0)) != data["tax"]:
		return f"The tax rows do not add up to tax {data['tax']}"
	if data["net"] + data["tax"] != data["total"]:
		return f"net {data['net']} plus tax {data['tax']} is not total {data['total']}"
	return None


def snapshot(data, precision):
	"""What the till said of an expense, as the log keeps it (strings at the currency's decimals)."""
	money = lambda value: format_money(value, precision)  # noqa: E731
	return {
		"supplier": data.get("supplier"),
		"payee_name": data.get("payee_name"),
		"bill_no": data.get("bill_no"),
		"bill_date": str(data["bill_date"]) if data.get("bill_date") else None,
		"mode_of_payment": data["mode_of_payment"],
		"lines": [
			{
				"category": line["category"],
				"description": line.get("description"),
				"amount": money(line["amount"]),
			}
			for line in data["lines"]
		],
		"tax_template": data.get("tax_template"),
		"taxes": [
			{
				"account_head": row["account_head"],
				"rate": format_number(row["rate"]),
				"amount": money(row["amount"]),
			}
			for row in data["taxes"]
		],
		"net": money(data["net"]),
		"tax": money(data["tax"]),
		"total": money(data["total"]),
		"notes": data.get("notes"),
	}


def last_snapshot(expense):
	"""The till's latest version of the expense: the `after` of its last add or edit log row."""
	for row in reversed(expense.get("log") or []):
		if row.action in (ADD, EDIT) and row.after:
			try:
				return json.loads(row.after)
			except ValueError:
				return None
	return None


def last_change(expense):
	"""The last till action on the expense (add, edit or delete)."""
	for row in reversed(expense.get("log") or []):
		if row.action in CHANGES:
			return row.action
	return None


def deleted_at_till(names) -> set:
	"""The ones of expenses `names` whose last till action was a delete: a delete the server could
	not finish (the invoice would not cancel) leaves the row `Not booked`, yet the till counted the
	cash back in at the delete. Read in one query (`last_change` reads one loaded expense)."""
	names = list(names or ())
	if not names:
		return set()
	last = {}
	for row in frappe.get_all(
		"POS Expense Log",
		filters={"parent": ["in", names], "parenttype": EXPENSE, "action": ["in", CHANGES]},
		fields=["parent", "action"],
		order_by="idx asc",
	):
		last[row.parent] = row.action
	return {name for name, action in last.items() if action == DELETE}


def add_log(
	expense, action, user=None, approver=None, device=None, before=None, after=None, reason=None, at=None
):
	expense.append(
		"log",
		{
			"action": action,
			"at": at or now_datetime(),
			"user": user,
			"approver": approver,
			"device": device,
			"before": json.dumps(before, ensure_ascii=False, sort_keys=True) if before is not None else None,
			"after": json.dumps(after, ensure_ascii=False, sort_keys=True) if after is not None else None,
			"reason": reason,
		},
	)


# accounts


def cash_account(mode, company):
	"""The account a Mode of Payment pays from for `company` (its Mode of Payment Account, the
	lookup the till's Sales Invoice payments use), or None."""
	from crenya_pos_api.extension import mode_of_payment_account

	try:
		return mode_of_payment_account(mode, company) or None
	except frappe.ValidationError:
		_drop_last_message()
		return None


def _drop_last_message():
	try:
		frappe.clear_last_message()
	except Exception:
		pass


def line_expense_account(category, company):
	"""The expense account of a line: the category's when it is an account of `company`, else the
	company's default expense account. BookingError when neither is set."""
	account = frappe.db.get_value(CATEGORY, category, "expense_account")
	if account and frappe.db.get_value("Account", account, "company") == company:
		return account
	default = frappe.get_cached_value("Company", company, "default_expense_account")
	if default:
		return default
	raise BookingError(
		_("Set an expense account on category {0} or the company {1}").format(category, company)
	)


# the Purchase Invoice


def _bill_clash(supplier, bill_no, pi_name=None):
	"""The submitted or draft Purchase Invoice of `supplier` that already has `bill_no`, when
	Accounts Settings checks supplier invoice uniqueness (ERPNext would refuse it forever)."""
	if not bill_no or not cint(
		frappe.db.get_single_value("Accounts Settings", "check_supplier_invoice_uniqueness")
	):
		return None
	filters = {"supplier": supplier, "bill_no": bill_no, "docstatus": ["<", 2]}
	if pi_name:
		filters["name"] = ["!=", pi_name]
	return frappe.db.get_value(PURCHASE_INVOICE, filters, "name")


def _device_label(device):
	if not device:
		return ""
	return frappe.db.get_value("Crenya POS Device", device, "device_name") or device


def _taxes(expense, template, precision):
	"""The tax rows the PI books: the till's amounts on the template's heads (the last snapshot);
	an expense without one (none logged) puts its whole tax on a one-row template."""
	rows = template_rows(template, expense.company)
	if not rows:
		raise BookingError(
			_("Tax template {0} is not an enabled template of {1}").format(template, expense.company)
		)
	shot = last_snapshot(expense) or {}
	amounts = {row["account_head"]: as_decimal(row["amount"]) for row in shot.get("taxes") or []}
	if not amounts and len(rows) == 1:
		amounts = {rows[0].account_head: as_decimal(expense.tax_amount)}
	return [
		{
			"charge_type": "Actual",
			"account_head": row.account_head,
			"description": row.description or row.account_head,
			"tax_amount": flt(quantize(amounts.get(row.account_head, 0), precision), precision),
			"category": "Total",
			"add_deduct_tax": "Add",
		}
		for row in rows
	]


def resolve_product(expense, product=None):
	"""The product that words an expense's messages: the caller's (the actor's device context or the
	web request; callers pass it), else the one of the expense's own device. Never a guess: an
	expense that has neither is a programming error."""
	product = product or product_of_expense(expense)
	if product is None:
		raise ValueError(f"POS Expense {expense.get('name')} has no device of a known product: pass product=")
	return product


def build_purchase_invoice(expense, profile, notes=None, amended_from=None, product=None):
	"""The unsaved paid Purchase Invoice of a POS Expense (see the module docstring). `product`
	(default: the product of the expense's device) words the messages."""
	product = resolve_product(expense, product)
	company = expense.company
	precision = currency_precision(expense.currency or None)
	supplier = expense.supplier or cash_supplier(profile)
	if not supplier:
		raise BookingError(_("Set this outlet's Cash Supplier ({0})").format(product.settings_hint))
	account = cash_account(expense.mode_of_payment, company)
	if not account:
		raise BookingError(
			_("Mode of payment {0} has no account for company {1}").format(expense.mode_of_payment, company)
		)
	uom = frappe.db.get_single_value("Stock Settings", "stock_uom")
	if not uom:
		raise BookingError(_("Set the Default Unit of Measure in Stock Settings"))

	payee = expense.payee_name or frappe.db.get_value("Supplier", supplier, "supplier_name") or supplier
	remarks = [
		" · ".join(
			part
			for part in (payee, _device_label(expense.device), expense.user, f"POS Expense {expense.name}")
			if part
		)
	]
	bill_no = expense.bill_no
	clash = _bill_clash(supplier, bill_no)
	if clash:
		remarks.append(_("Bill no. {0} (already used by {1})").format(bill_no, clash))
		if notes is not None:
			notes.append(f"Bill no. {bill_no} is already on {clash}: booked without it")
		bill_no = None

	pi = frappe.new_doc(PURCHASE_INVOICE)
	# the till's device or an outlet manager on app.crenya.com books it, neither with an accounts
	# role: ERPNext's own reads (the supplier's party details in set_missing_values) and the
	# insert and submit skip the user's permissions. Callers authorise the expense first.
	pi.flags.ignore_permissions = True
	pi.update(
		{
			"supplier": supplier,
			"company": company,
			"currency": expense.currency,
			"posting_date": expense.expense_date,
			"set_posting_time": 1,
			"bill_no": bill_no,
			"bill_date": expense.bill_date,
			"cost_center": profile.get("cost_center"),
			"is_paid": 1,
			"mode_of_payment": expense.mode_of_payment,
			"cash_bank_account": account,
			"disable_rounded_total": 1,
			"update_stock": 0,
			"remarks": "\n".join(remarks),
			"amended_from": amended_from,
		}
	)
	for line in expense.lines:
		pi.append(
			"items",
			{
				"item_name": line.category,
				"description": line.description or line.category,
				"uom": uom,
				"stock_uom": uom,
				"conversion_factor": 1,
				"qty": 1,
				"rate": flt(line.amount, precision),
				"expense_account": line_expense_account(line.category, company),
				"cost_center": profile.get("cost_center"),
			},
		)
	if expense.taxes_and_charges:
		pi.taxes_and_charges = expense.taxes_and_charges
		for row in _taxes(expense, expense.taxes_and_charges, precision):
			pi.append("taxes", row)
	pi.run_method("set_missing_values")
	pi.run_method("calculate_taxes_and_totals")
	pi.paid_amount = flt(pi.grand_total, precision)
	check_total(pi, expense, precision)
	return pi


def check_total(pi, expense, precision):
	"""The Purchase Invoice books exactly the cash that left the drawer, or nothing: ERPNext may
	change the total on insert or submit (a supplier's tax withholding, a rounding rule)."""
	if flt(pi.grand_total, precision) != flt(expense.amount, precision):
		raise BookingError(
			_("The purchase invoice total {0} is not the expense total {1}").format(
				pi.grand_total, expense.amount
			)
		)


def attach_photo_to_invoice(expense, pi_name):
	"""The expense's private photo File attached to its Purchase Invoice too (once)."""
	if not expense.photo or not pi_name:
		return
	if frappe.db.exists(
		"File",
		{"file_url": expense.photo, "attached_to_doctype": PURCHASE_INVOICE, "attached_to_name": pi_name},
	):
		return
	frappe.get_doc(
		{
			"doctype": "File",
			"file_url": expense.photo,
			"file_name": expense.photo.rsplit("/", 1)[-1],
			"attached_to_doctype": PURCHASE_INVOICE,
			"attached_to_name": pi_name,
			"is_private": 1,
		}
	).insert(ignore_permissions=True)


def _error_text(exc):
	text = cstr(getattr(exc, "message", None) or exc) or exc.__class__.__name__
	return re.sub(r"<[^>]+>", "", text).strip()[:MAX_ERROR_LENGTH]


def _cancel_pi(name):
	pi = frappe.get_doc(PURCHASE_INVOICE, name)
	if pi.docstatus == 1:
		pi.flags.ignore_permissions = True
		pi.cancel()


def _post(expense, notes, cancel_first=None, amended_from=None, product=None):
	"""Cancel `cancel_first` (when given) and submit the expense's new Purchase Invoice, in one
	savepoint. Returns the PI's name; BookingError (or ERPNext's error) when it fails, with every
	write of the attempt undone."""
	frappe.db.savepoint(BOOKING_SAVEPOINT)
	try:
		if cancel_first:
			_cancel_pi(cancel_first)
		profile = frappe.get_doc("POS Profile", expense.outlet)
		pi = build_purchase_invoice(expense, profile, notes, amended_from=amended_from, product=product)
		precision = currency_precision(expense.currency or None)
		pi.insert()
		check_total(pi, expense, precision)
		pi.submit()
		check_total(pi, expense, precision)
		attach_photo_to_invoice(expense, pi.name)
		return pi.name
	except Exception:
		frappe.db.rollback(save_point=BOOKING_SAVEPOINT)
		frappe.clear_messages()
		raise


def _booked(expense, pi_name, notes):
	expense.purchase_invoice = pi_name
	expense.status = BOOKED
	expense.posting_error = None
	add_log(expense, LOG_BOOKED, after={"purchase_invoice": pi_name})
	expense.save(ignore_permissions=True)
	if notes is not None:
		notes.append(f"Booked as Purchase Invoice {pi_name}")


def _failed(expense, exc, notes, status=NOT_BOOKED):
	error = _error_text(exc)
	expense.status = status
	expense.posting_error = error
	add_log(expense, LOG_FAILED, reason=error[:140])
	expense.save(ignore_permissions=True)
	if notes is not None:
		notes.append(f"Not booked: {error}")


def transient(exc):
	"""A database deadlock, lock timeout or a document changed meanwhile: the event is retried
	(re-raised), never kept as `Not booked`."""
	return isinstance(
		exc, frappe.QueryDeadlockError | frappe.QueryTimeoutError | frappe.TimestampMismatchError
	)


def invoice_submitted(name):
	return bool(name) and frappe.db.get_value(PURCHASE_INVOICE, name, "docstatus") == 1


def book(expense, notes=None, product=None):
	"""Book an expense that has no submitted Purchase Invoice: a new one (amending the cancelled one
	it had, when it had one). A failure keeps it `Not booked` with its error. Returns the status."""
	if invoice_submitted(expense.purchase_invoice):
		return expense.status
	old = expense.purchase_invoice or None
	try:
		name = _post(expense, notes, amended_from=old, product=product)
	except Exception as e:
		if transient(e):
			raise
		_failed(expense, e, notes)
		return expense.status
	_booked(expense, name, notes)
	return expense.status


def rebook(expense, notes=None, product=None):
	"""Book an edited expense: cancel its Purchase Invoice and submit the amended one, together. A
	failure keeps the old invoice, and the expense `Not booked` until a retry."""
	old = expense.purchase_invoice or None
	if not invoice_submitted(old):
		return book(expense, notes, product)
	try:
		name = _post(expense, notes, cancel_first=old, amended_from=old, product=product)
	except Exception as e:
		if transient(e):
			raise
		_failed(expense, e, notes)
		return expense.status
	_booked(expense, name, notes)
	return expense.status


def unbook(expense, notes=None):
	"""Cancel a deleted expense's Purchase Invoice; the expense becomes `Cancelled` (never deleted).
	When the invoice cannot be cancelled the expense stays `Not booked` with the error, and a retry
	cancels it."""
	old = expense.purchase_invoice or None
	if invoice_submitted(old):
		frappe.db.savepoint(BOOKING_SAVEPOINT)
		try:
			_cancel_pi(old)
		except Exception as e:
			frappe.db.rollback(save_point=BOOKING_SAVEPOINT)
			frappe.clear_messages()
			if transient(e):
				raise
			_failed(expense, e, notes)
			return expense.status
		if notes is not None:
			notes.append(f"Purchase Invoice {old} cancelled")
	expense.status = CANCELLED
	expense.posting_error = None
	expense.save(ignore_permissions=True)
	return expense.status


def retry(expense, notes=None, product=None):
	"""Finish what the last till action needed (app.crenya.com's Retry booking): a delete cancels
	the invoice, an edit reposts it, an add books it."""
	if last_change(expense) == DELETE:
		return unbook(expense, notes)
	if invoice_submitted(expense.purchase_invoice):
		return rebook(expense, notes, product)
	return book(expense, notes, product)


# the outlet's Cash Supplier (app.crenya.com)


def supplier_group_of(profile):
	"""The group of the outlet's expense suppliers: its setting, else Buying Settings' default,
	else the root Supplier Group."""
	from frappe.utils.nestedset import get_root_of

	return (
		profile.get(SUPPLIER_GROUP_FIELD)
		or frappe.db.get_single_value("Buying Settings", "supplier_group")
		or get_root_of("Supplier Group")
	)


def _enabled(supplier):
	"""`supplier` enabled again when it was disabled (the outlet's own Cash Supplier: a till cannot
	book without it); None when there is no such Supplier."""
	if not supplier or not frappe.db.exists("Supplier", supplier):
		return None
	if cint(frappe.db.get_value("Supplier", supplier, "disabled")):
		doc = frappe.get_doc("Supplier", supplier)
		doc.disabled = 0
		doc.save(ignore_permissions=True)
	return supplier


def ensure_cash_supplier(profile):
	"""The outlet's enabled Cash Supplier, made (and set on the outlet) when it has none:
	(name, created). A disabled one is enabled again."""
	current = _enabled(profile.get(CASH_SUPPLIER_FIELD))
	if current:
		return current, False
	supplier_name = _("Cash Purchases - {0}").format(profile.name)
	name = _enabled(frappe.db.get_value("Supplier", {"supplier_name": supplier_name}, "name"))
	created = False
	if not name:
		name = (
			frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": supplier_name,
					"supplier_group": supplier_group_of(profile),
					"supplier_type": "Individual",
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
		created = True
	frappe.db.set_value("POS Profile", profile.name, CASH_SUPPLIER_FIELD, name)
	frappe.clear_document_cache("POS Profile", profile.name)
	return name, created


# setup checks (app.crenya.com's Settings → Expenses)


def setup_checks(profile):
	"""What booking this outlet's expenses needs, read live: `setup_checks.check` rows (the dry run
	of the outlet expenses plan's Task 0)."""
	company = profile.get("company")
	checks = []
	supported = expenses_supported()
	checks.append(
		check(
			"module",
			"ok" if supported else "missing",
			_("crenya erp's Expenses module"),
			None if supported else _(UNSUPPORTED),
		)
	)
	cash_types = set(frappe.get_all("Mode of Payment", filters={"type": "Cash"}, pluck="name"))
	modes = [row.mode_of_payment for row in profile.get("payments") or [] if row.mode_of_payment]
	cash_mode = next((mode for mode in modes if mode in cash_types), None)
	checks.append(
		check(
			"cash_mode",
			"ok" if cash_mode else "missing",
			_("A cash mode of payment on the outlet"),
			cash_mode or _("Add a Cash-type mode of payment to the outlet"),
		)
	)
	missing_accounts = [mode for mode in modes if not cash_account(mode, company)]
	checks.append(
		check(
			"cash_account",
			"missing"
			if cash_mode in missing_accounts or not cash_mode
			else ("warn" if missing_accounts else "ok"),
			_("An account for each mode of payment"),
			_("No account for {0} in company {1}").format(", ".join(missing_accounts), company)
			if missing_accounts
			else None,
		)
	)
	payable = frappe.get_cached_value("Company", company, "default_payable_account") if company else None
	checks.append(
		check(
			"payable_account",
			"ok" if payable else "missing",
			_("The company's default payable account"),
			payable or _("Set Default Payable Account on company {0}").format(company),
		)
	)
	expense_account = (
		frappe.get_cached_value("Company", company, "default_expense_account") if company else None
	)
	account_type = expense_account and frappe.db.get_value("Account", expense_account, "account_type")
	# the active categories without an expense account of the company book to its default
	rows = (
		frappe.get_all(
			CATEGORY, filters={"is_active": 1}, fields=["name", "expense_account"], order_by="name asc"
		)
		if supported
		else []
	)
	accounts = dict(
		frappe.get_all(
			"Account",
			filters={"name": ["in", [r.expense_account for r in rows if r.expense_account] or [""]]},
			fields=["name", "company"],
			as_list=True,
		)
	)
	without = [r.name for r in rows if accounts.get(r.expense_account) != company]
	cogs = account_type == "Cost of Goods Sold"
	if rows and not without and (cogs or not expense_account):
		# every line books to its category's own account: the default is never used
		checks.append(
			check(
				"expense_account",
				"ok",
				_("The company's default expense account"),
				_("Not used: every expense category has its own expense account"),
			)
		)
	elif not expense_account:
		checks.append(
			check(
				"expense_account",
				"missing",
				_("The company's default expense account"),
				_(
					"Set Default Expense Account on company {0}, or an expense account on every category"
				).format(company),
			)
		)
	else:
		checks.append(
			check(
				"expense_account",
				"warn" if cogs else "ok",
				_("The company's default expense account"),
				_(
					"{0} is a Cost of Goods Sold account: categories without their own account book there"
				).format(expense_account)
				if cogs
				else expense_account,
			)
		)
	if supported:
		if not rows:
			checks.append(
				check(
					"categories", "missing", _("Expense categories"), _("Add at least one expense category")
				)
			)
		else:
			checks.append(
				check(
					"categories",
					"warn" if without else "ok",
					_("Expense categories"),
					_("Without an expense account of {0}: {1} (they book to {2})").format(
						company, ", ".join(without), expense_account or _("nothing: booking fails")
					)
					if without
					else None,
				)
			)
	template = profile.get(TAX_TEMPLATE_FIELD)
	rows = template_rows(template, company)
	checks.append(
		check(
			"tax_template",
			"ok" if rows else ("missing" if template else "warn"),
			_("Purchase VAT template"),
			template
			if rows
			else (
				_("{0} is not an enabled template of {1}").format(template, company)
				if template
				else _("None: expenses are booked without VAT")
			),
		)
	)
	uom = frappe.db.get_single_value("Stock Settings", "stock_uom")
	checks.append(
		check(
			"stock_uom",
			"ok" if uom else "missing",
			_("Default Unit of Measure (Stock Settings)"),
			uom or _("Set the Default Unit of Measure in Stock Settings"),
		)
	)
	supplier = cash_supplier(profile)
	checks.append(
		check(
			"cash_supplier",
			"ok" if supplier else "missing",
			_("Cash Supplier"),
			supplier or _("Create the Cash Supplier"),
		)
	)
	return checks
