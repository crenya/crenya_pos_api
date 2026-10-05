"""End-to-end tests of the sync protocol against a real ERPNext site.

Run with: bench --site <test-site> run-tests --app crenya_pos_api
push_batch commits per event by design, so fixtures and documents created here
persist on the test site (all names are prefixed with _Test Crenya / _TC).
"""

import uuid
from decimal import Decimal

import frappe
from frappe.utils import cint, get_datetime, get_system_timezone, nowdate, nowtime

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import loyalty as loyalty_api
from crenya_pos_api.api import returns as returns_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.sync import errors
from crenya_pos_api.sync.context import check_protocol_version, get_device_context
from crenya_pos_api.sync.cursor import decode_cursor
from crenya_pos_api.sync.hashing import payload_hash
from crenya_pos_api.tests import fixtures
from crenya_pos_api.utils.dates import parse_iso_utc, to_site_naive

EVENT = "Crenya Sync Event"
SHIFT = "Crenya POS Shift"


def new_id():
	return str(uuid.uuid4())


def money(value):
	return f"{Decimal(value).quantize(Decimal('0.001')):f}"


class TestCrenyaSync(FrappeTestCase):
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

	# capabilities / registration / bootstrap

	def test_capabilities(self):
		caps = sync_api.get_sync_capabilities()
		self.assertEqual(caps["protocol_version"], 2)
		self.assertTrue(caps["features"]["sales"])
		self.assertEqual(caps["user"], "Administrator")

	def test_protocol_unsupported(self):
		with self.assertRaises(errors.ProtocolUnsupportedError):
			check_protocol_version(3)
		check_protocol_version(2)
		check_protocol_version(1)

	def test_register_is_idempotent_and_short_code_stable(self):
		again = device_api.register_device(
			device_id=self.device_id, device_name="Till 1 renamed", pos_profile=self.profile.name
		)
		self.assertEqual(again["device_short"], self.device["device_short"])
		self.assertEqual(again["offline_prefix"], f"OFF-{self.device['device_short']}-")

		other = device_api.register_device(
			device_id=new_id(), device_name="Till 2", pos_profile=self.profile.name
		)
		self.assertNotEqual(other["device_short"], self.device["device_short"])

	def test_bootstrap(self):
		data = device_api.get_bootstrap(device_id=self.device_id)
		self.assertEqual(data["profile"]["currency"], "OMR")
		self.assertEqual(data["profile"]["currency_precision"], 3)
		self.assertTrue(data["profile"]["disable_rounded_total"])
		self.assertEqual(data["taxes"][0]["rate"], "5")
		self.assertTrue(data["taxes"][0]["included_in_print_rate"])
		self.assertEqual(data["payment_methods"][0]["mode_of_payment"], fixtures.CASH)
		self.assertTrue(data["payment_methods"][0]["default"])
		zero = [t for t in data["item_tax_templates"] if t["name"] == fixtures.ZERO_TEMPLATE]
		self.assertEqual(zero[0]["taxes"][0]["tax_rate"], "0")

	def test_bootstrap_allow_negative_stock(self):
		expected = bool(cint(frappe.db.get_single_value("Stock Settings", "allow_negative_stock")))
		data = device_api.get_bootstrap(device_id=self.device_id)
		self.assertIs(data["profile"]["allow_negative_stock"], expected)

	def test_capabilities_announce_shifts(self):
		self.assertTrue(sync_api.get_sync_capabilities()["features"]["shifts"])

	def test_device_of_another_user_is_rejected(self):
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
		frappe.set_user(email)
		try:
			with self.assertRaises(frappe.PermissionError):
				get_device_context(self.device_id)
		finally:
			frappe.set_user("Administrator")

	def test_unknown_device(self):
		with self.assertRaises(errors.DeviceNotRegisteredError):
			get_device_context(new_id())

	# push: sales

	def test_sale_submit(self):
		payload = self.sale_payload(lines=[(fixtures.MILK, "2", "0.600"), (fixtures.BREAD, "1", "0.500")])
		result = self.assertOk(self.push_one(self.event(payload)))

		self.assertEqual(result["doctype"], "Sales Invoice")
		self.assertEqual(result["docstatus"], 1)
		self.assertEqual(result["totals"]["grand_total"], "1.700")
		self.assertEqual(result["totals"]["outstanding_amount"], "0.000")

		doc = frappe.get_doc("Sales Invoice", result["name"])
		self.assertEqual(doc.is_pos, 1)
		self.assertEqual(doc.set_posting_time, 1)
		self.assertEqual(doc.update_stock, 1)
		self.assertEqual(doc.ignore_pricing_rule, 1)
		self.assertEqual(doc.pos_profile, self.profile.name)
		self.assertEqual(doc.crenya_local_id, payload["local_id"])
		self.assertEqual(doc.crenya_offline_number, payload["offline_number"])
		self.assertEqual(doc.crenya_device, self.device_id)
		self.assertEqual(str(doc.posting_date), payload["posting_date"])
		self.assertEqual(doc.taxes[0].account_head, fixtures.VAT_ACCOUNT)
		# VAT is inside the milk price only; bread is zero rated
		self.assertAlmostEqual(doc.total_taxes_and_charges, 0.057, places=3)
		self.assertAlmostEqual(doc.net_total, 1.643, places=3)
		self.assertEqual([row.warehouse for row in doc.items], [fixtures.WAREHOUSE] * 2)
		self.assertAlmostEqual(doc.items[0].rate, 0.6, places=3)

		event = frappe.get_doc(EVENT, result["event_id"])
		self.assertEqual(event.status, "ok")
		self.assertEqual(event.attempts, 1)
		self.assertEqual(event.result_name, doc.name)

	def test_duplicate_event_returns_stored_result(self):
		event = self.event(self.sale_payload())
		first = self.assertOk(self.push_one(event))
		second = self.push_one(event)
		self.assertEqual(second["status"], "duplicate")
		self.assertEqual(second["name"], first["name"])
		self.assertEqual(second["totals"], first["totals"])
		self.assertEqual(len(self.invoices_with_local_id(event["local_id"])), 1)

	def test_payload_conflict(self):
		payload = self.sale_payload()
		event = self.event(payload)
		self.assertOk(self.push_one(event))

		changed = dict(payload, remarks="edited after sync")
		conflict = self.event(changed, event_id=event["event_id"])
		self.assertError(self.push_one(conflict), "payload_conflict", False)
		self.assertEqual(frappe.db.get_value(EVENT, event["event_id"], "status"), "ok")

		tampered = self.event(self.sale_payload())
		tampered["payload"]["remarks"] = "changed without rehash"
		self.assertError(self.push_one(tampered), "payload_conflict", False)
		self.assertFalse(self.invoices_with_local_id(tampered["local_id"]))

	def test_lost_response_matches_by_local_id(self):
		payload = self.sale_payload()
		first = self.assertOk(self.push_one(self.event(payload)))

		# the till never saw the answer and re-queued the sale under a new event id
		retry = self.event(payload)
		result = self.push_one(retry)
		self.assertEqual(result["status"], "duplicate")
		self.assertEqual(result["name"], first["name"])
		self.assertEqual(len(self.invoices_with_local_id(payload["local_id"])), 1)
		recorded = frappe.get_doc(EVENT, retry["event_id"])
		self.assertEqual(recorded.status, "ok")
		self.assertEqual(recorded.result_name, first["name"])

	def test_customer_then_invoice_in_one_batch(self):
		customer = self.customer_payload()
		invoice = self.sale_payload(customer=None, customer_local_id=customer["local_id"])
		results = self.push(self.event(customer, "Customer"), self.event(invoice, sequence_no=2))
		self.assertOk(results[0])
		self.assertOk(results[1])

		customer_name = results[0]["name"]
		self.assertEqual(
			frappe.db.get_value("Customer", customer_name, "crenya_local_id"), customer["local_id"]
		)
		self.assertEqual(frappe.db.get_value("Sales Invoice", results[1]["name"], "customer"), customer_name)

		again = self.push_one(self.event(customer, "Customer"))
		self.assertEqual(again["status"], "duplicate")
		self.assertEqual(again["name"], customer_name)

	def test_dependency_missing_then_retry(self):
		customer = self.customer_payload("Late Customer")
		invoice_event = self.event(self.sale_payload(customer=None, customer_local_id=customer["local_id"]))

		self.assertError(self.push_one(invoice_event), "dependency_missing", True)
		event = frappe.get_doc(EVENT, invoice_event["event_id"])
		self.assertEqual((event.status, event.error_code, event.attempts), ("error", "dependency_missing", 1))

		self.assertOk(self.push_one(self.event(customer, "Customer")))
		self.assertOk(self.push_one(invoice_event))
		event.reload()
		self.assertEqual((event.status, event.attempts), ("ok", 2))
		self.assertIsNone(event.error_code)

	def test_total_mismatch(self):
		payload = self.sale_payload(grand_total="1.500")
		result = self.assertError(self.push_one(self.event(payload)), "total_mismatch", False)
		self.assertIn("1.200", result["error"]["message"])
		self.assertIn("1.500", result["error"]["message"])
		self.assertFalse(self.invoices_with_local_id(payload["local_id"]))

	def test_payment_difference_within_tolerance_is_absorbed(self):
		payload = self.sale_payload(payments=[{"mode_of_payment": fixtures.CASH, "amount": "1.195"}])
		event = self.event(payload)
		result = self.assertOk(self.push_one(event))
		self.assertEqual(result["totals"]["outstanding_amount"], "0.000")
		self.assertAlmostEqual(
			frappe.db.get_value("Sales Invoice", result["name"], "paid_amount"), 1.2, places=3
		)
		self.assertIn("absorbed", frappe.db.get_value(EVENT, event["event_id"], "note"))

		short = self.sale_payload(payments=[{"mode_of_payment": fixtures.CASH, "amount": "1.000"}])
		self.assertError(self.push_one(self.event(short)), "total_mismatch", False)

	def test_unknown_mode_of_payment_is_validation(self):
		payload = self.sale_payload(payments=[{"mode_of_payment": "_Test Crenya Voucher", "amount": "1.200"}])
		self.assertError(self.push_one(self.event(payload)), "validation", False)

	def test_partial_batch_keeps_good_events(self):
		good_a = self.event(self.sale_payload())
		bad = self.event(self.sale_payload(grand_total="9.999"))
		good_b = self.event(self.sale_payload(lines=[(fixtures.BREAD, "3", "0.500")]))
		results = self.push(good_a, bad, good_b)

		self.assertEqual([r["status"] for r in results], ["ok", "error", "ok"])
		self.assertEqual(
			[r["event_id"] for r in results], [good_a["event_id"], bad["event_id"], good_b["event_id"]]
		)
		self.assertEqual(len(self.invoices_with_local_id(good_a["local_id"])), 1)
		self.assertEqual(len(self.invoices_with_local_id(good_b["local_id"])), 1)
		self.assertFalse(self.invoices_with_local_id(bad["local_id"]))
		self.assertEqual(frappe.db.get_value(EVENT, bad["event_id"], "status"), "error")

	def test_invalid_payload_is_validation(self):
		payload = self.sale_payload()
		payload["items"][0]["qty"] = "two"
		self.assertError(self.push_one(self.event(payload)), "validation", False)

	# push: shifts

	def test_invoice_with_shift_local_id(self):
		shift_id = new_id()
		with_shift = self.assertOk(self.push_one(self.event(self.sale_payload(shift_local_id=shift_id))))
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", with_shift["name"], "crenya_shift_id"), shift_id
		)

		# tills without shift support omit the key entirely
		legacy = self.sale_payload()
		self.assertNotIn("shift_local_id", legacy)
		without_shift = self.assertOk(self.push_one(self.event(legacy)))
		self.assertFalse(frappe.db.get_value("Sales Invoice", without_shift["name"], "crenya_shift_id"))

	def test_invoice_records_cashier(self):
		payload = self.sale_payload()
		self.assertEqual(payload["cashier"], frappe.session.user)
		result = self.assertOk(self.push_one(self.event(payload)))
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", result["name"], "crenya_cashier"), frappe.session.user
		)
		self.assertEqual(frappe.db.get_value("Sales Invoice", result["name"], "owner"), frappe.session.user)

		# an unknown till cashier never blocks the sale: the field stays empty and the event says why
		ghost = "_test_crenya_no_such_cashier@example.com"
		event = self.event(self.sale_payload(cashier=ghost))
		result = self.assertOk(self.push_one(event))
		self.assertFalse(frappe.db.get_value("Sales Invoice", result["name"], "crenya_cashier"))
		self.assertIn(ghost, frappe.db.get_value(EVENT, event["event_id"], "note"))

	def test_shift_push_creates_record_and_counts_invoices(self):
		shift = self.shift_payload()
		sales = [self.sale_payload(shift_local_id=shift["local_id"]) for _ in range(2)]
		shift["invoice_local_ids"] = [sale["local_id"] for sale in sales]
		for sale in sales:
			self.assertOk(self.push_one(self.event(sale)))
		# an invoice of another shift must not be counted
		self.assertOk(self.push_one(self.event(self.sale_payload(shift_local_id=new_id()))))

		event = self.event(shift, SHIFT)
		result = self.assertOk(self.push_one(event))
		self.assertEqual(result["doctype"], SHIFT)
		self.assertEqual(result["name"], shift["local_id"])
		self.assertEqual(result["docstatus"], 0)
		self.assertIsNone(result["totals"])
		self.assertIsNone(result["fawtara_status"])
		self.assertTrue(result["modified"])

		doc = frappe.get_doc(SHIFT, result["name"])
		self.assertEqual(doc.local_id, shift["local_id"])
		self.assertEqual(doc.shift_number, shift["shift_number"])
		self.assertEqual(doc.device, self.device_id)
		self.assertEqual(doc.pos_profile, self.profile.name)
		self.assertEqual(doc.company, fixtures.COMPANY)
		self.assertEqual(doc.cashier, frappe.session.user)
		self.assertEqual(doc.invoice_count_on_server, 2)
		self.assertEqual((doc.sales_count, doc.returns_count), (2, 0))
		self.assertAlmostEqual(doc.opening_float, 20.0, places=3)
		self.assertAlmostEqual(doc.net_total, 2.4, places=3)
		self.assertAlmostEqual(doc.tax_total, 0.114, places=3)
		self.assertEqual(doc.notes, shift["notes"])

		time_zone = get_system_timezone()
		self.assertEqual(
			get_datetime(doc.opened_at), to_site_naive(parse_iso_utc(shift["opened_at"]), time_zone)
		)
		self.assertEqual(
			get_datetime(doc.closed_at), to_site_naive(parse_iso_utc(shift["closed_at"]), time_zone)
		)

		self.assertEqual(len(doc.payments), 1)
		payment = doc.payments[0]
		self.assertEqual(payment.mode_of_payment, fixtures.CASH)
		self.assertAlmostEqual(payment.expected, 22.4, places=3)
		self.assertAlmostEqual(payment.counted, 22.3, places=3)
		self.assertAlmostEqual(payment.difference, -0.1, places=3)

		recorded = frappe.get_doc(EVENT, event["event_id"])
		self.assertEqual((recorded.status, recorded.aggregate_type), ("ok", SHIFT))
		self.assertEqual((recorded.result_doctype, recorded.result_name), (SHIFT, doc.name))

	def test_shift_before_its_invoices(self):
		shift = self.shift_payload()
		result = self.assertOk(self.push_one(self.event(shift, SHIFT)))
		self.assertEqual(frappe.db.get_value(SHIFT, result["name"], "invoice_count_on_server"), 0)
		# the shift never blocks its invoices
		self.assertOk(self.push_one(self.event(self.sale_payload(shift_local_id=shift["local_id"]))))

	def test_duplicate_shift_event(self):
		shift = self.shift_payload()
		event = self.event(shift, SHIFT)
		first = self.assertOk(self.push_one(event))

		again = self.push_one(event)
		self.assertEqual(again["status"], "duplicate")
		self.assertEqual((again["doctype"], again["name"]), (SHIFT, first["name"]))
		self.assertEqual(again["docstatus"], 0)
		self.assertIsNone(again["totals"])

		# lost response: the till re-queued the shift under a new event id
		retry = self.event(shift, SHIFT)
		matched = self.push_one(retry)
		self.assertEqual(matched["status"], "duplicate")
		self.assertEqual(matched["name"], first["name"])
		self.assertEqual(frappe.db.get_value(EVENT, retry["event_id"], "status"), "ok")
		self.assertEqual(frappe.db.count(SHIFT, {"local_id": shift["local_id"]}), 1)

		conflict = self.event(dict(shift, notes="edited after sync"), SHIFT, event_id=event["event_id"])
		self.assertError(self.push_one(conflict), "payload_conflict", False)

	def test_invalid_shift_is_validation(self):
		bad_time = self.shift_payload(closed_at="2026-09-29 12:05:30")
		self.assertError(self.push_one(self.event(bad_time, SHIFT)), "validation", False)
		self.assertFalse(frappe.db.exists(SHIFT, bad_time["local_id"]))

		other_company = self.shift_payload(company="_Test Crenya Other Co")
		self.assertError(self.push_one(self.event(other_company, SHIFT)), "validation", False)

		unknown_mode = self.shift_payload(
			payments=[{"mode_of_payment": "_Test Crenya Voucher", "expected": "1", "counted": "1"}]
		)
		self.assertError(self.push_one(self.event(unknown_mode, SHIFT)), "validation", False)
		self.assertFalse(frappe.db.exists(SHIFT, unknown_mode["local_id"]))

	# push: returns

	def test_return_against_local_id(self):
		sale = self.sale_payload(lines=[(fixtures.MILK, "3", "0.600")])
		sold = self.assertOk(self.push_one(self.event(sale)))

		ret = self.return_payload(sale, [(fixtures.MILK, "-1", "0.600", 1)])
		result = self.assertOk(self.push_one(self.event(ret)))
		self.assertEqual(result["totals"]["grand_total"], "-0.600")

		doc = frappe.get_doc("Sales Invoice", result["name"])
		original = frappe.get_doc("Sales Invoice", sold["name"])
		self.assertEqual(doc.is_return, 1)
		self.assertEqual(doc.return_against, sold["name"])
		self.assertEqual(doc.items[0].sales_invoice_item, original.items[0].name)
		self.assertLess(doc.items[0].qty, 0)
		self.assertLess(doc.payments[0].amount, 0)

		lookup = returns_api.get_invoice_for_return(device_id=self.device_id, invoice=sold["name"])
		self.assertEqual(lookup["crenya_local_id"], sale["local_id"])
		self.assertEqual(lookup["items"][0]["row_name"], original.items[0].name)
		self.assertEqual(lookup["items"][0]["returned_qty"], "1")
		self.assertEqual(lookup["items"][0]["returnable_qty"], "2")
		self.assertEqual(lookup["taxes_and_charges"], fixtures.TAX_TEMPLATE)
		self.assertEqual(lookup["loyalty_amount"], "0.000")

		by_offline_number = returns_api.get_invoice_for_return(
			device_id=self.device_id, invoice=sale["offline_number"]
		)
		self.assertEqual(by_offline_number["name"], sold["name"])

	def test_return_against_row_name(self):
		sale = self.sale_payload(lines=[(fixtures.MILK, "1", "0.600"), (fixtures.MILK, "2", "0.600")])
		sold = self.assertOk(self.push_one(self.event(sale)))
		second_row = frappe.get_doc("Sales Invoice", sold["name"]).items[1].name

		ret = self.return_payload(sale, [(fixtures.MILK, "-2", "0.600", None)])
		ret["items"][0]["against_row_name"] = second_row
		result = self.assertOk(self.push_one(self.event(ret)))
		self.assertEqual(
			frappe.get_doc("Sales Invoice", result["name"]).items[0].sales_invoice_item, second_row
		)

	def test_over_return_is_validation(self):
		sale = self.sale_payload(lines=[(fixtures.MILK, "1", "0.600")])
		self.assertOk(self.push_one(self.event(sale)))

		ret = self.return_payload(sale, [(fixtures.MILK, "-2", "0.600", 1)])
		self.assertError(self.push_one(self.event(ret)), "validation", False)
		self.assertFalse(self.invoices_with_local_id(ret["local_id"]))

	def test_return_against_unknown_local_id_is_dependency_missing(self):
		ghost = {"local_id": new_id()}
		ret = self.return_payload(ghost, [(fixtures.MILK, "-1", "0.600", 1)])
		self.assertError(self.push_one(self.event(ret)), "dependency_missing", True)

	# counter features: tax templates

	def test_bootstrap_tax_templates_loyalty_and_phone_code(self):
		data = device_api.get_bootstrap(device_id=self.device_id)
		templates = {row["name"]: row for row in data["profile"]["tax_templates"]}
		self.assertIn(fixtures.TAX_TEMPLATE, templates)
		self.assertIn(fixtures.EXCLUSIVE_TEMPLATE, templates)
		self.assertIn(fixtures.ZERO_SALES_TEMPLATE, templates)
		self.assertNotIn(fixtures.ACTUAL_TEMPLATE, templates, "Actual charges cannot be computed offline")

		self.assertEqual(data["profile"]["tax_templates"][0]["name"], fixtures.TAX_TEMPLATE)
		self.assertEqual(
			[name for name, row in templates.items() if row["is_default"]], [fixtures.TAX_TEMPLATE]
		)
		exclusive = templates[fixtures.EXCLUSIVE_TEMPLATE]
		self.assertEqual(exclusive["title"], fixtures.EXCLUSIVE_TEMPLATE_TITLE)
		self.assertEqual(
			exclusive["taxes"],
			[
				{
					"account_head": fixtures.VAT_ACCOUNT,
					"description": "VAT 5%",
					"rate": "5",
					"included_in_print_rate": False,
				}
			],
		)
		self.assertEqual(templates[fixtures.ZERO_SALES_TEMPLATE]["taxes"][0]["rate"], "0")

		self.assertIs(data["profile"]["loyalty_enabled"], True)
		from frappe.geo.country_info import get_country_info

		country = frappe.db.get_value("Company", fixtures.COMPANY, "country")
		self.assertEqual(data["company"]["phone_country_code"], get_country_info(country).get("isd"))

	def test_invoice_with_alternate_tax_template(self):
		payload = self.sale_payload(
			taxes_and_charges=fixtures.EXCLUSIVE_TEMPLATE,
			grand_total="1.260",
			payments=[{"mode_of_payment": fixtures.CASH, "amount": "1.260"}],
		)
		event = self.event(payload)
		result = self.assertOk(self.push_one(event))
		self.assertEqual(result["totals"]["grand_total"], "1.260")
		self.assertEqual(result["totals"]["outstanding_amount"], "0.000")

		doc = frappe.get_doc("Sales Invoice", result["name"])
		self.assertEqual(doc.taxes_and_charges, fixtures.EXCLUSIVE_TEMPLATE)
		self.assertEqual(len(doc.taxes), 1)
		self.assertEqual(cint(doc.taxes[0].included_in_print_rate), 0)
		self.assertAlmostEqual(doc.net_total, 1.2, places=3)
		self.assertAlmostEqual(doc.total_taxes_and_charges, 0.06, places=3)
		self.assertFalse(frappe.db.get_value(EVENT, event["event_id"], "note"))
		lookup = returns_api.get_invoice_for_return(device_id=self.device_id, invoice=doc.name)
		self.assertEqual(lookup["taxes_and_charges"], fixtures.EXCLUSIVE_TEMPLATE)

		zero = self.assertOk(
			self.push_one(self.event(self.sale_payload(taxes_and_charges=fixtures.ZERO_SALES_TEMPLATE)))
		)
		zero_doc = frappe.get_doc("Sales Invoice", zero["name"])
		self.assertEqual(zero_doc.taxes_and_charges, fixtures.ZERO_SALES_TEMPLATE)
		self.assertAlmostEqual(zero_doc.total_taxes_and_charges, 0, places=3)

		# omitted / null -> the profile's template
		default = self.assertOk(self.push_one(self.event(self.sale_payload(taxes_and_charges=None))))
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", default["name"], "taxes_and_charges"), fixtures.TAX_TEMPLATE
		)

	def test_disallowed_tax_template_is_validation(self):
		for template in (fixtures.ACTUAL_TEMPLATE, "_Test Crenya No Such Template"):
			payload = self.sale_payload(taxes_and_charges=template)
			result = self.assertError(self.push_one(self.event(payload)), "validation", False)
			self.assertIn(template, result["error"]["message"])
			self.assertFalse(self.invoices_with_local_id(payload["local_id"]))

	# counter features: loyalty

	def loyal_customer(self, earn_qty=50):
		"""A new enrolled customer who earned `earn_qty` x 0.600 OMR worth of points at the till."""
		customer = fixtures.make_customer(
			f"_Test Crenya Loyal {uuid.uuid4().hex[:8]}", loyalty_program=fixtures.LOYALTY_PROGRAM
		)
		frappe.db.commit()
		if earn_qty:
			sale = self.sale_payload(lines=[(fixtures.MILK, str(earn_qty), "0.600")], customer=customer)
			result = self.assertOk(self.push_one(self.event(sale)))
			self.assertEqual(
				frappe.db.get_value("Sales Invoice", result["name"], "loyalty_program"),
				fixtures.LOYALTY_PROGRAM,
			)
		return customer

	def loyalty_details(self, customer):
		return loyalty_api.get_details(device_id=self.device_id, customer=customer)

	def test_loyalty_details(self):
		customer = self.loyal_customer(earn_qty=50)
		details = self.loyalty_details(customer)
		self.assertEqual(details["customer"], customer)
		self.assertEqual(details["loyalty_program"], fixtures.LOYALTY_PROGRAM)
		# 50 x 0.600 = 30.000 OMR at 1 point per OMR
		self.assertEqual(details["loyalty_points"], 30)
		self.assertEqual(details["conversion_factor"], "0.01")
		self.assertEqual(details["max_redeemable_amount"], "0.300")
		self.assertEqual(details["currency"], fixtures.CURRENCY)

		none = self.loyalty_details(self.walk_in)
		self.assertIsNone(none["loyalty_program"])
		self.assertEqual((none["loyalty_points"], none["max_redeemable_amount"]), (0, "0.000"))

		with self.assertRaises(frappe.ValidationError):
			self.loyalty_details("_Test Crenya No Such Customer")

	def test_loyalty_redemption(self):
		customer = self.loyal_customer(earn_qty=50)
		payload = self.sale_payload(
			customer=customer,
			payments=[{"mode_of_payment": fixtures.CASH, "amount": "1.000"}],
			loyalty={"points": 20, "amount": "0.200"},
		)
		event = self.event(payload)
		result = self.assertOk(self.push_one(event))
		self.assertEqual(result["totals"]["grand_total"], "1.200")
		self.assertEqual(result["totals"]["outstanding_amount"], "0.000")

		doc = frappe.get_doc("Sales Invoice", result["name"])
		self.assertEqual(doc.redeem_loyalty_points, 1)
		self.assertEqual(doc.loyalty_points, 20)
		self.assertEqual(doc.loyalty_program, fixtures.LOYALTY_PROGRAM)
		self.assertAlmostEqual(doc.loyalty_amount, 0.2, places=3)
		self.assertEqual(doc.loyalty_redemption_account, fixtures.LOYALTY_ACCOUNT)
		self.assertAlmostEqual(sum(row.amount for row in doc.payments), 1.0, places=3)
		self.assertAlmostEqual(doc.paid_amount, 1.2, places=3)
		self.assertFalse(frappe.db.get_value(EVENT, event["event_id"], "note"))

		redeemed = frappe.get_all(
			"Loyalty Point Entry",
			filters={"invoice": doc.name, "loyalty_points": ["<", 0]},
			pluck="loyalty_points",
		)
		self.assertEqual(sum(redeemed), -20)
		# 30 earned - 20 redeemed + 1 earned on the 1.200 sale
		self.assertEqual(self.loyalty_details(customer)["loyalty_points"], 11)

		# a till may not return an invoice partly paid with points
		lookup = returns_api.get_invoice_for_return(device_id=self.device_id, invoice=doc.name)
		self.assertEqual(lookup["loyalty_amount"], "0.200")
		self.assertEqual(lookup["taxes_and_charges"], fixtures.TAX_TEMPLATE)
		for reference in (
			{"return_against_local_id": payload["local_id"]},
			{"return_against": doc.name, "return_against_local_id": None},
		):
			ret = self.return_payload(payload, [(fixtures.MILK, "-1", "0.600", 1)], **reference)
			result = self.assertError(self.push_one(self.event(ret)), "validation", False)
			self.assertEqual(
				result["error"]["message"],
				"Return this invoice from ERPNext: it was partly paid with loyalty points",
			)
			self.assertFalse(self.invoices_with_local_id(ret["local_id"]))
		self.assertEqual(self.loyalty_details(customer)["loyalty_points"], 11)

	def test_loyalty_pays_whole_invoice(self):
		customer = self.loyal_customer(earn_qty=100)
		payload = self.sale_payload(
			lines=[(fixtures.BREAD, "1", "0.500")],
			customer=customer,
			payments=[],
			loyalty={"points": 50, "amount": "0.500"},
		)
		result = self.assertOk(self.push_one(self.event(payload)))
		self.assertEqual(result["totals"]["outstanding_amount"], "0.000")
		doc = frappe.get_doc("Sales Invoice", result["name"])
		# the zero payment row satisfies ERPNext's POS check; ERPNext clears it on submit
		self.assertFalse([row for row in doc.payments if row.amount])
		self.assertAlmostEqual(doc.loyalty_amount, 0.5, places=3)
		self.assertAlmostEqual(doc.paid_amount, 0.5, places=3)
		self.assertEqual(doc.loyalty_points, 50)

	def test_loyalty_over_redemption_is_validation(self):
		customer = self.loyal_customer(earn_qty=50)
		payload = self.sale_payload(
			lines=[(fixtures.MILK, "100", "0.600")],
			customer=customer,
			payments=[{"mode_of_payment": fixtures.CASH, "amount": "50.000"}],
			loyalty={"points": 1000, "amount": "10.000"},
		)
		self.assertError(self.push_one(self.event(payload)), "validation", False)
		self.assertFalse(self.invoices_with_local_id(payload["local_id"]))
		self.assertEqual(self.loyalty_details(customer)["loyalty_points"], 30)

	def points_balance(self, customer):
		from erpnext.accounts.doctype.loyalty_program.loyalty_program import (
			get_loyalty_program_details_with_points,
		)

		details = get_loyalty_program_details_with_points(
			customer, loyalty_program=fixtures.LOYALTY_PROGRAM, company=fixtures.COMPANY
		)
		return cint(details.loyalty_points)

	def test_return_rebooks_earned_points(self):
		customer = fixtures.make_customer(
			f"_Test Crenya Loyal {uuid.uuid4().hex[:8]}", loyalty_program=fixtures.LOYALTY_PROGRAM
		)
		frappe.db.commit()
		# 50 x 0.600 = 30.000 OMR -> 30 points
		sale = self.sale_payload(lines=[(fixtures.MILK, "50", "0.600")], customer=customer)
		self.assertOk(self.push_one(self.event(sale)))
		before = self.points_balance(customer)
		self.assertEqual(before, 30)

		# return 20 x 0.600 = 12.000 OMR -> the sale now earns 18 points
		ret = self.return_payload(sale, [(fixtures.MILK, "-20", "0.600", 1)])
		result = self.assertOk(self.push_one(self.event(ret)))
		doc = frappe.get_doc("Sales Invoice", result["name"])
		self.assertEqual(doc.loyalty_program, fixtures.LOYALTY_PROGRAM)
		self.assertEqual(cint(doc.redeem_loyalty_points), 0)
		self.assertFalse(doc.loyalty_points)

		after = self.points_balance(customer)
		self.assertEqual(after, 18)
		self.assertEqual(before - after, 12)
		self.assertEqual(self.loyalty_details(customer)["loyalty_points"], 18)

	def test_loyalty_rejections(self):
		walk_in = self.sale_payload(
			payments=[{"mode_of_payment": fixtures.CASH, "amount": "1.100"}],
			loyalty={"points": 10, "amount": "0.100"},
		)
		result = self.assertError(self.push_one(self.event(walk_in)), "validation", False)
		self.assertIn("default customer", result["error"]["message"])

		not_enrolled = fixtures.make_customer(f"_Test Crenya Not Enrolled {uuid.uuid4().hex[:8]}")
		frappe.db.commit()
		payload = self.sale_payload(
			customer=not_enrolled,
			payments=[{"mode_of_payment": fixtures.CASH, "amount": "1.100"}],
			loyalty={"points": 10, "amount": "0.100"},
		)
		result = self.assertError(self.push_one(self.event(payload)), "validation", False)
		self.assertIn("Customer has no loyalty program", result["error"]["message"])

		customer = self.loyal_customer(earn_qty=50)
		wrong_amount = self.sale_payload(
			customer=customer,
			payments=[{"mode_of_payment": fixtures.CASH, "amount": "0.700"}],
			loyalty={"points": 10, "amount": "0.500"},
		)
		self.assertError(self.push_one(self.event(wrong_amount)), "validation", False)

		# payments must cover rounded_total - loyalty_amount, not the full total
		overpaid = self.sale_payload(
			customer=customer,
			payments=[{"mode_of_payment": fixtures.CASH, "amount": "1.200"}],
			loyalty={"points": 20, "amount": "0.200"},
		)
		self.assertError(self.push_one(self.event(overpaid)), "total_mismatch", False)

		sale = self.sale_payload(customer=customer, lines=[(fixtures.MILK, "2", "0.600")])
		self.assertOk(self.push_one(self.event(sale)))
		ret = self.return_payload(
			sale, [(fixtures.MILK, "-1", "0.600", 1)], loyalty={"points": 1, "amount": "0.010"}
		)
		self.assertError(self.push_one(self.event(ret)), "validation", False)

	# pull

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

	def test_pull_changes_keyset_paging_with_ties(self):
		tie = "2001-01-01 00:00:00.000000"
		codes = [f"_TC-TIE-{uuid.uuid4().hex[:8]}-{i}" for i in range(5)]
		for code in codes:
			fixtures.ensure_item(code, 1.0, is_stock_item=0)
			frappe.db.set_value("Item", code, "modified", tie, update_modified=False)
		outside = f"_TC-OUT-{uuid.uuid4().hex[:8]}"
		fixtures.ensure_item(outside, 1.0, item_group=fixtures.ITEM_GROUP_OUTSIDE, is_stock_item=0)
		frappe.db.commit()

		records, cursor, pages = self._pull_all("item", limit=2)
		seen = [r["item_code"] for r in records]
		self.assertEqual(len(seen), len(set(seen)), "a record was delivered twice")
		self.assertGreaterEqual(pages, 3)
		tied = [code for code in seen if code in codes]
		self.assertEqual(tied, sorted(codes), "ties must be ordered by name and none skipped")
		self.assertNotIn(outside, seen, "item group outside the profile must be filtered")
		self.assertIn(fixtures.MILK, seen, "descendant item groups must be included")

		milk = next(r for r in records if r["item_code"] == fixtures.MILK)
		self.assertEqual(milk["barcodes"][0]["barcode"], f"{fixtures.MILK}-EAN")
		self.assertEqual(milk["uoms"][0]["conversion_factor"], "1")
		bread = next(r for r in records if r["item_code"] == fixtures.BREAD)
		self.assertEqual(bread["item_tax_template"], fixtures.ZERO_TEMPLATE)

		# the modified order must be non-decreasing across pages
		modified = [r["modified"] for r in records]
		self.assertEqual(modified, sorted(modified))

		# nothing new: an empty page that keeps the position
		page = sync_api.pull_changes(device_id=self.device_id, entity="item", cursor=cursor, limit=2)
		self.assertEqual(page["records"], [])
		self.assertFalse(page["has_more"])

		frappe.delete_doc("Item", codes[0], force=True)
		frappe.db.commit()
		page = sync_api.pull_changes(device_id=self.device_id, entity="item", cursor=cursor, limit=2)
		self.assertIn(codes[0], page["tombstones"])
		self.assertEqual(
			decode_cursor(page["next_cursor"], "item").modified, decode_cursor(cursor, "item").modified
		)

	def test_pull_other_entities(self):
		prices, _cursor, _pages = self._pull_all("item_price", limit=1000)
		milk_price = [p for p in prices if p["item_code"] == fixtures.MILK]
		self.assertEqual(milk_price[0]["price_list_rate"], "0.600")

		stock, _cursor, _pages = self._pull_all("stock", limit=1000)
		self.assertTrue(all(row["warehouse"] == fixtures.WAREHOUSE for row in stock))

		customers, _cursor, _pages = self._pull_all("customer", limit=1000)
		self.assertIn(self.walk_in, [c["name"] for c in customers])

	def test_pull_rejects_bad_cursor_and_entity(self):
		with self.assertRaises(frappe.ValidationError):
			sync_api.pull_changes(device_id=self.device_id, entity="item", cursor="garbage!!")
		with self.assertRaises(frappe.ValidationError):
			sync_api.pull_changes(device_id=self.device_id, entity="sales_order")
		item_cursor = sync_api.pull_changes(device_id=self.device_id, entity="item", limit=1)["next_cursor"]
		with self.assertRaises(frappe.ValidationError):
			sync_api.pull_changes(device_id=self.device_id, entity="customer", cursor=item_cursor)
