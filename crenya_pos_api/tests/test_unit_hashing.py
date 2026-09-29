import hashlib
import unittest

from crenya_pos_api.sync.hashing import canonical_json, payload_hash


class TestCanonicalHash(unittest.TestCase):
	def test_keys_sorted_recursively_without_whitespace(self):
		payload = {"b": 1, "a": {"d": [{"z": "1", "y": None}], "c": "x"}}
		self.assertEqual(canonical_json(payload), '{"a":{"c":"x","d":[{"y":null,"z":"1"}]},"b":1}')

	def test_non_ascii_is_not_escaped(self):
		self.assertEqual(canonical_json({"name": "كرينيا"}), '{"name":"كرينيا"}')

	def test_hash_is_sha256_of_utf8_canonical_json(self):
		payload = {"local_id": "u1", "customer_name": "أحمد", "items": [{"qty": "2", "rate": "0.600"}]}
		expected = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
		self.assertEqual(payload_hash(payload), expected)
		self.assertEqual(len(payload_hash(payload)), 64)

	def test_key_order_does_not_change_hash(self):
		self.assertEqual(payload_hash({"a": "1", "b": "2"}), payload_hash({"b": "2", "a": "1"}))

	def test_value_change_changes_hash(self):
		self.assertNotEqual(payload_hash({"qty": "2"}), payload_hash({"qty": "2.0"}))

	def test_known_vector(self):
		# same bytes the till hashes for this payload
		self.assertEqual(
			payload_hash({"b": "2", "a": "1"}),
			hashlib.sha256(b'{"a":"1","b":"2"}').hexdigest(),
		)


if __name__ == "__main__":
	unittest.main()
