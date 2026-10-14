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
	settings_hint="Shops → Settings → Expenses",
	approver_roles=manager_roles,
	outlet_users=applicable_users,
)

# the restaurant's wording and keys; its approvers are the restaurant's own roles, which
# crenya_restaurant registers over this entry (`register_product`)
RESTAURANT = ExpenseProduct(
	key="restaurant",
	device_types=("restaurant_pos",),
	bootstrap_key="restaurant.expenses",
	capability="restaurant_expenses",
	settings_hint="Restaurants → Settings → Expenses",
	approver_roles=manager_roles,
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


def product_of_expense(expense) -> ExpenseProduct:
	"""The product of a POS Expense: by the type of the device that made it, else the retail
	till's (an expense always has a device; the fallback is the neutral wording)."""
	import frappe

	device = expense.get("device")
	device_type = frappe.db.get_value("Crenya POS Device", device, "device_type") if device else None
	return product_for(device_type) or RETAIL
