"""Credit sales: a customer's credit limit and balance as ERPNext reads them, and the request-local
waiver of ERPNext's limit check for the invoices a till pushes.

ERPNext stays the authority. The limit is `get_credit_limit` (the Customer's *Credit Limit* row for
the company, else its Customer Group's, else the Company's) and the balance
`get_customer_outstanding`, so the answer equals what a desk invoice of the same customer is checked
against. **A limit of 0 or none means no limit**: ERPNext's `check_credit_limit` returns when the limit
is 0, and so does everything here (`credit_limit` and `available` are null, `over_limit` false).

The server never refuses a sale the customer already took (spec C5): ERPNext's check would turn a
done credit sale, or a cash sale to an already over-limit customer, into a refused one. The invoice
is therefore posted inside `credit_limit_waived()`, and a note says when the customer ended above the
limit. The till's manager PIN is the approval; the web lists such customers.

**The House Account facility (0.14.0, restaurant spec D6/RC14).** A customer may be put on a House
Account only when its Customer `crenya_credit_enabled` is set and it is not disabled
(`credit_enabled_for`). `crenya_credit_changed_at` records, on the server's clock, when either of the
two last changed (`stamp_credit_change`, the Customer `validate` hook): it is the record's own
`modified` of that save, so a till that pulled the changed record echoes exactly that time back as
the sale's `credit_customer_as_of`. Only the device types an app lists in
`crenya_pos_credit_customer_flag_device_types` get `credit_enabled` (never a retail "till"), and
which sales are refused for it is that app's rule (crenya_restaurant's invoice extender).
"""

from contextlib import contextmanager
from decimal import Decimal

import frappe
from frappe.query_builder import Case
from frappe.query_builder.functions import Min, Sum
from frappe.utils import cint, getdate, now_datetime, nowdate

from crenya_pos_api.sync.context import DEFAULT_DEVICE_TYPE, money_precision
from crenya_pos_api.sync.errors import InvalidRequestError, raise_api_error
from crenya_pos_api.utils.dates import utc_now_iso
from crenya_pos_api.utils.decimal import as_decimal, format_money, quantize

# POS Profile Check: the outlet may sell on account (Credit sales section, setup/install.py)
CREDIT_SALES_FIELD = "crenya_credit_sales_enabled"
# the request-local flag `credit_limit_waived` sets and the wrapper reads
WAIVER_FLAG = "crenya_credit_waived"
WRAPPED_MARK = "_crenya_waiver"


def customer_credit_limit(customer, company):
	"""The customer's credit limit for `company` as ERPNext reads it (Decimal), or None when it has
	none (a limit of 0 is no limit)."""
	from erpnext.selling.doctype.customer.customer import get_credit_limit

	limit = as_decimal(get_credit_limit(customer, company))
	return limit if limit > 0 else None


LIMIT_SOURCES = ("customer", "group", "company")


def customer_credit_limit_sources(rows, company):
	"""{customer name: (credit limit as a Decimal or None, where it comes from)} for the customer
	`rows` (name, customer_group), in three queries for the page: ERPNext's `get_credit_limit`
	order, the customer's own row ("customer"), else its group's ("group"), else the company's
	("company"); 0 or none is no limit (None, None)."""
	names = [row.name for row in rows]
	groups = list({row.customer_group for row in rows if row.customer_group})

	def limits(parenttype, parents):
		if not parents:
			return {}
		found = frappe.get_all(
			"Customer Credit Limit",
			filters={"parent": ["in", parents], "parenttype": parenttype, "company": company},
			fields=["parent", "credit_limit", "bypass_credit_limit_check"],
			order_by="idx asc",
		)
		result = {}
		for row in found:
			if row.parent in result:
				continue
			# ERPNext's `get_credit_limit` ignores a Customer Group row that bypasses the check
			# (v15 and v16) and falls to the company's limit
			bypass = parenttype == "Customer Group" and row.bypass_credit_limit_check
			result[row.parent] = None if bypass else row.credit_limit
		return result

	own = limits("Customer", names)
	by_group = limits("Customer Group", groups)
	company_limit = frappe.get_cached_value("Company", company, "credit_limit")
	out = {}
	for row in rows:
		for source, value in (
			("customer", own.get(row.name)),
			("group", by_group.get(row.customer_group)),
			("company", company_limit),
		):
			if value:
				limit = as_decimal(value)
				out[row.name] = (limit, source) if limit > 0 else (None, None)
				break
		else:
			out[row.name] = (None, None)
	return out


def customer_credit_limits(rows, company):
	"""{customer name: credit limit as a Decimal or None} for the pull's customer `rows`
	(`customer_credit_limit_sources` without the source); 0 or none is no limit."""
	return {name: limit for name, (limit, _source) in customer_credit_limit_sources(rows, company).items()}


def payable_of(row, precision):
	"""What an invoice row (grand_total, rounded_total, disable_rounded_total) is payable for, as
	ERPNext decides it: the grand total when rounding is off, else the rounded total (Decimal)."""
	if cint(row.get("disable_rounded_total")):
		return quantize(row.get("grand_total") or 0, precision)
	return quantize(row.get("rounded_total") or row.get("grand_total") or 0, precision)


def on_account_part(invoice, payments, precision):
	"""What a sale put on the customer's account when it was sold (pure, Decimal >= 0): its payable
	total less the payment rows and any loyalty points it redeemed. A sale paid in full has none."""
	paid = sum((as_decimal(p.get("amount")) for p in payments or []), Decimal(0))
	loyalty = as_decimal(invoice.get("loyalty_amount") or 0)
	return max(
		payable_of(invoice, precision) - quantize(paid, precision) - quantize(loyalty, precision), Decimal(0)
	)


def credited_to_account(returns, payments_by_return, precision):
	"""What returns of an invoice credited to the customer's account (pure, Decimal >= 0): for each
	return that does not reduce its own outstanding (`update_outstanding_for_self` 0, the shape a
	credited return posts), its total less the money refunded in its payment rows. A plain refund
	credits nothing."""
	total = Decimal(0)
	for ret in returns or []:
		if cint(ret.get("update_outstanding_for_self", 1)):
			continue
		refunded = sum(
			(abs(as_decimal(p.get("amount"))) for p in (payments_by_return or {}).get(ret.get("name"), [])),
			Decimal(0),
		)
		total += max(abs(payable_of(ret, precision)) - quantize(refunded, precision), Decimal(0))
	return quantize(total, precision)


def _capped_overdue(total, outstanding, precision):
	"""The overdue figure, capped (Decimal): never above the balance, never below 0, so an advance
	or a credit note on the account lowers it."""
	return max(min(quantize(total, precision), quantize(outstanding, precision)), Decimal(0))


def overdue_figures(rows, outstanding, precision, today=None):
	"""(overdue, oldest_unpaid) of a customer's bills, the one rule of the overdue figure (pure,
	except `today` defaulting to the site's date).

	`rows` are the customer's submitted non-return bills of one company, `[{outstanding_amount,
	due_date, posting_date}]` (a row with nothing outstanding is ignored). `overdue` is the sum of
	what is outstanding on the bills due before `today` (a bill due today is not overdue; a bill with
	no due date is not), capped at `max(outstanding, 0)` where `outstanding` is the account's balance.
	`oldest_unpaid` is the earliest posting date among the bills with something outstanding, whatever
	their due date, or None."""
	today = getdate(today) if today else getdate(nowdate())
	total, oldest = Decimal(0), None
	for row in rows or []:
		amount = as_decimal(row.get("outstanding_amount"))
		if amount <= 0:
			continue
		due, posted = row.get("due_date"), row.get("posting_date")
		if due and getdate(due) < today:
			total += amount
		if posted and (oldest is None or getdate(posted) < oldest):
			oldest = getdate(posted)
	return _capped_overdue(total, outstanding, precision), oldest


def _figures_answer(overdue, oldest, precision):
	return {
		"overdue": format_money(overdue, precision),
		"oldest_unpaid": None if oldest is None else oldest.isoformat(),
	}


def overdue_of(customer, company, today=None, outstanding=None):
	"""{"overdue": decimal string, "oldest_unpaid": "YYYY-MM-DD" | None} of `customer`'s bills in
	`company` (`overdue_figures` over one query: submitted, not a return, something outstanding).
	`today` defaults to the site's date; `outstanding` to ERPNext's balance of the customer."""
	from erpnext.selling.doctype.customer.customer import get_customer_outstanding

	precision = money_precision()
	if outstanding is None:
		outstanding = as_decimal(get_customer_outstanding(customer, company))
	invoice = frappe.qb.DocType("Sales Invoice")
	rows = (
		frappe.qb.from_(invoice)
		.select(invoice.outstanding_amount, invoice.due_date, invoice.posting_date)
		.where(
			(invoice.customer == customer)
			& (invoice.company == company)
			& (invoice.docstatus == 1)
			& (invoice.is_return == 0)
			& (invoice.outstanding_amount > 0)
		)
		.run(as_dict=True)
	)
	return _figures_answer(*overdue_figures(rows, outstanding, precision, today), precision)


def overdue_by_customer(names, company, precision, outstanding=None, today=None):
	"""{customer: {"overdue", "oldest_unpaid"}} for every name in `names`, from one grouped query
	(`Sum` of what is outstanding on the bills due before `today`, `Min` posting date); the same
	answers as `overdue_of` for each. `outstanding` is {customer: balance}; a customer missing from
	it is read from ERPNext."""
	from erpnext.selling.doctype.customer.customer import get_customer_outstanding

	names = list(names)
	if not names:
		return {}
	today = getdate(today) if today else getdate(nowdate())
	invoice = frappe.qb.DocType("Sales Invoice")
	overdue_sum = Sum(Case().when(invoice.due_date < str(today), invoice.outstanding_amount).else_(0))
	found = {
		row["customer"]: row
		for row in (
			frappe.qb.from_(invoice)
			.select(invoice.customer, overdue_sum.as_("overdue"), Min(invoice.posting_date).as_("oldest"))
			.where(
				(invoice.customer.isin(names))
				& (invoice.company == company)
				& (invoice.docstatus == 1)
				& (invoice.is_return == 0)
				& (invoice.outstanding_amount > 0)
			)
			.groupby(invoice.customer)
			.run(as_dict=True)
		)
	}
	outstanding = outstanding or {}
	answers = {}
	for name in names:
		row = found.get(name)
		balance = outstanding.get(name)
		if balance is None:
			balance = as_decimal(get_customer_outstanding(name, company))
		total = as_decimal(row["overdue"]) if row else Decimal(0)
		oldest = getdate(row["oldest"]) if row and row["oldest"] else None
		answers[name] = _figures_answer(_capped_overdue(total, balance, precision), oldest, precision)
	return answers


def credit_status(customer, company):
	"""{customer, credit_limit, outstanding, available, over_limit, overdue, oldest_unpaid,
	credit_days, as_of} (money as decimal strings at the currency's precision). `credit_limit` and
	`available` are null for a customer with no limit; `available` is never negative; `over_limit`
	is the balance above the limit; `overdue`, `oldest_unpaid` (a date) and `credit_days` are the
	House Account phase 2 figures (`overdue_of`, `credit_terms.customer_terms`)."""
	from erpnext.selling.doctype.customer.customer import get_customer_outstanding

	from crenya_pos_api.sync.credit_terms import customer_terms

	precision = money_precision()
	limit = customer_credit_limit(customer, company)
	outstanding = quantize(get_customer_outstanding(customer, company), precision)
	over_limit = limit is not None and outstanding > quantize(limit, precision)
	available = None if limit is None else max(quantize(limit, precision) - outstanding, Decimal(0))
	return {
		"customer": customer,
		"credit_limit": None if limit is None else format_money(limit, precision),
		"outstanding": format_money(outstanding, precision),
		"available": None if available is None else format_money(available, precision),
		"over_limit": over_limit,
		**overdue_of(customer, company, outstanding=outstanding),
		"credit_days": customer_terms(customer, company)["credit_days"],
		"as_of": utc_now_iso(),
	}


def over_limit_note(status):
	"""The Sync Event note for a customer who ended above the limit (from a `credit_status`), else
	None."""
	if not status["over_limit"]:
		return None
	precision = money_precision()
	over = quantize(status["outstanding"], precision) - quantize(status["credit_limit"], precision)
	return f"Customer {status['customer']} is over the credit limit by {format_money(over, precision)}"


def credit_device_types():
	"""The device types that may take credit: "till" and every type an app lists in the
	`crenya_pos_credit_device_types` hook (read once per request; a bad hook is logged and ignored)."""
	from crenya_pos_api.sync import registry

	return registry._device_types(registry.CREDIT_DEVICE_TYPES_HOOK, frozenset({DEFAULT_DEVICE_TYPE}))


def is_credit_device(device_type):
	"""Whether a device of `device_type` (blank = the default "till") may take credit."""
	return (device_type or DEFAULT_DEVICE_TYPE) in credit_device_types()


# Customer fields of the House Account facility (setup/install.py CREDIT_CUSTOMER_FIELDS)
CREDIT_ENABLED_FIELD = "crenya_credit_enabled"
CREDIT_CHANGED_AT_FIELD = "crenya_credit_changed_at"
# a change of either is a change of the facility
FACILITY_FIELDS = (CREDIT_ENABLED_FIELD, "disabled")
# who may change a credit limit or switch a customer's House Account facility (P4)
CREDIT_EDITOR_ROLES = ("System Manager", "Accounts Manager")


def facility_editor_refusal():
	"""The words of a refused facility switch (the web method's and the Customer hook's)."""
	return frappe._("A House Account is switched on or off by a System Manager or an Accounts Manager")


def customer_flag_device_types():
	"""The device types whose customer records and credit status carry `credit_enabled`: every type
	an app lists in the `crenya_pos_credit_customer_flag_device_types` hook (read once per request; a
	bad hook is logged and ignored), never "till": a retail till keeps 0.13.2's answers."""
	from crenya_pos_api.sync import registry

	return registry._device_types(registry.CREDIT_CUSTOMER_FLAG_HOOK, frozenset()) - {DEFAULT_DEVICE_TYPE}


def is_customer_flag_device(device_type):
	"""Whether a device of `device_type` (blank = the default "till", which never is) gets
	`credit_enabled`."""
	return (device_type or DEFAULT_DEVICE_TYPE) in customer_flag_device_types()


def sends_credit_flag(ctx):
	"""Whether the device is told each customer's `credit_enabled` (pull and status): the gate of
	`credit_limit` (an outlet that sells on account, a device that may take credit) and a
	customer-flag device type."""
	return _credit_on(ctx) and is_credit_device(ctx.device_type) and is_customer_flag_device(ctx.device_type)


def credit_enabled_for(customer):
	"""Whether a Customer (a Document or a dict / row with `crenya_credit_enabled` and `disabled`) may
	be put on a House Account: the facility is on and the customer is not disabled."""
	return bool(cint(customer.get(CREDIT_ENABLED_FIELD))) and not cint(customer.get("disabled"))


# the grant flag (0.15.0, provider settlement NC4/NC5, R6): server code that creates a customer with
# the House Account on, after its own check of who granted it, sets
# `customer.flags[GRANT_FLAG] = "till"` (a customer extender, for the till's manager PIN) or "web"
# (the web's New customer). `doc.flags` is never filled from a request: Frappe drops a "flags" key
# of a document dict (BaseDocument reserved keywords) and the till's Customer payload is read field
# by field. The flag counts only on the insert of a new customer; an existing one stays P4 only.
GRANT_FLAG = "crenya_credit_grant"
TILL_GRANT = "till"
WEB_GRANT = "web"
GRANT_SOURCES = frozenset({TILL_GRANT, WEB_GRANT})


def _granted_on_create(doc):
	"""Whether server code granted the facility to this new customer (`GRANT_FLAG`): only inside
	`insert()` (Frappe's `flags.in_insert`, which a request cannot set) and with no saved version."""
	flags = (getattr(doc, "flags", None) or {}) if doc is not None else {}
	if doc is None or not flags.get("in_insert") or doc.get_doc_before_save() is not None:
		return False
	grant = flags.get(GRANT_FLAG)
	return isinstance(grant, str) and grant in GRANT_SOURCES


def _may_switch_facility(doc=None):
	"""Whether this save may change `crenya_credit_enabled`: Administrator, a System Manager or an
	Accounts Manager (P4), the app's own install, patch or migrate, or a new customer the server
	granted it to (`GRANT_FLAG`)."""
	flags = frappe.flags
	if flags.in_install or flags.in_patch or flags.in_migrate:
		return True
	if _granted_on_create(doc):
		return True
	user = frappe.session.user if getattr(frappe.local, "session", None) else None
	if user == "Administrator":
		return True
	return bool(set(frappe.get_roles(user)) & set(CREDIT_EDITOR_ROLES))


def stamp_credit_change(doc, method=None):
	"""Customer `validate`: refuse a change of `crenya_credit_enabled` (a new customer created
	enabled counts) by anyone but Administrator, a System Manager or an Accounts Manager (or server
	code that granted it to the new customer it creates, `GRANT_FLAG`), whatever the path (desk
	form, `frappe.client.set_value`, an import): Sales Users may write Customers, but only
	accountants switch a House Account (P4). A change of `disabled` stays open to every writer: it
	only ever turns the facility off. Then set `crenya_credit_changed_at` when `crenya_credit_enabled`
	or `disabled` changed (a new customer: when it is created enabled or disabled). The time is the
	save's own `modified` (Frappe sets it before `validate`), so it is the `modified` a till pulls with
	the changed record; any other edit leaves it as it was. A write that skips the save
	(`frappe.db.set_value`, `db_set`, SQL) is neither checked nor stamped: change the flag through a
	save (`save_credit_facility`)."""
	if not doc.meta.has_field(CREDIT_CHANGED_AT_FIELD):
		return
	before = doc.get_doc_before_save()
	if before is None:
		flag_changed = bool(cint(doc.get(CREDIT_ENABLED_FIELD)))
		changed = any(cint(doc.get(field)) for field in FACILITY_FIELDS)
	else:
		flag_changed = cint(doc.get(CREDIT_ENABLED_FIELD)) != cint(before.get(CREDIT_ENABLED_FIELD))
		changed = any(cint(doc.get(field)) != cint(before.get(field)) for field in FACILITY_FIELDS)
	if flag_changed and not _may_switch_facility(doc):
		frappe.throw(facility_editor_refusal(), frappe.PermissionError)
	if changed:
		doc.set(CREDIT_CHANGED_AT_FIELD, doc.modified or now_datetime())


def _require_credit_device(ctx):
	if not is_credit_device(ctx.device_type):
		raise_api_error(InvalidRequestError, f"Credit is for tills, not for {ctx.device_type} devices")


NOT_FOUND = "Not found"


SWITCHED_OFF = "Credit sales are switched off for this outlet"


def _credit_on(ctx):
	return bool(cint(ctx.profile.get(CREDIT_SALES_FIELD)))


def customer_in_outlet(ctx, customer):
	"""Whether `customer` exists and lies inside the outlet's customer groups (any, when it limits
	none)."""
	if not customer or not isinstance(customer, str) or not frappe.db.exists("Customer", customer):
		return False
	roots = [row.customer_group for row in ctx.profile.get("customer_groups") or []]
	if roots:
		from crenya_pos_api.sync.pull import _descendants

		group = frappe.db.get_value("Customer", customer, "customer_group")
		return group in _descendants("Customer Group", roots)
	return True


def status_for(ctx, customer):
	"""`credit_status` for a customer of the device's outlet (an existing customer inside its customer
	groups, when it limits them). One answer for a customer that is not there and one that is not the
	outlet's, so the call tells nothing about other customers. An outlet that no longer sells on
	account still answers for a customer who owes (they can pay, and an old on-account invoice can be
	returned); for any other it answers that credit is switched off. A customer-flag device of an
	outlet that sells on account also gets `credit_enabled` (`sends_credit_flag`)."""
	_require_credit_device(ctx)
	if not customer_in_outlet(ctx, customer):
		raise_api_error(InvalidRequestError, NOT_FOUND)
	status = credit_status(customer, ctx.company)
	if not _credit_on(ctx) and quantize(status["outstanding"], money_precision()) <= 0:
		raise_api_error(InvalidRequestError, SWITCHED_OFF)
	if sends_credit_flag(ctx):
		# a customer-flag device of an outlet that sells on account (restaurant D6): the facility
		# as the booking rule reads it
		row = frappe.db.get_value("Customer", customer, list(FACILITY_FIELDS), as_dict=True)
		status["credit_enabled"] = credit_enabled_for(row)
	return status


def invoice_settlements(invoice):
	"""How a submitted sale was settled after it was sold (CC12, R20): one row per submitted Receive
	Payment Entry that references it, oldest first: `[{payment_entry, mode_of_payment, mode_type,
	from_till, allocated}]` (`allocated` a decimal string at the outlet's precision, summed when an entry
	references the bill twice). `from_till` is true only for a till's `Customer Payment`: the entry
	carries the till's id and its remarks name the device; a web settlement or a desk entry is false.
	`mode_type` is the Mode of Payment's own `type` (Cash, Bank, ...)."""
	from crenya_pos_api.sync.customer_payment import LOCAL_ID_FIELD, PAYMENT_ENTRY, TILL_REMARKS_PREFIX

	refs = frappe.get_all(
		"Payment Entry Reference",
		filters={
			"parenttype": PAYMENT_ENTRY,
			"reference_doctype": "Sales Invoice",
			"reference_name": invoice,
		},
		fields=["parent", "allocated_amount"],
	)
	if not refs:
		return []
	allocated = {}
	for ref in refs:
		allocated[ref.parent] = allocated.get(ref.parent, Decimal(0)) + as_decimal(ref.allocated_amount)
	entries = frappe.get_all(
		PAYMENT_ENTRY,
		filters={"name": ["in", list(allocated)], "docstatus": 1, "payment_type": "Receive"},
		fields=["name", "mode_of_payment", LOCAL_ID_FIELD, "remarks"],
		order_by="posting_date asc, creation asc, name asc",
	)
	modes = [entry.mode_of_payment for entry in entries if entry.mode_of_payment]
	types = (
		{
			row.name: row.type
			for row in frappe.get_all(
				"Mode of Payment", filters={"name": ["in", modes]}, fields=["name", "type"]
			)
		}
		if modes
		else {}
	)
	precision = money_precision()
	return [
		{
			"payment_entry": entry.name,
			"mode_of_payment": entry.mode_of_payment,
			"mode_type": types.get(entry.mode_of_payment),
			"from_till": bool(entry.get(LOCAL_ID_FIELD))
			and (entry.remarks or "").startswith(TILL_REMARKS_PREFIX),
			"allocated": format_money(allocated[entry.name], precision),
		}
		for entry in entries
	]


def invoice_outstanding_for(ctx, invoice):
	"""{name, grand_total, outstanding, as_of, settlements} of a submitted sale of this outlet (by name
	or by the till's local id): what a return of it can credit to the account, and how it was settled
	after the sale (`invoice_settlements`). Answered whatever the outlet's credit switch says:
	invoices sold on account while it was on are returned after it is off."""
	_require_credit_device(ctx)
	if not invoice or not isinstance(invoice, str):
		raise_api_error(InvalidRequestError, NOT_FOUND)
	fields = ["name", "docstatus", "is_return", "company", "pos_profile", "grand_total", "outstanding_amount"]
	row = frappe.db.get_value("Sales Invoice", invoice, fields, as_dict=True) or frappe.db.get_value(
		"Sales Invoice", {"crenya_local_id": invoice}, fields, as_dict=True
	)
	if not row or row.docstatus != 1 or row.is_return:
		raise_api_error(InvalidRequestError, NOT_FOUND)
	if row.company != ctx.company or row.pos_profile != ctx.profile.name:
		raise_api_error(InvalidRequestError, NOT_FOUND)
	precision = money_precision()
	return {
		"name": row.name,
		"grand_total": format_money(row.grand_total, precision),
		"outstanding": format_money(row.outstanding_amount, precision),
		"as_of": utc_now_iso(),
		"settlements": invoice_settlements(row.name),
	}


# the rows of one `get_open_invoices` answer: the default, and the most "Show more" may ask for (Q5)
OPEN_INVOICES_DEFAULT_LIMIT = 200
OPEN_INVOICES_MAX_LIMIT = 1000


def customer_advance(customer, company):
	"""What the customer has paid ahead (Decimal, unrounded): the unallocated part of its submitted
	Receive Payment Entries of the company, plus the absolute value of its negative Sales Invoice
	outstandings there (credit left on the account, e.g. a House Account cancel of a settled bill,
	CC12), the latter in one aggregate query. A return posted with `update_outstanding_for_self` 0 (a
	credit offset) is booked against its original, which already carries the credit; ERPNext still
	stores a negative outstanding on the return itself with no ledger entry behind it, so it is left
	out. A credit note that keeps its own outstanding (`update_outstanding_for_self` 1) counts."""
	from frappe.query_builder.functions import Sum

	rows = frappe.get_all(
		"Payment Entry",
		filters={
			"party_type": "Customer",
			"party": customer,
			"company": company,
			"docstatus": 1,
			"payment_type": "Receive",
		},
		fields=["unallocated_amount"],
	)
	paid_ahead = sum((as_decimal(row.unallocated_amount) for row in rows), Decimal(0))
	invoice = frappe.qb.DocType("Sales Invoice")
	owed_back = (
		frappe.qb.from_(invoice)
		.select(Sum(invoice.outstanding_amount))
		.where(
			(invoice.customer == customer)
			& (invoice.company == company)
			& (invoice.docstatus == 1)
			& (invoice.outstanding_amount < 0)
			& ~((invoice.is_return == 1) & (invoice.update_outstanding_for_self == 0))
		)
		.run()
	)
	credit_notes = as_decimal(owed_back[0][0]) if owed_back and owed_back[0][0] is not None else Decimal(0)
	return paid_ahead + abs(credit_notes)


def open_invoices_answer(customer, company, precision, limit=OPEN_INVOICES_DEFAULT_LIMIT):
	"""The wire answer of `get_open_invoices` (money as decimal strings at `precision`): the customer's
	open bills of the company (all its outlets, Q1: the debt is the customer's, each row names its
	outlet) oldest first, at most `limit` (above 1000 cut to 1000), `more` when there are others;
	`outstanding` is ERPNext's net account balance (`get_customer_outstanding`, as `get_status`
	gives it), already net of the advance and not the sum of the rows: a screen shows it beside
	`advance` and never subtracts the advance again. `advance` is the unallocated receipts plus
	credit notes left on the account (RC9)."""
	from erpnext.selling.doctype.customer.customer import get_customer_outstanding

	from crenya_pos_api.sync.customer_payment import open_invoices

	limit = max(1, min(cint(limit) or OPEN_INVOICES_DEFAULT_LIMIT, OPEN_INVOICES_MAX_LIMIT))
	found = open_invoices(customer, company, limit + 1)
	return {
		"customer": customer,
		"outstanding": format_money(get_customer_outstanding(customer, company), precision),
		"advance": format_money(customer_advance(customer, company), precision),
		"as_of": utc_now_iso(),
		"more": len(found) > limit,
		"invoices": [
			{
				"name": row["name"],
				"local_id": row["local_id"] or None,
				"offline_number": row["offline_number"] or None,
				"outlet": row["pos_profile"],
				"posting_date": str(row["posting_date"]),
				"grand_total": format_money(row["total"], precision),
				"outstanding": format_money(row["outstanding"], precision),
			}
			for row in found[:limit]
		],
	}


def open_invoices_for(ctx, customer, limit=None):
	"""`open_invoices_answer` for a customer of the device's outlet: the device must be allowed
	credit and the outlet must sell on account; the walk-in and a customer that is not there or not
	the outlet's get one answer (no tell about other customers)."""
	_require_credit_device(ctx)
	if not _credit_on(ctx):
		raise_api_error(InvalidRequestError, SWITCHED_OFF)
	if not customer_in_outlet(ctx, customer) or customer == ctx.profile.customer:
		raise_api_error(InvalidRequestError, NOT_FOUND)
	return open_invoices_answer(
		customer, ctx.company, money_precision(ctx.currency), cint(limit) or OPEN_INVOICES_DEFAULT_LIMIT
	)


def _install_waiver():
	"""Wrap ERPNext's `check_credit_limit` once. Sales Invoice imports it inside its own
	`check_credit_limit` at call time (v15 and v16), so the module attribute is the one it calls."""
	import erpnext.selling.doctype.customer.customer as customer_module

	original = customer_module.check_credit_limit
	if getattr(original, WRAPPED_MARK, False):
		return

	def check_credit_limit(*args, **kwargs):
		if getattr(frappe.local, WAIVER_FLAG, False):
			return None
		return original(*args, **kwargs)

	setattr(check_credit_limit, WRAPPED_MARK, True)
	customer_module.check_credit_limit = check_credit_limit


@contextmanager
def credit_limit_waived():
	"""ERPNext's credit limit check is skipped while the block runs, for this request only: the flag
	lives in `frappe.local` (one request, one thread) and is cleared when the block ends, however it
	ends. A desk user submitting the same invoice is still refused."""
	_install_waiver()
	before = getattr(frappe.local, WAIVER_FLAG, False)
	setattr(frappe.local, WAIVER_FLAG, True)
	try:
		yield
	finally:
		setattr(frappe.local, WAIVER_FLAG, before)
