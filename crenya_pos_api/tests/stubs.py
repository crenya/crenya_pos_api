"""A test helper: put stand-in modules into `sys.modules` for a block and take out exactly those
again. `mock.patch.dict(sys.modules, ...)` is not used because it restores the whole dict on exit and
so drops every module imported meanwhile (ERPNext's controllers, loaded lazily), after which a cached
document's class is no longer the class in `sys.modules` and Frappe cannot pickle it."""

import sys
import types
from contextlib import contextmanager


@contextmanager
def stub_modules(modules):
	"""{dotted name: module, or None to make the import fail} for the duration of the block."""
	missing = object()
	saved = {name: sys.modules.get(name, missing) for name in modules}
	sys.modules.update(modules)
	try:
		yield
	finally:
		for name, module in saved.items():
			if module is missing:
				sys.modules.pop(name, None)
			else:
				sys.modules[name] = module


def restaurant_constants(roles):
	"""crenya_restaurant.constants with VOID_APPROVER_ROLES = `roles`, as stand-in modules."""
	constants = types.ModuleType("crenya_restaurant.constants")
	constants.VOID_APPROVER_ROLES = tuple(roles)
	return {
		"crenya_restaurant": types.ModuleType("crenya_restaurant"),
		"crenya_restaurant.constants": constants,
	}
