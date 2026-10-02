"""Pull entity `uom`: record shape (no site needed)."""

import unittest
from datetime import datetime

import frappe

from crenya_pos_api.sync.pull import ENTITIES, UomSpec, build_uom_records


class TestUomEntity(unittest.TestCase):
	def test_registered(self):
		self.assertIsInstance(ENTITIES["uom"], UomSpec)
		self.assertEqual(ENTITIES["uom"].doctype, "UOM")
		self.assertFalse(ENTITIES["uom"].follows_stock)
		self.assertEqual(ENTITIES["uom"].filters(None, None), [])

	def test_records(self):
		modified = datetime(2026, 10, 2, 9, 30, 15, 123456)
		rows = [
			frappe._dict(name="Nos", must_be_whole_number=1, modified=modified),
			frappe._dict(name="Kg", must_be_whole_number=0, modified=modified),
			frappe._dict(name="Box", must_be_whole_number=None, modified=modified),
			frappe._dict(name="Unit", must_be_whole_number="1", modified=modified),
		]
		records = build_uom_records(rows)
		self.assertEqual(
			records[0],
			{"name": "Nos", "must_be_whole_number": True, "modified": "2026-10-02 09:30:15.123456"},
		)
		self.assertEqual([r["must_be_whole_number"] for r in records], [True, False, False, True])
		for record in records:
			self.assertIs(type(record["must_be_whole_number"]), bool)
			self.assertEqual(set(record), {"name", "must_be_whole_number", "modified"})

	def test_spec_build_uses_shape(self):
		row = frappe._dict(name="Nos", must_be_whole_number=1, modified=datetime(2026, 1, 1))
		self.assertEqual(UomSpec().build([row], None), build_uom_records([row]))

	def test_empty(self):
		self.assertEqual(build_uom_records([]), [])
