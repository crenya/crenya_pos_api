"""The pure parts of the House Account reports (crenya_pos_api/api/web_admin/credit_reports.py):
ageing from ERPNext's Accounts Receivable rows and the totals row, at OMR's 3 decimals."""

import unittest

from crenya_pos_api.api.web_admin.credit_reports import (
	ageing_buckets,
	is_zero_row,
	statement_description,
	statement_rows,
	sum_rows,
	transaction_subtotals,
)


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


class TestStatementRows(unittest.TestCase):
	def test_statement_rows_running_balance_omr(self):
		entries = [
			{
				"posting_date": "2026-10-02",
				"voucher_type": "Sales Invoice",
				"voucher_no": "SI-1",
				"debit": 12,
				"credit": 0,
				"bill_no": "OFF-1",
			},
			{
				"posting_date": "2026-10-03",
				"voucher_type": "Payment Entry",
				"voucher_no": "PE-1",
				"debit": 0,
				"credit": 5,
			},
			{
				"posting_date": "2026-10-04",
				"voucher_type": "Sales Invoice",
				"voucher_no": "SI-2",
				"debit": 0,
				"credit": 1,
				"is_return": True,
			},
		]
		rows, closing = statement_rows("8.000", entries, 3)
		self.assertEqual([row["balance"] for row in rows], ["20.000", "15.000", "14.000"])
		self.assertEqual(closing, "14.000")
		self.assertEqual([row["description"] for row in rows], ["sale", "payment", "credit_note"])
		self.assertEqual((rows[0]["bill_no"], rows[1]["bill_no"]), ("OFF-1", None))
		self.assertEqual((rows[0]["debit"], rows[0]["credit"]), ("12.000", "0.000"))

	def test_closing_is_opening_without_entries(self):
		self.assertEqual(statement_rows("8", [], 3), ([], "8.000"))

	def test_description_of_other_vouchers_is_raw(self):
		self.assertEqual(statement_description("Journal Entry"), "journal")
		self.assertEqual(statement_description("Period Closing Voucher"), "Period Closing Voucher")


class TestTransactionSubtotals(unittest.TestCase):
	def rows(self):
		def row(day, kind, amount, shift="S1", cashier="c@x", number="1"):
			return {
				"posting_date": day,
				"kind": kind,
				"amount": amount,
				"shift_id": shift,
				"shift_number": number if shift else None,
				"cashier": cashier if shift else None,
				"cashier_name": "C" if shift else None,
			}

		return [
			row("2026-10-01", "sale", "13.000"),
			row("2026-10-01", "cancel", "-3.000"),
			row("2026-10-01", "collection", "5.000"),
			row("2026-10-02", "collection", "2.000", shift=None),
			row("2026-10-02", "sale", "1.000", shift="S2", number=None),
		]

	def test_days_shifts_totals(self):
		days, shifts, totals = transaction_subtotals(self.rows(), 3)
		self.assertEqual(
			days,
			[
				{"posting_date": "2026-10-01", "sales": "10.000", "collections": "5.000"},
				{"posting_date": "2026-10-02", "sales": "1.000", "collections": "2.000"},
			],
		)
		self.assertEqual(
			[(s["shift_id"], s["sales"], s["collections"]) for s in shifts],
			[("S1", "10.000", "5.000"), ("S2", "1.000", "0.000")],
		)
		self.assertEqual(totals, {"sales": "11.000", "collections": "7.000"})

	def test_empty(self):
		self.assertEqual(transaction_subtotals([], 3), ([], [], {"sales": "0.000", "collections": "0.000"}))
