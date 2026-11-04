"""The cash over/short Journal Entry of a pushed shift (0.17.0): off by default, one submitted entry
per shift when the outlet's switch is on, "Not posted" with the reason (the shift still recorded)
when it cannot be posted, and Retry from app.crenya.com.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_cash_difference
"""

import threading
from decimal import Decimal

import frappe
from frappe.utils import getdate, nowdate

from crenya_pos_api import cash_difference
from crenya_pos_api.api import web_admin
from crenya_pos_api.setup.install import CASH_DIFFERENCE_ACCOUNT_FIELD, CASH_DIFFERENCE_SWITCH_FIELD
from crenya_pos_api.sync.approvals import APPROVER_ROLE
from crenya_pos_api.sync.context import POS_USER_ROLE
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import SyncCase
from crenya_pos_api.tests.web_admin_case import MANAGER, ensure_user

SHIFT = "Crenya POS Shift"
OVER_SHORT = f"_Test Crenya Cash Over Short - {fixtures.ABBR}"
CARD = "_Test Crenya Cash Diff Card"
OTHER_COMPANY = "_Test Crenya Cash Other Co"
OTHER_ABBR = "_TCX"


def ensure_over_short_account():
	if not frappe.db.exists("Account", OVER_SHORT):
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "_Test Crenya Cash Over Short",
				"parent_account": f"Indirect Expenses - {fixtures.ABBR}",
				"company": fixtures.COMPANY,
				"account_currency": fixtures.CURRENCY,
			}
		).insert(ignore_permissions=True)
	return OVER_SHORT


def other_company_account():
	"""A leaf expense account of another company."""
	found = frappe.db.get_value(
		"Account", {"company": ["!=", fixtures.COMPANY], "is_group": 0, "root_type": "Expense"}, "name"
	)
	if found:
		return found
	frappe.get_doc(
		{
			"doctype": "Company",
			"company_name": OTHER_COMPANY,
			"abbr": OTHER_ABBR,
			"default_currency": fixtures.CURRENCY,
			"country": "Oman",
			"chart_of_accounts": "Standard",
		}
	).insert(ignore_permissions=True)
	return frappe.db.get_value(
		"Account", {"company": OTHER_COMPANY, "is_group": 0, "root_type": "Expense"}, "name"
	)


def gl(entry):
	"""{account: (debit, credit)} of a submitted Journal Entry's GL Entries, as Decimals at 3."""
	rows = frappe.get_all(
		"GL Entry",
		filters={"voucher_type": "Journal Entry", "voucher_no": entry, "is_cancelled": 0},
		fields=["account", "debit", "credit"],
	)
	return {
		row.account: (
			Decimal(str(row.debit)).quantize(Decimal("0.001")),
			Decimal(str(row.credit)).quantize(Decimal("0.001")),
		)
		for row in rows
	}


class TestCashDifference(SyncCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_over_short_account()
		company = frappe.get_doc("Company", fixtures.COMPANY)
		fixtures.ensure_mode_of_payment(CARD, "Bank", company)
		ensure_user(MANAGER, [POS_USER_ROLE, APPROVER_ROLE], pin="135790")
		cls.cash_account = frappe.db.get_value(
			"Mode of Payment Account",
			{"parent": fixtures.CASH, "company": fixtures.COMPANY},
			"default_account",
		)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def outlet(self, on=1, account=OVER_SHORT):
		"""The outlet's switch and account for the test, put back after it."""
		fields = [CASH_DIFFERENCE_SWITCH_FIELD, CASH_DIFFERENCE_ACCOUNT_FIELD]
		saved = frappe.db.get_value("POS Profile", self.profile.name, fields, as_dict=True)
		frappe.db.set_value(
			"POS Profile",
			self.profile.name,
			{CASH_DIFFERENCE_SWITCH_FIELD: on, CASH_DIFFERENCE_ACCOUNT_FIELD: account},
			update_modified=False,
		)
		frappe.clear_document_cache("POS Profile", self.profile.name)
		frappe.db.commit()

		def restore():
			frappe.db.set_value("POS Profile", self.profile.name, saved, update_modified=False)
			frappe.clear_document_cache("POS Profile", self.profile.name)
			frappe.db.commit()

		self.addCleanup(restore)

	def push_shift(self, payments, closed_at=None, event_id=None, payload=None):
		payload = payload or self.shift_payload(
			payments=payments,
			opened_at=f"{nowdate()}T04:00:00Z",
			closed_at=closed_at or f"{nowdate()}T08:05:30Z",
		)
		result = self.assertOk(self.push_one(self.event(payload, SHIFT, event_id=event_id)))
		return result, payload

	def cash(self, expected, counted):
		return {
			"mode_of_payment": fixtures.CASH,
			"expected": expected,
			"counted": counted,
			"difference": str(Decimal(counted) - Decimal(expected)),
		}

	def state(self, shift):
		return frappe.db.get_value(
			SHIFT,
			shift,
			["cash_difference_state", "cash_difference_entry", "cash_difference_error"],
			as_dict=True,
		)

	def entries(self, shift):
		return frappe.get_all(
			"Journal Entry", filters={"crenya_pos_shift": shift, "docstatus": ["!=", 2]}, pluck="name"
		)

	def test_switch_off_state_off_no_entry(self):
		self.outlet(on=0)
		result, _payload = self.push_shift([self.cash("67.900", "67.850")])
		self.assertEqual(self.state(result["name"]).cash_difference_state, "Off")
		self.assertEqual(self.entries(result["name"]), [])

	def test_short_omr_posts_one_entry(self):
		self.outlet()
		result, payload = self.push_shift([self.cash("67.900", "67.850")])
		row = self.state(result["name"])
		self.assertEqual(row.cash_difference_state, "Posted", row.cash_difference_error)
		self.assertEqual(self.entries(result["name"]), [row.cash_difference_entry])
		entry = frappe.get_doc("Journal Entry", row.cash_difference_entry)
		self.assertEqual(entry.docstatus, 1)
		self.assertEqual(entry.crenya_pos_shift, result["name"])
		self.assertEqual(entry.company, fixtures.COMPANY)
		self.assertEqual(getdate(entry.posting_date), getdate(nowdate()))
		self.assertIn(payload["shift_number"], entry.user_remark)
		self.assertEqual(
			gl(entry.name),
			{
				OVER_SHORT: (Decimal("0.050"), Decimal("0.000")),
				self.cash_account: (Decimal("0.000"), Decimal("0.050")),
			},
		)
		# the Z's block
		self.assertEqual(
			cash_difference.cash_difference_block(frappe.get_doc(SHIFT, result["name"])),
			{"state": "Posted", "entry": entry.name, "error": None, "can_retry": False},
		)

	def test_over_posts_reverse(self):
		self.outlet()
		result, _payload = self.push_shift([self.cash("67.900", "68.000")])
		entry = self.state(result["name"]).cash_difference_entry
		self.assertEqual(
			gl(entry),
			{
				self.cash_account: (Decimal("0.100"), Decimal("0.000")),
				OVER_SHORT: (Decimal("0.000"), Decimal("0.100")),
			},
		)

	def test_card_difference_not_posted(self):
		self.outlet()
		card = {"mode_of_payment": CARD, "expected": "10.000", "counted": "9.000", "difference": "-1.000"}
		result, _payload = self.push_shift([self.cash("67.900", "67.900"), card])
		self.assertEqual(self.state(result["name"]).cash_difference_state, "Not needed")
		self.assertEqual(self.entries(result["name"]), [])

	def test_missing_account_records_shift_not_posted(self):
		self.outlet(account=None)
		event_id = frappe.generate_hash(length=20)
		result, _payload = self.push_shift([self.cash("67.900", "67.850")], event_id=event_id)
		self.assertTrue(frappe.db.exists(SHIFT, result["name"]))
		self.assertEqual(frappe.db.get_value("Crenya Sync Event", event_id, "status"), "ok")
		self.assertIn("not posted", frappe.db.get_value("Crenya Sync Event", event_id, "note") or "")
		row = self.state(result["name"])
		self.assertEqual(row.cash_difference_state, "Not posted")
		self.assertIn("over/short account", row.cash_difference_error)
		self.assertTrue(row.cash_difference_error.startswith("missing_account: "))
		self.assertEqual(
			cash_difference.cash_difference_block(frappe.get_doc(SHIFT, result["name"])),
			{"state": "Not posted", "entry": None, "error": "missing_account", "can_retry": True},
		)
		self.assertEqual(self.entries(result["name"]), [])
		# the account set, the manager retries: one entry
		frappe.db.set_value(
			"POS Profile", self.profile.name, CASH_DIFFERENCE_ACCOUNT_FIELD, OVER_SHORT, update_modified=False
		)
		frappe.db.commit()
		frappe.set_user(MANAGER)
		block = web_admin.post_cash_difference(result["name"])
		self.assertEqual(block["state"], "Posted", block)
		frappe.set_user("Administrator")
		self.assertEqual(self.entries(result["name"]), [block["entry"]])

	def frozen(self, day):
		"""Freeze the company's accounts up to `day` (ERPNext v16: the Company; v15: Accounts Settings)."""
		if frappe.get_meta("Company").has_field("accounts_frozen_till_date"):
			saved = frappe.db.get_value("Company", fixtures.COMPANY, "accounts_frozen_till_date")
			frappe.db.set_value("Company", fixtures.COMPANY, "accounts_frozen_till_date", day)

			def restore():
				frappe.db.set_value("Company", fixtures.COMPANY, "accounts_frozen_till_date", saved)
				frappe.clear_document_cache("Company", fixtures.COMPANY)
				frappe.db.commit()
		else:
			saved = frappe.db.get_single_value("Accounts Settings", "acc_frozen_upto")
			frappe.db.set_single_value("Accounts Settings", "acc_frozen_upto", day)

			def restore():
				frappe.db.set_single_value("Accounts Settings", "acc_frozen_upto", saved)
				frappe.db.commit()

		frappe.clear_document_cache("Company", fixtures.COMPANY)
		frappe.db.commit()
		return restore

	def test_frozen_date_not_posted_then_retry(self):
		self.outlet()
		restore = self.frozen(nowdate())
		self.addCleanup(restore)
		result, _payload = self.push_shift([self.cash("67.900", "67.850")])
		row = self.state(result["name"])
		self.assertEqual(row.cash_difference_state, "Not posted")
		self.assertIn("frozen", row.cash_difference_error)
		self.assertEqual(
			cash_difference.cash_difference_block(frappe.get_doc(SHIFT, result["name"]))["error"],
			"frozen_date",
		)
		self.assertEqual(self.entries(result["name"]), [])
		# never another date: the owner unfreezes, Retry posts at the close date
		restore()
		frappe.set_user(MANAGER)
		block = web_admin.post_cash_difference(result["name"])
		frappe.set_user("Administrator")
		self.assertEqual(block["state"], "Posted", block)
		self.assertEqual(
			getdate(frappe.db.get_value("Journal Entry", block["entry"], "posting_date")), getdate(nowdate())
		)

	def test_resent_shift_event_one_entry(self):
		self.outlet()
		event_id = frappe.generate_hash(length=20)
		result, payload = self.push_shift([self.cash("67.900", "67.850")], event_id=event_id)
		again = self.push_one(self.event(payload, SHIFT, event_id=event_id))
		self.assertEqual(again["status"], "duplicate")
		# a new event id for the same shift: matched by its local id
		self.assertEqual(self.push_one(self.event(payload, SHIFT))["status"], "duplicate")
		self.assertEqual(len(self.entries(result["name"])), 1)
		# Retry of a posted shift answers it as it is
		frappe.set_user(MANAGER)
		self.assertEqual(web_admin.post_cash_difference(result["name"])["state"], "Posted")
		frappe.set_user("Administrator")
		self.assertEqual(len(self.entries(result["name"])), 1)

	def test_retry_twice_parallel_one_entry(self):
		self.outlet(account=None)
		result, _payload = self.push_shift([self.cash("67.900", "67.850")])
		shift = result["name"]
		self.assertEqual(self.state(shift).cash_difference_state, "Not posted")
		frappe.db.set_value(
			"POS Profile", self.profile.name, CASH_DIFFERENCE_ACCOUNT_FIELD, OVER_SHORT, update_modified=False
		)
		frappe.db.commit()
		site, sites_path = frappe.local.site, frappe.local.sites_path
		start, answers, problems = threading.Barrier(2), [], []

		def press():
			frappe.init(site=site, sites_path=sites_path)
			frappe.connect()
			try:
				frappe.set_user("Administrator")
				start.wait(30)
				answers.append(cash_difference.post_for_shift(shift))
			except Exception as exc:  # the main thread reports it
				problems.append(exc)
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=press) for _ in range(2)]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join(120)
		self.assertEqual(problems, [])
		self.assertEqual([answer["state"] for answer in answers], ["Posted", "Posted"])
		self.assertEqual(answers[0]["entry"], answers[1]["entry"])
		self.assertEqual(self.entries(shift), [answers[0]["entry"]])

	def test_retry_refused_for_api_key_session(self):
		self.outlet(account=None)
		result, _payload = self.push_shift([self.cash("67.900", "67.850")])
		frappe.set_user(MANAGER)
		with fixtures.signed_with_key():
			with self.assertRaises(frappe.PermissionError):
				web_admin.post_cash_difference(result["name"])
		frappe.set_user("Administrator")
		self.assertEqual(self.state(result["name"]).cash_difference_state, "Not posted")

	def test_account_of_other_company_refused_on_save(self):
		account = other_company_account()
		frappe.db.commit()
		modified = web_admin.outlet_settings(self.profile.name)["modified"]
		self.keep_settings()
		with self.assertRaises(frappe.ValidationError) as caught:
			web_admin.save_outlet_settings(
				self.profile.name,
				{CASH_DIFFERENCE_SWITCH_FIELD: 1, CASH_DIFFERENCE_ACCOUNT_FIELD: account},
				modified,
			)
		self.assertIn("is not an account of", str(caught.exception))
		frappe.db.rollback()
		# a group of the company is refused too; the company's leaf is saved
		with self.assertRaises(frappe.ValidationError):
			web_admin.save_outlet_settings(
				self.profile.name,
				{CASH_DIFFERENCE_ACCOUNT_FIELD: f"Indirect Expenses - {fixtures.ABBR}"},
				modified,
			)
		frappe.db.rollback()
		answer = web_admin.save_outlet_settings(
			self.profile.name,
			{CASH_DIFFERENCE_SWITCH_FIELD: 1, CASH_DIFFERENCE_ACCOUNT_FIELD: OVER_SHORT},
			modified,
		)
		fields = {field["fieldname"]: field for section in answer["sections"] for field in section["fields"]}
		self.assertEqual(fields[CASH_DIFFERENCE_ACCOUNT_FIELD]["value"], OVER_SHORT)
		self.assertIn(CASH_DIFFERENCE_ACCOUNT_FIELD, answer["owner_fields"])

	def keep_settings(self):
		fields = [CASH_DIFFERENCE_SWITCH_FIELD, CASH_DIFFERENCE_ACCOUNT_FIELD]
		saved = frappe.db.get_value("POS Profile", self.profile.name, fields, as_dict=True)

		def restore():
			frappe.db.set_value("POS Profile", self.profile.name, saved)
			frappe.clear_document_cache("POS Profile", self.profile.name)
			frappe.db.commit()

		self.addCleanup(restore)

	def test_manager_cannot_set_account(self):
		self.keep_settings()
		frappe.set_user(MANAGER)
		answer = web_admin.outlet_settings(self.profile.name)
		fields = {field["fieldname"]: field for section in answer["sections"] for field in section["fields"]}
		self.assertTrue(fields[CASH_DIFFERENCE_SWITCH_FIELD]["read_only"])
		self.assertTrue(fields[CASH_DIFFERENCE_ACCOUNT_FIELD]["read_only"])
		for values in ({CASH_DIFFERENCE_ACCOUNT_FIELD: OVER_SHORT}, {CASH_DIFFERENCE_SWITCH_FIELD: 1}):
			with self.assertRaises(frappe.PermissionError):
				web_admin.save_outlet_settings(self.profile.name, values, answer["modified"])

	def test_setup_check_when_switched_on_without_account(self):
		self.outlet(account=None)
		frappe.set_user(MANAGER)
		checks = web_admin.outlet_settings(self.profile.name)["setup_checks"]
		row = next(row for row in checks if row["key"] == "cash_difference_account")
		self.assertEqual(row["status"], "missing")
		frappe.set_user("Administrator")
		self.outlet(on=0)
		self.assertNotIn(
			"cash_difference_account",
			[row["key"] for row in web_admin.outlet_settings(self.profile.name)["setup_checks"]],
		)
