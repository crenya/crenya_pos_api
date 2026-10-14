"""Store controls of a retail till: which actions need a manager's approval, and who approves.

`APPROVAL_ACTIONS` is the one table of the actions: the till reads the bootstrap's `approvals`
(`approvals_bootstrap`), the POS Profile's custom fields are made from it (setup/install.py) and
the web settings form shows those fields. A profile's `allow_discount_change` and
`allow_rate_change` keep their meaning, what a cashier may do alone; beyond that, an action
switched on here is allowed with the PIN of a holder of an approver role (or at once when such a
holder is signed in at the till).
"""

from frappe.utils import cint, flt

from crenya_pos_api.sync.context import POS_USER_ROLE
from crenya_pos_api.utils.decimal import format_number

APPROVER_ROLE = "Crenya POS Manager"
# (action key, POS Profile field, label, label_ar); the single source the web form and the till read
APPROVAL_ACTIONS = (
	("price_change", "crenya_approve_price_change", "Price change", "تغيير السعر"),
	("discount_over_limit", "crenya_approve_discount_over_limit", "Discount above the limit", "خصم فوق الحد"),
	("line_void", "crenya_approve_line_void", "Remove a scanned line", "حذف صنف ممسوح"),
	("bill_clear", "crenya_approve_bill_clear", "Clear the bill", "مسح الفاتورة"),
	("return", "crenya_approve_return", "Return against an invoice", "مرتجع على فاتورة"),
	("open_return", "crenya_approve_open_return", "Return without an invoice", "مرتجع بدون فاتورة"),
	("no_sale", "crenya_approve_no_sale", "Open the drawer (no sale)", "فتح الدرج بدون بيع"),
	("stock_mode", "crenya_approve_stock_mode", "Change the stock check", "تغيير فحص المخزون"),
	("settings", "crenya_approve_settings", "Till settings and reset", "إعدادات الجهاز وإعادة الضبط"),
	# credit sales (0.13.0): selling on account over the customer's limit needs a manager (on by
	# default); selling on account at all can be made to need one too (off by default)
	(
		"credit_over_limit",
		"crenya_approve_credit_over_limit",
		"Sell on account over the limit",
		"بيع آجل فوق الحد",
	),
	("credit_sale", "crenya_approve_credit_sale", "Sell on account", "بيع بالآجل"),
)
# the actions whose Check starts off on a new POS Profile (every other action starts on)
APPROVAL_DEFAULT_OFF = frozenset({"credit_sale"})
# the privileges the retail staff screen offers and the roles each one gives
RETAIL_PRIVILEGES = {"cashier": (POS_USER_ROLE,), "manager": (POS_USER_ROLE, APPROVER_ROLE)}
# their labels (English, Arabic), in RETAIL_PRIVILEGES order: the web's staff screen offers these
RETAIL_PRIVILEGE_LABELS = {"cashier": ("Cashier", "كاشير"), "manager": ("Manager", "مدير")}
# the most a cashier may discount a bill or line alone, in percent; 0 or blank: no limit
MAX_DISCOUNT_FIELD = "crenya_max_discount_percent"


def max_discount_percent(profile):
	"""The profile's discount limit as a decimal string ("10", "12.5"), or None for no limit (a
	Percent field cannot be empty in the database: blank is 0)."""
	value = flt(profile.get(MAX_DISCOUNT_FIELD))
	return format_number(value) if value > 0 else None


def required_actions(profile):
	"""The action keys whose approval Check is on, in APPROVAL_ACTIONS order. A profile not yet
	migrated (without the fields) requires none: today's behaviour, as the patch keeps it."""
	return [key for key, field, _label, _label_ar in APPROVAL_ACTIONS if cint(profile.get(field))]


CREDIT_ACTIONS = ("credit_over_limit", "credit_sale")


def approvals_bootstrap(profile) -> dict:
	"""The bootstrap's `approvals` of a retail till of `profile`. It is always sent to a till. An
	outlet that sells on account (Credit sales switch) also lists its two credit actions in `labels`
	(`credit_over_limit`, `credit_sale`; whether each is required is the owner's Check, in `required`),
	so the till never has to guess the approver role or the wording."""
	from crenya_pos_api.sync.credit import CREDIT_SALES_FIELD

	block = {
		"approver_roles": [APPROVER_ROLE],
		"max_discount_percent": max_discount_percent(profile),
		"required": required_actions(profile),
	}
	if cint(profile.get(CREDIT_SALES_FIELD)):
		block["labels"] = [
			{"action": key, "label": label, "label_ar": label_ar}
			for key, _field, label, label_ar in APPROVAL_ACTIONS
			if key in CREDIT_ACTIONS
		]
	return block
