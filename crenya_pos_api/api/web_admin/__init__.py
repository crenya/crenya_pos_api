"""app.crenya.com's Shops pages: `/api/method/crenya_pos_api.api.web_admin.<name>`.

The same method names and answer shapes as crenya_restaurant.api.web_admin, so the web's shared
till-admin pages differ only by the API path. Each submodule holds one page's methods; they are
re-exported here so `crenya_pos_api.api.web_admin.<name>` resolves. Who manages a shop: `scope`.
"""

from crenya_pos_api.api.web_admin.credit import credit_customers, save_credit_limit
from crenya_pos_api.api.web_admin.day_close import day_close, day_closes
from crenya_pos_api.api.web_admin.expenses import (
	ensure_cash_supplier,
	expenses_not_booked,
	retry_expense_booking,
	till_expense,
)
from crenya_pos_api.api.web_admin.loyalty import loyalty_programs, save_loyalty_program
from crenya_pos_api.api.web_admin.outlets import devices, downloads, outlets, set_device_enabled
from crenya_pos_api.api.web_admin.promotions import promotions, save_promotion, set_promotion_enabled
from crenya_pos_api.api.web_admin.refused import refused_sales, retry_sale
from crenya_pos_api.api.web_admin.settings import (
	company_wording,
	denominations,
	outlet_settings,
	save_company_wording,
	save_denominations,
	save_outlet_settings,
	save_scale_rules,
	save_terminal,
	scale_rules,
	terminals,
)
from crenya_pos_api.api.web_admin.staff import staff_clear_pin, staff_get, staff_save

__all__ = [
	"company_wording",
	"credit_customers",
	"day_close",
	"day_closes",
	"denominations",
	"devices",
	"downloads",
	"ensure_cash_supplier",
	"expenses_not_booked",
	"loyalty_programs",
	"outlet_settings",
	"outlets",
	"promotions",
	"refused_sales",
	"retry_expense_booking",
	"retry_sale",
	"save_company_wording",
	"save_credit_limit",
	"save_denominations",
	"save_loyalty_program",
	"save_outlet_settings",
	"save_promotion",
	"save_scale_rules",
	"save_terminal",
	"scale_rules",
	"set_device_enabled",
	"set_promotion_enabled",
	"staff_clear_pin",
	"staff_get",
	"staff_save",
	"terminals",
	"till_expense",
]
