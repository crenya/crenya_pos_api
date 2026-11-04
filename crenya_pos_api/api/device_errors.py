"""A retail till's errors into the site's Error Log (0.17.0):
`/api/method/crenya_pos_api.api.device_errors.report`.

POST, JSON body, the till's `Authorization: token <key>:<secret>`. The till sends only when the
bootstrap announces `features.device_errors`. Rules: `crenya_pos_api.device_errors` (at most 5
errors a call, 30 calls an hour per till, the same error once per 10 minutes).
"""

import frappe

from crenya_pos_api import device_errors
from crenya_pos_api.sync.context import DEFAULT_DEVICE_TYPE, get_device_context


@frappe.whitelist(methods=["POST"])
def report(device_id: str | None = None, errors: list | str | None = None) -> dict:
	"""Logs up to five errors of the till: each {"id"? (the till's report id: logged once), "message",
	"detail"?, "kind"?, "app"?, "version"?,
	"at"?}. Answers {"logged", "skipped"}; HTTP 429 over the till's hourly limit."""
	request = getattr(frappe.local, "request", None)
	device_errors.check_payload_size(getattr(request, "content_length", None))
	if isinstance(errors, str):
		device_errors.check_payload_size(len(errors.encode()))
		errors = frappe.parse_json(errors)
	return device_errors.record(
		get_device_context(device_id),
		errors,
		{DEFAULT_DEVICE_TYPE},
		device_errors.retail_hit,
		title=device_errors.retail_title,
	)
