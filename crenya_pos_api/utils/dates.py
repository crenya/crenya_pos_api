"""Timestamp helpers shared by the API modules."""

import re
from datetime import datetime, timezone

_MODIFIED_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d{1,6})?$")


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
