"""The bill photo of a till expense (moved down from crenya_restaurant, 0.13.0):
`/api/method/crenya_pos_api.api.expenses.attach_photo`.

POST, JSON body, the device's `Authorization: token <key>:<secret>` (a paired till's call comes
through its hub). The photo is not an outbox event: the till keeps the JPEG as a file and sends it
once the expense has its server name (Ruling 10 of the outlet expenses plan). Errors are
`frappe.PermissionError` (403) or `frappe.ValidationError` (417) with a clear message. Both
products use it: the retail till and the restaurant till.
"""

import base64
import binascii
import hashlib

import frappe
from frappe import _
from frappe.utils import cstr

from crenya_pos_api.expenses import booking
from crenya_pos_api.expenses.product import product_for
from crenya_pos_api.sync.context import check_protocol_version, get_device_context
from crenya_pos_api.web.context import checked, invalid, refuse


def image_kind(content):
	"""'png' or 'jpeg' by the file's magic number, else None."""
	if not isinstance(content, bytes | bytearray):
		return None
	if content.startswith(b"\x89PNG\r\n\x1a\n"):
		return "png"
	if content.startswith(b"\xff\xd8\xff"):
		return "jpeg"
	return None


def decode_image(data):
	"""The bytes of a till's image: base64 (a `data:...;base64,` prefix is dropped), at most
	`booking.PHOTO_MAX_BYTES`, PNG or JPEG. Returns (bytes, kind); ValueError says what is wrong."""
	limit = booking.PHOTO_MAX_BYTES
	if not isinstance(data, str):
		raise ValueError("The image must be sent as base64 text")
	text = data.strip()
	if text.startswith("data:") and "," in text:
		text = text.split(",", 1)[1]
	text = "".join(text.split())
	# a quick bound before decoding: 4 base64 characters per 3 bytes
	if len(text) > (limit + 2) // 3 * 4:
		raise ValueError(f"The image is larger than {limit // 1024} KB")
	try:
		content = base64.b64decode(text, validate=True)
	except (binascii.Error, ValueError):
		raise ValueError("The image is not valid base64")
	if not content:
		raise ValueError("The image is empty")
	if len(content) > limit:
		raise ValueError(f"The image is larger than {limit // 1024} KB")
	kind = image_kind(content)
	if not kind:
		raise ValueError("The image must be a PNG or JPEG file")
	return content, kind


def check_readable(content):
	"""A PNG or JPEG header with a body no image library can read is refused."""
	from io import BytesIO

	from PIL import Image

	try:
		with Image.open(BytesIO(content)) as image:
			image.verify()
	except Exception:
		invalid(_("The image could not be read; send a PNG or JPEG file"))


def _file_of(expense):
	"""The private File of the expense's photo, attached to it."""
	if not expense.photo:
		return None
	return frappe.db.get_value(
		"File",
		{
			"file_url": expense.photo,
			"attached_to_doctype": booking.EXPENSE,
			"attached_to_name": expense.name,
			"is_private": 1,
		},
		["name", "content_hash"],
		as_dict=True,
	)


@frappe.whitelist(methods=["POST"])
def attach_photo(
	device_id: str | None = None,
	local_id: str | None = None,
	photo: str | None = None,
	sha256: str | None = None,
	protocol_version: int | str | None = None,
) -> dict:
	"""Attach the bill photo to the till's expense `local_id`: `photo` is base64 of a JPEG of at
	most 1 MB and `sha256` its hex digest. It becomes a private File attached to the POS Expense
	(`photo`) and to its Purchase Invoice. The same photo again changes nothing. Answers
	{"file": its private address, "already": whether the expense had it}."""
	check_protocol_version(protocol_version)
	ctx = get_device_context(device_id)
	if product_for(ctx.device_type) is None:
		refuse(_("Expenses are recorded on a till, not on a {0} device").format(ctx.device_type))
	if not booking.expenses_supported():
		invalid(_(booking.UNSUPPORTED))
	name = (
		frappe.db.get_value(booking.EXPENSE, {"local_id": cstr(local_id).strip()}, "name")
		if local_id
		else None
	)
	if not name:
		invalid(_("Expense {0} has not reached the server yet").format(local_id or "(none)"))
	expense = frappe.get_doc(booking.EXPENSE, name)
	if expense.outlet != ctx.profile.name:
		refuse(_("Expense {0} belongs to another outlet").format(local_id))

	content, kind = checked(decode_image, photo)
	if kind != "jpeg":
		invalid(_("The bill photo must be a JPEG"))
	if hashlib.sha256(content).hexdigest() != cstr(sha256).strip().lower():
		invalid(_("sha256 does not match the photo"))
	check_readable(content)

	from frappe.core.doctype.file.utils import get_content_hash

	current = _file_of(expense)
	if current and current.content_hash == get_content_hash(content):
		booking.attach_photo_to_invoice(
			expense,
			expense.purchase_invoice if booking.invoice_submitted(expense.purchase_invoice) else None,
		)
		return {"file": expense.photo, "already": True}
	# the device was authorised by get_device_context; nobody writes these in the desk
	new_file = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"{expense.local_id}.jpg",
			"attached_to_doctype": booking.EXPENSE,
			"attached_to_name": expense.name,
			"attached_to_field": "photo",
			"is_private": 1,
			"content": content,
		}
	).insert(ignore_permissions=True)
	expense.photo = new_file.file_url
	expense.save(ignore_permissions=True)
	booking.attach_photo_to_invoice(
		expense,
		expense.purchase_invoice if booking.invoice_submitted(expense.purchase_invoice) else None,
	)
	return {"file": expense.photo, "already": False}
