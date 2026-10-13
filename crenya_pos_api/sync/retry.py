"""Retrying a refused sale from app.crenya.com: the `sync_retry` pull entity.

A refused event (a Crenya Sync Event with status `error`, e.g. out of stock) stays on the till
that pushed it. Once the owner has fixed the cause, *Refused sales → Retry* sets the event's
`retry_requested_at` (`api/web_admin/refused.py`). A till that pulls `sync_retry` receives
`{event_id, retry_requested_at}` for each of its own events asked to be retried since it was
last processed, and sends that event again unchanged (same event_id and payload: the push is
idempotent). Once the server has processed it again (ok, or refused again), `processed_at` is
later than the request and the event is sent as a tombstone, so the till drops it.

Every device type may pull it; a till that does not know the entity never asks for it, and its
pushes are processed as before. The entity is `sync.pull.SyncRetrySpec` (beside the other
built-in entities, which this module's rule serves).
"""

ENTITY = "sync_retry"
RETRY_FIELD = "retry_requested_at"


def retry_pending(retry_requested_at, processed_at) -> bool:
	"""Whether a retry asked at `retry_requested_at` still waits for the till (pure): asked, and
	not processed since."""
	if not retry_requested_at:
		return False
	return not processed_at or retry_requested_at > processed_at
