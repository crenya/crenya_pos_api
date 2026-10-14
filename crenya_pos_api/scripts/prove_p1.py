"""Retail P1 Task 0: proofs of the ERPNext assumptions behind credit sales and retail expenses.

Run on a throwaway CI bench only (never a live site):

	bench --site test_site execute crenya_pos_api.scripts.prove_p1.run

Every step writes real documents (as the device user where the till would), prints one
`PROOF <id> PASS|FAIL key=value ...` line with the observed values (OMR, 3 decimals), and rolls its
documents back. Only the shared masters of tests/fixtures.py are committed. Not shipped in the
app's hooks; kept as the evidence of the plan's Task 0 (docs/specs/2026-10-14-retail-p1-proofs.md).
"""

import traceback
import uuid
from contextlib import contextmanager

import frappe
from frappe.utils import add_days, flt, nowdate

from crenya_pos_api.tests import fixtures as fx

RESULTS = []


def m(value):
	return f"{flt(value):.3f}"


def report(step, ok, **values):
	RESULTS.append((step, ok))
	detail = " ".join(f"{k}={v}" for k, v in values.items())
	print(f"PROOF {step} {'PASS' if ok else 'FAIL'} {detail}", flush=True)


def guarded(step):
	"""Run a step; an exception is a FAIL with its message, and the step's documents are rolled back."""

	def deco(fn):
		def run_step(*args, **kwargs):
			try:
				fn(*args, **kwargs)
			except Exception as e:
				first = (str(e) or repr(e)).replace("\n", " ")[:300]
				report(step, False, error=f"{type(e).__name__}: {first}")
				traceback.print_exc()
			finally:
				frappe.set_user("Administrator")
				frappe.db.rollback()

		return run_step

	return deco


def attempt(fn):
	"""Run fn inside a savepoint; (result, None) or (None, message) with the attempt rolled back."""
	frappe.db.savepoint("attempt")
	try:
		return fn(), None
	except Exception as e:
		frappe.db.rollback(save_point="attempt")
		frappe.set_user("Administrator")
		return None, (str(e) or repr(e)).replace("\n", " ")[:200]


# ---- the throwaway waiver (a throwaway version of Task 2's credit_limit_waived) ----


@contextmanager
def credit_limit_waived():
	import erpnext.selling.doctype.customer.customer as customer_module

	if not getattr(customer_module.check_credit_limit, "_crenya_wrapped", False):
		original = customer_module.check_credit_limit

		def wrapper(*args, **kwargs):
			if getattr(frappe.local, "crenya_credit_waived", False):
				return None
			return original(*args, **kwargs)

		wrapper._crenya_wrapped = True
		customer_module.check_credit_limit = wrapper
	frappe.local.crenya_credit_waived = True
	try:
		yield
	finally:
		frappe.local.crenya_credit_waived = False


# ---- builders ----


class Ctx:
	profile = None
	company = None
	cash_account = None
	device_user = None


def customer(name, limit=None, loyalty=None):
	cust = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": f"_TC P1 {name} {uuid.uuid4().hex[:6]}",
			"customer_group": fx.leaf_customer_group(),
			"territory": frappe.utils.nestedset.get_root_of("Territory"),
			"loyalty_program": loyalty,
		}
	)
	if limit:
		cust.append("credit_limits", {"company": fx.COMPANY, "credit_limit": limit})
	return cust.insert(ignore_permissions=True).name


def sales_invoice(cust, pos, paid=None, qty=10, rate=10, days_ago=0, ret_against=None, loyalty=None, **extra):
	"""A Sales Invoice built like the till's: is_pos with payment rows, or on account (is_pos=0)."""
	date = add_days(nowdate(), -days_ago)
	doc = frappe.new_doc("Sales Invoice")
	doc.update(
		{
			"customer": cust,
			"company": fx.COMPANY,
			"currency": fx.CURRENCY,
			"posting_date": date,
			"set_posting_time": 1,
			"posting_time": "12:00:00",
			"due_date": date,
			"selling_price_list": fx.PRICE_LIST,
			"ignore_pricing_rule": 1,
			"update_stock": 1,
			"cost_center": fx.COST_CENTER,
			"disable_rounded_total": 1,
			"taxes_and_charges": fx.TAX_TEMPLATE,
			"is_pos": 1 if pos else 0,
			"loyalty_program": loyalty,
		}
	)
	if pos:
		doc.pos_profile = Ctx.profile.name
		doc.account_for_change_amount = Ctx.cash_account
		doc.write_off_account = Ctx.profile.write_off_account
		doc.write_off_cost_center = fx.COST_CENTER
	doc.append("items", {"item_code": fx.MILK, "qty": qty, "rate": rate, "warehouse": fx.WAREHOUSE})
	template = frappe.get_doc("Sales Taxes and Charges Template", fx.TAX_TEMPLATE)
	for row in template.taxes:
		doc.append(
			"taxes",
			{
				k: row.get(k)
				for k in ("charge_type", "account_head", "description", "rate", "included_in_print_rate", "cost_center")
			},
		)
	if ret_against:
		doc.is_return = 1
		doc.return_against = ret_against
		doc.update_outstanding_for_self = 0
	if paid is not None:
		doc.append("payments", {"mode_of_payment": fx.CASH, "amount": paid, "account": Ctx.cash_account})
	doc.update(extra)
	return doc


def post(doc, user=None):
	"""Insert and submit as `user` (the device user, as the sync does), permissions ignored after the
	device check like `submit_invoice`."""
	if user:
		frappe.set_user(user)
	try:
		doc.insert(ignore_permissions=True)
		doc.submit()
	finally:
		frappe.set_user("Administrator")
	return doc


def outstanding(name):
	return frappe.db.get_value("Sales Invoice", name, "outstanding_amount")


def gl(voucher_type, name):
	return sorted(
		(r.account, flt(r.debit, 3), flt(r.credit, 3))
		for r in frappe.get_all(
			"GL Entry",
			{"voucher_type": voucher_type, "voucher_no": name, "is_cancelled": 0},
			["account", "debit", "credit"],
		)
	)


def sle(name):
	return sorted(
		(r.item_code, r.warehouse, flt(r.actual_qty, 3))
		for r in frappe.get_all(
			"Stock Ledger Entry",
			{"voucher_type": "Sales Invoice", "voucher_no": name, "is_cancelled": 0},
			["item_code", "warehouse", "actual_qty"],
		)
	)


def tax_gl(name):
	return [e for e in gl("Sales Invoice", name) if e[0] == fx.VAT_ACCOUNT]


# ---- setup ----


def setup():
	frappe.set_user("Administrator")
	frappe.db.set_single_value("System Settings", "currency_precision", "3")
	frappe.db.set_default("currency_precision", "3")
	frappe.clear_cache()
	profile = fx.setup_fixtures()
	company = frappe.get_doc("Company", fx.COMPANY)
	Ctx.profile = profile
	Ctx.company = company
	Ctx.cash_account = company.default_cash_account or f"Cash - {fx.ABBR}"
	from crenya_pos_api.sync.device_user import ensure_device_user

	Ctx.device_user = ensure_device_user(str(uuid.uuid4()), "Administrator")
	frappe.db.commit()
	roles = sorted(frappe.get_roles(Ctx.device_user))
	print(f"SETUP frappe={frappe.__version__} device_user={Ctx.device_user} roles={roles}", flush=True)
	print(
		"SETUP erpnext=%s precision=%s"
		% (
			__import__("erpnext").__version__,
			frappe.db.get_single_value("System Settings", "currency_precision"),
		),
		flush=True,
	)


# ---- steps ----


@guarded("1")
def step1_shapes():
	cust = customer("shapes", limit=1000)
	cash = post(sales_invoice(customer("cashref"), pos=True, paid=100), Ctx.device_user)
	a = post(sales_invoice(cust, pos=True, paid=40), Ctx.device_user)
	b = post(sales_invoice(cust, pos=False), Ctx.device_user)
	from erpnext.accounts.utils import get_balance_on  # noqa: F401
	from erpnext.selling.doctype.customer.customer import get_customer_outstanding

	cust_out = get_customer_outstanding(cust, fx.COMPANY)
	oa, ob = outstanding(a.name), outstanding(b.name)
	same_tax = tax_gl(a.name) == tax_gl(cash.name) == tax_gl(b.name)
	same_stock = sle(a.name) == sle(cash.name) == sle(b.name)
	ok = flt(oa, 3) == 60 and flt(ob, 3) == 100 and same_tax and same_stock
	report(
		"1a",
		flt(oa, 3) == 60,
		invoice="is_pos=1 paid 40/100",
		outstanding=m(oa),
		status=a.status,
		due_date=a.due_date,
		grand_total=m(a.grand_total),
	)
	report(
		"1b",
		flt(ob, 3) == 100,
		invoice="is_pos=0 on account 100",
		outstanding=m(ob),
		status=b.status,
		due_date=b.due_date,
		grand_total=m(b.grand_total),
	)
	report(
		"1c",
		same_tax and same_stock,
		tax_gl_a=tax_gl(a.name),
		tax_gl_cash=tax_gl(cash.name),
		tax_gl_b=tax_gl(b.name),
		sle_a=sle(a.name),
		sle_b=sle(b.name),
		sle_cash=sle(cash.name),
	)
	report(
		"1d",
		flt(cust_out, 3) == 160,
		get_customer_outstanding=m(cust_out),
		gl_a=gl("Sales Invoice", a.name),
		gl_b=gl("Sales Invoice", b.name),
	)
	report("1", ok and flt(cust_out, 3) == 160)


def _crossing(cust, user, expect_error=True):
	"""Submit an invoice taking `cust` over the limit; return the error text or None."""
	return attempt(lambda: post(sales_invoice(cust, pos=False), user))[1]


@guarded("2")
def step2_limit():
	# the customer holds a limit of 150; an Administrator-posted invoice of 100 sits on account
	cust = customer("limit", limit=150)
	post(sales_invoice(cust, pos=False), None)
	roles = sorted(frappe.get_roles(Ctx.device_user))
	has_cc = "Credit Controller" in roles
	cc_setting = frappe.db.get_single_value("Accounts Settings", "credit_controller")

	refusal = _crossing(cust, Ctx.device_user)
	report(
		"2a",
		bool(refusal) and "Credit Limit" in refusal,
		baseline="device user over limit, no waiver",
		refused=bool(refusal),
		message=refusal,
		device_has_credit_controller=has_cc,
		accounts_settings_credit_controller=cc_setting,
	)

	def waived_sale():
		with credit_limit_waived():
			return post(sales_invoice(cust, pos=False), Ctx.device_user)

	doc, err = attempt(waived_sale)
	waived = doc.name if doc else None
	report("2b", bool(waived), waiver="over-limit credit sale inside the waiver", posted=waived, error=err)

	# the customer is now over the limit: a cash sale to them
	over = flt(frappe.db.get_value("Sales Invoice", waived, "grand_total"), 3) if waived else 0
	cash_refused = _crossing_cash(cust, Ctx.device_user)
	report(
		"2c",
		bool(cash_refused),
		baseline="cash sale (paid 20) to an already over-limit customer, no waiver",
		refused=bool(cash_refused),
		message=cash_refused,
	)
	def waived_cash():
		with credit_limit_waived():
			return post(sales_invoice(cust, pos=True, paid=20, qty=2), Ctx.device_user)

	doc, err = attempt(waived_cash)
	report("2d", bool(doc), waiver="cash sale to over-limit customer inside the waiver", posted=doc and doc.name, error=err)

	leaked = _crossing(cust, Ctx.device_user)
	report(
		"2e",
		bool(leaked),
		check="next request without the flag is refused again (waiver not leaked)",
		refused=bool(leaked),
		message=leaked,
	)

	# fallback: the device user is given the Accounts Settings Credit Controller role
	try:
		if not frappe.db.exists("Role", "_TC Credit Controller"):
			frappe.get_doc({"doctype": "Role", "role_name": "_TC Credit Controller", "desk_access": 0}).insert(
				ignore_permissions=True
			)
		frappe.db.set_single_value("Accounts Settings", "credit_controller", "_TC Credit Controller")
		user = frappe.get_doc("User", Ctx.device_user)
		user.append("roles", {"role": "_TC Credit Controller"})
		user.flags.ignore_permissions = True
		user.save(ignore_permissions=True)
		frappe.clear_cache(user=Ctx.device_user)
		doc = post(sales_invoice(cust, pos=False), Ctx.device_user)
		report("2f", True, fallback="Credit Controller role on the device user, no waiver", posted=doc.name)
	except Exception as e:
		report("2f", False, fallback="Credit Controller role", error=str(e).replace("\n", " ")[:200])


def _crossing_cash(cust, user):
	return attempt(lambda: post(sales_invoice(cust, pos=True, paid=20, qty=2), user))[1]


def _receive(cust, amount):
	"""A Payment Entry (Receive) built like the server will: references to the oldest outstanding
	documents from get_outstanding_reference_documents, the rest unallocated."""
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_outstanding_reference_documents
	from erpnext.accounts.party import get_party_account

	party_account = get_party_account("Customer", cust, fx.COMPANY)
	rows = get_outstanding_reference_documents(
		{
			"party_type": "Customer",
			"party": cust,
			"company": fx.COMPANY,
			"party_account": party_account,
			"posting_date": nowdate(),
			"company_currency": fx.CURRENCY,
			"party_account_currency": fx.CURRENCY,
			"get_outstanding_invoices": 1,
		}
	)
	rows = sorted(rows, key=lambda r: (str(r.get("posting_date")), r.get("voucher_no")))
	pe = frappe.new_doc("Payment Entry")
	pe.update(
		{
			"payment_type": "Receive",
			"party_type": "Customer",
			"party": cust,
			"company": fx.COMPANY,
			"posting_date": nowdate(),
			"mode_of_payment": fx.CASH,
			"paid_from": party_account,
			"paid_to": Ctx.cash_account,
			"paid_amount": amount,
			"received_amount": amount,
			"source_exchange_rate": 1,
			"target_exchange_rate": 1,
		}
	)
	left = flt(amount, 3)
	for r in rows:
		take = min(left, flt(r.get("outstanding_amount"), 3))
		if take <= 0:
			continue
		pe.append(
			"references",
			{
				"reference_doctype": r.get("voucher_type"),
				"reference_name": r.get("voucher_no"),
				"total_amount": r.get("invoice_amount"),
				"outstanding_amount": r.get("outstanding_amount"),
				"allocated_amount": take,
				"due_date": r.get("due_date"),
			},
		)
		left = flt(left - take, 3)
	pe.insert(ignore_permissions=True)
	pe.submit()
	return pe, rows


@guarded("3")
def step3_receipt():
	cust = customer("receipt", limit=1000)
	a = post(sales_invoice(cust, pos=True, paid=40, days_ago=1), Ctx.device_user)  # outstanding 60.000
	b = post(sales_invoice(cust, pos=False, qty=5), Ctx.device_user)  # outstanding 50.000
	before = (m(outstanding(a.name)), m(outstanding(b.name)))
	pe, rows = _receive(cust, 70)
	after = (m(outstanding(a.name)), m(outstanding(b.name)))
	order = [r.reference_name for r in pe.references]
	ok = (
		after == ("0.000", "40.000")
		and order == [a.name, b.name]
		and flt(pe.unallocated_amount, 3) == 0
	)
	report(
		"3a",
		ok,
		case="70.000 against 60.000 (is_pos partial, oldest) + 50.000 (on account)",
		before=before,
		after=after,
		allocated=[(r.reference_name, m(r.allocated_amount)) for r in pe.references],
		unallocated=m(pe.unallocated_amount),
		payment_entry=pe.name,
		oldest_first=order == [a.name, b.name],
	)
	cust2 = customer("advance", limit=1000)
	c = post(sales_invoice(cust2, pos=False, qty=2.5), Ctx.device_user)  # 25.000
	pe2, _ = _receive(cust2, 40)
	ok2 = flt(outstanding(c.name), 3) == 0 and flt(pe2.unallocated_amount, 3) == 15
	report(
		"3b",
		ok2,
		case="40.000 against a 25.000 debt",
		invoice_outstanding=m(outstanding(c.name)),
		allocated=m(sum(flt(r.allocated_amount) for r in pe2.references)),
		unallocated_advance=m(pe2.unallocated_amount),
		payment_entry=pe2.name,
	)
	report("3", ok and ok2)


@guarded("4")
def step4_return():
	cust = customer("return", limit=1000)
	# (a) all credited: no payment rows
	orig_a = post(sales_invoice(cust, pos=True, paid=40), Ctx.device_user)
	before = outstanding(orig_a.name)
	frappe.db.savepoint("step4a")
	try:
		ret = post(
			sales_invoice(cust, pos=True, qty=-3, ret_against=orig_a.name), Ctx.device_user
		)
		report(
			"4a",
			flt(before, 3) - flt(outstanding(orig_a.name), 3) == 30,
			case="return 30.000, is_pos=1, no payment rows, update_outstanding_for_self=0",
			original_before=m(before),
			original_after=m(outstanding(orig_a.name)),
			return_doc=ret.name,
			return_outstanding=m(ret.outstanding_amount),
			return_paid=m(ret.paid_amount),
		)
	except Exception as e:
		frappe.db.rollback(save_point="step4a")
		report("4a", False, case="is_pos=1 return without payment rows", error=str(e).replace("\n", " ")[:200])
		# the shape the plan falls back to: is_pos=0
		orig_a = post(sales_invoice(cust, pos=True, paid=40), Ctx.device_user)
		before = outstanding(orig_a.name)
		ret = post(sales_invoice(cust, pos=False, qty=-3, ret_against=orig_a.name), Ctx.device_user)
		report(
			"4a-fallback",
			flt(before, 3) - flt(outstanding(orig_a.name), 3) == 30,
			case="return 30.000 as is_pos=0, update_outstanding_for_self=0",
			original_before=m(before),
			original_after=m(outstanding(orig_a.name)),
			return_doc=ret.name,
		)
	# (b) partial refund 10.000
	orig_b = post(sales_invoice(cust, pos=True, paid=40), Ctx.device_user)
	before_b = outstanding(orig_b.name)
	ret_b = post(
		sales_invoice(cust, pos=True, paid=-10, qty=-3, ret_against=orig_b.name), Ctx.device_user
	)
	drop = flt(before_b, 3) - flt(outstanding(orig_b.name), 3)
	report(
		"4b",
		drop == 20,
		case="return 30.000 with a 10.000 refund row",
		original_before=m(before_b),
		original_after=m(outstanding(orig_b.name)),
		reduced_by=m(drop),
		return_doc=ret_b.name,
		return_paid=m(ret_b.paid_amount),
		return_outstanding=m(ret_b.outstanding_amount),
		return_gl=gl("Sales Invoice", ret_b.name),
	)


def _points(name):
	return [
		(r.loyalty_points, flt(r.purchase_amount, 3))
		for r in frappe.get_all(
			"Loyalty Point Entry",
			{"invoice_type": "Sales Invoice", "invoice": name},
			["loyalty_points", "purchase_amount"],
		)
	]


@guarded("5")
def step5_loyalty():
	cust = customer("loyalty", limit=1000, loyalty=fx.LOYALTY_PROGRAM)
	paid = post(
		sales_invoice(cust, pos=True, paid=100, loyalty=fx.LOYALTY_PROGRAM), Ctx.device_user
	)
	partial = post(
		sales_invoice(cust, pos=True, paid=40, loyalty=fx.LOYALTY_PROGRAM), Ctx.device_user
	)
	credit = post(
		sales_invoice(cust, pos=False, loyalty=fx.LOYALTY_PROGRAM), Ctx.device_user
	)
	pp, pt, pc = _points(paid.name), _points(partial.name), _points(credit.name)
	report(
		"5",
		bool(pp),
		program=fx.LOYALTY_PROGRAM,
		paid_100=pp,
		partial_40_of_100=pt,
		on_account_100=pc,
		earned_on_unpaid_part=bool(pc) and (not pt or sum(p for p, _ in pc) == sum(p for p, _ in pp)),
		note="PASS means the cash invoice earned; the observed lists decide section 5.7 (points on the unpaid part)",
	)


@guarded("6")
def step6_expenses():
	profile = Ctx.profile
	cash = next(
		(
			p.mode_of_payment
			for p in profile.payments
			if frappe.db.get_value("Mode of Payment", p.mode_of_payment, "type") == "Cash"
		),
		None,
	)
	acct = cash and frappe.db.get_value(
		"Mode of Payment Account", {"parent": cash, "company": profile.company}, "default_account"
	)
	company = frappe.get_doc("Company", profile.company)
	payable = company.default_payable_account
	expense = company.default_expense_account
	tax = frappe.db.get_value("Purchase Taxes and Charges Template", {"company": profile.company}, "name")
	uom = frappe.db.get_single_value("Stock Settings", "stock_uom")
	supplier = frappe.db.get_value("Supplier", {"disabled": 0}, "name")
	missing = [
		k
		for k, v in {
			"cash mode": cash,
			"cash account": acct,
			"payable": payable,
			"expense account": expense,
			"purchase tax template": tax,
			"stock uom": uom,
			"supplier": supplier,
		}.items()
		if not v
	]
	print(f"STEP6 profile={profile.name} retail(no crenya_restaurant_mode)=True missing_on_bare_ci_site={missing}")
	# the bare CI site has no supplier and no purchase VAT template: make them (rolled back) so the
	# posting itself is proven; on a customer site the setup check reports them (restaurant Task 4)
	if not supplier:
		supplier = frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": "_TC P1 Cash Supplier",
				"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
				or frappe.db.get_value("Supplier Group", {}, "name"),
			}
		).insert(ignore_permissions=True).name
	if not tax:
		tax = frappe.get_doc(
			{
				"doctype": "Purchase Taxes and Charges Template",
				"title": "_TC P1 Input VAT 5%",
				"company": profile.company,
				"taxes": [
					{
						"category": "Total",
						"add_deduct_tax": "Add",
						"charge_type": "On Net Total",
						"account_head": fx.VAT_ACCOUNT,
						"description": "VAT 5%",
						"rate": 5,
					}
				],
			}
		).insert(ignore_permissions=True).name
	pi = frappe.new_doc("Purchase Invoice")
	pi.update(
		{
			"supplier": supplier,
			"company": profile.company,
			"posting_date": nowdate(),
			"set_posting_time": 1,
			"is_paid": 1,
			"mode_of_payment": cash,
			"cash_bank_account": acct,
			"cost_center": profile.cost_center,
			"disable_rounded_total": 1,
			"taxes_and_charges": tax,
			"update_stock": 0,
		}
	)
	pi.append(
		"items",
		{
			"item_name": "Probe category",
			"description": "probe",
			"uom": uom or "Nos",
			"conversion_factor": 1,
			"qty": 1,
			"rate": 10,
			"expense_account": expense,
		},
	)
	row = frappe.get_doc("Purchase Taxes and Charges Template", tax).taxes[0]
	pi.append(
		"taxes",
		{
			"charge_type": "Actual",
			"account_head": row.account_head,
			"description": row.description,
			"tax_amount": 0.5,
			"category": "Total",
			"add_deduct_tax": "Add",
		},
	)
	pi.run_method("set_missing_values")
	pi.run_method("calculate_taxes_and_totals")
	pi.paid_amount = flt(pi.grand_total)
	pi.insert(ignore_permissions=True)
	pi.submit()
	entries = gl("Purchase Invoice", pi.name)
	cash_credit = sum(c for a_, d, c in entries if a_ == acct)
	exp_debit = sum(d for a_, d, c in entries if a_ == expense)
	vat_debit = sum(d for a_, d, c in entries if a_ == row.account_head)
	report(
		"6",
		m(pi.grand_total) == "10.500" and m(cash_credit) == "10.500" and m(exp_debit) == "10.000" and m(vat_debit) == "0.500",
		grand_total=m(pi.grand_total),
		cash_credit=m(cash_credit),
		expense_debit=m(exp_debit),
		input_vat_debit=m(vat_debit),
		item_less_line_accepted=True,
		missing_before_setup=missing,
		gl=entries,
	)


def run():
	setup()
	for step in (step1_shapes, step2_limit, step3_receipt, step4_return, step5_loyalty, step6_expenses):
		step()
	frappe.db.rollback()
	print("SUMMARY " + " ".join(f"{s}={'PASS' if ok else 'FAIL'}" for s, ok in RESULTS), flush=True)
