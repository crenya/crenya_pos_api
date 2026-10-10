"""Frappe v15 / v16 differences, one function per difference.

Callers never test `frappe.__version__` themselves; they call a helper here.
"""

import frappe


def frappe_major() -> int:
	"""Major version of the running Frappe (15, 16, ...)."""
	return int(str(frappe.__version__).split(".", 1)[0])


def erpnext_before_tests():
	"""Complete the ERPNext setup wizard on a fresh test site.

	v15 ships `erpnext.setup.utils.before_tests` (also an ERPNext hook). v16 removed it, so
	the same steps are done here. On v15 the ERPNext function itself is called, unchanged.
	"""
	if frappe_major() < 16:
		if frappe.db.a_row_exists("Company"):
			return
		from erpnext.setup.utils import before_tests

		return before_tests()

	from erpnext.setup.utils import _enable_all_roles_for_admin, set_defaults_for_tests
	from frappe.desk.page.setup_wizard.setup_wizard import setup_complete
	from frappe.utils import now_datetime

	frappe.clear_cache()
	if not frappe.db.a_row_exists("Company"):
		year = now_datetime().year
		setup_complete(
			{
				"currency": "USD",
				"full_name": "Test User",
				"company_name": "Wind Power LLC",
				"timezone": "America/New_York",
				"company_abbr": "WP",
				"industry": "Manufacturing",
				"country": "United States",
				"fy_start_date": f"{year}-01-01",
				"fy_end_date": f"{year}-12-31",
				"language": "english",
				"company_tagline": "Testing",
				"email": "test@erpnext.com",
				"password": "test",
				"chart_of_accounts": "Standard",
			}
		)
	frappe.db.sql("delete from `tabItem Price`")
	_enable_all_roles_for_admin()
	set_defaults_for_tests()
	frappe.db.commit()  # nosemgrep
