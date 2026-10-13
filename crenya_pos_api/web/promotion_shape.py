"""A promotion as app.crenya.com edits it, and the ERPNext Pricing Rule it is (pure: no site).

Only the Pricing Rules a till evaluates offline (`sync/promotions.py`, pos-core promotions.rs)
can be made here: on item codes, item groups or brands; a discount percentage, a discount amount
or a rate, or free items (buy X get Y, the same item or another, optionally again for every X);
min / max quantity, valid from / upto and priority. Coupons, customer-only rules, margins and
whole-bill (Transaction) rules are not: the till drops coupon rules (a P2 item).

Promotion: {name?, title, enabled, apply_on, targets, kind, value?, free_item?, free_qty?,
same_item?, recursive?, min_qty?, max_qty?, valid_from?, valid_upto?, priority?, modified?};
decimals are strings.
"""

from datetime import date

from crenya_pos_api.utils.decimal import (
	DecimalParseError,
	as_decimal,
	format_money,
	format_number,
	parse_decimal,
)

# apply_on: (Pricing Rule apply_on, child table, child field)
APPLY_ON = {
	"item_code": ("Item Code", "items", "item_code"),
	"item_group": ("Item Group", "item_groups", "item_group"),
	"brand": ("Brand", "brands", "brand"),
}
# kind: (price_or_product_discount, rate_or_discount, value field)
KINDS = {
	"discount_percentage": ("Price", "Discount Percentage", "discount_percentage"),
	"discount_amount": ("Price", "Discount Amount", "discount_amount"),
	"rate": ("Price", "Rate", "rate"),
	"free_item": ("Product", None, None),
}
MONEY_KINDS = ("discount_amount", "rate")
VALUE_FIELDS = ("discount_percentage", "discount_amount", "rate")
MAX_PRIORITY = 20
MAX_TITLE_LENGTH = 140
KEYS = (
	"name",
	"title",
	"enabled",
	"apply_on",
	"targets",
	"kind",
	"value",
	"free_item",
	"free_qty",
	"same_item",
	"recursive",
	"min_qty",
	"max_qty",
	"valid_from",
	"valid_upto",
	"priority",
	"modified",
)
# a coupon rule is never a promotion here
COUPON_FIELD = "coupon_code_based"


def _decimal(value, field, required=False):
	"""A decimal string as a Decimal (None when empty and not `required`); DecimalParseError."""
	return parse_decimal(value, field, allow_none=not required)


def _number(value) -> float:
	"""A checked decimal string as the float a Pricing Rule field holds (ERPNext compares them as
	numbers: a string "0" would read as set)."""
	return float(as_decimal(value))


def _flag(value):
	return value in (True, 1, "1", "true")


def _date(value, field):
	if value in (None, ""):
		return None
	if not isinstance(value, str):
		raise ValueError(f"{field} must be YYYY-MM-DD")
	try:
		return date.fromisoformat(value)
	except ValueError:
		raise ValueError(f"{field} must be YYYY-MM-DD")


def _decimals(value) -> int:
	return max(-value.as_tuple().exponent, 0)


def promotion_problem(data, precision) -> str | None:
	"""Why `data` is not a promotion the till evaluates, or None. `precision`: the decimals of
	the outlet's currency (a discount amount or rate has at most that many)."""
	if not isinstance(data, dict):
		return "A promotion must be an object"
	if data.get(COUPON_FIELD):
		return "Coupon promotions are not supported at the till"
	unknown = sorted(set(data) - set(KEYS))
	if unknown:
		return f"Not a promotion field: {', '.join(unknown)}"
	title = data.get("title")
	if not isinstance(title, str) or not title.strip() or len(title.strip()) > MAX_TITLE_LENGTH:
		return f"A title of at most {MAX_TITLE_LENGTH} characters is required"
	if data.get("apply_on") not in APPLY_ON:
		return f"apply_on must be one of {', '.join(APPLY_ON)}"
	targets = data.get("targets")
	if (
		not isinstance(targets, list)
		or not targets
		or not all(isinstance(t, str) and t.strip() for t in targets)
	):
		return "Choose at least one item, item group or brand"
	if len({t.strip() for t in targets}) != len(targets):
		return "An item, item group or brand is listed twice"
	kind = data.get("kind")
	if kind not in KINDS:
		return f"kind must be one of {', '.join(KINDS)}"
	try:
		if kind == "free_item":
			problem = _free_item_problem(data)
		else:
			problem = _value_problem(data, kind, precision)
		if problem:
			return problem
		min_qty = _decimal(data.get("min_qty"), "min_qty")
		max_qty = _decimal(data.get("max_qty"), "max_qty")
		if (min_qty is not None and min_qty < 0) or (max_qty is not None and max_qty < 0):
			return "Quantities cannot be negative"
		if min_qty and max_qty and min_qty > max_qty:
			return "Min quantity cannot be more than max quantity"
		valid_from = _date(data.get("valid_from"), "valid_from")
		valid_upto = _date(data.get("valid_upto"), "valid_upto")
	except (DecimalParseError, ValueError) as e:
		return str(e)
	if valid_from and valid_upto and valid_upto < valid_from:
		return "Valid upto cannot be before valid from"
	priority = data.get("priority")
	if priority not in (None, "") and (
		isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= MAX_PRIORITY
	):
		return f"Priority must be a whole number from 1 to {MAX_PRIORITY}"
	return None


def _value_problem(data, kind, precision):
	value = _decimal(data.get("value"), "value", required=True)
	if value < 0:
		return "The value cannot be negative"
	if kind == "discount_percentage" and not 0 < value <= 100:
		return "A discount percentage must be more than 0 and at most 100"
	if kind in MONEY_KINDS and _decimals(value) > precision:
		return f"The amount has more than {precision} decimals"
	if kind == "discount_amount" and value == 0:
		return "A discount amount must be more than 0"
	return None


def _free_item_problem(data):
	same = _flag(data.get("same_item"))
	free_item = data.get("free_item")
	if not same and (not isinstance(free_item, str) or not free_item.strip()):
		return "Choose the free item, or give the same item"
	free_qty = _decimal(data.get("free_qty"), "free_qty", required=True)
	if free_qty <= 0:
		return "The free quantity must be more than 0"
	if _flag(data.get("recursive")):
		min_qty = _decimal(data.get("min_qty"), "min_qty")
		if not min_qty or min_qty <= 0:
			return "A repeating offer needs the quantity to buy (min_qty)"
	return None


def to_pricing_rule(data) -> dict:
	"""The Pricing Rule fields and child rows of a checked promotion (`promotion_problem` None).
	Every value field is written, so a promotion changed to another kind keeps no old value; the
	caller adds the company, currency and price list."""
	rule_apply_on, table, field = APPLY_ON[data["apply_on"]]
	discount, rate_or_discount, value_field = KINDS[data["kind"]]
	free = data["kind"] == "free_item"
	same = free and _flag(data.get("same_item"))
	recursive = free and _flag(data.get("recursive"))
	min_qty = _number(data.get("min_qty"))
	rule = {
		"title": data["title"].strip(),
		"disable": 0 if _flag(data.get("enabled", True)) else 1,
		"selling": 1,
		"buying": 0,
		"applicable_for": "",
		"apply_rule_on_other": "",
		COUPON_FIELD: 0,
		"apply_on": rule_apply_on,
		"items": [],
		"item_groups": [],
		"brands": [],
		"price_or_product_discount": discount,
		"rate_or_discount": rate_or_discount or "",
		**dict.fromkeys(VALUE_FIELDS, 0.0),
		"same_item": 1 if same else 0,
		"free_item": None if (not free or same) else data["free_item"].strip(),
		"free_qty": _number(data.get("free_qty")) if free else 0.0,
		"is_recursive": 1 if recursive else 0,
		# whole free units for every `min_qty` bought (pos-core: floor per recurse_for)
		"recurse_for": min_qty if recursive else 0.0,
		"round_free_qty": 1 if recursive else 0,
		"min_qty": min_qty,
		"max_qty": _number(data.get("max_qty")),
		"valid_from": data.get("valid_from") or None,
		"valid_upto": data.get("valid_upto") or None,
		"priority": str(data["priority"]) if data.get("priority") not in (None, "") else "",
	}
	rule[table] = [{field: target.strip()} for target in data["targets"]]
	if value_field:
		rule[value_field] = _number(data["value"])
	return rule


def from_pricing_rule(rule: dict, targets: dict, precision) -> dict | None:
	"""The promotion of a Pricing Rule (`rule` its fields; `targets` {child table: names}), or
	None when the rule is not of this shape (a coupon, a whole-bill or customer rule)."""
	apply_on = next((key for key, (label, _t, _f) in APPLY_ON.items() if label == rule.get("apply_on")), None)
	if apply_on is None or int(rule.get(COUPON_FIELD) or 0) or (rule.get("applicable_for") or ""):
		return None
	if rule.get("price_or_product_discount") == "Product":
		kind = "free_item"
	else:
		kind = next(
			(
				key
				for key, (_d, label, _f) in KINDS.items()
				if label and label == rule.get("rate_or_discount")
			),
			None,
		)
	if kind is None:
		return None
	promotion = {
		"name": rule.get("name"),
		"title": rule.get("title") or rule.get("name"),
		"enabled": not int(rule.get("disable") or 0),
		"apply_on": apply_on,
		"targets": list(targets.get(APPLY_ON[apply_on][1]) or []),
		"kind": kind,
		"min_qty": format_number(rule.get("min_qty") or 0),
		"max_qty": format_number(rule.get("max_qty") or 0),
		"valid_from": _iso(rule.get("valid_from")),
		"valid_upto": _iso(rule.get("valid_upto")),
		"priority": int(rule["priority"]) if str(rule.get("priority") or "").isdigit() else None,
		"modified": rule.get("modified"),
	}
	if kind == "free_item":
		promotion.update(
			{
				"same_item": bool(int(rule.get("same_item") or 0)),
				"free_item": rule.get("free_item") or None,
				"free_qty": format_number(rule.get("free_qty") or 0),
				"recursive": bool(int(rule.get("is_recursive") or 0)),
			}
		)
	else:
		value = rule.get(KINDS[kind][2]) or 0
		promotion["value"] = format_money(value, precision) if kind in MONEY_KINDS else format_number(value)
	return promotion


def _iso(value):
	if value in (None, ""):
		return None
	return value.isoformat() if hasattr(value, "isoformat") else str(value)[:10]
