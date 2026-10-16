"""The product seam of the expenses engine: what differs between the retail till and the
restaurant till, by the device's type.

One `ExpenseProduct` per product. The engine (`booking`) names no product: every sentence that
points the owner at a settings page takes it from the product, and the push handler, bootstrap
and approvals read the device types, bootstrap key, capability and approver roles from it.
crenya_pos_api registers `RETAIL`; crenya_restaurant registers its own `RESTAURANT` at import
(replacing the neutral one below, which carries the settings page and the keys only).
"""

from collections.abc import Callable
from typing import Any, NamedTuple

from crenya_pos_api.sync.approvals import APPROVER_ROLE
from crenya_pos_api.sync.context import DEFAULT_DEVICE_TYPE, profile_users


class ExpenseProduct(NamedTuple):
	key: str  # "restaurant" | "retail"
	device_types: tuple[str, ...]  # who may push expenses
	bootstrap_key: str  # "restaurant.expenses" | "expenses"
	capability: str  # "restaurant_expenses" | "retail_expenses"
	settings_hint: str  # the owner's settings page, in the messages
	approver_roles: Callable[[Any], tuple[str, ...]]  # fn(DeviceContext)
	outlet_users: Callable[[Any], set[str]]  # fn(POS Profile): who may approve at this outlet


def _restaurant_roles(_ctx=None):
	"""The approver roles of a restaurant till until crenya_restaurant registers its own product
	(release 0.15.7): its `VOID_APPROVER_ROLES`, read from the installed app (0.15.6 and later keep
	them in `constants`). Without crenya_restaurant nobody can ask: that is a programming error."""
	try:
		from crenya_restaurant.constants import VOID_APPROVER_ROLES
	except ImportError:
		raise RuntimeError("The restaurant product is not registered: crenya_restaurant registers it")
	return tuple(VOID_APPROVER_ROLES)


def _unregistered(*_args):
	raise RuntimeError("The restaurant product is not registered: crenya_restaurant registers it")


def manager_roles(_ctx=None):
	"""The approver roles of a retail till: Crenya POS Manager."""
	return (APPROVER_ROLE,)


def applicable_users(profile):
	"""The users of the outlet's *Applicable for Users* (empty: every holder of an approver role)."""
	return set(profile_users(profile))


RETAIL = ExpenseProduct(
	key="retail",
	device_types=(DEFAULT_DEVICE_TYPE,),
	bootstrap_key="expenses",
	capability="retail_expenses",
	settings_hint="POS Profile → Expenses",
	approver_roles=manager_roles,
	outlet_users=applicable_users,
)

# the restaurant's wording and keys. Its approver roles are the restaurant's own (crenya_restaurant
# registers its product over this entry with `register_product`; until it does, the roles are read
# from the installed crenya_restaurant, so a restaurant 0.15.6 whose hook entries this app ignores
# keeps working, and asking without crenya_restaurant fails loudly rather than answering with the
# retail rule). The outlet rule is the same table on both: the profile's Applicable for Users.
RESTAURANT = ExpenseProduct(
	key="restaurant",
	device_types=("restaurant_pos",),
	bootstrap_key="restaurant.expenses",
	capability="restaurant_expenses",
	settings_hint="Restaurants → Settings → Expenses",
	approver_roles=_restaurant_roles,
	outlet_users=applicable_users,
)

_PRODUCTS = {RETAIL.key: RETAIL, RESTAURANT.key: RESTAURANT}


def register_product(product: ExpenseProduct) -> None:
	"""Add `product`, or replace the one with its key (crenya_restaurant registers its own at
	import)."""
	_PRODUCTS[product.key] = product


def product_for(device_type: str | None) -> ExpenseProduct | None:
	"""The product whose devices have `device_type`, or None (a kitchen display or waiter phone)."""
	device_type = device_type or DEFAULT_DEVICE_TYPE
	for product in _PRODUCTS.values():
		if device_type in product.device_types:
			return product
	return None


def product_of_expense(expense) -> ExpenseProduct | None:
	"""The product of a POS Expense by the type of the device that made it, or None (no device, a
	deleted one, or a type no product owns). Callers that know the product pass it instead."""
	import frappe

	device = expense.get("device")
	if not device or not frappe.db.exists("Crenya POS Device", device):
		return None
	return product_for(frappe.db.get_value("Crenya POS Device", device, "device_type"))
