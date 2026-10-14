"""A shop's till expenses on app.crenya.com (moved down from crenya_restaurant's web admin, 0.13.0),
for the shop's managers (`scope.manage`; a manager needs no POS Expense right): the Cash Supplier
button, the expenses left `Not booked` with Retry, and one expense with its lines and log. The
answer shapes are the restaurant's, so the web's shared expense components read both products alike.

From a browser session only (`require_web_session`): a till's or a person's API key is refused.
The booking engine is always told the product (retail): a missing product would be swallowed into
`Not booked`.
"""

import frappe
from frappe import _

from crenya_pos_api.api.web_admin.scope import manage_company, managed_shops, user_may_use_company
from crenya_pos_api.expenses import booking
from crenya_pos_api.expenses.product import RETAIL
from crenya_pos_api.sync.context import money_precision
from crenya_pos_api.sync.locale_data import currency_info
from crenya_pos_api.utils.decimal import format_money
from crenya_pos_api.web.context import invalid, is_system_manager, require_web_session
from crenya_pos_api.web.devices import wire

# the longest Not booked list
MAX_NOT_BOOKED = 100


def expense_currency(currency, company) -> dict:
	"""The currency block of an expense: its own currency, else its company's."""
	code = currency or frappe.get_cached_value("Company", company, "default_currency")
	return currency_info(code, money_precision(code))


def _booking_attempts(names) -> dict:
	"""{expense: (failed bookings, when the last one ran)} from the expenses' logs."""
	rows = frappe.get_all(
		"POS Expense Log",
		filters={
			"parenttype": booking.EXPENSE,
			"parent": ["in", list(names) or [""]],
			"action": booking.LOG_FAILED,
		},
		fields=["parent", "at"],
		order_by="at asc",
	)
	attempts = {}
	for row in rows:
		count, _last = attempts.get(row.parent, (0, None))
		attempts[row.parent] = (count + 1, row.at)
	return attempts


def not_booked_rows(outlets) -> dict:
	"""The till expenses of `outlets` left `Not booked`, newest business date first. Each row: name,
	outlet, date, amount (a string at its currency's precision) with the currency block, category
	(the first line's), supplier, payee, posting_error, failed booking attempts and when the last ran,
	and `can_retry`. {"rows", "more"}."""
	if not booking.expenses_supported():
		return {"rows": [], "more": False}
	# frappe.get_all reads without the user's permissions: the scope is the caller's `outlets`
	rows = frappe.get_all(
		booking.EXPENSE,
		filters={
			"outlet": ["in", list(outlets) or [""]],
			"status": booking.NOT_BOOKED,
			"local_id": ["is", "set"],
		},
		fields=[
			"name",
			"outlet",
			"company",
			"expense_date",
			"amount",
			"currency",
			"category",
			"supplier",
			"payee_name",
			"posting_error",
			"status",
		],
		order_by="expense_date desc, creation desc",
		limit=MAX_NOT_BOOKED + 1,
	)
	more = len(rows) > MAX_NOT_BOOKED
	rows = rows[:MAX_NOT_BOOKED]
	attempts = _booking_attempts(row.name for row in rows)
	answer = []
	for row in rows:
		money = expense_currency(row.currency, row.company)
		count, last = attempts.get(row.name, (0, None))
		answer.append(
			{
				"name": row.name,
				"outlet": row.outlet,
				"expense_date": wire(row.expense_date),
				"amount": format_money(row.amount or 0, money["precision"]),
				"currency": money,
				"category": row.category or None,
				"supplier": row.supplier or None,
				"payee_name": row.payee_name or None,
				"posting_error": row.posting_error or None,
				"attempts": count,
				"last_tried": wire(last),
				"can_retry": row.status == booking.NOT_BOOKED,
			}
		)
	return {"rows": answer, "more": more}


def _managed_outlets(outlet: str | None = None) -> list[str]:
	"""The shops the session user manages: `outlet` alone when given (`manage`: frappe.PermissionError
	for one they do not manage)."""
	if outlet:
		manage_company(outlet)
		return [outlet]
	return [row.name for row in managed_shops() if user_may_use_company(row.company)]


def _till_expense_outlet(name) -> str:
	"""The outlet of till expense `name` (a POS Expense with `local_id`); frappe.PermissionError for
	none, as for one of an outlet the user does not manage."""
	if not booking.expenses_supported():
		invalid(_(booking.UNSUPPORTED))
	outlet = (
		frappe.db.get_value(booking.EXPENSE, {"name": name, "local_id": ["is", "set"]}, "outlet")
		if isinstance(name, str) and name
		else None
	)
	if not outlet:
		raise frappe.PermissionError
	return outlet


@frappe.whitelist(methods=["POST"])
def ensure_cash_supplier(outlet: str) -> dict:
	"""The shop's Cash Supplier, created in its expense supplier group and set on the shop when it has
	none: {"cash_supplier": name, "created": bool}. A supplier an owner disabled is not enabled again
	by a manager (it says so; a System Manager may), and one that already has the name but is in
	another group is not adopted."""
	require_web_session()
	ctx = manage_company(outlet)
	name, created = booking.ensure_cash_supplier(ctx.profile, may_enable=is_system_manager())
	if created:
		ctx.log(frappe.get_doc("Supplier", name))
		ctx.log(frappe.get_doc("POS Profile", outlet))
	return {"cash_supplier": name, "created": created}


@frappe.whitelist(methods=["GET", "POST"])
def expenses_not_booked(outlet: str | None = None) -> dict:
	"""The till expenses left `Not booked` at the shops the user manages (`outlet` alone when given),
	newest business date first: read here for its managers, who need no POS Expense right. Each one
	`can_retry`. {"rows", "more"}."""
	require_web_session()
	# who may ask is settled first, on a site with the Expenses module or without it
	outlets = _managed_outlets(outlet)
	if not booking.expenses_supported():
		return {"rows": [], "more": False}
	return not_booked_rows(outlets)


@frappe.whitelist(methods=["GET", "POST"])
def till_expense(name: str) -> dict:
	"""One till expense, with its lines and log, for a manager of its shop (who needs no POS Expense
	right): {"expense", "currency" (its block), "can_retry"}."""
	require_web_session()
	manage_company(_till_expense_outlet(name))
	expense = frappe.get_doc(booking.EXPENSE, name)
	return {
		"expense": expense.as_dict(convert_dates_to_str=True),
		"currency": expense_currency(expense.currency, expense.company),
		"can_retry": expense.status == booking.NOT_BOOKED,
	}


@frappe.whitelist(methods=["POST"])
def retry_expense_booking(name: str) -> dict:
	"""Book again a till expense its booking left `Not booked` (the Purchase Invoice made, reposted
	or cancelled as the till's last change needs). Answers {"name", "status", "purchase_invoice",
	"posting_error"}. A manager of the shop needs no accounts role: the booking writes with
	ignore_permissions, only after `manage`."""
	require_web_session()
	ctx = manage_company(_till_expense_outlet(name))
	expense = frappe.get_doc(booking.EXPENSE, name)
	if expense.status != booking.NOT_BOOKED:
		invalid(
			_("Expense {0} is {1}: nothing to book again").format(
				name, expense.status or _("not a till expense")
			)
		)
	booking.retry(expense, product=RETAIL)
	ctx.log(expense)
	return {
		"name": expense.name,
		"status": expense.status,
		"purchase_invoice": expense.purchase_invoice or None,
		"posting_error": expense.posting_error or None,
	}
