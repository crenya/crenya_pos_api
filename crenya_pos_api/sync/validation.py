"""Structural validation of push events and payloads (no database access).

Everything here raises `SyncError(validation)` with a message naming the bad
field, and returns normalized values (Decimals, ints) for the builders.
"""

import re
from datetime import date, time

from crenya_pos_api.sync.errors import VALIDATION, SyncError
from crenya_pos_api.utils.dates import parse_iso_utc
from crenya_pos_api.utils.decimal import DecimalParseError, parse_decimal

AGGREGATE_CUSTOMER = "Customer"
AGGREGATE_SALES_INVOICE = "Sales Invoice"
AGGREGATE_SHIFT = "Crenya POS Shift"
AGGREGATE_TYPES = (AGGREGATE_CUSTOMER, AGGREGATE_SALES_INVOICE, AGGREGATE_SHIFT)
OPERATIONS = ("submit",)

MAX_EVENTS_PER_BATCH = 50
MAX_INVOICE_LINES = 500
MAX_ID_LENGTH = 140
MAX_SHIFT_PAYMENT_ROWS = 50
MAX_SHIFT_INVOICE_IDS = 100000
MAX_NOTES_LENGTH = 2000
MAX_PRICING_RULES_PER_LINE = 20

_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,139}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}(\.\d{1,6})?$")


def _fail(message):
	raise SyncError(VALIDATION, message)


def _decimal(value, field, allow_none=False):
	try:
		return parse_decimal(value, field, allow_none=allow_none)
	except DecimalParseError as e:
		_fail(str(e))


def _required_str(data, key, field=None, max_length=MAX_ID_LENGTH):
	field = field or key
	value = data.get(key)
	if not isinstance(value, str) or not value.strip():
		_fail(f"{field} is required")
	value = value.strip()
	if len(value) > max_length:
		_fail(f"{field} is longer than {max_length} characters")
	return value


def _optional_str(data, key, field=None, max_length=MAX_ID_LENGTH):
	field = field or key
	value = data.get(key)
	if value is None or (isinstance(value, str) and not value.strip()):
		return None
	if not isinstance(value, str):
		_fail(f"{field} must be a string")
	value = value.strip()
	if len(value) > max_length:
		_fail(f"{field} is longer than {max_length} characters")
	return value


def _int(value, field, minimum=None, allow_none=False):
	if value is None and allow_none:
		return None
	if isinstance(value, bool) or not isinstance(value, int):
		if isinstance(value, str) and re.match(r"^-?\d+$", value.strip()):
			value = int(value.strip())
		else:
			_fail(f"{field} must be an integer")
	if minimum is not None and value < minimum:
		_fail(f"{field} must be >= {minimum}")
	return value


def _flag(value, field):
	if value in (0, 1, True, False, None):
		return 1 if value else 0
	if value in ("0", "1"):
		return int(value)
	_fail(f"{field} must be 0 or 1")


def validate_envelope(event):
	"""Validate the event envelope; returns a normalized dict."""
	if not isinstance(event, dict):
		_fail("event must be an object")

	event_id = event.get("event_id")
	if not isinstance(event_id, str) or not _EVENT_ID_RE.match(event_id):
		_fail("event_id is missing or has invalid characters")

	aggregate_type = event.get("aggregate_type")
	if aggregate_type not in AGGREGATE_TYPES:
		_fail(f"aggregate_type must be one of {', '.join(AGGREGATE_TYPES)}")

	operation = event.get("operation")
	if operation not in OPERATIONS:
		_fail(f"operation {operation!r} is not supported for {aggregate_type}")

	local_id = _required_str(event, "local_id")
	sequence_no = _int(event.get("sequence_no"), "sequence_no", minimum=0)

	payload_hash = event.get("payload_hash")
	if not isinstance(payload_hash, str) or not _HASH_RE.match(payload_hash.lower()):
		_fail("payload_hash must be a sha256 hex digest")

	payload = event.get("payload")
	if not isinstance(payload, dict):
		_fail("payload must be an object")

	payload_local_id = payload.get("local_id")
	if payload_local_id is not None and payload_local_id != local_id:
		_fail("payload.local_id does not match the event local_id")

	return {
		"event_id": event_id,
		"aggregate_type": aggregate_type,
		"operation": operation,
		"local_id": local_id,
		"sequence_no": sequence_no,
		"payload_hash": payload_hash.lower(),
		"payload": payload,
	}


def validate_customer_payload(payload):
	return {
		"local_id": _required_str(payload, "local_id"),
		"customer_name": _required_str(payload, "customer_name", max_length=140),
		"mobile_no": _optional_str(payload, "mobile_no", max_length=40),
		"email_id": _optional_str(payload, "email_id", max_length=140),
		"tax_id": _optional_str(payload, "tax_id", max_length=40),
		"customer_group": _optional_str(payload, "customer_group"),
		"territory": _optional_str(payload, "territory"),
	}


def _validate_date(value, field):
	if not isinstance(value, str) or not _DATE_RE.match(value):
		_fail(f"{field} must be YYYY-MM-DD")
	try:
		date.fromisoformat(value)
	except ValueError:
		_fail(f"{field} is not a valid date")
	return value


def _validate_time(value, field):
	if not isinstance(value, str) or not _TIME_RE.match(value):
		_fail(f"{field} must be HH:MM:SS")
	try:
		time.fromisoformat(value.split(".")[0])
	except ValueError:
		_fail(f"{field} is not a valid time")
	return value


def _validate_pricing_rules(value, field):
	"""Optional list of Pricing Rule names applied to a line (stored for reporting only)."""
	if value is None:
		return []
	if not isinstance(value, list):
		_fail(f"{field} must be a list")
	if len(value) > MAX_PRICING_RULES_PER_LINE:
		_fail(f"{field} may have at most {MAX_PRICING_RULES_PER_LINE} entries")
	names = []
	for index, name in enumerate(value):
		label = f"{field}[{index}]"
		if not isinstance(name, str) or not name.strip():
			_fail(f"{label} must be a non-empty string")
		name = name.strip()
		if len(name) > MAX_ID_LENGTH:
			_fail(f"{label} is longer than {MAX_ID_LENGTH} characters")
		names.append(name)
	return list(dict.fromkeys(names))


def _validate_item(row, index, is_return):
	label = f"items[{index}]"
	if not isinstance(row, dict):
		_fail(f"{label} must be an object")

	qty = _decimal(row.get("qty"), f"{label}.qty")
	if qty == 0:
		_fail(f"{label}.qty must not be zero")
	if is_return and qty > 0:
		_fail(f"{label}.qty must be negative on a return")
	if not is_return and qty < 0:
		_fail(f"{label}.qty must be positive on a sale")

	rate = _decimal(row.get("rate"), f"{label}.rate")
	if rate < 0:
		_fail(f"{label}.rate must not be negative")

	conversion_factor = _decimal(row.get("conversion_factor"), f"{label}.conversion_factor", allow_none=True)
	if conversion_factor is None:
		conversion_factor = parse_decimal("1")
	if conversion_factor <= 0:
		_fail(f"{label}.conversion_factor must be positive")

	price_list_rate = _decimal(row.get("price_list_rate"), f"{label}.price_list_rate", allow_none=True)
	if price_list_rate is not None and price_list_rate < 0:
		_fail(f"{label}.price_list_rate must not be negative")

	discount_percentage = _decimal(
		row.get("discount_percentage"), f"{label}.discount_percentage", allow_none=True
	)
	if discount_percentage is not None and not (0 <= discount_percentage <= 100):
		_fail(f"{label}.discount_percentage must be between 0 and 100")

	amount = _decimal(row.get("amount"), f"{label}.amount", allow_none=True)

	against_line_no = _int(row.get("against_line_no"), f"{label}.against_line_no", minimum=1, allow_none=True)
	against_row_name = _optional_str(row, "against_row_name", f"{label}.against_row_name")
	if not is_return and (against_line_no or against_row_name):
		_fail(f"{label} references an original row but the invoice is not a return")

	return {
		"line_no": _int(row.get("line_no", index + 1), f"{label}.line_no", minimum=1),
		"item_code": _required_str(row, "item_code", f"{label}.item_code"),
		"item_name": _optional_str(row, "item_name", f"{label}.item_name", max_length=500),
		"qty": qty,
		"uom": _optional_str(row, "uom", f"{label}.uom"),
		"conversion_factor": conversion_factor,
		"price_list_rate": price_list_rate,
		"discount_percentage": discount_percentage,
		"rate": rate,
		"amount": amount,
		"item_tax_template": _optional_str(row, "item_tax_template", f"{label}.item_tax_template"),
		"against_line_no": against_line_no,
		"against_row_name": against_row_name,
		# optional: tills without promotions omit both keys
		"pricing_rules": _validate_pricing_rules(row.get("pricing_rules"), f"{label}.pricing_rules"),
		"is_free_item": _flag(row.get("is_free_item"), f"{label}.is_free_item"),
		# optional: tills without batch support omit it; one batch per line (the till splits lines)
		"batch_no": _optional_str(row, "batch_no", f"{label}.batch_no"),
	}


def _validate_payments(payments, is_return):
	if payments is None:
		payments = []
	if not isinstance(payments, list):
		_fail("payments must be a list")

	merged = {}
	for index, row in enumerate(payments):
		label = f"payments[{index}]"
		if not isinstance(row, dict):
			_fail(f"{label} must be an object")
		mode = _required_str(row, "mode_of_payment", f"{label}.mode_of_payment")
		amount = _decimal(row.get("amount"), f"{label}.amount")
		if is_return and amount > 0:
			_fail(f"{label}.amount must be negative on a return")
		if not is_return and amount < 0:
			_fail(f"{label}.amount must not be negative on a sale")
		merged[mode] = merged.get(mode, parse_decimal("0")) + amount

	return [{"mode_of_payment": mode, "amount": amount} for mode, amount in merged.items()]


CLIENT_TOTAL_FIELDS = (
	"net_total",
	"total_taxes",
	"grand_total",
	"rounding_adjustment",
	"rounded_total",
	"paid_amount",
)


def _validate_client_totals(totals):
	if not isinstance(totals, dict):
		_fail("client_totals is required")
	result = {}
	for field in CLIENT_TOTAL_FIELDS:
		result[field] = _decimal(
			totals.get(field), f"client_totals.{field}", allow_none=field != "grand_total"
		)
	return result


MAX_LOYALTY_POINTS = 10**9


def _validate_loyalty(value, is_return):
	"""Optional `loyalty: {points, amount}` redemption; None when absent."""
	if value is None:
		return None
	if not isinstance(value, dict):
		_fail("loyalty must be an object")
	if is_return:
		_fail("loyalty points cannot be redeemed on a return")
	points = _int(value.get("points"), "loyalty.points", minimum=1)
	if points > MAX_LOYALTY_POINTS:
		_fail(f"loyalty.points must be at most {MAX_LOYALTY_POINTS}")
	amount = _decimal(value.get("amount"), "loyalty.amount")
	if amount <= 0:
		_fail("loyalty.amount must be positive")
	return {"points": points, "amount": amount}


def is_open_return(data):
	"""A return that names no original invoice (a return without an invoice)."""
	return bool(data["is_return"]) and not (data["return_against"] or data["return_against_local_id"])


def _validate_open_return(lines, remarks):
	"""Return without an invoice: no original rows to point at, and the reason is required."""
	for index, line in enumerate(lines):
		if line["against_line_no"] or line["against_row_name"]:
			_fail(f"items[{index}] references an original row but the return names no original invoice")
	if not remarks:
		_fail("remarks (the return reason) is required on a return without an invoice")


def validate_invoice_payload(payload):
	"""Validate a Sales Invoice payload; returns a normalized dict."""
	is_return = _flag(payload.get("is_return"), "is_return")

	items = payload.get("items")
	if not isinstance(items, list) or not items:
		_fail("items must be a non-empty list")
	if len(items) > MAX_INVOICE_LINES:
		_fail(f"an invoice may have at most {MAX_INVOICE_LINES} lines")

	lines = [_validate_item(row, index, is_return) for index, row in enumerate(items)]
	line_numbers = [line["line_no"] for line in lines]
	if len(set(line_numbers)) != len(line_numbers):
		_fail("items[].line_no must be unique")

	return_against = _optional_str(payload, "return_against")
	return_against_local_id = _optional_str(payload, "return_against_local_id")
	if not is_return and (return_against or return_against_local_id):
		_fail("return_against is only allowed on a return")

	remarks = _optional_str(payload, "remarks", max_length=2000)
	if is_return and not (return_against or return_against_local_id):
		_validate_open_return(lines, remarks)

	return {
		"local_id": _required_str(payload, "local_id"),
		"offline_number": _optional_str(payload, "offline_number"),
		"pos_profile": _required_str(payload, "pos_profile"),
		"company": _required_str(payload, "company"),
		"customer": _optional_str(payload, "customer"),
		"customer_local_id": _optional_str(payload, "customer_local_id"),
		"posting_date": _validate_date(payload.get("posting_date"), "posting_date"),
		"posting_time": _validate_time(payload.get("posting_time"), "posting_time"),
		"currency": _optional_str(payload, "currency"),
		"is_return": is_return,
		"return_against": return_against,
		"return_against_local_id": return_against_local_id,
		"taxes_and_charges": _optional_str(payload, "taxes_and_charges"),
		"buyer_vatin": _optional_str(payload, "buyer_vatin", max_length=40),
		"cashier": _optional_str(payload, "cashier"),
		"remarks": remarks,
		# optional: tills older than shift support omit the key
		"shift_local_id": _optional_str(payload, "shift_local_id"),
		"items": lines,
		"payments": _validate_payments(payload.get("payments"), is_return),
		"client_totals": _validate_client_totals(payload.get("client_totals")),
		# optional: tills without loyalty support omit the key
		"loyalty": _validate_loyalty(payload.get("loyalty"), is_return),
	}


def _validate_utc_datetime(value, field):
	if value is None or (isinstance(value, str) and not value.strip()):
		_fail(f"{field} is required")
	try:
		return parse_iso_utc(value)
	except ValueError:
		_fail(f"{field} must be an ISO-8601 UTC timestamp, e.g. 2026-09-29T12:05:00Z")


def _validate_shift_payments(payments):
	if payments is None:
		payments = []
	if not isinstance(payments, list):
		_fail("payments must be a list")
	if len(payments) > MAX_SHIFT_PAYMENT_ROWS:
		_fail(f"a shift may have at most {MAX_SHIFT_PAYMENT_ROWS} payment rows")

	rows = []
	seen = set()
	for index, row in enumerate(payments):
		label = f"payments[{index}]"
		if not isinstance(row, dict):
			_fail(f"{label} must be an object")
		mode = _required_str(row, "mode_of_payment", f"{label}.mode_of_payment")
		if mode in seen:
			_fail(f"{label}.mode_of_payment {mode} appears more than once")
		seen.add(mode)
		expected = _decimal(row.get("expected"), f"{label}.expected")
		counted = _decimal(row.get("counted"), f"{label}.counted")
		difference = _decimal(row.get("difference"), f"{label}.difference", allow_none=True)
		if difference is None:
			difference = counted - expected
		rows.append(
			{"mode_of_payment": mode, "expected": expected, "counted": counted, "difference": difference}
		)
	return rows


def _validate_invoice_local_ids(value):
	if value is None:
		return []
	if not isinstance(value, list):
		_fail("invoice_local_ids must be a list")
	if len(value) > MAX_SHIFT_INVOICE_IDS:
		_fail(f"invoice_local_ids may have at most {MAX_SHIFT_INVOICE_IDS} entries")
	result = []
	for index, local_id in enumerate(value):
		field = f"invoice_local_ids[{index}]"
		if not isinstance(local_id, str) or not local_id.strip():
			_fail(f"{field} must be a non-empty string")
		local_id = local_id.strip()
		if len(local_id) > MAX_ID_LENGTH:
			_fail(f"{field} is longer than {MAX_ID_LENGTH} characters")
		result.append(local_id)
	return list(dict.fromkeys(result))


def validate_shift_payload(payload):
	"""Validate a closed Crenya POS Shift payload; returns a normalized dict.

	`opened_at` / `closed_at` come back as aware UTC datetimes; the builder converts
	them to the site time zone.
	"""
	opened_at = _validate_utc_datetime(payload.get("opened_at"), "opened_at")
	closed_at = _validate_utc_datetime(payload.get("closed_at"), "closed_at")
	if closed_at < opened_at:
		_fail("closed_at must not be before opened_at")

	opening_float = _decimal(payload.get("opening_float"), "opening_float")
	if opening_float < 0:
		_fail("opening_float must not be negative")

	return {
		"local_id": _required_str(payload, "local_id"),
		"shift_number": _required_str(payload, "shift_number"),
		"pos_profile": _required_str(payload, "pos_profile"),
		"company": _required_str(payload, "company"),
		"cashier": _optional_str(payload, "cashier"),
		"opened_at": opened_at,
		"closed_at": closed_at,
		"opening_float": opening_float,
		"sales_count": _int(payload.get("sales_count"), "sales_count", minimum=0),
		"returns_count": _int(payload.get("returns_count"), "returns_count", minimum=0),
		"sales_total": _decimal(payload.get("sales_total"), "sales_total"),
		"returns_total": _decimal(payload.get("returns_total"), "returns_total"),
		"net_total": _decimal(payload.get("net_total"), "net_total"),
		"tax_total": _decimal(payload.get("tax_total"), "tax_total"),
		"payments": _validate_shift_payments(payload.get("payments")),
		"invoice_local_ids": _validate_invoice_local_ids(payload.get("invoice_local_ids")),
		"notes": _optional_str(payload, "notes", max_length=MAX_NOTES_LENGTH),
	}
