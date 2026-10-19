"""A customer payment that names the bills it settles (0.14.0, `invoices`) on an ERPNext site: the
ticked bills in the order given, re-read at booking; a ticked bill paid meanwhile, unknown, foreign
or a return is skipped with a note and the money stays an advance; a payload without `invoices` is
booked exactly as before; the web's `named_lock` and a till's event wait for each other.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_customer_payment_ticks
"""

import threading
import time
from decimal import Decimal
from unittest import mock

import frappe
from frappe.utils import nowdate

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.sync import customer_payment as payments
from crenya_pos_api.sync import events
from crenya_pos_api.sync.customer_payment import (
	FEATURE_SETTLE_INVOICES,
	PaymentBooking,
	post_payment_entry,
)
from crenya_pos_api.sync.events import customer_lock_key
from crenya_pos_api.sync.invoice_builder import mode_of_payment_account
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import money, new_id
from crenya_pos_api.tests.test_integration_credit import EVENT, make_customer
from crenya_pos_api.tests.test_integration_customer_payment import AGGREGATE, PaymentCase, outstanding

PAYMENT_ENTRY = "Payment Entry"


def web_payment(customer, amount, invoices=None):
	"""A Payment Entry booked the way the web's Settle books one (`post_payment_entry`, no till)."""
	booking = PaymentBooking(
		company=fixtures.COMPANY,
		customer=customer,
		mode_of_payment=fixtures.CASH,
		amount=Decimal(amount),
		currency=fixtures.CURRENCY,
		posting_date=nowdate(),
		local_id=new_id(),
		reference="WEB-1",
		remarks="Settled on app.crenya.com by Administrator",
		invoices=invoices,
	)
	return post_payment_entry(booking, [])


class TicksCase(PaymentCase):
	def bills(self, customer, *totals):
		"""[(name, the till's local id)] of sales wholly on account, oldest first."""
		made = []
		for total in totals:
			sale = self.on_account_sale(customer, total=total)
			made.append((self.post(sale)["name"], sale["local_id"]))
		return made

	def note(self, event_id):
		return frappe.db.get_value(EVENT, event_id, "note") or ""

	def references(self, name):
		entry = frappe.get_doc(PAYMENT_ENTRY, name)
		return [(row.reference_name, Decimal(money(row.allocated_amount))) for row in entry.references]


class TestTickedReceipts(TicksCase):
	def test_payload_without_invoices_books_oldest_first_as_before(self):
		"""Every field of the Payment Entry of a retail payload is what 0.13.2 wrote, and the answer
		has no `skipped`."""
		customer = make_customer("asbefore", limit=None)
		(first, _), (second, _) = self.bills(customer, "6.000", "10.000")
		payload = self.payment(customer, "12.000")
		result = self.assertOk(self.pay(payload))
		self.assertEqual(
			result["payment"], {"allocated": "12.000", "unallocated": "0.000", "invoices": [first, second]}
		)
		from erpnext.accounts.party import get_party_account

		entry = frappe.get_doc(PAYMENT_ENTRY, result["name"])
		self.assertEqual(
			{
				"payment_type": entry.payment_type,
				"party_type": entry.party_type,
				"party": entry.party,
				"company": entry.company,
				"posting_date": str(entry.posting_date),
				"mode_of_payment": entry.mode_of_payment,
				"paid_from": entry.paid_from,
				"paid_to": entry.paid_to,
				"paid_amount": Decimal(money(entry.paid_amount)),
				"received_amount": Decimal(money(entry.received_amount)),
				"source_exchange_rate": entry.source_exchange_rate,
				"target_exchange_rate": entry.target_exchange_rate,
				"crenya_local_id": entry.crenya_local_id,
				"reference_no": entry.reference_no,
				"reference_date": str(entry.reference_date),
				"custom_remarks": entry.custom_remarks,
				"remarks": entry.remarks,
				"unallocated_amount": Decimal(money(entry.unallocated_amount)),
				"docstatus": entry.docstatus,
			},
			{
				"payment_type": "Receive",
				"party_type": "Customer",
				"party": customer,
				"company": fixtures.COMPANY,
				"posting_date": payload["business_date"],
				"mode_of_payment": fixtures.CASH,
				"paid_from": get_party_account("Customer", customer, fixtures.COMPANY),
				"paid_to": mode_of_payment_account(fixtures.CASH, fixtures.COMPANY),
				"paid_amount": Decimal("12.000"),
				"received_amount": Decimal("12.000"),
				"source_exchange_rate": 1,
				"target_exchange_rate": 1,
				"crenya_local_id": payload["local_id"],
				"reference_no": payload["local_id"],
				"reference_date": payload["business_date"],
				"custom_remarks": 1,
				"remarks": (
					f"Received at the till {self.device_id} by Administrator; "
					f"shift {payload['shift_local_id']}"
				),
				"unallocated_amount": Decimal("0.000"),
				"docstatus": 1,
			},
		)
		self.assertEqual(
			self.references(result["name"]), [(first, Decimal("6.000")), (second, Decimal("6.000"))]
		)
		# an empty list is the same as none
		third = self.bills(customer, "3.000")[0][0]
		empty = self.assertOk(self.pay(self.payment(customer, "1.000", invoices=[])))
		self.assertEqual(
			empty["payment"], {"allocated": "1.000", "unallocated": "0.000", "invoices": [second]}
		)
		self.assertEqual(outstanding(third), Decimal("3.000"))

	def test_ticks_allocate_in_given_order(self):
		"""Spec 15.3: bills 6, 10, 13; tick the 3rd then the 1st, pay 15: 13 then 2, the 2nd untouched;
		a bill ticked by its till id (`local:`) is the same bill."""
		customer = make_customer("ticks", limit=None)
		(b1, _), (b2, _), (b3, b3_local) = self.bills(customer, "6.000", "10.000", "13.000")
		result = self.assertOk(self.pay(self.payment(customer, "15.000", invoices=[f"local:{b3_local}", b1])))
		self.assertEqual(
			result["payment"],
			{"allocated": "15.000", "unallocated": "0.000", "invoices": [b3, b1], "skipped": []},
		)
		self.assertEqual(self.references(result["name"]), [(b3, Decimal("13.000")), (b1, Decimal("2.000"))])
		self.assertEqual(
			(outstanding(b1), outstanding(b2), outstanding(b3)),
			(Decimal("4.000"), Decimal("10.000"), Decimal("0.000")),
		)

	def test_ticked_more_than_owed_stays_an_advance_not_spread(self):
		customer = make_customer("tickadv", limit=None)
		(b1, _), (b2, _) = self.bills(customer, "6.000", "10.000")
		result = self.assertOk(
			self.pay(self.payment(customer, "20.000", invoices=[b1], advance_confirmed=True))
		)
		self.assertEqual(
			result["payment"],
			{"allocated": "6.000", "unallocated": "14.000", "invoices": [b1], "skipped": []},
		)
		self.assertEqual(outstanding(b2), Decimal("10.000"))

	def test_ticked_invoice_paid_meanwhile_becomes_advance(self):
		"""Review Focus 2: the till ticked B2 offline; before its receipt is booked the web settles B2.
		At booking B2 is skipped with a note, the money is an advance and B1 is untouched."""
		customer = make_customer("meanwhile", limit=None)
		(b1, _), (b2, _) = self.bills(customer, "6.000", "10.000")
		real_resolve = payments.resolve_customer
		web = []

		def web_settles_first(ctx, data):
			# between the push and the booking: a Payment Entry against B2 is submitted
			web.append(web_payment(customer, "10.000", invoices=[b2]).name)
			return real_resolve(ctx, data)

		event_id = new_id()
		with mock.patch.object(payments, "resolve_customer", web_settles_first):
			result = self.assertOk(
				self.pay(self.payment(customer, "10.000", invoices=[b2]), event_id=event_id)
			)
		self.assertEqual(len(web), 1)
		self.assertEqual(self.references(web[0]), [(b2, Decimal("10.000"))])
		self.assertEqual(
			result["payment"],
			{"allocated": "0.000", "unallocated": "10.000", "invoices": [], "skipped": [b2]},
		)
		self.assertIn(f"Ticked invoice {b2} skipped", self.note(event_id))
		self.assertEqual((outstanding(b1), outstanding(b2)), (Decimal("6.000"), Decimal("0.000")))
		entry = frappe.get_doc(PAYMENT_ENTRY, result["name"])
		self.assertEqual(Decimal(money(entry.unallocated_amount)), Decimal("10.000"))
		self.assertTrue(entry.remarks.endswith(f"; skipped ticked invoices: {b2}"))

	def test_unresolved_local_invoice_skipped_with_note(self):
		"""Review Focus 4: a `local:` id no Sales Invoice has (its sale was refused) is skipped, never a
		refusal; the rest of the ticks are booked."""
		customer = make_customer("unresolved", limit=None)
		(b1, _) = self.bills(customer, "6.000")[0]
		missing = f"local:{new_id()}"
		event_id = new_id()
		result = self.assertOk(
			self.pay(self.payment(customer, "10.000", invoices=[missing, b1]), event_id=event_id)
		)
		self.assertEqual(
			result["payment"],
			{"allocated": "6.000", "unallocated": "4.000", "invoices": [b1], "skipped": [missing]},
		)
		self.assertIn(
			f"Ticked invoice {missing} skipped: no Sales Invoice has this till id yet", self.note(event_id)
		)

	def test_foreign_customer_or_company_invoice_skipped(self):
		customer = make_customer("foreign", limit=None)
		other = make_customer("foreignother", limit=None)
		(own, _) = self.bills(customer, "5.000")[0]
		(theirs, _) = self.bills(other, "7.000")[0]
		(moved, _) = self.bills(customer, "8.000")[0]
		frappe.db.set_value("Sales Invoice", moved, "company", "_Test Crenya Other Company")
		try:
			event_id = new_id()
			result = self.assertOk(
				self.pay(
					self.payment(customer, "9.000", invoices=[theirs, moved, own, "NO-SUCH-BILL"]),
					event_id=event_id,
				)
			)
		finally:
			frappe.db.set_value("Sales Invoice", moved, "company", fixtures.COMPANY)
		self.assertEqual(
			result["payment"],
			{
				"allocated": "5.000",
				"unallocated": "4.000",
				"invoices": [own],
				"skipped": [theirs, moved, "NO-SUCH-BILL"],
			},
		)
		note = self.note(event_id)
		for name in (theirs, moved, "NO-SUCH-BILL"):
			self.assertIn(f"Ticked invoice {name} skipped", note)
		self.assertEqual(outstanding(theirs), Decimal("7.000"))

	def test_return_invoice_never_ticked(self):
		customer = make_customer("tickreturn", limit=500)
		sale = self.on_account_sale(customer, "100.000", cash="40.000")
		sold = self.post(sale)["name"]
		ret = self.return_payload(sale, [(fixtures.MILK, "-1", "50.000", 1)], credit_offset="50.000")
		ret["payments"] = []
		credit_note = self.post(ret)["name"]
		self.assertEqual(frappe.db.get_value("Sales Invoice", credit_note, "is_return"), 1)
		result = self.assertOk(self.pay(self.payment(customer, "5.000", invoices=[credit_note, sold])))
		self.assertEqual(result["payment"]["skipped"], [credit_note])
		self.assertEqual(result["payment"]["invoices"], [sold])

	def test_advance_confirmed_never_refuses(self):
		customer = make_customer("advconfirm", limit=None)
		(b1, _) = self.bills(customer, "2.000")[0]
		for value in (True, False, "maybe", {"x": 1}, None):
			payload = self.payment(customer, "3.000", invoices=[b1], advance_confirmed=value)
			result = self.assertOk(self.pay(payload))
			remarks = frappe.db.get_value(PAYMENT_ENTRY, result["name"], "remarks")
			self.assertEqual("advance confirmed at the till" in remarks, value is True, value)
		# and without ticks (an untick overpay from a retail till sends no key at all)
		self.assertOk(self.pay(self.payment(customer, "3.000", advance_confirmed=True)))

	def test_two_hundred_and_one_ticked_bills_are_accepted(self):
		"""The till may tick up to 1000 bills: a receipt naming 201 books (the unknown names are
		skipped with notes, the money stays an advance), never refused for the count."""
		customer = make_customer("manyticks", limit=None)
		payload = self.payment(customer, "1.000", invoices=[f"SINV-NOPE-{n}" for n in range(201)])
		result = self.assertOk(self.pay(payload))
		self.assertEqual(len(result["skipped"]), 201)
		self.assertEqual(result["payment"]["unallocated"], "1.000")

	def test_invoices_list_too_long_refused(self):
		customer = make_customer("toolong", limit=None)
		payload = self.payment(customer, "1.000", invoices=[f"SINV-{n}" for n in range(1001)])
		self.assertError(self.pay(payload), "validation", False)
		self.assertEqual(self.entries(payload["local_id"]), [])
		not_a_list = self.payment(customer, "1.000", invoices="SINV-1")
		self.assertError(self.pay(not_a_list), "validation", False)

	def test_result_lists_skipped(self):
		"""The answer names the skipped bills, again when a resend is answered as a duplicate."""
		customer = make_customer("skippedresult", limit=None)
		(b1, _) = self.bills(customer, "4.000")[0]
		missing = f"local:{new_id()}"
		payload = self.payment(customer, "4.000", invoices=[missing, b1])
		first = self.assertOk(self.pay(payload))
		self.assertEqual(first["payment"]["skipped"], [missing])
		again = self.pay(payload)
		self.assertEqual(again["status"], "duplicate")
		self.assertEqual(again["payment"]["skipped"], [missing])
		self.assertEqual(again["payment"]["invoices"], [b1])
		# a payment without ticks never says `skipped`, even when the cashier's note ends like the
		# skipped part of the remarks
		plain = self.assertOk(self.pay(self.payment(customer, "1.000")))
		self.assertNotIn("skipped", plain["payment"])
		noted = self.payment(customer, "1.000", note="; skipped ticked invoices: X")
		fresh = self.assertOk(self.pay(noted))
		self.assertTrue(frappe.db.get_value(PAYMENT_ENTRY, fresh["name"], "remarks").endswith("invoices: X"))
		self.assertNotIn("skipped", fresh["payment"])


class TestCapability(TicksCase):
	def test_capability_and_till_bootstrap_flag(self):
		self.assertEqual(FEATURE_SETTLE_INVOICES, "credit_settle_invoices")
		self.assertEqual(sync_api.get_sync_capabilities()["features"][FEATURE_SETTLE_INVOICES], 1)
		boot = device_api.get_bootstrap(device_id=self.device_id)
		self.assertEqual(boot["features"][FEATURE_SETTLE_INVOICES], 1)
		self.assertEqual(boot["features"]["credit_sales"], 1)


class TestOneLockForTillAndWeb(TicksCase):
	def test_named_lock_name_equals_event_lock_name(self):
		customer = make_customer("lockname", limit=None)
		event = self.event(self.payment(customer, "1.000", invoices=["X"]), AGGREGATE)
		self.assertEqual(events._lock_name(event), events.lock_name(customer_lock_key(customer)))

	def test_two_payments_same_customer_serialised(self):
		"""The web holds the customer's lock (`named_lock`) and settles B1; a till's receipt ticking B1
		waits for it, then reads B1 paid: skipped, all an advance. B1 is allocated once."""
		customer = make_customer("serialised", limit=None)
		(b1, _) = self.bills(customer, "10.000")[0]
		till = self.payment(customer, "10.000", invoices=[b1])
		event = self.event(till, AGGREGATE)
		site = frappe.local.site
		sites_path = frappe.local.sites_path
		device_id = self.device_id
		answers = []

		def worker():
			frappe.init(site=site, sites_path=sites_path)
			frappe.connect()
			try:
				frappe.set_user("Administrator")
				answers.extend(sync_api.push_batch(device_id=device_id, events=[event]))
			except Exception as exc:  # the main thread reports it
				answers.append(exc)
			finally:
				frappe.destroy()

		thread = None
		with events.named_lock(customer_lock_key(customer)):
			thread = threading.Thread(target=worker)
			thread.start()
			# the till's receipt is waiting for the lock; the web books and commits meanwhile
			time.sleep(2)
			self.assertEqual(self.entries(till["local_id"]), [])
			# no commit here: named_lock commits the web's work before it lets the till in
			web = web_payment(customer, "10.000", invoices=[b1])
		thread.join(60)
		self.assertFalse(thread.is_alive())
		self.assertEqual(len(answers), 1, msg=answers)
		lost = answers[0]
		self.assertNotIsInstance(lost, Exception, msg=repr(lost))
		self.assertEqual(lost["status"], "ok", msg=lost.get("error"))
		self.assertEqual(self.references(web.name), [(b1, Decimal("10.000"))])
		self.assertEqual(
			lost["payment"], {"allocated": "0.000", "unallocated": "10.000", "invoices": [], "skipped": [b1]}
		)
		frappe.db.rollback()
		self.assertEqual(outstanding(b1), Decimal("0.000"))
		allocated = frappe.db.sql(
			"select coalesce(sum(allocated_amount), 0) from `tabPayment Entry Reference` "
			"where reference_doctype = 'Sales Invoice' and reference_name = %s and docstatus = 1",
			(b1,),
		)[0][0]
		self.assertEqual(Decimal(money(allocated)), Decimal("10.000"))
