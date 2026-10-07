"""Device registration, POS Profile listing and bootstrap data."""

import re

import frappe
from frappe.utils import cint, get_system_timezone, now

from crenya_pos_api.sync.context import (
	DEFAULT_DEVICE_TYPE,
	DEVICE_DOCTYPE,
	DEVICE_TYPE_RE,
	assert_supported_taxes,
	find_device_name,
	get_profile,
	get_update_stock,
	is_rounded_total_disabled,
	is_system_manager,
	money_precision,
	qty_precision,
	rate_precision,
	require_device_role,
	user_can_use_profile,
)
from crenya_pos_api.sync.errors import (
	DeviceNotRegisteredError,
	DevicePermissionError,
	InvalidRequestError,
	raise_api_error,
)
from crenya_pos_api.sync.locale_data import (
	cash_denominations,
	company_phone_country_code,
	currency_info,
	phone_country_codes,
)
from crenya_pos_api.sync.loyalty import loyalty_enabled
from crenya_pos_api.sync.open_returns import allows_return_without_invoice
from crenya_pos_api.sync.registry import extend_bootstrap, extend_profiles
from crenya_pos_api.sync.scale_rules import scale_barcode_rules
from crenya_pos_api.sync.tax_wording import bootstrap_company_wording, company_wording_fields
from crenya_pos_api.sync.taxes import get_tax_templates
from crenya_pos_api.sync.terminals import payment_terminals
from crenya_pos_api.utils.dates import utc_now_iso
from crenya_pos_api.utils.decimal import format_money, format_number

_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,139}$")
_SHORT_RE = re.compile(r"^D(\d+)$")


def offline_prefix(device_short):
	return f"OFF-{device_short}-"


def list_profiles():
	require_device_role()
	user = frappe.session.user
	profiles = frappe.get_all(
		"POS Profile",
		filters={"disabled": 0},
		fields=["name", "company", "warehouse", "currency"],
		order_by="name asc",
	)
	if not profiles:
		return []

	users_by_profile = {}
	for row in frappe.get_all(
		"POS Profile User",
		filters={"parenttype": "POS Profile", "parent": ["in", [p.name for p in profiles]]},
		fields=["parent", "user"],
	):
		if row.user:
			users_by_profile.setdefault(row.parent, set()).add(row.user)

	manager = is_system_manager(user)
	listed = [
		{
			"name": profile.name,
			"company": profile.company,
			"warehouse": profile.warehouse,
			"currency": profile.currency
			or frappe.get_cached_value("Company", profile.company, "default_currency"),
		}
		for profile in profiles
		if manager or not users_by_profile.get(profile.name) or user in users_by_profile[profile.name]
	]
	# other apps' crenya_pos_profile_flags hooks, e.g. which profiles are restaurant outlets
	return extend_profiles(listed)


def _next_device_short():
	shorts = frappe.get_all(DEVICE_DOCTYPE, pluck="device_short")
	numbers = [int(match.group(1)) for short in shorts if short and (match := _SHORT_RE.match(short))]
	return f"D{(max(numbers) if numbers else 0) + 1:02d}"


def _clean(value, max_length=140):
	if value is None:
		return None
	value = str(value).strip()
	return value[:max_length] or None


def _device_type(value):
	"""Optional kind of device (till, kds, waiter, ...): lower case letters, digits, _ and -."""
	if value is None or (isinstance(value, str) and not value.strip()):
		return None
	if not isinstance(value, str) or not DEVICE_TYPE_RE.match(value.strip()):
		raise_api_error(
			InvalidRequestError,
			"device_type must be lower case letters, digits, _ or - (at most 40), e.g. till",
		)
	return value.strip()


def case_variant_refusal(stored, user, pos_profile, device_type=None):
	"""Why a registration is refused whose id differs only in case from the stored device.

	Ids that differ only in case are the same device (`stored`: its name, user, pos_profile,
	device_type and enabled), so such a registration may only find that device again: never
	for another user or POS Profile, never as another device type. Returns the message, or
	None when it is the same device; it then gets the stored record back and changes nothing."""
	name = stored.get("name")
	if stored.get("user") != user:
		return (
			f"Device {name} is already registered to another user; a device id that differs "
			"only in case is the same device"
		)
	if stored.get("pos_profile") != pos_profile:
		return (
			f"Device {name} is already registered to another POS Profile; a device id that "
			"differs only in case is the same device"
		)
	stored_type = stored.get("device_type") or DEFAULT_DEVICE_TYPE
	if device_type and device_type != stored_type:
		return (
			f"Device {name} is already registered as a {stored_type} device; a device id that "
			"differs only in case is the same device"
		)
	return None


def _registration(device):
	return {
		"device_id": device.name,
		"device_short": device.device_short,
		"pos_profile": device.pos_profile,
		"offline_prefix": offline_prefix(device.device_short),
		"device_type": device.get("device_type") or DEFAULT_DEVICE_TYPE,
	}


def register(
	device_id, device_name=None, pos_profile=None, app_version=None, platform=None, device_type=None
):
	require_device_role()
	if not isinstance(device_id, str) or not _DEVICE_ID_RE.match(device_id):
		raise_api_error(InvalidRequestError, "device_id must be the till's UUID")
	device_type = _device_type(device_type)
	# a site whose last deploy skipped `bench migrate` has no device_type column yet
	has_device_type = frappe.get_meta(DEVICE_DOCTYPE).has_field("device_type")
	# the stored device this id names, compared ignoring case (ids are UUIDs)
	stored_name = find_device_name(device_id)

	# the type the device will have: the one sent, else the stored one, else "till"; a role
	# scoped to other device types (e.g. restaurant staff) cannot register a retail till
	stored_type = None
	if has_device_type and stored_name:
		stored_type = frappe.db.get_value(DEVICE_DOCTYPE, stored_name, "device_type")
	require_device_role((device_type if has_device_type else None) or stored_type or DEFAULT_DEVICE_TYPE)

	profile = get_profile(pos_profile)
	user = frappe.session.user
	if not user_can_use_profile(profile, user):
		raise_api_error(DevicePermissionError, f"User {user} may not use POS Profile {profile.name}")
	assert_supported_taxes(profile)

	values = {
		"device_name": _clean(device_name) or device_id,
		"pos_profile": profile.name,
		"user": user,
		"app_version": _clean(app_version),
		"platform": _clean(platform),
		"last_seen": now(),
	}
	if has_device_type and device_type:
		# re-registering without a device_type keeps the stored one
		values["device_type"] = device_type

	if stored_name:
		device = frappe.get_doc(DEVICE_DOCTYPE, stored_name, for_update=True)
		if not cint(device.enabled):
			raise_api_error(DeviceNotRegisteredError, f"Device {stored_name} is disabled")
		if stored_name != device_id:
			# another spelling of a registered device: it may find that device again, never
			# take it over, rename it or change its type
			refusal = case_variant_refusal(
				device.as_dict(), user, profile.name, device_type if has_device_type else None
			)
			if refusal:
				raise_api_error(DevicePermissionError, refusal)
			return _registration(device)
		device.update(values)
		device.save(ignore_permissions=True)
	else:
		device = None
		for _attempt in range(3):
			try:
				device = frappe.new_doc(DEVICE_DOCTYPE)
				device.update(values)
				device.update(
					{
						"device_id": device_id,
						"device_short": _next_device_short(),
						"enabled": 1,
						"registered_on": values["last_seen"],
					}
				)
				if has_device_type and not device_type:
					device.device_type = DEFAULT_DEVICE_TYPE
				device.insert(ignore_permissions=True)
				break
			except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
				# another till took the same short code (or registered this id) concurrently
				frappe.db.rollback()
				if find_device_name(device_id):
					return register(device_id, device_name, pos_profile, app_version, platform, device_type)
				device = None
		if device is None:
			raise_api_error(InvalidRequestError, "Could not allocate a device code, retry")

	return _registration(device)


def _company_address_lines(profile, company):
	address_name = profile.get("company_address")
	if not address_name:
		dl = frappe.qb.DocType("Dynamic Link")
		address = frappe.qb.DocType("Address")
		rows = (
			frappe.qb.from_(address)
			.join(dl)
			.on((dl.parent == address.name) & (dl.parenttype == "Address"))
			.select(address.name)
			.where(
				(dl.link_doctype == "Company")
				& (dl.link_name == company)
				& (address.is_your_company_address == 1)
				& (address.disabled == 0)
			)
			.orderby(address.is_primary_address, order=frappe.qb.desc)
			.limit(1)
			.run(pluck=True)
		)
		address_name = rows[0] if rows else None
	if not address_name:
		return []
	address = frappe.db.get_value(
		"Address", address_name, ["address_line1", "address_line2", "city", "country"], as_dict=True
	)
	if not address:
		return []
	lines = [
		address.address_line1,
		address.address_line2,
		", ".join(part for part in (address.city, address.country) if part),
	]
	return [line for line in lines if line]


def _company_row(company):
	meta = frappe.get_meta("Company")
	fields = ["name", "company_name", "tax_id", "phone_no", "email", "country"]
	for custom in ("crenya_company_name_ar", "crenya_cr_number"):
		if meta.has_field(custom):
			fields.append(custom)
	fields.extend(company_wording_fields(meta))
	return frappe.db.get_value("Company", company, fields, as_dict=True) or frappe._dict()


def _company_info(profile, row, taxes):
	company = profile.company
	info = {
		"name": company,
		"company_name": row.get("company_name") or company,
		"company_name_ar": row.get("crenya_company_name_ar") or None,
		"cr_number": row.get("crenya_cr_number") or None,
		"address_lines": _company_address_lines(profile, company),
		"phone": row.get("phone_no") or None,
		"email": row.get("email") or None,
		"phone_country_code": company_phone_country_code(row.get("country")),
	}
	# country, tax_id (gstin fallback), tax name / tax id label / titles (+ _ar), receipt_qr, tax_code_label
	info.update(bootstrap_company_wording(row, taxes))
	return info


def _payment_methods(profile):
	rows = [row for row in profile.get("payments") or [] if row.mode_of_payment]
	types = dict(
		frappe.get_all(
			"Mode of Payment",
			filters={"name": ["in", [row.mode_of_payment for row in rows] or [""]]},
			fields=["name", "type"],
			as_list=True,
		)
	)
	return [
		{
			"mode_of_payment": row.mode_of_payment,
			"type": types.get(row.mode_of_payment),
			"default": bool(cint(row.default)),
		}
		for row in rows
	]


def _taxes(profile):
	template = profile.get("taxes_and_charges")
	if not template:
		return []
	rows = frappe.get_all(
		"Sales Taxes and Charges",
		filters={"parent": template, "parenttype": "Sales Taxes and Charges Template"},
		fields=["idx", "charge_type", "account_head", "description", "rate", "included_in_print_rate"],
		order_by="idx asc",
	)
	return [
		{
			"idx": cint(row.idx),
			"charge_type": row.charge_type,
			"account_head": row.account_head,
			"description": row.description,
			"rate": format_number(row.rate),
			"included_in_print_rate": bool(cint(row.included_in_print_rate)),
		}
		for row in rows
	]


def _item_tax_templates(company):
	template = frappe.qb.DocType("Item Tax Template")
	names = (
		frappe.qb.from_(template)
		.select(template.name)
		.where(
			(template.disabled == 0)
			& ((template.company == company) | template.company.isnull() | (template.company == ""))
		)
		.orderby(template.name)
		.run(pluck=True)
	)
	if not names:
		return []
	details = {}
	for row in frappe.get_all(
		"Item Tax Template Detail",
		filters={"parent": ["in", names], "parenttype": "Item Tax Template"},
		fields=["parent", "tax_type", "tax_rate"],
		order_by="parent asc, idx asc",
	):
		details.setdefault(row.parent, []).append(
			{"tax_type": row.tax_type, "tax_rate": format_number(row.tax_rate)}
		)
	return [{"name": name, "taxes": details.get(name, [])} for name in names]


def bootstrap(ctx):
	profile = ctx.profile
	assert_supported_taxes(profile)
	currency = ctx.currency
	precision = money_precision(currency)
	company = _company_row(profile.company)
	taxes = _taxes(profile)
	smallest_fraction = frappe.get_cached_value("Currency", currency, "smallest_currency_fraction_value")

	doc = {
		"device": {
			"device_id": ctx.device_id,
			"device_short": ctx.device["device_short"],
			"pos_profile": profile.name,
			"device_type": ctx.device_type,
		},
		"profile": {
			"name": profile.name,
			"company": profile.company,
			"warehouse": profile.warehouse,
			"selling_price_list": profile.selling_price_list,
			"currency": currency,
			"currency_precision": precision,
			"smallest_currency_fraction_value": format_number(smallest_fraction or 0, precision),
			"customer": profile.customer,
			"update_stock": bool(get_update_stock(profile)),
			"ignore_pricing_rule": True,
			"allow_rate_change": bool(cint(profile.get("allow_rate_change"))),
			"allow_discount_change": bool(cint(profile.get("allow_discount_change"))),
			"allow_return_without_invoice": allows_return_without_invoice(profile),
			"disable_rounded_total": bool(is_rounded_total_disabled(profile)),
			"taxes_and_charges": profile.taxes_and_charges,
			"tax_templates": get_tax_templates(profile),
			"loyalty_enabled": loyalty_enabled(profile.company),
			"item_groups": [row.item_group for row in profile.get("item_groups") or []],
			"customer_groups": [row.customer_group for row in profile.get("customer_groups") or []],
			"write_off_limit": format_money(profile.get("write_off_limit") or 0, precision),
			"print_format": profile.get("print_format") or None,
			"allow_negative_stock": bool(
				cint(frappe.db.get_single_value("Stock Settings", "allow_negative_stock"))
			),
			"scale_barcode_rules": scale_barcode_rules(profile),
		},
		"company": _company_info(profile, company, taxes),
		"settings": {
			# ERPNext rounds item row qty to this (property setters included); batch shares must be exact
			"qty_precision": qty_precision(),
			# ERPNext rounds item row rate to this; amount = flt(rate * qty) per row
			"rate_precision": rate_precision(currency),
		},
		"currency": currency_info(currency, precision),
		"phone_country_codes": phone_country_codes(company.get("country") or None),
		"cash_denominations": cash_denominations(currency),
		"site_timezone": get_system_timezone(),
		"payment_methods": _payment_methods(profile),
		# secrets included: get_device_context has authorized this registered device
		"payment_terminals": payment_terminals(profile),
		"taxes": taxes,
		"item_tax_templates": _item_tax_templates(profile.company),
		"server_time": utc_now_iso(),
	}
	# other apps' crenya_pos_bootstrap hooks: fn(ctx, doc), adding their own keys
	return extend_bootstrap(ctx, doc)
