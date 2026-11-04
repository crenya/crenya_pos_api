"""Shops web admin (0.17.0): the Reports tab's sales by item and by cashier
(crenya_pos_api/api/web_admin/sales.py) and the Z page's control events, discounts, refunds and
over/short (day_close.day_close). OMR, 3 decimals.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_web_admin_sales
"""

from decimal import Decimal

import frappe
from frappe.utils import add_days, nowdate

from crenya_pos_api.api import web_admin
from crenya_pos_api.sync.approvals import APPROVER_ROLE
from crenya_pos_api.sync.context import POS_USER_ROLE
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import SyncCase, new_id
from crenya_pos_api.tests.web_admin_case import CASHIER, MANAGER, ensure_user

SHIFT = "Crenya POS Shift"
LOG = "Crenya POS Control Log"


class SalesCase(SyncCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(MANAGER, [POS_USER_ROLE, APPROVER_ROLE])
		ensure_user(CASHIER, [POS_USER_ROLE])
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def sell(self, lines, shift, cashier="Administrator", list_rates=None):
		payload = self.sale_payload(lines=lines, shift_local_id=shift, cashier=cashier)
		for item, list_rate in zip(payload["items"], list_rates or [], strict=False):
			if list_rate:
				item["price_list_rate"] = list_rate
		result = self.assertOk(self.push_one(self.event(payload)))
		return payload, result["name"]

	def refund(self, sale, lines, shift, cashier="Administrator", list_rates=None):
		payload = self.return_payload(sale, lines, shift_local_id=shift, cashier=cashier)
		# a return carries the original line's price list rate
		for item, list_rate in zip(payload["items"], list_rates or [], strict=False):
			if list_rate:
				item["price_list_rate"] = list_rate
		return self.assertOk(self.push_one(self.event(payload)))["name"]

	def log(self, shift, events):
		seq = int(frappe.db.sql("select ifnull(max(seq), 0) from `tabCrenya POS Control Event`")[0][0]) + 1
		rows = []
		for index, values in enumerate(events):
			row = {
				"seq": seq + index,
				"event_type": "line_voided",
				"at": f"{nowdate()}T05:00:{index:02d}Z",
				"actor": "Administrator",
				"approver": None,
				"action": None,
				"subject": None,
				"amount": None,
				"item_code": None,
				"line_no": None,
				"reason": None,
				"outside_shift": False,
				"detail": None,
			}
			row.update(values)
			rows.append(row)
		payload = {
			"local_id": f"{shift}:1",
			"shift_local_id": shift,
			"pos_profile": self.profile.name,
			"chunk": 1,
			"chunks": 1,
			"events": rows,
		}
		return self.assertOk(self.push_one(self.event(payload, LOG)))

	def close(self, shift_id):
		payload = self.shift_payload(
			local_id=shift_id, opened_at=f"{nowdate()}T04:00:00Z", closed_at=f"{nowdate()}T09:00:00Z"
		)
		return self.assertOk(self.push_one(self.event(payload, SHIFT)))["name"]


class TestZAdditions(SalesCase):
	def test_z_lists_control_events_oldest_first_with_names(self):
		shift = new_id()
		self.log(
			shift,
			[
				{"event_type": "no_sale", "at": f"{nowdate()}T06:00:00Z", "reason": "change"},
				{
					"event_type": "line_voided",
					"at": f"{nowdate()}T05:00:00Z",
					"approver": "Administrator",
					"amount": "1.250",
					"item_code": fixtures.MILK,
				},
				{"event_type": "approval_refused", "at": f"{nowdate()}T05:30:00Z", "outside_shift": True},
			],
		)
		name = self.close(shift)
		frappe.set_user(MANAGER)
		answer = web_admin.day_close(name)
		events = answer["control_events"]
		self.assertEqual([e["event_type"] for e in events], ["line_voided", "approval_refused", "no_sale"])
		first = events[0]
		self.assertEqual(
			(first["label"], first["label_ar"], first["amount"], first["item_code"]),
			("Line removed", "حذف صنف", "1.250", fixtures.MILK),
		)
		self.assertEqual(
			first["actor_name"], frappe.db.get_value("User", "Administrator", "full_name") or "Administrator"
		)
		self.assertTrue(events[1]["outside_shift"])
		self.assertEqual(
			set(first),
			{
				"at",
				"event_type",
				"label",
				"label_ar",
				"actor_name",
				"approver_name",
				"amount",
				"item_code",
				"subject",
				"outside_shift",
			},
		)
		self.assertIs(answer["control_events_truncated"], False)

	def test_z_truncates_at_2000(self):
		import importlib
		from unittest import mock

		# the module (the package re-exports the function of the same name)
		day_close = importlib.import_module("crenya_pos_api.api.web_admin.day_close")

		shift = new_id()
		self.log(shift, [{}, {}, {}])
		name = self.close(shift)
		with mock.patch.object(day_close, "MAX_CONTROL_EVENTS", 2):
			answer = web_admin.day_close(name)
		self.assertEqual(len(answer["control_events"]), 2)
		self.assertIs(answer["control_events_truncated"], True)
		self.assertEqual(day_close.MAX_CONTROL_EVENTS, 2000)

	def test_z_discounts_from_invoices(self):
		shift = new_id()
		# a price override 2.000 from 2.500, approved at the till
		_sale, overridden = self.sell([(fixtures.MILK, "1", "2.000")], shift, list_rates=["2.500"])
		# a line discount of 0.500 and an invoice discount of 1.000
		_sale, discounted = self.sell([(fixtures.MILK, "1", "0.500")], shift, list_rates=["1.000"])
		frappe.db.set_value("Sales Invoice", discounted, "discount_amount", 1.0, update_modified=False)
		self.log(
			shift,
			[
				{
					"event_type": "approval_used",
					"action": "price_change",
					"approver": "Administrator",
					"item_code": fixtures.MILK,
				}
			],
		)
		name = self.close(shift)
		discounts = web_admin.day_close(name)["discounts"]
		self.assertEqual((discounts["count"], discounts["amount"]), (3, "2.000"))
		override = next(r for r in discounts["rows"] if r["invoice"] == overridden)
		self.assertEqual(
			(override["list_rate"], override["rate"], override["discount"]), ("2.500", "2.000", "0.500")
		)
		self.assertTrue(override["approver"])
		invoice_row = next(
			r for r in discounts["rows"] if r["invoice"] == discounted and r["item_code"] is None
		)
		self.assertEqual(invoice_row["discount"], "1.000")

	def test_z_refunds_from_returns(self):
		shift = new_id()
		sale, _name = self.sell([(fixtures.MILK, "2", "0.600")], shift)
		returned = self.refund(sale, [(fixtures.MILK, "-1", "0.600", 1)], shift)
		name = self.close(shift)
		refunds = web_admin.day_close(name)["refunds"]
		self.assertEqual((refunds["count"], refunds["amount"]), (1, "0.600"))
		row = refunds["rows"][0]
		self.assertEqual((row["invoice"], row["amount"]), (returned, "0.600"))
		self.assertTrue(row["return_against"])
		self.assertTrue(row["cashier_name"])

	def test_z_cash_difference_block(self):
		name = self.close(new_id())
		self.assertEqual(
			web_admin.day_close(name)["cash_difference"],
			{"state": "Off", "entry": None, "error": None, "can_retry": False},
		)

	def test_z_without_control_log_is_unchanged_plus_empty_lists(self):
		# a 0.10.0 till's shift: no log, no invoices of the shift
		name = self.close(new_id())
		answer = web_admin.day_close(name)
		self.assertEqual(
			[s["key"] for s in answer["sections"]],
			["sales", "payments", "returns", "store_controls", "paid_out"],
		)
		self.assertEqual((answer["control_events"], answer["control_events_truncated"]), ([], False))
		self.assertEqual(answer["discounts"], {"count": 0, "amount": "0.000", "rows": []})
		self.assertEqual(answer["refunds"], {"count": 0, "amount": "0.000", "rows": []})


def allow_company(user, company):
	"""A User Permission limiting `user` to `company`; returns its name."""
	doc = frappe.get_doc(
		{"doctype": "User Permission", "user": user, "allow": "Company", "for_value": company}
	)
	doc.flags.ignore_links = True
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def drop_permissions(user):
	for name in frappe.get_all("User Permission", filters={"user": user, "allow": "Company"}, pluck="name"):
		frappe.delete_doc("User Permission", name, ignore_permissions=True, force=True)
	frappe.db.commit()


class TestZCompanyScope(SalesCase):
	def test_z_other_company_manager_refused(self):
		"""Review W2 M1 (T12 F7); the list's range is a day either side (the site's time zone): a manager limited to another company opens neither this shop's Z nor
		its list; limited to this company, both; a System Manager is unchanged."""
		name = self.close(new_id())
		self.addCleanup(drop_permissions, MANAGER)
		allow_company(MANAGER, "_Test Crenya Other Company")
		frappe.set_user(MANAGER)
		with self.assertRaises(frappe.PermissionError):
			web_admin.day_close(name)
		with self.assertRaises(frappe.PermissionError):
			web_admin.day_closes(add_days(nowdate(), -1), add_days(nowdate(), 1), self.profile.name)
		frappe.set_user("Administrator")
		allow_company(MANAGER, fixtures.COMPANY)
		frappe.set_user(MANAGER)
		self.assertEqual(web_admin.day_close(name)["shift"], name)
		self.assertIn(
			name,
			[
				r["shift"]
				for r in web_admin.day_closes(
					add_days(nowdate(), -1), add_days(nowdate(), 1), self.profile.name
				)["rows"]
			],
		)
		frappe.set_user("Administrator")
		self.assertEqual(web_admin.day_close(name)["shift"], name)


class TestSalesReports(SalesCase):
	def report(self, tab, user=MANAGER):
		frappe.set_user(user)
		try:
			return web_admin.sales(tab, nowdate(), nowdate(), self.profile.name)
		finally:
			frappe.set_user("Administrator")

	def item(self, answer, code):
		return next(
			(r for r in answer["rows"] if r["item_code"] == code),
			{"qty": "0", "amount": "0.000", "discounts": "0.000"},
		)

	def cashier(self, answer, user):
		return next(
			(r for r in answer["rows"] if r["cashier"] == user),
			{
				"bills": 0,
				"refunds_count": 0,
				"refunds": "0.000",
				"sales": "0.000",
				"net_total": "0.000",
				"discounts": "0.000",
				"line_voids_count": 0,
				"no_sales_count": 0,
			},
		)

	def test_sales_items_netted_with_returns_omr(self):
		before = self.item(self.report("items"), fixtures.BREAD)
		shift = new_id()
		sale, _name = self.sell([(fixtures.BREAD, "3", "0.400")], shift, list_rates=["0.500"])
		self.refund(sale, [(fixtures.BREAD, "-1", "0.400", 1)], shift, list_rates=["0.500"])
		answer = self.report("items")
		self.assertEqual(
			(answer["tab"], answer["from_date"], answer["to_date"]), ("items", nowdate(), nowdate())
		)
		self.assertEqual(answer["currency"]["precision"], 3)
		after = self.item(answer, fixtures.BREAD)
		self.assertEqual(Decimal(after["qty"]) - Decimal(before["qty"]), Decimal(2))
		self.assertEqual(Decimal(after["discounts"]) - Decimal(before["discounts"]), Decimal("0.200"))
		self.assertEqual(len(after["amount"].split(".")[1]), 3)
		self.assertTrue({"item_code", "item_name", "item_group", "qty", "amount", "discounts"} <= set(after))

	def test_sales_cashiers_counts_voids_and_no_sales(self):
		before = self.cashier(self.report("cashiers"), "Administrator")
		shift = new_id()
		sale, _name = self.sell([(fixtures.MILK, "2", "0.600")], shift)
		self.refund(sale, [(fixtures.MILK, "-1", "0.600", 1)], shift)
		self.log(
			shift, [{"event_type": "line_voided"}, {"event_type": "line_voided"}, {"event_type": "no_sale"}]
		)
		after = self.cashier(self.report("cashiers"), "Administrator")
		self.assertEqual(after["bills"] - before["bills"], 1)
		self.assertEqual(after["refunds_count"] - before["refunds_count"], 1)
		self.assertEqual(Decimal(after["refunds"]) - Decimal(before["refunds"]), Decimal("0.600"))
		self.assertEqual(Decimal(after["net_total"]) - Decimal(before["net_total"]), Decimal("0.600"))
		self.assertEqual(after["line_voids_count"] - before["line_voids_count"], 2)
		self.assertEqual(after["no_sales_count"] - before["no_sales_count"], 1)
		self.assertTrue(after["cashier_name"])

	def test_sales_sql_totals_hand_computed(self):
		"""Review W2 M2: the grouped SQL against a hand-computed fixture (OMR, 3 decimals): a sale of 3
		at 0.400 (list 0.500), a return of 1, and a sale of 2 cancelled (left out)."""
		items_before = self.item(self.report("items"), fixtures.BREAD)
		cashier_before = self.cashier(self.report("cashiers"), "Administrator")
		shift = new_id()
		sale, _name = self.sell([(fixtures.BREAD, "3", "0.400")], shift, list_rates=["0.500"])
		self.refund(sale, [(fixtures.BREAD, "-1", "0.400", 1)], shift, list_rates=["0.500"])
		_cancelled, cancelled = self.sell([(fixtures.BREAD, "2", "0.400")], shift, list_rates=["0.500"])
		frappe.get_doc("Sales Invoice", cancelled).cancel()
		frappe.db.commit()
		item = self.item(self.report("items"), fixtures.BREAD)
		delta = {
			key: Decimal(item[key]) - Decimal(items_before[key]) for key in ("qty", "amount", "discounts")
		}
		self.assertEqual(
			delta, {"qty": Decimal(2), "amount": Decimal("0.800"), "discounts": Decimal("0.200")}
		)
		cashier = self.cashier(self.report("cashiers"), "Administrator")
		self.assertEqual(
			{key: cashier[key] - cashier_before[key] for key in ("bills", "refunds_count")},
			{"bills": 1, "refunds_count": 1},
		)
		money = {
			key: Decimal(cashier[key]) - Decimal(cashier_before[key])
			for key in ("sales", "refunds", "net_total", "discounts")
		}
		self.assertEqual(
			money,
			{
				"sales": Decimal("1.200"),
				"refunds": Decimal("0.400"),
				"net_total": Decimal("0.800"),
				"discounts": Decimal("0.200"),
			},
		)
		for key in ("sales", "refunds", "net_total", "discounts"):
			self.assertEqual(len(cashier[key].split(".")[1]), 3, key)

	def test_sales_bad_tab_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.report("providers")

	def test_sales_range_over_366_refused(self):
		frappe.set_user(MANAGER)
		with self.assertRaises(frappe.ValidationError):
			web_admin.sales("items", "2025-01-01", "2026-01-02", self.profile.name)

	def test_sales_cashier_refused(self):
		with self.assertRaises(frappe.PermissionError):
			self.report("items", CASHIER)

	def test_sales_other_company_manager_refused(self):
		doc = frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": MANAGER,
				"allow": "Company",
				"for_value": "_Test Crenya Other Company",
			}
		)
		doc.flags.ignore_links = True
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		try:
			with self.assertRaises(frappe.PermissionError):
				self.report("items")
		finally:
			frappe.delete_doc("User Permission", doc.name, ignore_permissions=True, force=True)
			frappe.db.commit()

	def test_sales_api_key_session_refused(self):
		for user in (MANAGER, "Administrator"):
			frappe.set_user(user)
			with fixtures.signed_with_key():
				with self.assertRaises(frappe.PermissionError):
					web_admin.sales("items", nowdate(), nowdate(), self.profile.name)
		frappe.set_user("Administrator")
