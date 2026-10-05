import base64
import json
import unittest

from crenya_pos_api.sync.cursor import Cursor, InvalidCursor, decode_cursor, encode_cursor


def _raw(data):
	return base64.b64encode(json.dumps(data).encode()).decode()


class TestCursor(unittest.TestCase):
	def test_round_trip(self):
		cursor = Cursor(
			entity="item",
			modified="2026-09-29 14:03:15.123456",
			name="MILK-1L",
			tomb_creation="2026-09-29 14:00:00.000000",
			tomb_name="abc123",
		)
		self.assertEqual(decode_cursor(encode_cursor(cursor), "item"), cursor)

	def test_encoded_form_is_base64_json_with_m_and_n(self):
		token = encode_cursor(Cursor(entity="item", modified="2026-09-29 14:03:15.000000", name="A"))
		data = json.loads(base64.urlsafe_b64decode(token))
		self.assertEqual(data["m"], "2026-09-29 14:03:15.000000")
		self.assertEqual(data["n"], "A")
		self.assertTrue(token.startswith("eyJ"))

	def test_scope_round_trips_and_is_left_out_when_none(self):
		cursor = Cursor(
			entity="cashier",
			modified="2026-09-29 14:03:15.000000",
			name="a@x",
			tomb_creation="2026-09-29 14:00:00.000000",
			tomb_name="t1",
			scope="abc",
		)
		token = encode_cursor(cursor)
		self.assertEqual(json.loads(base64.urlsafe_b64decode(token))["s"], "abc")
		self.assertEqual(decode_cursor(token, "cashier"), cursor)
		plain = encode_cursor(Cursor(entity="item", modified="2026-09-29 14:03:15.000000", name="A"))
		self.assertNotIn("s", json.loads(base64.urlsafe_b64decode(plain)))
		self.assertIsNone(decode_cursor(plain, "item").scope)
		with self.assertRaises(InvalidCursor):
			decode_cursor(_raw({"m": "2026-09-29 14:03:15", "n": "X", "s": 5}), "item")

	def test_empty_token_means_start(self):
		self.assertIsNone(decode_cursor(None, "item"))
		self.assertIsNone(decode_cursor("", "item"))

	def test_unicode_names_survive(self):
		cursor = Cursor(entity="customer", modified="2026-09-29 14:03:15.000001", name="أحمد/+=")
		self.assertEqual(decode_cursor(encode_cursor(cursor), "customer").name, "أحمد/+=")

	def test_standard_alphabet_and_missing_padding_accepted(self):
		token = _raw({"m": "2026-09-29 14:03:15", "n": "X"}).rstrip("=")
		cursor = decode_cursor(token, "item")
		self.assertEqual(cursor.modified, "2026-09-29 14:03:15")
		self.assertEqual(cursor.entity, "item")

	def test_rejects_garbage(self):
		for token in ("not base64!!", _raw([1, 2]), base64.b64encode(b"\xff\xfe").decode(), 123):
			with self.assertRaises(InvalidCursor, msg=repr(token)):
				decode_cursor(token, "item")

	def test_empty_object_has_no_position(self):
		cursor = decode_cursor(_raw({}), "item")
		self.assertIsNone(cursor.modified)
		self.assertIsNone(cursor.tomb_creation)

	def test_rejects_partial_position(self):
		with self.assertRaises(InvalidCursor):
			decode_cursor(_raw({"m": "2026-09-29 14:03:15"}), "item")

	def test_rejects_bad_timestamp(self):
		with self.assertRaises(InvalidCursor):
			decode_cursor(_raw({"m": "2026-09-29'; drop table", "n": "X"}), "item")
		with self.assertRaises(InvalidCursor):
			decode_cursor(_raw({"tc": "yesterday"}), "item")

	def test_rejects_cursor_of_other_entity(self):
		token = encode_cursor(Cursor(entity="item", modified="2026-09-29 14:03:15.000000", name="A"))
		with self.assertRaises(InvalidCursor):
			decode_cursor(token, "customer")

	def test_rejects_non_string_fields(self):
		with self.assertRaises(InvalidCursor):
			decode_cursor(_raw({"m": 5, "n": "A"}), "item")


if __name__ == "__main__":
	unittest.main()
