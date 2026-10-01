"""Opaque keyset cursor for pull_changes.

The cursor is base64 JSON. `m`/`n` are the (modified, name) of the last record
delivered; `tc`/`tn` the (creation, name) of the last tombstone delivered; `e`
pins the cursor to its entity. Entities that also follow stock movements
(`batch`) carry `sm`/`sn`, the (modified, name) of the last Stock Ledger Entry
looked at; the keys are left out for all other entities. Clients store it
verbatim.
"""

import base64
import binascii
import json
from dataclasses import dataclass

from crenya_pos_api.utils.dates import is_db_datetime

MAX_CURSOR_LENGTH = 4096


class InvalidCursor(ValueError):
	pass


@dataclass(frozen=True)
class Cursor:
	entity: str
	modified: str | None = None
	name: str | None = None
	tomb_creation: str | None = None
	tomb_name: str | None = None
	stock_modified: str | None = None
	stock_name: str | None = None

	def to_dict(self):
		data = {
			"e": self.entity,
			"m": self.modified,
			"n": self.name,
			"tc": self.tomb_creation,
			"tn": self.tomb_name,
		}
		if self.stock_modified is not None:
			data["sm"] = self.stock_modified
			data["sn"] = self.stock_name or ""
		return data


def encode_cursor(cursor):
	raw = json.dumps(cursor.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
	return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _optional_str(data, key):
	value = data.get(key)
	if value is not None and not isinstance(value, str):
		raise InvalidCursor(f"cursor field {key} must be a string")
	return value


def decode_cursor(token, entity=None):
	"""Decode a cursor token; returns None for an empty token (start from the beginning)."""
	if token is None or token == "":
		return None
	if not isinstance(token, str) or len(token) > MAX_CURSOR_LENGTH:
		raise InvalidCursor("cursor must be a string")

	padded = token + "=" * (-len(token) % 4)
	try:
		# accept both url-safe and standard alphabets
		raw = base64.b64decode(padded.replace("-", "+").replace("_", "/"), validate=True)
		data = json.loads(raw.decode("utf-8"))
	except (binascii.Error, ValueError, UnicodeDecodeError):
		raise InvalidCursor("cursor is not valid")

	if not isinstance(data, dict):
		raise InvalidCursor("cursor is not valid")

	modified = _optional_str(data, "m")
	name = _optional_str(data, "n")
	tomb_creation = _optional_str(data, "tc")
	tomb_name = _optional_str(data, "tn")
	stock_modified = _optional_str(data, "sm")
	stock_name = _optional_str(data, "sn")
	cursor_entity = _optional_str(data, "e")

	if (modified is None) != (name is None):
		raise InvalidCursor("cursor must carry both m and n")
	for value in (modified, tomb_creation, stock_modified):
		if value is not None and not is_db_datetime(value):
			raise InvalidCursor("cursor timestamp is not valid")
	if tomb_creation is None and tomb_name is not None:
		raise InvalidCursor("cursor tombstone position is not valid")
	if stock_modified is None and stock_name is not None:
		raise InvalidCursor("cursor stock position is not valid")
	if entity is not None and cursor_entity is not None and cursor_entity != entity:
		raise InvalidCursor(f"cursor belongs to entity {cursor_entity}, not {entity}")

	return Cursor(
		entity=cursor_entity or entity or "",
		modified=modified,
		name=name,
		tomb_creation=tomb_creation,
		tomb_name=tomb_name or "",
		stock_modified=stock_modified,
		stock_name=(stock_name or "") if stock_modified is not None else None,
	)
