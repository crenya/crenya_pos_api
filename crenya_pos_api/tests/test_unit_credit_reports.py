"""The pure parts of the House Account reports (crenya_pos_api/api/web_admin/credit_reports.py):
ageing from ERPNext's Accounts Receivable rows and the totals row, at OMR's 3 decimals."""

import unittest

from crenya_pos_api.api.web_admin.credit_reports import ageing_buckets, is_zero_row, sum_rows


class TestAgeingBuckets(unittest.TestCase):
	def test_ageing_buckets_omr(self):
		rows = [
			{
				"party": "C1",
				"posting_date": "2026-09-20",
				"outstanding": 5,
				"range1": 5,
				"range2": 0,
				"range3": 0,
				"range4": 0,
			},
			{
				"party": "C1",
				"posting_date": "2026-07-01",
				"outstanding": 2.5,
				"range1": 0,
				"range2": 0,
				"range3": 2.5,
				"range4": 0,
			},
		]
		answer = ageing_buckets(rows, 3)
		self.assertEqual(
			answer["C1"],
			{
				"range_0_30": "5.000",
				"range_31_60": "0.000",
				"range_61_90": "2.500",
				"range_90_plus": "0.000",
				"oldest_unpaid": "2026-07-01",
			},
		)

	def test_oldest_comes_from_rows_with_something_outstanding(self):
		rows = [
			# an unallocated payment is a negative row: it ages but is not an unpaid bill
			{
				"party": "C1",
				"posting_date": "2026-01-01",
				"outstanding": -4,
				"range1": 0,
				"range2": 0,
				"range3": 0,
				"range4": -4,
			},
			{
				"party": "C1",
				"posting_date": "2026-09-01",
				"outstanding": 6,
				"range1": 6,
				"range2": 0,
				"range3": 0,
				"range4": 0,
			},
			{
				"party": "C2",
				"posting_date": "2026-02-01",
				"outstanding": 0,
				"range1": 0,
				"range2": 0,
				"range3": 0,
				"range4": 0,
			},
		]
		answer = ageing_buckets(rows, 3)
		self.assertEqual(answer["C1"]["oldest_unpaid"], "2026-09-01")
		self.assertEqual(answer["C1"]["range_90_plus"], "-4.000")
		self.assertIsNone(answer["C2"]["oldest_unpaid"])

	def test_no_rows_no_parties(self):
		self.assertEqual(ageing_buckets([], 3), {})
		self.assertEqual(ageing_buckets(None, 3), {})


class TestTotals(unittest.TestCase):
	def test_totals_row(self):
		rows = [
			{"opening": "1.001", "closing": "2.002"},
			{"opening": "0.500", "closing": "0.003"},
		]
		self.assertEqual(sum_rows(rows, ("opening", "closing"), 3), {"opening": "1.501", "closing": "2.005"})

	def test_totals_of_nothing_are_zero(self):
		self.assertEqual(sum_rows([], ("opening",), 3), {"opening": "0.000"})

	def test_zero_row(self):
		zero = {
			key: "0.000"
			for key in (
				"opening",
				"charged",
				"paid",
				"closing",
				"range_0_30",
				"range_31_60",
				"range_61_90",
				"range_90_plus",
				"overdue",
			)
		}
		self.assertTrue(is_zero_row(zero))
		self.assertFalse(is_zero_row({**zero, "paid": "0.001"}))
