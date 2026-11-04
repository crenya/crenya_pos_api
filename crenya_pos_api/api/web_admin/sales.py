"""A shop's Reports tab on app.crenya.com (0.17.0): sales by item and by cashier
(`web.sales_reports`), in crenya_restaurant's `sales` answer shape so the web's shared Reports page
reads both products. For a manager of the shop permitted on its company (slice 1 T12 F7)."""

import frappe
from frappe import _

from crenya_pos_api.api.web_admin.scope import manage_company
from crenya_pos_api.sync.context import money_precision
from crenya_pos_api.web import sales_reports
from crenya_pos_api.web.context import invalid, require_web_session
from crenya_pos_api.web.devices import full_names
from crenya_pos_api.web.reports import common, date_range


@frappe.whitelist(methods=["GET", "POST"])
def sales(tab: str, from_date: str, to_date: str, outlet: str) -> dict:
	"""{...common, tab, from_date, to_date, rows}: `tab` "items" (item_code, item_name, item_group, qty,
	amount, discounts) or "cashiers" (cashier, cashier_name, bills, refunds_count, refunds, sales,
	net_total, discounts, line_voids_count, no_sales_count); at most 366 days."""
	require_web_session()
	ctx = manage_company(outlet)
	if tab not in sales_reports.TABS:
		invalid(_("Unknown report {0}").format(tab))
	first, last = date_range(from_date, to_date)
	precision = money_precision(ctx.currency)
	if tab == "items":
		rows = sales_reports.item_rows(
			sales_reports.item_sums(ctx.company, ctx.outlet, first, last), precision
		)
	else:
		sums = sales_reports.cashier_sums(ctx.company, ctx.outlet, first, last)
		controls = sales_reports.control_counts(ctx.outlet, first, last)
		names = full_names([row["cashier"] for row in sums] + list(controls))
		rows = sales_reports.cashier_rows(sums, controls, names, precision)
	return {
		**common(ctx.company, ctx.currency),
		"tab": tab,
		"from_date": first.isoformat(),
		"to_date": last.isoformat(),
		"rows": rows,
	}
