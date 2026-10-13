"""The return lookup at the till (audit P0 #10): the device is the authority, so a cashier whose
only role is Crenya POS User reads its company's invoice to return against, and an invoice of
another company is never found.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_returns
"""

import unittest
from types import SimpleNamespace

import frappe

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import returns as returns_api
from crenya_pos_api.sync import returns
from crenya_pos_api.sync.context import DeviceContext
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import SyncCase, new_id


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
