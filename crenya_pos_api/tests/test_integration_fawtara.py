"""Receipt verification page and `fawtara.get_status` against a real ERPNext site.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_fawtara
Invoices and customers created here persist on the test site (_Test Crenya / _TC names).
"""

import uuid
from decimal import Decimal

import frappe
from frappe.utils import nowdate, nowtime, set_request
from frappe.website.serve import get_response

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import fawtara as fawtara_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.sync import fawtara
from crenya_pos_api.sync.hashing import payload_hash
from crenya_pos_api.tests import fixtures

VERIFY_ROUTE = "fawtara/verify"


def new_id():
	return str(uuid.uuid4())


class TestCrenyaFawtara(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

		cls._saved_precision = frappe.db.get_single_value("System Settings", "currency_precision")
		frappe.db.set_single_value("System Settings", "currency_precision", "3")
		frappe.db.set_default("currency_precision", "3")
		frappe.clear_cache()

		cls.profile = fixtures.setup_fixtures()
		cls.customer_name = f"_Test Crenya Verify Buyer {uuid.uuid4().hex[:8]}"
		cls.customer = fixtures.make_customer(cls.customer_name)
		frappe.db.set_value("Company", fixtures.COMPANY, "tax_id", "OM1100999999")
		cls.device_id = new_id()
		cls.device = device_api.register_device(
			device_id=cls.device_id, device_name="Verify Till", pos_profile=cls.profile.name
		)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("System Settings", "currency_precision", cls._saved_precision or "")
		frappe.db.set_default("currency_precision", cls._saved_precision or "")
		frappe.db.commit()
		frappe.clear_cache()
		super().tearDownClass()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.form_dict = frappe._dict()

	# helpers

	def push_sale(self, customer=None, is_return=False, against=None):
		qty = "-1" if is_return else "2"
		amount = f"{Decimal(qty) * Decimal('0.600'):.3f}"
		payload = {
			"local_id": new_id(),
			"offline_number": f"OFF-{self.device['device_short']}-{uuid.uuid4().hex[:6]}",
			"pos_profile": self.profile.name,
			"company": fixtures.COMPANY,
			"customer": customer or self.profile.customer,
			"posting_date": nowdate(),
			"posting_time": nowtime()[:8],
			"currency": fixtures.CURRENCY,
			"is_return": 1 if is_return else 0,
			"return_against_local_id": against,
			"taxes_and_charges": fixtures.TAX_TEMPLATE,
			"items": [
				{
					"line_no": 1,
					"item_code": fixtures.MILK,
					"qty": qty,
					"uom": "Nos",
					"conversion_factor": "1",
					"price_list_rate": "0.600",
					"rate": "0.600",
					"amount": amount,
					"against_line_no": 1 if is_return else None,
				}
			],
			"payments": [{"mode_of_payment": fixtures.CASH, "amount": amount}],
			"client_totals": {"grand_total": amount, "rounded_total": amount},
		}
		event = {
			"event_id": new_id(),
			"aggregate_type": "Sales Invoice",
			"operation": "submit",
			"local_id": payload["local_id"],
			"sequence_no": 1,
			"payload_hash": payload_hash(payload),
			"payload": payload,
		}
		result = sync_api.push_batch(device_id=self.device_id, events=[event])[0]
		self.assertEqual(result["status"], "ok", msg=result.get("error"))
		return payload, result

	def render_verify(self, local_id):
		frappe.set_user("Guest")
		try:
			set_request(method="GET", path=f"/{VERIFY_ROUTE}", query_string={"id": local_id})
			frappe.local.form_dict = frappe._dict(id=local_id)
			response = get_response(VERIFY_ROUTE)
			return response.status_code, frappe.safe_decode(response.get_data())
		finally:
			frappe.set_user("Administrator")

	# page

	def test_capabilities_announce_verify_page(self):
		self.assertTrue(sync_api.get_sync_capabilities()["features"]["verify_page"])

	def test_verify_page_shows_seller_facts_only(self):
		payload, result = self.push_sale(customer=self.customer)
		status, html = self.render_verify(payload["local_id"])
		self.assertEqual(status, 200)
		self.assertIn("Invoice verified", html)
		self.assertIn("تم التحقق من الفاتورة", html)
		self.assertIn(result["name"], html)
		self.assertIn(payload["offline_number"], html)
		self.assertIn(frappe.db.get_value("Company", fixtures.COMPANY, "company_name"), html)
		self.assertIn("OM1100999999", html)
		self.assertIn("1.200", html)
		self.assertIn("0.057", html)
		self.assertIn("Tax invoice", html)
		self.assertNotIn(self.customer_name, html)
		self.assertNotIn(self.customer, html)

	def test_verify_page_credit_note(self):
		sale, _result = self.push_sale()
		ret, result = self.push_sale(is_return=True, against=sale["local_id"])
		status, html = self.render_verify(ret["local_id"])
		self.assertEqual(status, 200)
		self.assertIn("Credit note", html)
		self.assertIn(result["name"], html)

	def test_verify_page_unknown_id_is_neutral_404(self):
		for local_id in (new_id(), "<script>alert(1)</script>", "", "x" * 300):
			with self.subTest(local_id=local_id[:20]):
				status, html = self.render_verify(local_id)
				self.assertEqual(status, 404)
				self.assertIn("Invoice not found yet", html)
				self.assertNotIn("<script>alert(1)</script>", html)
				self.assertNotIn(fixtures.COMPANY, html)

	def test_verify_page_rate_limit(self):
		saved_ip = getattr(frappe.local, "request_ip", None)
		saved_limit = frappe.conf.get("crenya_pos_verify_rate_limit")
		ip = f"203.0.113.{uuid.uuid4().int % 250 + 1}"
		frappe.local.request_ip = ip
		frappe.conf.crenya_pos_verify_rate_limit = 2
		try:
			self.assertFalse(fawtara.verify_rate_limited())
			self.assertFalse(fawtara.verify_rate_limited())
			status, html = self.render_verify(new_id())
			self.assertEqual(status, 429)
			self.assertIn("Too many requests", html)
		finally:
			frappe.cache.delete_value(f"crenya_pos_verify:{ip}")
			frappe.local.request_ip = saved_ip
			if saved_limit is None:
				frappe.conf.pop("crenya_pos_verify_rate_limit", None)
			else:
				frappe.conf.crenya_pos_verify_rate_limit = saved_limit

	# get_status

	def test_get_status(self):
		payload, result = self.push_sale()
		unknown = new_id()
		statuses = fawtara_api.get_status(device_id=self.device_id, local_ids=[unknown, payload["local_id"]])
		self.assertEqual(len(statuses), 1)
		entry = statuses[0]
		self.assertEqual(entry["local_id"], payload["local_id"])
		self.assertEqual(entry["name"], result["name"])
		self.assertEqual(set(entry), {"local_id", "name", "fawtara_status", "document_id"})
		if not fawtara.compliance_installed():
			self.assertIsNone(entry["fawtara_status"])
			self.assertIsNone(entry["document_id"])

		# a JSON string works as well (form posts)
		as_json = fawtara_api.get_status(
			device_id=self.device_id, local_ids=frappe.as_json([payload["local_id"]])
		)
		self.assertEqual(as_json[0]["name"], result["name"])
		self.assertEqual(fawtara_api.get_status(device_id=self.device_id, local_ids=[]), [])

	def test_get_status_rejects_bad_input(self):
		for local_ids in ([new_id() for _ in range(51)], "not json", {"a": 1}, [""], [7], ["x" * 141]):
			with self.subTest(local_ids=str(local_ids)[:30]):
				with self.assertRaises(frappe.ValidationError):
					fawtara_api.get_status(device_id=self.device_id, local_ids=local_ids)
		with self.assertRaises(frappe.ValidationError):
			fawtara_api.get_status(device_id=new_id(), local_ids=[])

	def test_get_status_with_einvoicing_fields(self):
		if not fawtara.compliance_installed():
			self.skipTest("oman_compliance is not installed on this site")
		payload, result = self.push_sale()
		frappe.db.set_value(
			"Sales Invoice",
			result["name"],
			{"fawtara_status": "Accepted", "fawtara_document_id": "ASP-TEST-1"},
			update_modified=False,
		)
		entry = fawtara_api.get_status(device_id=self.device_id, local_ids=[payload["local_id"]])[0]
		self.assertEqual((entry["fawtara_status"], entry["document_id"]), ("Accepted", "ASP-TEST-1"))

		status, html = self.render_verify(payload["local_id"])
		self.assertEqual(status, 200)
		self.assertIn("Accepted", html)
		self.assertIn("ASP-TEST-1", html)
