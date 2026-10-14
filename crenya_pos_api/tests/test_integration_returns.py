"""The return lookup at the till (audit P0 #10): the device is the authority, so a cashier whose
only role is Crenya POS User reads its company's invoice to return against, and an invoice of
another company is never found.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_returns
"""

import unittest
from decimal import Decimal
from types import SimpleNamespace

import frappe

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import returns as returns_api
from crenya_pos_api.sync import returns
from crenya_pos_api.sync.context import DeviceContext
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import SyncCase, money, new_id
from crenya_pos_api.tests.test_integration_credit import EVENT, CreditCase, make_customer


class TestReturnLookup(SyncCase):
	def sold(self):
		sale = self.sale_payload(lines=[(fixtures.MILK, "2", "0.600")])
		return self.assertOk(self.push_one(self.event(sale)))

	def test_cashier_with_only_pos_user_reads_return_invoice(self):
		sold = self.sold()
		cashier = self._cashier()
		self.assertFalse(frappe.has_permission("Sales Invoice", "read", sold["name"], user=cashier))
		device_id = new_id()
		frappe.set_user(cashier)
		try:
			device_api.register_device(
				device_id=device_id, device_name="Returns till", pos_profile=self.profile.name
			)
			lookup = returns_api.get_invoice_for_return(device_id=device_id, invoice=sold["name"])
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(lookup["name"], sold["name"])
		self.assertEqual(lookup["items"][0]["item_code"], fixtures.MILK)

	def test_return_lookup_refuses_other_company(self):
		sold = self.sold()
		profile = frappe._dict(self.profile.as_dict())
		profile.company = "_Test Crenya Other Company"
		other = DeviceContext(device={"name": "other", "device_type": "till"}, profile=profile)
		with self.assertRaises(frappe.DoesNotExistError):
			returns.get_invoice_for_return(other, sold["name"])
		# the invoice's own company still finds it
		mine = DeviceContext(device={"name": "mine", "device_type": "till"}, profile=self.profile)
		self.assertEqual(returns.get_invoice_for_return(mine, sold["name"])["name"], sold["name"])


class TestInvoiceOfCompany(unittest.TestCase):
	"""The rule on its own: the invoice and its POS Profile are the device's company's."""

	def test_rule(self):
		ctx = SimpleNamespace(company="A")
		self.assertTrue(returns.invoice_of_company(ctx, frappe._dict(company="A", pos_profile=None), None))
		self.assertTrue(returns.invoice_of_company(ctx, frappe._dict(company="A", pos_profile="P"), "A"))
		self.assertFalse(returns.invoice_of_company(ctx, frappe._dict(company="B", pos_profile=None), None))
		# a POS Profile of another company, or one that no longer exists
		self.assertFalse(returns.invoice_of_company(ctx, frappe._dict(company="A", pos_profile="P"), "B"))
		self.assertFalse(returns.invoice_of_company(ctx, frappe._dict(company="A", pos_profile="P"), None))


class TestCreditInvoiceReturns(CreditCase):
	"""Returns of on-account invoices (retail P1 task 9): the part credited to the customer's account
	first (`credit_offset`), the rest refunded, at OMR's 3 decimals. An invoice of 100.000 with 40.000
	paid owes 60.000."""

	def sold(self, customer, cash="40.000"):
		"""10 x 10.000 on account but for `cash`; the sale and its posted invoice."""
		sale = self.sale_payload(
			lines=[(fixtures.MILK, "10", "10.000")],
			payments=[{"mode_of_payment": fixtures.CASH, "amount": cash}] if Decimal(cash) else [],
			customer=customer,
			on_account=money(Decimal("100.000") - Decimal(cash)),
		)
		return sale, self.post(sale)

	def give_back(self, sale, qty, offset, refund=None, **over):
		"""A return of `qty` x 10.000 crediting `offset` to the account and refunding `refund` in cash."""
		total = Decimal(qty) * Decimal("10.000")
		ret = self.return_payload(sale, [(fixtures.MILK, f"-{qty}", "10.000", 1)], **over)
		if offset is not None:
			ret["credit_offset"] = offset
		ret["payments"] = (
			[{"mode_of_payment": fixtures.CASH, "amount": f"-{refund}"}]
			if refund
			else ([] if offset is not None and Decimal(offset) == total else ret["payments"])
		)
		return ret

	def owed(self, name):
		return Decimal(money(frappe.db.get_value("Sales Invoice", name, "outstanding_amount")))

	def test_return_all_credited_reduces_original_outstanding(self):
		customer = make_customer("retall", limit=500)
		sale, sold = self.sold(customer)
		self.assertEqual(self.owed(sold["name"]), Decimal("60.000"))
		result = self.post(self.give_back(sale, 3, "30.000"))
		self.assertEqual(self.owed(sold["name"]), Decimal("30.000"))
		doc = frappe.get_doc("Sales Invoice", result["name"])
		self.assertEqual((doc.is_return, doc.update_outstanding_for_self, len(doc.payments)), (1, 0, 0))

	def test_return_split_credit_and_refund(self):
		customer = make_customer("retsplit", limit=500)
		sale, sold = self.sold(customer)
		result = self.post(self.give_back(sale, 3, "20.000", refund="10.000"))
		self.assertEqual(self.owed(sold["name"]), Decimal("40.000"))
		doc = frappe.get_doc("Sales Invoice", result["name"])
		self.assertEqual([Decimal(money(row.amount)) for row in doc.payments], [Decimal("-10.000")])

	def test_offline_credit_return_credits_the_whole_return_to_the_account(self):
		"""An offline till cannot know the outstanding: it credits everything (founder decision 3)."""
		customer = make_customer("retoffline", limit=500)
		sale, sold = self.sold(customer, cash="100.000")
		self.assertEqual(self.owed(sold["name"]), Decimal("0.000"))
		event = self.event(self.give_back(sale, 3, "30.000"))
		result = self.assertOk(self.push_one(event))
		doc = frappe.get_doc("Sales Invoice", result["name"])
		self.assertEqual((len(doc.payments), Decimal(money(doc.grand_total))), (0, Decimal("-30.000")))

	def test_credit_above_outstanding_leaves_customer_in_credit_with_note(self):
		from erpnext.selling.doctype.customer.customer import get_customer_outstanding

		customer = make_customer("retcredit", limit=500)
		sale, _sold = self.sold(customer, cash="100.000")
		event = self.event(self.give_back(sale, 3, "30.000"))
		self.assertOk(self.push_one(event))
		note = frappe.db.get_value(EVENT, event["event_id"], "note")
		self.assertIn("is more than invoice", note)
		self.assertIn("left in credit by 30.000", note)
		# the customer is owed 30.000: ERPNext's balance of the customer is negative
		self.assertEqual(
			Decimal(money(get_customer_outstanding(customer, fixtures.COMPANY))), Decimal("-30.000")
		)

	def test_credit_within_outstanding_has_no_note(self):
		customer = make_customer("retnonote", limit=500)
		sale, _sold = self.sold(customer)
		event = self.event(self.give_back(sale, 3, "30.000"))
		self.assertOk(self.push_one(event))
		self.assertNotIn("left in credit", frappe.db.get_value(EVENT, event["event_id"], "note") or "")

	def test_return_without_credit_offset_is_unchanged(self):
		"""A 0.9.0 till sends no credit_offset: the whole return is refunded and the original's
		outstanding is reduced as ERPNext does for any return."""
		customer = make_customer("retplain2", limit=500)
		sale, _sold = self.sold(customer)
		ret = self.give_back(sale, 3, None)
		self.assertNotIn("credit_offset", ret)
		result = self.assertOk(self.push_one(self.event(ret)))
		doc = frappe.get_doc("Sales Invoice", result["name"])
		self.assertEqual(doc.update_outstanding_for_self, 1)
		self.assertEqual([Decimal(money(row.amount)) for row in doc.payments], [Decimal("-30.000")])

	def test_invoice_outstanding_answer(self):
		from crenya_pos_api.api import credit as credit_api

		customer = make_customer("retanswer", limit=500)
		_sale, sold = self.sold(customer)
		answer = credit_api.get_invoice_outstanding(device_id=self.device_id, invoice=sold["name"])
		self.assertEqual(
			(answer["name"], answer["grand_total"], answer["outstanding"]),
			(sold["name"], "100.000", "60.000"),
		)
		self.assertTrue(answer["as_of"].endswith("Z"))
		# by the till's local id too
		by_local = credit_api.get_invoice_outstanding(device_id=self.device_id, invoice=_sale["local_id"])
		self.assertEqual(by_local["name"], sold["name"])
		# after a return credited to the account, the answer follows
		self.post(self.give_back(_sale, 3, "30.000"))
		after = credit_api.get_invoice_outstanding(device_id=self.device_id, invoice=sold["name"])
		self.assertEqual(after["outstanding"], "30.000")


class TestCreditOffsetOnTheDefaultCustomer(CreditCase):
	def test_a_return_of_a_walk_in_sale_cannot_be_credited_to_an_account(self):
		sale = self.sale_payload(lines=[(fixtures.MILK, "2", "10.000")])
		self.post(sale)
		ret = self.return_payload(sale, [(fixtures.MILK, "-1", "10.000", 1)], credit_offset="10.000")
		ret["payments"] = []
		result = self.push_one(self.event(ret))
		self.assertError(result, "validation", False)
		self.assertIn("default customer", result["error"]["message"])
		# refunded as always
		plain = self.return_payload(sale, [(fixtures.MILK, "-1", "10.000", 1)])
		self.assertOk(self.push_one(self.event(plain)))
