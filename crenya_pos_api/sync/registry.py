"""Extension registry: built-in pull entities, push aggregates and hooks of other apps.

Other Frappe apps plug into the till protocol from their `hooks.py`:

        crenya_pos_pull_entities = {"<entity>": "<dotted path of an EntitySpec subclass or instance>"}
        crenya_pos_aggregates = {"<aggregate_type>": "<dotted path of an AggregateHandler subclass>"}
        crenya_pos_bootstrap = ["<dotted path of fn(ctx, doc)>"]          # mutates the bootstrap dict
        crenya_pos_capabilities = ["<dotted path of fn() -> dict>"]       # merged into `features`
        crenya_pos_invoice_extenders = ["<dotted path of fn(ctx, doc, data, notes)>"]
        crenya_pos_device_roles = ["<role allowed to use a device besides Crenya POS User>"]

Built-ins always come first and cannot be replaced; hook entries follow in app install
order. When two apps register the same entity or aggregate name the later app wins (as
Frappe does for its own override hooks). Hooks are read once per request. A hook that
cannot be imported, or that does not have the expected shape, raises
`ExtensionHookError` (code `internal`, retryable) naming the hook and the path.

Apps import this module through `crenya_pos_api.extension`, never directly.
"""

import importlib
import re

import frappe

from crenya_pos_api.sync.context import POS_USER_ROLE
from crenya_pos_api.sync.errors import ExtensionHookError, clean_message, error_dict

ENTITY_HOOK = "crenya_pos_pull_entities"
AGGREGATE_HOOK = "crenya_pos_aggregates"
BOOTSTRAP_HOOK = "crenya_pos_bootstrap"
CAPABILITY_HOOK = "crenya_pos_capabilities"
INVOICE_EXTENDER_HOOK = "crenya_pos_invoice_extenders"
DEVICE_ROLE_HOOK = "crenya_pos_device_roles"

_CACHE_ATTR = "crenya_pos_registry"
_ENTITY_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_OPERATION_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_AGGREGATE_TYPE_LENGTH = 140


# hook plumbing


def _fail(message):
	message = clean_message(message)
	try:
		frappe.local.response["error"] = error_dict(
			ExtensionHookError.code, message, ExtensionHookError.retryable
		)
	except (AttributeError, RuntimeError):
		# no request context (unit tests / console / scheduler)
		pass
	raise ExtensionHookError(message)


def _hooks_available():
	"""Hooks are read from the site's installed apps; without a site (plain unit tests) only
	the built-ins exist."""
	return bool(getattr(frappe.local, "site", None))


def _hook(name, default):
	if not _hooks_available():
		return default
	# a hook no app defines comes back as [] whatever its shape
	value = frappe.get_hooks(name)
	return value if value else default


def _cache():
	"""Per-request cache: frappe.local is reset at the start of every request and job."""
	cache = getattr(frappe.local, _CACHE_ATTR, None)
	if cache is None:
		cache = {}
		if _hooks_available():
			setattr(frappe.local, _CACHE_ATTR, cache)
	return cache


def clear_cache():
	"""Forget the hooks read in this request (tests; after installing an app in a console)."""
	if getattr(frappe.local, _CACHE_ATTR, None) is not None:
		setattr(frappe.local, _CACHE_ATTR, None)


def _cached(key, loader):
	cache = _cache()
	if key not in cache:
		cache[key] = loader()
	return cache[key]


def load_object(path, hook):
	"""Import `package.module.attribute`; a failure names the hook and the path."""
	if not isinstance(path, str) or "." not in path.strip():
		_fail(f"{hook}: {path!r} is not a dotted path to a Python object")
	module_name, _, attribute = path.strip().rpartition(".")
	try:
		module = importlib.import_module(module_name)
	except Exception as exc:
		# anything raised while importing the app's module is reported, not just ImportError
		_fail(f"{hook}: cannot import {module_name} for {path} ({type(exc).__name__}: {exc})")
	try:
		return getattr(module, attribute)
	except AttributeError:
		_fail(f"{hook}: {module_name} has no attribute {attribute!r} ({path})")


def _last_path(value):
	"""Frappe merges dict hooks of all apps into {key: [path, ...]}; the last app wins."""
	if isinstance(value, list | tuple):
		return value[-1] if value else None
	return value


def _path_list(value):
	if isinstance(value, str):
		return [value]
	if isinstance(value, list | tuple):
		return list(value)
	return None


def _instance(obj, base, hook, key, path):
	if isinstance(obj, type) and issubclass(obj, base):
		try:
			return obj()
		except Exception as exc:
			_fail(f"{hook}[{key!r}]: {path} could not be created ({type(exc).__name__}: {exc})")
	if isinstance(obj, base):
		return obj
	_fail(f"{hook}[{key!r}]: {path} is not an {base.__name__} subclass or instance")


# pull entities


def _load_entities():
	from crenya_pos_api.sync.pull import ENTITIES, EntitySpec

	merged = dict(ENTITIES)
	hooked = _hook(ENTITY_HOOK, {})
	if not isinstance(hooked, dict):
		_fail(f"{ENTITY_HOOK} must be a dict of entity name -> dotted path")
	for name, value in hooked.items():
		path = _last_path(value)
		if not isinstance(name, str) or not _ENTITY_NAME_RE.match(name):
			_fail(f"{ENTITY_HOOK}: entity name {name!r} must be lower case letters, digits and _")
		if name in ENTITIES:
			_fail(f"{ENTITY_HOOK}: {name!r} is a built-in entity and cannot be replaced ({path})")
		spec = _instance(load_object(path, ENTITY_HOOK), EntitySpec, ENTITY_HOOK, name, path)
		if not isinstance(spec.doctype, str) or not spec.doctype:
			_fail(f"{ENTITY_HOOK}[{name!r}]: {path} has no doctype")
		merged[name] = spec
	return merged


def entities():
	"""{entity name: EntitySpec} — built-ins first, then hook entities."""
	return _cached("entities", _load_entities)


def entity(name):
	"""The EntitySpec of a pull entity, or None."""
	if not isinstance(name, str):
		return None
	return entities().get(name)


# push aggregates


def _check_handler(handler, aggregate_type, path):
	if handler.aggregate_type is None:
		handler.aggregate_type = aggregate_type
	elif handler.aggregate_type != aggregate_type:
		_fail(
			f"{AGGREGATE_HOOK}[{aggregate_type!r}]: {path} declares aggregate_type "
			f"{handler.aggregate_type!r}"
		)

	operations = handler.operations
	if isinstance(operations, str) or not isinstance(operations, list | tuple) or not operations:
		_fail(f"{AGGREGATE_HOOK}[{aggregate_type!r}]: {path} must list its operations")
	for operation in operations:
		if not isinstance(operation, str) or not _OPERATION_RE.match(operation):
			_fail(
				f"{AGGREGATE_HOOK}[{aggregate_type!r}]: operation {operation!r} must be lower case "
				"letters, digits and _"
			)
	handler.operations = tuple(dict.fromkeys(operations))

	days = handler.retention_days
	if days is not None and (isinstance(days, bool) or not isinstance(days, int) or days < 1):
		_fail(f"{AGGREGATE_HOOK}[{aggregate_type!r}]: retention_days must be None or a positive integer")


def _load_aggregates():
	from crenya_pos_api.sync.aggregates import AggregateHandler, builtin_handlers

	builtins = builtin_handlers()
	merged = dict(builtins)
	hooked = _hook(AGGREGATE_HOOK, {})
	if not isinstance(hooked, dict):
		_fail(f"{AGGREGATE_HOOK} must be a dict of aggregate_type -> dotted path")
	for aggregate_type, value in hooked.items():
		path = _last_path(value)
		if (
			not isinstance(aggregate_type, str)
			or not aggregate_type.strip()
			or aggregate_type != aggregate_type.strip()
			or len(aggregate_type) > MAX_AGGREGATE_TYPE_LENGTH
		):
			_fail(f"{AGGREGATE_HOOK}: aggregate_type {aggregate_type!r} is not a valid name")
		if aggregate_type in builtins:
			_fail(f"{AGGREGATE_HOOK}: {aggregate_type!r} is a built-in aggregate and cannot be replaced")
		handler = _instance(
			load_object(path, AGGREGATE_HOOK), AggregateHandler, AGGREGATE_HOOK, aggregate_type, path
		)
		_check_handler(handler, aggregate_type, path)
		merged[aggregate_type] = handler
	return merged


def aggregates():
	"""{aggregate_type: AggregateHandler} — Customer, Sales Invoice, Crenya POS Shift, then hooks."""
	return _cached("aggregates", _load_aggregates)


def handler(aggregate_type):
	"""The AggregateHandler of an aggregate type, or None."""
	if not isinstance(aggregate_type, str):
		return None
	return aggregates().get(aggregate_type)


def handler_for_doctype(doctype):
	"""The first handler whose documents are of `doctype`, or None."""
	for candidate in aggregates().values():
		if candidate.result_doctype == doctype:
			return candidate
	return None


# function hooks


def _load_functions(hook):
	paths = _path_list(_hook(hook, []))
	if paths is None:
		_fail(f"{hook} must be a list of dotted paths")
	functions = []
	for path in paths:
		function = load_object(path, hook)
		if not callable(function):
			_fail(f"{hook}: {path} is not callable")
		functions.append(function)
	return functions


def bootstrap_hooks():
	"""`crenya_pos_bootstrap` functions: fn(ctx, doc) -> None, mutating the bootstrap dict."""
	return _cached(BOOTSTRAP_HOOK, lambda: _load_functions(BOOTSTRAP_HOOK))


def capability_hooks():
	"""`crenya_pos_capabilities` functions: fn() -> dict of extra `features` flags."""
	return _cached(CAPABILITY_HOOK, lambda: _load_functions(CAPABILITY_HOOK))


def invoice_extenders():
	"""`crenya_pos_invoice_extenders` functions: fn(ctx, doc, data, notes) -> None."""
	return _cached(INVOICE_EXTENDER_HOOK, lambda: _load_functions(INVOICE_EXTENDER_HOOK))


def extend_bootstrap(ctx, doc):
	for function in bootstrap_hooks():
		function(ctx, doc)
	return doc


def extend_features(features):
	"""Merge the capability hooks into `features`; keys already present (core flags, or an
	earlier hook's) are kept."""
	for function in capability_hooks():
		extra = function()
		if extra is None:
			continue
		if not isinstance(extra, dict):
			_fail(f"{CAPABILITY_HOOK}: {function.__module__}.{function.__name__} must return a dict")
		for key, value in extra.items():
			features.setdefault(key, value)
	return features


def extend_invoice(ctx, doc, data, notes):
	"""Run the invoice extenders on a built Sales Invoice, before it is inserted."""
	for function in invoice_extenders():
		function(ctx, doc, data, notes)
	return doc


# device roles


def _load_device_roles():
	extra = _path_list(_hook(DEVICE_ROLE_HOOK, []))
	if extra is None:
		_fail(f"{DEVICE_ROLE_HOOK} must be a list of role names")
	roles = [POS_USER_ROLE]
	for role in extra:
		if not isinstance(role, str) or not role.strip():
			_fail(f"{DEVICE_ROLE_HOOK}: {role!r} is not a role name")
		roles.append(role.strip())
	return tuple(dict.fromkeys(roles))


def device_roles():
	"""Roles that may register and use a device: Crenya POS User, then `crenya_pos_device_roles`."""
	return _cached(DEVICE_ROLE_HOOK, _load_device_roles)
