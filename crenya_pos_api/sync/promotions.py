"""Which Pricing Rules a till can evaluate offline (pure helpers, no database access).

The till mirrors ERPNext's pricing-rule engine for a subset of rules. Rules
outside that subset are never offered as active: the `pricing_rule` feed sends
them with `disabled: 1`, so a till drops a rule that stops qualifying.
"""

from datetime import date, datetime

SUPPORTED_APPLY_ON = ("Item Code", "Item Group", "Brand", "Transaction")
SUPPORTED_APPLICABLE_FOR = ("", "Customer", "Customer Group")
SUPPORTED_DISCOUNT_KINDS = ("Price", "Product")


def _flag(value):
	try:
		return 1 if int(value or 0) else 0
	except (TypeError, ValueError):
		return 0


def _number(value):
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0


def _text(value):
	return value.strip() if isinstance(value, str) else ("" if value is None else str(value))


def _as_date(value):
	if value in (None, ""):
		return None
	if isinstance(value, datetime):
		return value.date()
	if isinstance(value, date):
		return value
	return date.fromisoformat(str(value)[:10])


def rule_qualifies(rule, company, currency=None):
	"""True when a till can evaluate the Pricing Rule `rule` (a dict of its fields) offline.

	Not offered: buying-only rules, rules of another company, rules with a Python
	`condition`, coupon rules, rules for Territory / Sales Partner / Campaign /
	Supplier, "apply rule on other" rules, rules with a margin, and rules ERPNext
	itself never applies automatically (`validate_applied_rule`) or would not apply
	in the till's currency (a `currency` other than the till's).
	"""
	if not _flag(rule.get("selling")):
		return False
	if _text(rule.get("company")) not in ("", company or ""):
		return False
	if _text(rule.get("condition")):
		return False
	if _flag(rule.get("coupon_code_based")):
		return False
	if _text(rule.get("applicable_for")) not in SUPPORTED_APPLICABLE_FOR:
		return False
	if _text(rule.get("apply_rule_on_other")):
		return False
	if _text(rule.get("margin_type")) and _number(rule.get("margin_rate_or_amount")):
		return False
	if _text(rule.get("apply_on")) not in SUPPORTED_APPLY_ON:
		return False
	if _text(rule.get("price_or_product_discount")) not in SUPPORTED_DISCOUNT_KINDS:
		return False
	if _flag(rule.get("validate_applied_rule")):
		return False
	if currency and _text(rule.get("currency")) not in ("", currency):
		return False
	return True


def rule_is_expired(rule, today):
	"""True when the rule's `valid_upto` lies before `today`."""
	valid_upto = _as_date(rule.get("valid_upto"))
	return valid_upto is not None and valid_upto < _as_date(today)


def rule_is_active(rule, company, today, currency=None):
	"""What the till receives as `disabled: 0`: enabled, not expired, and qualifying."""
	if _flag(rule.get("disable")):
		return False
	if rule_is_expired(rule, today):
		return False
	return rule_qualifies(rule, company, currency)
