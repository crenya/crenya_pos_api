"""Idempotent ERPNext master data for the integration tests (Oman: OMR, 5 % inclusive VAT)."""

import frappe
from frappe.utils import add_days, getdate, nowdate

COMPANY = "_Test Crenya POS Co"
ABBR = "_TCP"
CURRENCY = "OMR"
WAREHOUSE = f"Stores - {ABBR}"
COST_CENTER = f"Main - {ABBR}"
VAT_ACCOUNT = f"VAT 5% - {ABBR}"
TAX_TEMPLATE_TITLE = "_Test Crenya VAT 5%"
TAX_TEMPLATE = f"{TAX_TEMPLATE_TITLE} - {ABBR}"
ZERO_TEMPLATE_TITLE = "_Test Crenya Zero Rated"
ZERO_TEMPLATE = f"{ZERO_TEMPLATE_TITLE} - {ABBR}"
# further Sales Taxes and Charges Templates a till may switch to (or must not see)
EXCLUSIVE_TEMPLATE_TITLE = "_Test Crenya VAT 5% exclusive"
EXCLUSIVE_TEMPLATE = f"{EXCLUSIVE_TEMPLATE_TITLE} - {ABBR}"
ZERO_SALES_TEMPLATE_TITLE = "_Test Crenya VAT 0%"
ZERO_SALES_TEMPLATE = f"{ZERO_SALES_TEMPLATE_TITLE} - {ABBR}"
ACTUAL_TEMPLATE_TITLE = "_Test Crenya VAT + Delivery"
ACTUAL_TEMPLATE = f"{ACTUAL_TEMPLATE_TITLE} - {ABBR}"
LOYALTY_PROGRAM = "_Test Crenya Loyalty"
LOYALTY_ACCOUNT = f"_Test Crenya Loyalty Redemption - {ABBR}"
LOYALTY_CONVERSION_FACTOR = 0.01
PRICE_LIST = "_Test Crenya Selling"
ITEM_GROUP_ROOT = "_Test Crenya Groceries"
ITEM_GROUP_LEAF = "_Test Crenya Dairy"
ITEM_GROUP_OUTSIDE = "_Test Crenya Not On Till"
CUSTOMER = "_Test Crenya Walk-in"
POS_PROFILE = "_Test Crenya POS Profile"
MILK = "_TC-MILK-1L"
BREAD = "_TC-BREAD"
CASH = "Cash"


def _insert(doc):
	return frappe.get_doc(doc).insert(ignore_permissions=True)


def ensure_company():
	if not frappe.db.exists("Company", COMPANY):
		_insert(
			{
				"doctype": "Company",
				"company_name": COMPANY,
				"abbr": ABBR,
				"default_currency": CURRENCY,
				"country": "Oman",
				"chart_of_accounts": "Standard",
			}
		)
	return frappe.get_doc("Company", COMPANY)


def ensure_fiscal_year(date):
	from erpnext.accounts.utils import FiscalYearError, get_fiscal_year

	try:
		get_fiscal_year(date, company=COMPANY)
		return
	except FiscalYearError:
		pass
	year = getdate(date).year
	_insert(
		{
			"doctype": "Fiscal Year",
			"year": f"_Test Crenya {year}",
			"year_start_date": f"{year}-01-01",
			"year_end_date": f"{year}-12-31",
			"companies": [{"company": COMPANY}],
		}
	)


def ensure_accounts(company):
	if not frappe.db.exists("Account", VAT_ACCOUNT):
		_insert(
			{
				"doctype": "Account",
				"account_name": "VAT 5%",
				"parent_account": f"Duties and Taxes - {ABBR}",
				"company": COMPANY,
				"account_type": "Tax",
				"tax_rate": 5,
				"account_currency": CURRENCY,
			}
		)

	if not frappe.db.exists("Mode of Payment", CASH):
		_insert({"doctype": "Mode of Payment", "mode_of_payment": CASH, "type": "Cash", "enabled": 1})
	if not frappe.db.exists("Mode of Payment Account", {"parent": CASH, "company": COMPANY}):
		mode = frappe.get_doc("Mode of Payment", CASH)
		mode.append(
			"accounts",
			{"company": COMPANY, "default_account": company.default_cash_account or f"Cash - {ABBR}"},
		)
		mode.save(ignore_permissions=True)


def _vat_row(rate=5, included=1):
	return {
		"charge_type": "On Net Total",
		"account_head": VAT_ACCOUNT,
		"description": f"VAT {rate}%",
		"rate": rate,
		"included_in_print_rate": included,
		"cost_center": COST_CENTER,
	}


def ensure_sales_tax_template(name, title, rows):
	if not frappe.db.exists("Sales Taxes and Charges Template", name):
		_insert(
			{"doctype": "Sales Taxes and Charges Template", "title": title, "company": COMPANY, "taxes": rows}
		)


def ensure_taxes():
	ensure_sales_tax_template(TAX_TEMPLATE, TAX_TEMPLATE_TITLE, [_vat_row()])
	ensure_sales_tax_template(EXCLUSIVE_TEMPLATE, EXCLUSIVE_TEMPLATE_TITLE, [_vat_row(included=0)])
	ensure_sales_tax_template(ZERO_SALES_TEMPLATE, ZERO_SALES_TEMPLATE_TITLE, [_vat_row(rate=0)])
	# an Actual charge cannot be computed offline: never offered to the till
	ensure_sales_tax_template(
		ACTUAL_TEMPLATE,
		ACTUAL_TEMPLATE_TITLE,
		[
			_vat_row(),
			{
				"charge_type": "Actual",
				"account_head": VAT_ACCOUNT,
				"description": "Delivery",
				"tax_amount": 1,
				"cost_center": COST_CENTER,
			},
		],
	)
	if not frappe.db.exists("Item Tax Template", ZERO_TEMPLATE):
		_insert(
			{
				"doctype": "Item Tax Template",
				"title": ZERO_TEMPLATE_TITLE,
				"company": COMPANY,
				"taxes": [{"tax_type": VAT_ACCOUNT, "tax_rate": 0}],
			}
		)


def ensure_item_group(name, parent, is_group):
	if not frappe.db.exists("Item Group", name):
		_insert(
			{
				"doctype": "Item Group",
				"item_group_name": name,
				"parent_item_group": parent,
				"is_group": is_group,
			}
		)


def leaf_customer_group():
	name = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
	if name:
		return name
	from frappe.utils.nestedset import get_root_of

	_insert(
		{
			"doctype": "Customer Group",
			"customer_group_name": "_Test Crenya Customers",
			"parent_customer_group": get_root_of("Customer Group"),
			"is_group": 0,
		}
	)
	return "_Test Crenya Customers"


def ensure_item(item_code, rate, item_group=ITEM_GROUP_LEAF, zero_rated=False, is_stock_item=1):
	if not frappe.db.exists("Item", item_code):
		doc = {
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"item_group": item_group,
			"stock_uom": "Nos",
			"is_stock_item": is_stock_item,
			"is_sales_item": 1,
			"valuation_rate": 0.3,
			"item_defaults": [{"company": COMPANY, "default_warehouse": WAREHOUSE}],
			"barcodes": [{"barcode": f"{item_code}-EAN"}],
		}
		if zero_rated:
			doc["taxes"] = [{"item_tax_template": ZERO_TEMPLATE}]
		_insert(doc)
	if not frappe.db.exists("Item Price", {"item_code": item_code, "price_list": PRICE_LIST}):
		_insert(
			{
				"doctype": "Item Price",
				"item_code": item_code,
				"price_list": PRICE_LIST,
				"price_list_rate": rate,
				"currency": CURRENCY,
			}
		)


def ensure_stock(item_code, qty=1000):
	from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

	actual = frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": WAREHOUSE}, "actual_qty") or 0
	if actual >= qty / 2:
		return
	make_stock_entry(
		item_code=item_code,
		to_warehouse=WAREHOUSE,
		qty=qty,
		rate=0.3,
		company=COMPANY,
		posting_date=add_days(nowdate(), -1),
		posting_time="00:00:01",
	)


def ensure_loyalty_program():
	if not frappe.db.exists("Account", LOYALTY_ACCOUNT):
		_insert(
			{
				"doctype": "Account",
				"account_name": "_Test Crenya Loyalty Redemption",
				"parent_account": f"Indirect Expenses - {ABBR}",
				"company": COMPANY,
				"account_currency": CURRENCY,
			}
		)
	if not frappe.db.exists("Loyalty Program", LOYALTY_PROGRAM):
		_insert(
			{
				"doctype": "Loyalty Program",
				"loyalty_program_name": LOYALTY_PROGRAM,
				"loyalty_program_type": "Single Tier Program",
				"from_date": add_days(nowdate(), -30),
				"auto_opt_in": 0,
				"company": COMPANY,
				# 1 point per OMR spent, 1 point = 0.010 OMR
				"collection_rules": [{"tier_name": "Standard", "collection_factor": 1, "min_spent": 0}],
				"conversion_factor": LOYALTY_CONVERSION_FACTOR,
				"expiry_duration": 365,
				"expense_account": LOYALTY_ACCOUNT,
				"cost_center": COST_CENTER,
			}
		)


def make_customer(customer_name, loyalty_program=None):
	from frappe.utils.nestedset import get_root_of

	return _insert(
		{
			"doctype": "Customer",
			"customer_name": customer_name,
			"customer_group": leaf_customer_group(),
			"territory": get_root_of("Territory"),
			"loyalty_program": loyalty_program,
		}
	).name


def ensure_pos_profile(company, customer):
	if frappe.db.exists("POS Profile", POS_PROFILE):
		return frappe.get_doc("POS Profile", POS_PROFILE)
	profile = frappe.get_doc(
		{
			"doctype": "POS Profile",
			"company": COMPANY,
			"currency": CURRENCY,
			"warehouse": WAREHOUSE,
			"cost_center": COST_CENTER,
			"write_off_account": company.write_off_account or f"Write Off - {ABBR}",
			"write_off_cost_center": COST_CENTER,
			"write_off_limit": 1,
			"selling_price_list": PRICE_LIST,
			"customer": customer,
			"taxes_and_charges": TAX_TEMPLATE,
			"disable_rounded_total": 1,
			"ignore_pricing_rule": 1,
			"payments": [{"mode_of_payment": CASH, "default": 1}],
			"item_groups": [{"item_group": ITEM_GROUP_ROOT}],
		}
	)
	return profile.insert(ignore_permissions=True, set_name=POS_PROFILE)


def setup_fixtures():
	company = ensure_company()
	for date in (nowdate(), add_days(nowdate(), -1)):
		ensure_fiscal_year(date)
	ensure_accounts(company)
	ensure_taxes()
	ensure_loyalty_program()

	if not frappe.db.exists("Price List", PRICE_LIST):
		_insert(
			{
				"doctype": "Price List",
				"price_list_name": PRICE_LIST,
				"currency": CURRENCY,
				"selling": 1,
				"enabled": 1,
			}
		)

	from frappe.utils.nestedset import get_root_of

	root_group = get_root_of("Item Group")
	ensure_item_group(ITEM_GROUP_ROOT, root_group, 1)
	ensure_item_group(ITEM_GROUP_LEAF, ITEM_GROUP_ROOT, 0)
	ensure_item_group(ITEM_GROUP_OUTSIDE, root_group, 0)

	customer = frappe.db.get_value("Customer", {"customer_name": CUSTOMER}, "name")
	if not customer:
		customer = _insert(
			{
				"doctype": "Customer",
				"customer_name": CUSTOMER,
				"customer_group": leaf_customer_group(),
				"territory": get_root_of("Territory"),
			}
		).name

	ensure_item(MILK, 0.6)
	ensure_item(BREAD, 0.5, zero_rated=True)
	ensure_stock(MILK)
	ensure_stock(BREAD)

	profile = ensure_pos_profile(company, customer)
	frappe.db.commit()
	return profile
