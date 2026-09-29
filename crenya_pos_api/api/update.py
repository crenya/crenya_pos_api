"""Till auto-update endpoints for the Tauri updater.

Both return raw responses (not wrapped in `message`): Frappe passes a werkzeug
Response returned by a whitelisted method straight through to the client.
"""

import json

import frappe
from werkzeug.wrappers import Response

from crenya_pos_api.sync import update as update_service
from crenya_pos_api.sync.context import get_device_context


# GET requests are not committed, so the device's last_seen is not touched here
@frappe.whitelist(methods=["GET"])
def check(device_id: str | None = None, target: str | None = None, current_version: str | None = None):
	ctx = get_device_context(device_id, touch=False)
	document = update_service.check_update(ctx, target, current_version)
	if document is None:
		return Response(status=204)
	return Response(json.dumps(document), status=200, mimetype="application/json")


@frappe.whitelist(methods=["GET"])
def download(release: str | None = None, target: str | None = None, device_id: str | None = None):
	ctx = get_device_context(device_id, touch=False)
	filename, content = update_service.get_artifact(ctx, release, target)
	response = Response(content, status=200, mimetype="application/octet-stream")
	response.headers.add("Content-Disposition", "attachment", filename=filename)
	return response
