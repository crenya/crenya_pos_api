"""Public receipt verification page: /fawtara/verify?id=<crenya_local_id>.

Guest by design (the QR code on a till receipt points here). It shows only the
seller's facts of a submitted invoice, never customer data, answers unknown ids
with a neutral 404 page, is never cached and is rate limited per IP.
"""

import frappe

from crenya_pos_api.sync.fawtara import verification_facts, verify_rate_limited

no_cache = 1
sitemap = 0


def get_context(context):
	context.no_cache = 1
	context.invoice = None
	context.rate_limited = False

	if verify_rate_limited():
		context.rate_limited = True
		context.http_status_code = 429
		return context

	local_id = frappe.form_dict.get("id")
	facts = verification_facts(local_id.strip() if isinstance(local_id, str) else None)
	if not facts:
		context.http_status_code = 404
		return context

	context.invoice = facts
	return context
