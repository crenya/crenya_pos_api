"""The shared case of the sync integration tests: a registered till of the test POS Profile and
the helpers that build, push and pull its events (not a test module itself: the runner collects
test_*.py only).

push_batch commits per event by design, so fixtures and documents created here persist on the
test site (all names are prefixed with _Test Crenya / _TC).
"""

import uuid
from decimal import Decimal

import frappe
from frappe.utils import nowdate, nowtime

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.sync.hashing import payload_hash
from crenya_pos_api.tests import fixtures


def new_id():
	return str(uuid.uuid4())


def money(value):
	return f"{Decimal(value).quantize(Decimal('0.001')):f}"


class SyncCase(FrappeTestCase):
	"""A registered till of the test POS Profile (`fixtures`), pull lag 0, OMR at 3 decimals, and
	the helpers that build and push its events. Subclasses add the tests."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

		cls._saved_precision = frappe.db.get_single_value("System Settings", "currency_precision")
		frappe.db.set_single_value("System Settings", "currency_precision", "3")
		frappe.db.set_default("currency_precision", "3")
		frappe.clear_cache()

		cls._saved_conf = {
			key: frappe.conf.get(key) for key in ("crenya_pos_pull_lag_seconds", "crenya_pos_total_tolerance")
		}
		frappe.conf.crenya_pos_pull_lag_seconds = 0
		frappe.conf.crenya_pos_total_tolerance = "0.010"

		cls.profile = fixtures.setup_fixtures()
		cls.walk_in = cls.profile.customer
		cls.device_id = new_id()
		cls.device = device_api.register_device(
			device_id=cls.device_id,
			device_name="Till 1",
			pos_profile=cls.profile.name,
			app_version="0.1.0",
			platform="linux",
		)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.set_single_value("System Settings", "currency_precision", cls._saved_precision or "")
		frappe.db.set_default("currency_precision", cls._saved_precision or "")
		for key, value in cls._saved_conf.items():
			if value is None:
				frappe.conf.pop(key, None)
			else:
				frappe.conf[key] = value
		frappe.db.commit()
		frappe.clear_cache()
		super().tearDownClass()

	# helpers

	def sale_payload(self, lines=None, payments=None, grand_total=None, **overrides):
		lines = lines or [(fixtures.MILK, "2", "0.600")]
		items = []
		total = Decimal(0)
		for line_no, (item_code, qty, rate) in enumerate(lines, start=1):
			amount = Decimal(qty) * Decimal(rate)
			total += amount
			items.append(
				{
					"line_no": line_no,
					"item_code": item_code,
					"item_name": item_code,
					"qty": qty,
					"uom": "Nos",
					"conversion_factor": "1",
					"price_list_rate": rate,
					"discount_percentage": "0",
					"rate": rate,
					"amount": money(amount),
					"item_tax_template": fixtures.ZERO_TEMPLATE if item_code == fixtures.BREAD else None,
					"against_line_no": None,
					"against_row_name": None,
				}
			)
		grand_total = grand_total or money(total)
		payload = {
			"local_id": new_id(),
			"offline_number": f"OFF-{self.device['device_short']}-{uuid.uuid4().hex[:6]}",
			"pos_profile": self.profile.name,
			"company": fixtures.COMPANY,
			"customer": self.walk_in,
			"customer_local_id": None,
			"posting_date": nowdate(),
			"posting_time": nowtime()[:8],
			"currency": fixtures.CURRENCY,
			"is_return": 0,
			"return_against": None,
			"return_against_local_id": None,
			"taxes_and_charges": fixtures.TAX_TEMPLATE,
			"buyer_vatin": None,
			"cashier": frappe.session.user,
			"remarks": None,
			"items": items,
			"payments": payments
			if payments is not None
			else [{"mode_of_payment": fixtures.CASH, "amount": money(total)}],
			"client_totals": {"grand_total": grand_total, "rounded_total": grand_total},
		}
		payload.update(overrides)
		return payload

	def return_payload(self, sale, lines, **overrides):
		payload = self.sale_payload(
			lines=[(item_code, qty, rate) for item_code, qty, rate, _against in lines],
			is_return=1,
			return_against_local_id=sale["local_id"],
		)
		total = Decimal(0)
		for item, (_code, qty, rate, against) in zip(payload["items"], lines, strict=True):
			item["against_line_no"] = against
			total += Decimal(qty) * Decimal(rate)
		payload["payments"] = [{"mode_of_payment": fixtures.CASH, "amount": money(total)}]
		payload["client_totals"] = {"grand_total": money(total)}
		payload.update(overrides)
		return payload

	def customer_payload(self, name="Ahmed Al Balushi"):
		return {
			"local_id": new_id(),
			"customer_name": name,
			"mobile_no": "+96891234567",
			"email_id": None,
			"tax_id": None,
			"customer_group": None,
			"territory": None,
		}

	def shift_payload(self, **overrides):
		payload = {
			"local_id": new_id(),
			"shift_number": f"SH-{self.device['device_short']}-{uuid.uuid4().hex[:6]}",
			"pos_profile": self.profile.name,
			"company": fixtures.COMPANY,
			"cashier": frappe.session.user,
			"opened_at": "2026-09-29T04:00:00Z",
			"closed_at": "2026-09-29T12:05:30Z",
			"opening_float": "20.000",
			"sales_count": 2,
			"returns_count": 0,
			"sales_total": "2.400",
			"returns_total": "0.000",
			"net_total": "2.400",
			"tax_total": "0.114",
			"payments": [
				{
					"mode_of_payment": fixtures.CASH,
					"expected": "22.400",
					"counted": "22.300",
					"difference": "-0.100",
				}
			],
			"invoice_local_ids": [],
			"notes": "Till drawer short by 100 baisa",
		}
		payload.update(overrides)
		return payload

	def event(self, payload, aggregate_type="Sales Invoice", event_id=None, sequence_no=1):
		return {
			"event_id": event_id or new_id(),
			"aggregate_type": aggregate_type,
			"operation": "submit",
			"local_id": payload["local_id"],
			"sequence_no": sequence_no,
			"payload_hash": payload_hash(payload),
			"payload": payload,
		}

	def push(self, *events):
		return sync_api.push_batch(device_id=self.device_id, events=list(events))

	def push_one(self, event):
		results = self.push(event)
		self.assertEqual(len(results), 1)
		return results[0]

	def assertOk(self, result):
		self.assertEqual(result["status"], "ok", msg=result.get("error"))
		return result

	def assertError(self, result, code, retryable):
		self.assertEqual(result["status"], "error", msg=result)
		self.assertEqual(result["error"]["code"], code, msg=result["error"])
		self.assertEqual(result["error"]["retryable"], retryable)
		return result

	def invoices_with_local_id(self, local_id):
		return frappe.get_all("Sales Invoice", filters={"crenya_local_id": local_id}, pluck="name")

	# users and pulls

	def _cashier(self):
		email = "crenya.cashier.test@example.com"
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "Crenya Cashier",
					"send_welcome_email": 0,
					"roles": [{"role": "Crenya POS User"}],
				}
			).insert(ignore_permissions=True)
		return email

	def _pull_all(self, entity, limit, cursor=None):
		records, pages = [], 0
		while True:
			page = sync_api.pull_changes(device_id=self.device_id, entity=entity, cursor=cursor, limit=limit)
			pages += 1
			records.extend(page["records"])
			cursor = page["next_cursor"]
			if not page["has_more"]:
				return records, cursor, pages
			self.assertLess(pages, 500, "pull did not terminate")
