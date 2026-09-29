"""Scheduled jobs."""

import frappe
from frappe.utils import add_days, cint, now_datetime

DEFAULT_RETENTION_DAYS = 120
# the protocol promises at least 90 days of idempotency history
MIN_RETENTION_DAYS = 90


def get_retention_days():
	configured = cint(frappe.conf.get("crenya_pos_event_retention_days")) or DEFAULT_RETENTION_DAYS
	return max(configured, MIN_RETENTION_DAYS)


def purge_old_sync_events():
	"""Delete successful Crenya Sync Events older than the retention window. Errors are kept."""
	cutoff = add_days(now_datetime(), -get_retention_days())
	frappe.db.delete("Crenya Sync Event", {"status": "ok", "creation": ("<", cutoff)})
	frappe.db.commit()
