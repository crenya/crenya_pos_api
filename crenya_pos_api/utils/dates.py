"""Timestamp helpers shared by the API modules."""

import re
from datetime import datetime, timedelta, timezone

_MODIFIED_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d{1,6})?$")
_ISO_RE = re.compile(
	r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|z|[+-]\d{2}:?\d{2})$"
)


def utc_now_iso():
	"""Audit timestamp in UTC ISO-8601, e.g. 2026-09-29T10:00:00Z."""
	return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_db_datetime(value):
	"""Render a DB datetime exactly like Frappe stores it (microseconds always present)."""
	if value is None:
		return None
	if isinstance(value, datetime):
		return value.strftime("%Y-%m-%d %H:%M:%S.%f")
	return str(value)


def is_db_datetime(value):
	return isinstance(value, str) and bool(_MODIFIED_RE.match(value))


def format_date(value):
	if value is None or value == "":
		return None
	if hasattr(value, "isoformat"):
		return value.isoformat()
	return str(value)


def parse_iso_utc(value):
	"""Parse an ISO-8601 timestamp with an explicit offset (`Z` or `+hh:mm`) into an aware UTC datetime.

	Raises ValueError for anything else, including timestamps without an offset.
	"""
	match = _ISO_RE.match(value.strip()) if isinstance(value, str) else None
	if not match:
		raise ValueError(f"not an ISO-8601 UTC timestamp: {value!r}")
	year, month, day, hour, minute, second, fraction, offset = match.groups()
	moment = datetime(
		int(year),
		int(month),
		int(day),
		int(hour),
		int(minute),
		int(second),
		int((fraction or "0").ljust(6, "0")),
	)
	if offset not in ("Z", "z"):
		sign = -1 if offset[0] == "-" else 1
		digits = offset[1:].replace(":", "")
		hours, minutes = int(digits[:2]), int(digits[2:])
		if hours > 23 or minutes > 59:
			raise ValueError(f"invalid UTC offset in {value!r}")
		moment -= sign * timedelta(hours=hours, minutes=minutes)
	return moment.replace(tzinfo=timezone.utc)


def to_site_naive(moment, time_zone):
	"""Aware datetime -> naive wall-clock datetime in `time_zone` (how Frappe stores Datetime fields).

	An unknown or empty time zone keeps UTC.
	"""
	if moment.tzinfo is None:
		moment = moment.replace(tzinfo=timezone.utc)
	target = timezone.utc
	if time_zone:
		from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

		try:
			target = ZoneInfo(time_zone)
		except (ZoneInfoNotFoundError, ValueError):
			target = timezone.utc
	return moment.astimezone(target).replace(tzinfo=None)


def site_naive_to_utc_iso(value, time_zone):
	"""Naive wall-clock datetime in `time_zone` (how Frappe stores Datetime fields) -> `...Z` UTC ISO-8601.

	Accepts a datetime or a DB string; an unknown or empty time zone is read as UTC.
	"""
	if value is None or value == "":
		return None
	if isinstance(value, str):
		value = datetime.fromisoformat(value.strip())
	if value.tzinfo is None:
		source = timezone.utc
		if time_zone:
			from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

			try:
				source = ZoneInfo(time_zone)
			except (ZoneInfoNotFoundError, ValueError):
				source = timezone.utc
		value = value.replace(tzinfo=source)
	return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
