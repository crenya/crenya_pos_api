"""Credit days as a House Account payment terms template, the pure parts (no site): the template's
name, the days typed and the audit text of a credit terms change."""

import unittest
from decimal import Decimal

from crenya_pos_api.sync.credit_terms import UNSET, parse_days, template_name, terms_audit_text


class TestTemplateName(unittest.TestCase):
	def test_template_name(self):
		self.assertEqual(template_name(30), "House Account 30 days")
		self.assertEqual(template_name(0), "House Account 0 days")
		self.assertEqual(template_name(365), "House Account 365 days")


class TestParseDays(unittest.TestCase):
	def test_parse_days(self):
		for blank in (None, "", "  "):
			self.assertIsNone(parse_days(blank))
		for value, expected in ((0, 0), ("0", 0), ("30", 30), (" 45 ", 45), (365, 365), ("365", 365)):
			self.assertEqual(parse_days(value), expected)
			self.assertIsInstance(parse_days(value), int)
		for bad in (-1, 366, "-1", "366", "3.5", "abc", True, False, 3.5, 30.0, "٣٠"):
			with self.assertRaises(ValueError, msg=repr(bad)) as raised:
				parse_days(bad)
			self.assertEqual(str(raised.exception), "Credit days must be a whole number from 0 to 365")

	def test_unset_is_not_a_value(self):
		self.assertIsNot(UNSET, None)
		self.assertFalse(UNSET == 0)


class TestTermsAuditText(unittest.TestCase):
	def test_terms_audit_text_omr(self):
		change = {"old_limit": None, "new_limit": Decimal("50.000"), "old_days": None, "new_days": 30}
		self.assertEqual(
			terms_audit_text("Shop Co", change, "app.crenya.com", "boss@example.com", 3),
			"Credit terms for Shop Co changed: limit no limit of its own → 50.000, days none → 30"
			" from app.crenya.com by boss@example.com",
		)

	def test_terms_audit_text_omr_removed_and_zero_days(self):
		change = {"old_limit": Decimal("12.5"), "new_limit": None, "old_days": 30, "new_days": 0}
		text = terms_audit_text("Shop Co", change, "app.crenya.com", "boss@example.com", 3)
		self.assertIn("limit 12.500 → no limit of its own, days 30 → 0 from", text)
