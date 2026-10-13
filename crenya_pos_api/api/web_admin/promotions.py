"""A shop's promotions on app.crenya.com: the ERPNext Pricing Rules its tills evaluate offline
(`web.promotion_shape`), for the shop's managers (`scope.manage`).

A shop's promotions are the selling rules of its company for its selling price list, or for no
price list (every price list of the company). A rule saved here is for the shop's price list;
one for every price list is changed by a System Manager only (as the other records shared beyond
one outlet). Rules of another company, coupon rules and rules of another shape are never listed
or changed here. The tills receive a change with their next `pricing_rule` pull.
"""

import frappe
from frappe import _

from crenya_pos_api.api.web_admin.scope import manage
from crenya_pos_api.sync.context import money_precision
from crenya_pos_api.utils.dates import format_db_datetime
from crenya_pos_api.web.context import check_modified, checked, invalid, is_system_manager, parse_flag, refuse
from crenya_pos_api.web.promotion_shape import APPLY_ON, from_pricing_rule, promotion_problem, to_pricing_rule
from crenya_pos_api.web.reports import money_view

RULE_DOCTYPE = "Pricing Rule"
# the Pricing Rule's child table DocTypes of `promotion_shape.APPLY_ON`
CHILD_DOCTYPES = {
	"items": "Pricing Rule Item Code",
	"item_groups": "Pricing Rule Item Group",
	"brands": "Pricing Rule Brand",
}
# how many item group and brand names the editor offers (a datalist; any other may be typed)
MAX_CHOICES = 500
RULE_FIELDS = (
	"name",
	"title",
	"disable",
	"apply_on",
	"applicable_for",
	"coupon_code_based",
	"price_or_product_discount",
	"rate_or_discount",
	"discount_percentage",
	"discount_amount",
	"rate",
	"free_item",
	"free_qty",
	"same_item",
	"is_recursive",
	"min_qty",
	"max_qty",
	"valid_from",
	"valid_upto",
	"priority",
	"for_price_list",
	"modified",
)


def _scope_filters(ctx) -> tuple[dict, list]:
	"""(filters, or_filters) of the shop's rules."""
	price_list = ctx.profile.get("selling_price_list") or ""
	return (
		{"company": ctx.company, "selling": 1},
		[["for_price_list", "=", price_list], ["for_price_list", "is", "not set"]],
	)


def _rules(ctx, name=None) -> list:
	filters, or_filters = _scope_filters(ctx)
	if name:
		filters["name"] = name
	return frappe.get_all(
		RULE_DOCTYPE,
		filters=filters,
		or_filters=or_filters,
		fields=list(RULE_FIELDS),
		order_by="title asc",
	)


def _targets(names) -> dict:
	"""{rule: {child table: [names]}} of `names`."""
	grouped = {}
	for _label, table, field in APPLY_ON.values():
		for row in frappe.get_all(
			CHILD_DOCTYPES[table],
			filters={"parenttype": RULE_DOCTYPE, "parent": ["in", list(names) or [""]]},
			fields=["parent", field],
			order_by="idx asc",
		):
			if row.get(field):
				grouped.setdefault(row.parent, {}).setdefault(table, []).append(row.get(field))
	return grouped


def _promotions(ctx) -> dict:
	precision = money_precision(ctx.currency)
	rules = _rules(ctx)
	targets = _targets(rule.name for rule in rules)
	system_manager = is_system_manager()
	rows = []
	for rule in rules:
		promotion = from_pricing_rule(
			{**rule, "modified": format_db_datetime(rule.modified)}, targets.get(rule.name, {}), precision
		)
		if promotion is not None:
			promotion["can_edit"] = bool(rule.for_price_list) or system_manager
			rows.append(promotion)
	return {
		"currency": money_view(ctx.currency),
		"rows": rows,
		"choices": {
			"item_groups": frappe.get_all("Item Group", pluck="name", order_by="name asc", limit=MAX_CHOICES),
			"brands": frappe.get_all("Brand", pluck="name", order_by="name asc", limit=MAX_CHOICES),
		},
	}


def _own_rule(ctx, name, modified):
	"""The shop's rule `name` this user may change, locked; refused otherwise."""
	rules = _rules(ctx, name) if isinstance(name, str) and name else []
	if not rules:
		refuse(_("Promotion {0} is not a promotion of outlet {1}").format(name, ctx.outlet))
	if not rules[0].for_price_list and not is_system_manager():
		refuse(
			_("Promotion {0} is for every price list of {1}: only a System Manager changes it").format(
				name, ctx.company
			)
		)
	check_modified(RULE_DOCTYPE, name, modified)
	return frappe.get_doc(RULE_DOCTYPE, name)


@frappe.whitelist(methods=["GET", "POST"])
def promotions(outlet: str) -> dict:
	"""The shop's promotions, the currency they are priced in and the item groups and brands
	offered."""
	return _promotions(manage(outlet))


@frappe.whitelist(methods=["POST"])
def save_promotion(outlet: str, promotion: dict) -> dict:
	"""Create (no `name`) or change (with the `modified` `promotions` answered) a promotion of the
	shop: a Pricing Rule of its company and currency, for its price list. ERPNext's own checks
	still decide. Answers `promotions`."""
	ctx = manage(outlet)
	data = frappe.parse_json(promotion)
	problem = promotion_problem(data, money_precision(ctx.currency))
	if problem:
		invalid(problem)
	if data.get("name"):
		doc = _own_rule(ctx, data["name"], data.get("modified"))
	else:
		doc = frappe.new_doc(RULE_DOCTYPE)
		doc.for_price_list = ctx.profile.get("selling_price_list") or None
	doc.update(to_pricing_rule(data))
	doc.company = ctx.company
	doc.currency = ctx.currency
	doc.save(ignore_permissions=True)
	ctx.log(doc)
	return _promotions(ctx)


@frappe.whitelist(methods=["POST"])
def set_promotion_enabled(outlet: str, name: str, enabled: bool | int | str, modified: str) -> dict:
	"""Switch a promotion of the shop on or off; answers `promotions`."""
	ctx = manage(outlet)
	flag = checked(parse_flag, enabled, "enabled")
	if flag is None:
		invalid(_("enabled is required"))
	doc = _own_rule(ctx, name, modified)
	if bool(doc.disable) == flag:
		doc.disable = 0 if flag else 1
		doc.save(ignore_permissions=True)
		ctx.log(doc)
	return _promotions(ctx)
