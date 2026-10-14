"""A shop's settings on app.crenya.com: the outlet settings form with its setup checks, and the
list editors of what a till reads besides (scale barcode rules, cash denominations, payment
terminals, the company's receipt wording). The forms are built from field definitions (the
POS Profile's own fields read from its meta, this app's custom fields from `setup/install.py`),
never typed here. Who manages a shop: `scope.manage`; the accounting links of the outlet
(`OWNER_ONLY`) and the records shared beyond one outlet (denominations of a currency, terminals
of the whole company, the company's wording) are a System Manager's. Writes run with
ignore_permissions after that check, the DocTypes' own validation still decides, and each
changed record gets a comment naming the web user.
"""

import frappe
from frappe import _
from frappe.utils import cint, cstr

from crenya_pos_api.api.web_admin.scope import manage
from crenya_pos_api.expenses import booking as expenses_booking
from crenya_pos_api.expenses.fields import EXPENSE_PROFILE_FIELDS
from crenya_pos_api.setup.install import CREDIT_PROFILE_FIELDS, CUSTOM_FIELDS, STORE_CONTROL_FIELDS
from crenya_pos_api.sync import scale_rules as till_scale_rules
from crenya_pos_api.sync.context import money_precision
from crenya_pos_api.sync.locale_data import DENOMINATION_DOCTYPE, DENOMINATION_KINDS
from crenya_pos_api.sync.terminals import SECRET_FIELD, TERMINAL_DOCTYPE
from crenya_pos_api.utils.dates import format_db_datetime
from crenya_pos_api.utils.decimal import DecimalParseError, format_number, parse_decimal
from crenya_pos_api.web import forms
from crenya_pos_api.web.context import (
	check_modified,
	checked,
	invalid,
	is_system_manager,
	parse_flag,
	refuse,
	setting_value,
)
from crenya_pos_api.web.retail_checks import credit_setup_checks, retail_setup_checks
from crenya_pos_api.web.setup_checks import fraction_decimals, money_setup_checks

# when a saved setting reaches the outlet's tills: a till fetches the bootstrap (which carries
# the outlet's settings) at the start of every sync cycle
REACHES_TILLS = "next_sync"
# candidates a settings Link field offers (a datalist; any other value may still be typed)
MAX_LINK_OPTIONS = 50
# the form's layout: (section title, POS Profile fields) of ERPNext's own fields, read from the
# POS Profile meta, then this app's custom fields in their own order
RETAIL_SETTINGS_FIELDS = (
	(
		"Selling",
		(
			"selling_price_list",
			"customer",
			"allow_discount_change",
			"allow_rate_change",
			"crenya_allow_return_without_invoice",
			"item_groups",
		),
	),
	(
		"Stock and accounts",
		(
			"warehouse",
			"taxes_and_charges",
			"cost_center",
			"disable_rounded_total",
			"write_off_account",
			"write_off_cost_center",
		),
	),
)
# the accounting links of the outlet: a System Manager's
OWNER_ONLY = frozenset(
	{
		"taxes_and_charges",
		"write_off_account",
		"write_off_cost_center",
		"cost_center",
		# the expenses' accounting links (the switch and the PIN to add are a manager's; the Cash
		# Supplier may still be created by a manager: `expenses.ensure_cash_supplier`)
		expenses_booking.CASH_SUPPLIER_FIELD,
		expenses_booking.SUPPLIER_GROUP_FIELD,
		expenses_booking.TAX_TEMPLATE_FIELD,
	}
)
# shown, never written from the web (the item groups decide what a till sells: the Items pages)
READ_ONLY = frozenset({"item_groups"})
# Company custom fields of the receipt wording (setup/install.py), in their order
COMPANY_WORDING_FIELDS = tuple(
	field["fieldname"]
	for field in CUSTOM_FIELDS["Company"]
	if field["fieldtype"] not in forms.LAYOUT_FIELDTYPES
)
# terminal fields never written from the web: the company is the outlet's
TERMINAL_FIXED = ("company",)


def _custom_field(fieldname):
	return next(field for field in CUSTOM_FIELDS["POS Profile"] if field["fieldname"] == fieldname)


def _meta_field(meta, fieldname):
	field = meta.get_field(fieldname)
	if field is None:
		return None
	return {
		"fieldname": field.fieldname,
		"fieldtype": field.fieldtype,
		"label": field.label,
		"options": field.options,
		"reqd": field.reqd,
		"description": field.description,
		"read_only": field.read_only,
	}


def settings_fields() -> list[dict]:
	"""The form's field definitions, in order: RETAIL_SETTINGS_FIELDS' sections (each field from
	this app's custom field definitions, else the POS Profile meta; a field the site does not
	have is left out), then the Store controls section."""
	meta = frappe.get_meta("POS Profile")
	custom = {field["fieldname"] for field in CUSTOM_FIELDS["POS Profile"]}
	fields = []
	for title, names in RETAIL_SETTINGS_FIELDS:
		fields.append({"fieldname": f"_{title}", "fieldtype": "Section Break", "label": title})
		for name in names:
			field = _custom_field(name) if name in custom else _meta_field(meta, name)
			if field is not None:
				fields.append(field)
	# then the Credit sales and Expenses sections (their field definitions are this app's, one list
	# for the install and the form)
	return fields + list(STORE_CONTROL_FIELDS) + list(EXPENSE_PROFILE_FIELDS) + list(CREDIT_PROFILE_FIELDS)


def _names(doctype, filters):
	return frappe.get_all(doctype, filters=filters, pluck="name", order_by="name asc", limit=MAX_LINK_OPTIONS)


def _link_options(ctx) -> dict:
	"""{Link doctype: up to MAX_LINK_OPTIONS names} the form offers, of the outlet's company and
	currency."""
	return {
		"Price List": _names("Price List", {"enabled": 1, "selling": 1, "currency": ctx.currency}),
		"Sales Taxes and Charges Template": _names(
			"Sales Taxes and Charges Template", {"company": ctx.company, "disabled": 0}
		),
		"Warehouse": _names("Warehouse", {"company": ctx.company, "is_group": 0, "disabled": 0}),
		"Customer": _names("Customer", {"disabled": 0}),
		"Cost Center": _names("Cost Center", {"company": ctx.company, "is_group": 0, "disabled": 0}),
		"Account": _names("Account", {"company": ctx.company, "is_group": 0, "disabled": 0}),
		# the expenses: the shop's expense suppliers (its group, else every one), the groups and the
		# purchase VAT templates of its company
		"Supplier": _names(
			"Supplier",
			{
				"disabled": 0,
				**(
					{"supplier_group": group}
					if (group := ctx.profile.get(expenses_booking.SUPPLIER_GROUP_FIELD))
					else {}
				),
			},
		),
		"Supplier Group": _names("Supplier Group", {}),
		"Purchase Taxes and Charges Template": _names(
			"Purchase Taxes and Charges Template", {"company": ctx.company, "disabled": 0}
		),
	}


def _value(profile, field):
	if field["fieldtype"] == "Table":
		# the read-only item_groups: the item group names
		return [row.item_group for row in profile.get(field["fieldname"]) or [] if row.get("item_group")]
	return setting_value(profile.get(field["fieldname"]))


def _form(doc, fields, owner_editable, owner_only, read_only, options) -> list:
	sections = forms.settings_form(fields, owner_editable, owner_only=owner_only, read_only=read_only)
	for section in sections:
		for field in section["fields"]:
			field["value"] = _value(doc, field)
			if field["fieldtype"] == "Link" and field["options"] in options:
				field["linkOptions"] = options[field["options"]]
	return sections


def _settings(ctx) -> dict:
	owner_editable = is_system_manager()
	profile = ctx.profile
	return {
		"modified": format_db_datetime(profile.modified),
		"owner_fields": sorted(OWNER_ONLY),
		"can_edit_owner_fields": owner_editable,
		"reaches_tills": REACHES_TILLS,
		"sections": _form(
			profile, settings_fields(), owner_editable, OWNER_ONLY, READ_ONLY, _link_options(ctx)
		),
		# what the outlet's tills need, read live: [{key, status, label, detail, fix}]
		"setup_checks": money_setup_checks(ctx.currency) + retail_setup_checks(profile),
		# what booking the shop's expenses needs, and what selling on account needs, read live (the
		# restaurant's `expense_setup_checks` key, so the shared components read both)
		"expense_setup_checks": expenses_booking.setup_checks(profile),
		"credit_setup_checks": credit_setup_checks(profile),
	}


def _problem(values, fields, owner_editable, owner_only, read_only=frozenset()):
	problem = forms.settings_change_problem(
		values, fields, owner_editable, owner_only=owner_only, read_only=read_only
	)
	if problem:
		kind, message = problem
		if kind == "refused":
			refuse(message)
		invalid(message)


@frappe.whitelist(methods=["GET", "POST"])
def outlet_settings(outlet: str) -> dict:
	"""The shop's settings as a form: sections of fields with their values, and its setup checks."""
	return _settings(manage(outlet))


@frappe.whitelist(methods=["POST"])
def save_outlet_settings(outlet: str, values: dict, modified: str) -> dict:
	"""Write settings ({fieldname: value}) to the shop; `modified` is the one `outlet_settings`
	answered (a profile changed since is refused). The POS Profile's validate and on_update hooks
	run as for a desk save. Answers `outlet_settings`."""
	ctx = manage(outlet)
	values = frappe.parse_json(values)
	_problem(values, settings_fields(), is_system_manager(), OWNER_ONLY, READ_ONLY)
	price_list = values.get("selling_price_list")
	if price_list:
		row = frappe.db.get_value("Price List", price_list, ["selling", "enabled", "currency"], as_dict=True)
		if not row or not row.selling or not row.enabled or row.currency != ctx.currency:
			invalid(
				_("Price List {0} is not an enabled selling price list in {1}").format(
					price_list, ctx.currency
				)
			)
	check_modified("POS Profile", ctx.outlet, modified)
	if values:
		profile = frappe.get_doc("POS Profile", ctx.outlet)
		profile.update(values)
		profile.save(ignore_permissions=True)
		ctx.log(profile)
		frappe.clear_document_cache("POS Profile", ctx.outlet)
		ctx.profile = frappe.get_doc("POS Profile", ctx.outlet)
	return _settings(ctx)


# scale barcode rules (the outlet's own)

SCALE_RULE_KEYS = ("prefix", "plu_length", "value_type", "value_decimals")
VALUE_TYPE_LABELS = {wire: label for label, wire in till_scale_rules.VALUE_TYPES.items()}


def _scale_rules(ctx) -> dict:
	return {
		"modified": format_db_datetime(ctx.profile.modified),
		"rows": till_scale_rules.scale_barcode_rules(ctx.profile),
	}


def scale_rule_rows(rows) -> list[dict]:
	"""The wire rows as the child table's rows (pure): value_type "weight" | "price" becomes its
	Select label, value_decimals an int or blank; ValueError for anything that is not a row."""
	if not isinstance(rows, list):
		raise ValueError("rows must be a list")
	result = []
	for idx, row in enumerate(rows, start=1):
		if not isinstance(row, dict):
			raise ValueError(f"Scale barcode rule {idx} must be an object")
		unknown = sorted(set(row) - set(SCALE_RULE_KEYS))
		if unknown:
			raise ValueError(f"Scale barcode rule {idx}: unknown {', '.join(unknown)}")
		value_type = row.get("value_type")
		if value_type not in VALUE_TYPE_LABELS:
			raise ValueError(f"Scale barcode rule {idx}: value type must be {' or '.join(VALUE_TYPE_LABELS)}")
		decimals = row.get("value_decimals")
		if decimals is not None and (isinstance(decimals, bool) or not isinstance(decimals, int | str)):
			raise ValueError(f"Scale barcode rule {idx}: value decimals must be a whole number or blank")
		plu_length = row.get("plu_length")
		if (
			isinstance(plu_length, bool)
			or not isinstance(plu_length, int | str)
			or not cstr(plu_length).strip().isdigit()
		):
			raise ValueError(f"Scale barcode rule {idx}: PLU length must be a whole number")
		result.append(
			frappe._dict(
				idx=idx,
				prefix=cstr(row.get("prefix")).strip(),
				plu_length=int(cstr(plu_length).strip()),
				value_type=VALUE_TYPE_LABELS[value_type],
				value_decimals="" if decimals in (None, "") else cstr(decimals).strip(),
			)
		)
	return result


@frappe.whitelist(methods=["GET", "POST"])
def scale_rules(outlet: str) -> dict:
	"""The shop's scale barcode rules, as its tills read them."""
	return _scale_rules(manage(outlet))


@frappe.whitelist(methods=["POST"])
def save_scale_rules(outlet: str, rows: list, modified: str) -> dict:
	"""Replace the shop's scale barcode rules with `rows` ([{prefix, plu_length, value_type,
	value_decimals}]), checked by the till's own rules (`sync.scale_rules.validate_rules`);
	`modified` is the one `scale_rules` answered. Answers `scale_rules`."""
	ctx = manage(outlet)
	rows = checked(scale_rule_rows, frappe.parse_json(rows))
	till_scale_rules.validate_rules(rows)
	check_modified("POS Profile", ctx.outlet, modified)
	profile = frappe.get_doc("POS Profile", ctx.outlet)
	profile.set(till_scale_rules.RULES_FIELD, [{key: row[key] for key in SCALE_RULE_KEYS} for row in rows])
	profile.save(ignore_permissions=True)
	ctx.log(profile)
	frappe.clear_document_cache("POS Profile", ctx.outlet)
	ctx.profile = frappe.get_doc("POS Profile", ctx.outlet)
	return _scale_rules(ctx)


# cash denominations (of the outlet's currency: every outlet in that currency shares them)

DENOMINATION_KEYS = ("name", "value", "label", "kind", "enabled")


def _denominations(ctx) -> dict:
	rows = frappe.get_all(
		DENOMINATION_DOCTYPE,
		filters={"currency": ctx.currency},
		fields=["name", "value", "label", "kind", "enabled"],
		order_by="value desc",
	)
	return {
		"currency": ctx.currency,
		"can_edit": is_system_manager(),
		"rows": [
			{
				"name": row.name,
				"value": format_number(row.value),
				"label": row.label,
				"kind": row.kind,
				"enabled": cint(row.enabled),
			}
			for row in rows
		],
	}


def denomination_precision(currency) -> int:
	"""The decimals a denomination of `currency` may have: the currency's own (its Fraction Units:
	OMR 1000 baisa, 3), at least the money precision ERPNext books it at."""
	units = fraction_decimals(frappe.get_cached_value("Currency", currency, "fraction_units"))
	return max(units or 0, money_precision(currency))


def denomination_problem(row, precision) -> str | None:
	"""Why a denomination row cannot be saved (pure), or None: its value a positive decimal
	string with at most the currency's `precision` decimals, a label, a kind of
	DENOMINATION_KINDS, enabled 0 / 1."""
	if not isinstance(row, dict):
		return "A denomination must be an object"
	unknown = sorted(set(row) - set(DENOMINATION_KEYS))
	if unknown:
		return f"Unknown {', '.join(unknown)}"
	try:
		value = parse_decimal(row.get("value"), "value")
	except DecimalParseError as e:
		return str(e)
	if value <= 0:
		return "The value must be greater than zero"
	if -value.as_tuple().exponent > precision:
		return f"The value has more than {precision} decimals"
	if not cstr(row.get("label")).strip():
		return "A label is required"
	if row.get("kind") not in DENOMINATION_KINDS:
		return f"Kind must be one of {', '.join(DENOMINATION_KINDS)}"
	try:
		parse_flag(row.get("enabled"), "enabled", default=True)
	except ValueError as e:
		return str(e)
	return None


@frappe.whitelist(methods=["GET", "POST"])
def denominations(outlet: str) -> dict:
	"""The cash denominations of the shop's currency (the till's cash count), enabled or not."""
	return _denominations(manage(outlet))


@frappe.whitelist(methods=["POST"])
def save_denominations(outlet: str, rows: list) -> dict:
	"""Create (no `name`) or change the cash denominations of the shop's currency; rows not sent
	are left as they are (switch one off with `enabled` 0). Every outlet in that currency shares
	them: a System Manager's. Answers `denominations`."""
	ctx = manage(outlet)
	if not is_system_manager():
		refuse(
			_(
				"Cash denominations are shared by every outlet in {0}: only a System Manager changes them"
			).format(ctx.currency)
		)
	rows = frappe.parse_json(rows)
	if not isinstance(rows, list):
		invalid(_("rows must be a list"))
	precision = denomination_precision(ctx.currency)
	for idx, row in enumerate(rows, start=1):
		problem = denomination_problem(row, precision)
		if problem:
			invalid(_("Denomination {0}: {1}").format(idx, problem))
		if (
			row.get("name")
			and frappe.db.get_value(DENOMINATION_DOCTYPE, row["name"], "currency") != ctx.currency
		):
			invalid(
				_("Denomination {0}: {1} is not a denomination of {2}").format(idx, row["name"], ctx.currency)
			)
	for row in rows:
		doc = (
			frappe.get_doc(DENOMINATION_DOCTYPE, row["name"])
			if row.get("name")
			else frappe.new_doc(DENOMINATION_DOCTYPE)
		)
		doc.update(
			{
				"currency": ctx.currency,
				"value": cstr(parse_decimal(row["value"], "value")),
				"label": cstr(row["label"]).strip(),
				"kind": row["kind"],
				"enabled": int(parse_flag(row.get("enabled"), "enabled", default=True)),
			}
		)
		doc.save(ignore_permissions=True)
		ctx.log(doc)
	return _denominations(ctx)


# payment terminals (the outlet's own, and the company's shared ones)


def _terminal_fields() -> list:
	"""The terminal DocType's data fields (meta), the secret left out."""
	return [
		field
		for field in frappe.get_meta(TERMINAL_DOCTYPE).fields
		if field.fieldtype not in forms.LAYOUT_FIELDTYPES and field.fieldname != SECRET_FIELD
	]


def _select_options(meta, fieldname) -> list:
	field = meta.get_field(fieldname)
	return [option for option in (field.options or "").split("\n") if option] if field else []


def _terminals(ctx) -> dict:
	system_manager = is_system_manager()
	fields = [field.fieldname for field in _terminal_fields()]
	# the outlet's own, and the company's for every outlet (an empty pos_profile, NULL or "")
	rows = frappe.get_all(
		TERMINAL_DOCTYPE,
		filters={"company": ctx.company},
		or_filters=[["pos_profile", "=", ctx.outlet], ["pos_profile", "is", "not set"]],
		fields=["name", "modified", SECRET_FIELD, *fields],
		order_by="label asc",
	)
	meta = frappe.get_meta(TERMINAL_DOCTYPE)
	return {
		"rows": [
			{
				**{name: setting_value(row.get(name)) for name in fields},
				"name": row.name,
				"modified": format_db_datetime(row.modified),
				# the secret itself is never answered: whether one is stored
				f"has_{SECRET_FIELD}": bool(row.get(SECRET_FIELD)),
				"can_edit": bool(row.pos_profile) or system_manager,
			}
			for row in rows
		],
		"choices": {
			"providers": _select_options(meta, "provider"),
			"environments": _select_options(meta, "environment"),
			"modes_of_payment": [
				row.mode_of_payment for row in ctx.profile.get("payments") or [] if row.mode_of_payment
			],
		},
	}


@frappe.whitelist(methods=["GET", "POST"])
def terminals(outlet: str) -> dict:
	"""The payment terminals a till of the shop may drive: its own and its company's shared ones.
	The security token is never answered."""
	return _terminals(manage(outlet))


@frappe.whitelist(methods=["POST"])
def save_terminal(outlet: str, terminal: dict) -> dict:
	"""Create (no `name`) or change a payment terminal of the shop; an existing one needs the
	`modified` `terminals` answered. Its company is the shop's; `pos_profile` is the shop, or
	empty for every outlet of the company (a System Manager's, as is changing such a terminal).
	An empty security token keeps the stored one. Answers `terminals`."""
	ctx = manage(outlet)
	data = frappe.parse_json(terminal)
	if not isinstance(data, dict):
		invalid(_("terminal must be an object"))
	allowed = {field.fieldname for field in _terminal_fields()} - set(TERMINAL_FIXED)
	unknown = sorted(set(data) - allowed - {"name", "modified", SECRET_FIELD})
	if unknown:
		invalid(_("Not a terminal field: {0}").format(", ".join(unknown)))
	name = data.get("name")
	row = None
	if name:
		row = frappe.db.get_value(TERMINAL_DOCTYPE, name, ["company", "pos_profile"], as_dict=True)
		if row is None or row.company != ctx.company or row.pos_profile not in (None, "", ctx.outlet):
			refuse(_("Terminal {0} is not a terminal of outlet {1}").format(name, ctx.outlet))
	# the outlet it serves: as sent, else (a change) as stored
	pos_profile = data.get("pos_profile") if "pos_profile" in data or row is None else row.pos_profile
	if pos_profile not in (None, "", ctx.outlet):
		refuse(_("Terminal must be of outlet {0}, or of every outlet").format(ctx.outlet))
	shared = not pos_profile or (row is not None and not row.pos_profile)
	if shared and not is_system_manager():
		refuse(_("A terminal of every outlet of {0} is changed by a System Manager only").format(ctx.company))
	if name:
		check_modified(TERMINAL_DOCTYPE, name, data.get("modified"))
		doc = frappe.get_doc(TERMINAL_DOCTYPE, name)
	else:
		doc = frappe.new_doc(TERMINAL_DOCTYPE)
	doc.update({key: value for key, value in data.items() if key in allowed})
	doc.company = ctx.company
	doc.pos_profile = pos_profile or None
	if data.get(SECRET_FIELD):
		doc.set(SECRET_FIELD, data[SECRET_FIELD])
	doc.save(ignore_permissions=True)
	ctx.log(doc)
	return _terminals(ctx)


# the company's receipt wording (every outlet of the company prints it)


def company_wording_fields() -> list[dict]:
	"""The Company custom fields of the receipt wording, in order, under one section."""
	return [{"fieldname": "_wording", "fieldtype": "Section Break", "label": "Receipt wording"}] + [
		field for field in CUSTOM_FIELDS["Company"] if field["fieldtype"] != "Section Break"
	]


def _company_wording(ctx) -> dict:
	owner_editable = is_system_manager()
	company = frappe.get_doc("Company", ctx.company)
	fields = company_wording_fields()
	return {
		"modified": format_db_datetime(company.modified),
		"owner_fields": list(COMPANY_WORDING_FIELDS),
		"can_edit_owner_fields": owner_editable,
		"reaches_tills": REACHES_TILLS,
		"sections": _form(
			company, fields, owner_editable, frozenset(COMPANY_WORDING_FIELDS), frozenset(), {}
		),
	}


@frappe.whitelist(methods=["GET", "POST"])
def company_wording(outlet: str) -> dict:
	"""The receipt wording of the shop's company as a form (read only but for a System Manager)."""
	return _company_wording(manage(outlet))


@frappe.whitelist(methods=["POST"])
def save_company_wording(outlet: str, values: dict, modified: str) -> dict:
	"""Write the receipt wording ({fieldname: value}) of the shop's company, a System Manager's;
	`modified` is the Company's one `company_wording` answered. Answers `company_wording`."""
	ctx = manage(outlet)
	values = frappe.parse_json(values)
	_problem(values, company_wording_fields(), is_system_manager(), frozenset(COMPANY_WORDING_FIELDS))
	check_modified("Company", ctx.company, modified)
	if values:
		company = frappe.get_doc("Company", ctx.company)
		company.update(values)
		company.save(ignore_permissions=True)
		ctx.log(company)
	return _company_wording(ctx)
