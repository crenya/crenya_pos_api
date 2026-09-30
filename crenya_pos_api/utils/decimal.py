"""Decimal-string helpers for money and quantity values exchanged with the till.

The protocol carries every amount as a decimal string ("12.345"). Parsing and
formatting here is pure (no site needed); only `to_flt` touches Frappe, because
the conversion into document fields must follow the site's rounding method.
"""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_DECIMAL_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")
MAX_DECIMAL_LENGTH = 32


class DecimalParseError(ValueError):
	"""Raised when a payload value is not a valid decimal string."""


def parse_decimal(value, field="value", allow_none=False):
	"""Parse a protocol decimal (string or int) into a `Decimal`.

	Floats are rejected on purpose: the till must never send binary floats.
	"""
	if value is None or (isinstance(value, str) and not value.strip()):
		if allow_none:
			return None
		raise DecimalParseError(f"{field} is required")

	if isinstance(value, bool):
		raise DecimalParseError(f"{field} must be a decimal string, got a boolean")

	if isinstance(value, int):
		return Decimal(value)

	if isinstance(value, Decimal):
		if not value.is_finite():
			raise DecimalParseError(f"{field} must be a finite number")
		return value

	if not isinstance(value, str):
		raise DecimalParseError(f"{field} must be a decimal string, got {type(value).__name__}")

	text = value.strip()
	if len(text) > MAX_DECIMAL_LENGTH or not _DECIMAL_RE.match(text):
		raise DecimalParseError(f"{field} is not a valid decimal string: {value!r}")

	try:
		return Decimal(text)
	except InvalidOperation:
		raise DecimalParseError(f"{field} is not a valid decimal string: {value!r}")


def as_decimal(value):
	"""Coerce a server-side value (float from a document, int, str, None) to `Decimal`."""
	if value is None or value == "":
		return Decimal(0)
	if isinstance(value, Decimal):
		return value
	if isinstance(value, bool):
		return Decimal(int(value))
	if isinstance(value, int):
		return Decimal(value)
	if isinstance(value, float):
		# repr gives the shortest round-tripping form, so 1.2 -> "1.2" not 1.19999...
		return Decimal(repr(value))
	return parse_decimal(value)


def quantize(value, precision):
	"""Round half away from zero to `precision` decimal places."""
	exponent = Decimal(1).scaleb(-int(precision))
	return as_decimal(value).quantize(exponent, rounding=ROUND_HALF_UP)


def format_money(value, precision):
	"""Fixed-point string with exactly `precision` decimals, e.g. 1.2 -> "1.200"."""
	result = quantize(value, precision)
	if result == 0:
		result = abs(result)
	return f"{result:f}"


def format_number(value, precision=None):
	"""Plain decimal string without trailing zeros, e.g. 3.0 -> "3", 0.50 -> "0.5"."""
	result = quantize(value, precision) if precision is not None else as_decimal(value)
	if result == 0:
		return "0"
	text = f"{result.normalize():f}"
	return text


def format_optional_money(value, precision):
	return None if value is None else format_money(value, precision)


def to_flt(value, precision=None, field="value"):
	"""Parse a protocol decimal and convert it with Frappe's `flt` (site rounding rules)."""
	from frappe.utils import flt

	parsed = parse_decimal(value, field)
	return flt(f"{parsed:f}", precision)


def smallest_unit_tolerance(precision, units):
	"""`units` x the smallest unit of a currency with `precision` decimals (10 units: 3 -> 0.010, 2 -> 0.10)."""
	precision = max(int(precision or 0), 0)
	return (Decimal(units) * Decimal(1).scaleb(-precision)).quantize(Decimal(1).scaleb(-precision))


def within_tolerance(a, b, tolerance):
	"""True when |a - b| <= tolerance, compared exactly in decimal arithmetic."""
	return abs(as_decimal(a) - as_decimal(b)) <= as_decimal(tolerance)
