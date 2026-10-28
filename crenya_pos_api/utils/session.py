"""Running a few lines as Administrator inside a request without damaging the caller's session."""

from contextlib import contextmanager

import frappe


@contextmanager
def elevated_as_administrator():
	"""The session user is Administrator inside the block and exactly what it was after it, however
	the block ends.

	`frappe.set_user` also replaces `session.sid`, `session.data` and `form_dict`; calling it again
	with the old user does not bring those back, and on a cookie request the framework writes the
	wiped session data to the real sid after the response (the user is logged out, CSRF breaks). So the
	sid, the session data object (and its contents) and the form dict are put back as the same objects."""
	session = frappe.local.session
	user, sid, data = session.user, session.sid, session.data
	contents = dict(data) if data is not None else None
	form_dict = frappe.local.form_dict
	try:
		# inside the try: a set_user that fails part-way still gets the caller's session back
		frappe.set_user("Administrator")  # nosemgrep  # the block only: restored in `finally`
		yield
	finally:
		frappe.set_user(user)  # nosemgrep  # back to the caller
		session = frappe.local.session
		session.sid = sid
		session.data = data
		if data is not None and contents is not None and dict(data) != contents:
			data.clear()
			data.update(contents)
		frappe.local.form_dict = form_dict
