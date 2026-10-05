"""Scheduled jobs."""

import frappe
from frappe.utils import add_days, cint, now_datetime

EVENT_DOCTYPE = "Crenya Sync Event"
DEFAULT_RETENTION_DAYS = 120
MIN_RETENTION_DAYS = 90
PURGE_BATCH_SIZE = 1000


def get_retention_days():
	configured = cint(frappe.conf.get("crenya_pos_event_retention_days")) or DEFAULT_RETENTION_DAYS
	return max(configured, MIN_RETENTION_DAYS)


def retention_plan():
	"""[(retention days, aggregate types)]: each handler's `retention_days`, the site setting for
	handlers without one. Types no handler knows (an uninstalled app) use the site setting too
	and are listed as None."""
	from crenya_pos_api.sync.registry import aggregates

	default_days = get_retention_days()
	handlers = aggregates()
	by_days = {}
	for aggregate_type, handler in handlers.items():
		days = handler.retention_days or default_days
		by_days.setdefault(days, []).append(aggregate_type)
	plan = sorted(by_days.items())
	plan.append((default_days, None))
	return plan, list(handlers)


def _purge_batch(cutoff, aggregate_types, known_types, batch_size):
	event = frappe.qb.DocType(EVENT_DOCTYPE)
	condition = (event.status == "ok") & (event.creation < cutoff)
	if aggregate_types is None:
		condition &= event.aggregate_type.notin(known_types) | event.aggregate_type.isnull()
	else:
		condition &= event.aggregate_type.isin(aggregate_types)
	names = frappe.qb.from_(event).select(event.name).where(condition).limit(batch_size).run(pluck=True)
	if names:
		frappe.db.delete(EVENT_DOCTYPE, {"name": ("in", names)})
	return len(names)


def purge_old_sync_events(batch_size=None):
	"""Delete successful Crenya Sync Events older than their aggregate's retention, in batches
	(one commit each, so no long lock on the table). Errors are kept. Returns the count."""
	batch_size = cint(batch_size) or PURGE_BATCH_SIZE
	plan, known_types = retention_plan()
	deleted = 0
	for days, aggregate_types in plan:
		cutoff = add_days(now_datetime(), -days)
		while True:
			count = _purge_batch(cutoff, aggregate_types, known_types, batch_size)
			deleted += count
			# a daily job working through a large backlog: release the rows batch by batch
			frappe.db.commit()  # nosemgrep
			if count < batch_size:
				break
	return deleted
