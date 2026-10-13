"""app.crenya.com's Shops pages: `/api/method/crenya_pos_api.api.web_admin.<name>`.

The same method names and answer shapes as crenya_restaurant.api.web_admin, so the web's shared
till-admin pages differ only by the API path. Each submodule holds one page's methods; they are
re-exported here so `crenya_pos_api.api.web_admin.<name>` resolves. Who manages a shop: `scope`.
"""

from crenya_pos_api.api.web_admin.outlets import devices, downloads, outlets, set_device_enabled
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
	"denominations",
	"devices",
	"downloads",
	"outlet_settings",
	"outlets",
	"save_company_wording",
	"save_denominations",
	"save_outlet_settings",
	"save_scale_rules",
	"save_terminal",
	"scale_rules",
	"set_device_enabled",
	"staff_clear_pin",
	"staff_get",
	"staff_save",
	"terminals",
]
