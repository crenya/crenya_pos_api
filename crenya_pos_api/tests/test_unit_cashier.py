import base64
import unittest
from unittest import mock

from crenya_pos_api.sync import cashier
from crenya_pos_api.sync.cashier import (
	InvalidPin,
	check_pin_format,
	hash_pin,
	is_masked_pin,
	verify_pin,
)

VECTOR_SALT = bytes(range(16))
VECTOR = "pbkdf2_sha256$120000$AAECAwQFBgcICQoLDA0ODw==$JJfkH+VveNsEbC4vvoIyDBewEbKwjYEa29445Woz1lY="


class TestHashPin(unittest.TestCase):
	def test_known_vector(self):
		# the till verifies PINs against exactly this encoding
		self.assertEqual(hash_pin("4826", VECTOR_SALT), VECTOR)

	def test_random_salt_format(self):
		encoded = hash_pin("123456")
		algorithm, iterations, salt, key = encoded.split("$")
		self.assertEqual(algorithm, "pbkdf2_sha256")
		self.assertEqual(iterations, "120000")
		self.assertEqual(len(base64.b64decode(salt, validate=True)), 16)
		self.assertEqual(len(base64.b64decode(key, validate=True)), 32)
		self.assertNotEqual(hash_pin("123456"), encoded, "each hash must get a fresh salt")

	def test_verify(self):
		self.assertTrue(verify_pin("4826", VECTOR))
		self.assertFalse(verify_pin("4827", VECTOR))
		self.assertFalse(verify_pin("48260", VECTOR))
		encoded = hash_pin("0042")
		self.assertTrue(verify_pin("0042", encoded))
		self.assertFalse(verify_pin("042", encoded))

	def test_verify_rejects_malformed_hashes(self):
		for encoded in (
			None,
			"",
			"4826",
			"pbkdf2_sha256$120000$AAECAwQFBgcICQoLDA0ODw==",
			"md5$120000$AAECAwQFBgcICQoLDA0ODw==$JJfkH+VveNsEbC4vvoIyDBewEbKwjYEa29445Woz1lY=",
			"pbkdf2_sha256$0$AAECAwQFBgcICQoLDA0ODw==$JJfkH+VveNsEbC4vvoIyDBewEbKwjYEa29445Woz1lY=",
			"pbkdf2_sha256$abc$AAECAwQFBgcICQoLDA0ODw==$JJfkH+VveNsEbC4vvoIyDBewEbKwjYEa29445Woz1lY=",
			"pbkdf2_sha256$120000$not base64!$JJfkH+VveNsEbC4vvoIyDBewEbKwjYEa29445Woz1lY=",
			"pbkdf2_sha256$120000$$JJfkH+VveNsEbC4vvoIyDBewEbKwjYEa29445Woz1lY=",
		):
			with self.subTest(encoded=encoded):
				self.assertFalse(verify_pin("4826", encoded))

	def test_hash_rejects_invalid_pin_and_salt(self):
		with self.assertRaises(InvalidPin):
			hash_pin("12a4", VECTOR_SALT)
		for salt in (b"", "0123456789abcdef", 42):
			with self.subTest(salt=salt), self.assertRaises(ValueError):
				hash_pin("4826", salt)


class TestPinFormat(unittest.TestCase):
	def test_valid(self):
		for pin in ("0000", "4826", "12345", "123456", "000000"):
			with self.subTest(pin=pin):
				self.assertEqual(check_pin_format(pin), pin)

	def test_invalid(self):
		for pin in (
			None,
			"",
			"123",
			"1234567",
			"12a4",
			"12.4",
			"-123",
			" 1234",
			"1234 ",
			"1234\n",
			"\u0661\u0662\u0663\u0664",  # Arabic-Indic digits
			"\u06f1\u06f2\u06f3\u06f4",  # Extended Arabic-Indic digits
			"\uff11\uff12\uff13\uff14",  # full-width digits
			1234,
			b"1234",
		):
			with self.subTest(pin=pin), self.assertRaises(InvalidPin):
				check_pin_format(pin)

	def test_masked(self):
		self.assertTrue(is_masked_pin("****"))
		self.assertTrue(is_masked_pin("*"))
		self.assertFalse(is_masked_pin(""))
		self.assertFalse(is_masked_pin(None))
		self.assertFalse(is_masked_pin("12*4"))


class FakeUser:
	doctype = "User"

	def __init__(self, pin=None, pin_hash=None, name="cashier1@store.om", new=False):
		self.name = name
		self.values = {cashier.PIN_FIELD: pin, cashier.PIN_HASH_FIELD: pin_hash}
		self.new = new

	def get(self, key):
		return self.values.get(key)

	def set(self, key, value):
		self.values[key] = value

	def is_new(self):
		return self.new


class ThrowCalled(Exception):
	pass


class TestUserHook(unittest.TestCase):
	def setUp(self):
		patches = [
			mock.patch.object(cashier, "_forget_stored_pin"),
			mock.patch.object(cashier, "_stored_pin", return_value=None),
			mock.patch.object(cashier, "_", side_effect=lambda text: text),
			mock.patch.object(cashier.frappe, "throw", side_effect=ThrowCalled),
		]
		self.forget, self.stored, _translate, self.throw = (patch.start() for patch in patches)
		for patch in patches:
			self.addCleanup(patch.stop)

	def test_new_pin_is_hashed_and_cleared(self):
		doc = FakeUser(pin="4826")
		cashier.user_validate(doc)
		self.assertIsNone(doc.get(cashier.PIN_FIELD))
		self.assertTrue(verify_pin("4826", doc.get(cashier.PIN_HASH_FIELD)))
		self.forget.assert_called_once_with(doc)

	def test_empty_field_keeps_hash(self):
		for value in (None, ""):
			doc = FakeUser(pin=value, pin_hash=VECTOR)
			cashier.user_validate(doc)
			self.assertEqual(doc.get(cashier.PIN_HASH_FIELD), VECTOR)
		self.forget.assert_not_called()

	def test_invalid_pin_is_rejected(self):
		doc = FakeUser(pin="12", pin_hash=VECTOR)
		with self.assertRaises(ThrowCalled):
			cashier.user_validate(doc)
		self.assertEqual(doc.get(cashier.PIN_HASH_FIELD), VECTOR)
		self.assertIn("4 to 6 digits", self.throw.call_args.args[0])

	def test_masked_value_is_never_kept(self):
		doc = FakeUser(pin="****", pin_hash=VECTOR)
		cashier.user_validate(doc)
		self.assertIsNone(doc.get(cashier.PIN_FIELD))
		self.assertEqual(doc.get(cashier.PIN_HASH_FIELD), VECTOR)
		self.forget.assert_called_once_with(doc)

	def test_masked_value_with_stored_pin_is_hashed(self):
		self.stored.return_value = "1357"
		doc = FakeUser(pin="****", pin_hash=VECTOR)
		cashier.user_validate(doc)
		self.assertIsNone(doc.get(cashier.PIN_FIELD))
		self.assertTrue(verify_pin("1357", doc.get(cashier.PIN_HASH_FIELD)))
		self.forget.assert_called_once_with(doc)

	def test_second_run_is_a_no_op(self):
		doc = FakeUser(pin="4826")
		cashier.user_validate(doc)
		encoded = doc.get(cashier.PIN_HASH_FIELD)
		cashier.user_validate(doc)
		self.assertEqual(doc.get(cashier.PIN_HASH_FIELD), encoded)


if __name__ == "__main__":
	unittest.main()
