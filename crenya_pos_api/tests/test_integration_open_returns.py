"""Returns without an invoice against a real ERPNext site: the POS Profile flag, standalone
credit notes with stock coming back at valuation, and batched items going into a named
batch (an expired one included).

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_open_returns
push_batch commits per event, so documents created here persist (names start with _TC).
"""

import uuid
from decimal import Decimal

import frappe
from frappe.utils import flt, getdate, nowdate, nowtime

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.sync.batches import batch_quantities
from crenya_pos_api.sync.hashing import payload_hash
from crenya_pos_api.sync.open_returns import ALLOW_FIELD
from crenya_pos_api.tests import fixtures

REASON = "Customer returned an unopened pack"
# every receipt of the fixture items is valued at this rate (fixtures.ensure_stock / receive_batch)
VALUATION = 0.3
# a second batch tracked item, never in stock (its rows fail before ERPNext sees them)
OTHER_BATCH_ITEM = "_TC-IBU-200"


def new_id():
	return str(uuid.uuid4())


def money(value):
	return f"{Decimal(value).quantize(Decimal('0.001')):f}"


def batch_stock(batch_no):
	return batch_quantities([batch_no], fixtures.WAREHOUSE).get(batch_no, 0.0)


def bin_qty(item_code):
	return flt(
		frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": fixtures.WAREHOUSE}, "actual_qty")
	)


def bundle_batches(bundle):
	rows = frappe.get_all(
		"Serial and Batch Entry", filters={"parent": bundle}, fields=["batch_no", "qty"], order_by="idx"
	)
	return {row.batch_no: row.qty for row in rows}


def ledger_entries(voucher_no):
	return frappe.get_all(
		"Stock Ledger Entry",
		filters={"voucher_type": "Sales Invoice", "voucher_no": voucher_no, "is_cancelled": 0},
		fields=[
			"item_code",
			"actual_qty",
			"incoming_rate",
			"stock_value_difference",
			"serial_and_batch_bundle",
		],
		order_by="creation",
	)


class TestCrenyaOpenReturns(FrappeTestCase):
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
		fixtures.setup_batch_fixtures()
		cls.walk_in = cls.profile.customer
		cls.device_id = new_id()
		cls.device = device_api.register_device(
			device_id=cls.device_id, device_name="Open Return Till", pos_profile=cls.profile.name
		)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls.allow(False)
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

	def setUp(self):
		self.allow(True)

	# helpers

	@classmethod
	def allow(cls, value):
		frappe.db.set_value("POS Profile", cls.profile.name, ALLOW_FIELD, 1 if value else 0)
		frappe.db.commit()

	def line(self, line_no, qty, rate="0.600", item_code=fixtures.MILK, batch_no=None, **extra):
		values = {
			"line_no": line_no,
			"item_code": item_code,
			"item_name": item_code,
			"qty": qty,
			"uom": "Nos",
			"conversion_factor": "1",
			"price_list_rate": rate,
			"discount_percentage": "0",
			"rate": rate,
			"amount": money(Decimal(qty) * Decimal(rate)),
			"item_tax_template": None,
		}
		if batch_no:
			values["batch_no"] = batch_no
		values.update(extra)
		return values

	def payload(self, items, is_return=1, **overrides):
		# the profile's VAT is included in the rate: the grand total is the sum of the lines
		total = money(sum(Decimal(item["qty"]) * Decimal(item["rate"]) for item in items))
		payload = {
			"local_id": new_id(),
			"offline_number": f"OFF-{self.device['device_short']}-{uuid.uuid4().hex[:6]}",
			"pos_profile": self.profile.name,
			"company": fixtures.COMPANY,
			"customer": self.walk_in,
			"posting_date": nowdate(),
			"posting_time": nowtime()[:8],
			"currency": fixtures.CURRENCY,
			"is_return": is_return,
			"taxes_and_charges": fixtures.TAX_TEMPLATE,
			"cashier": frappe.session.user,
			"remarks": REASON if is_return else None,
			"items": items,
			"payments": [{"mode_of_payment": fixtures.CASH, "amount": total}],
			"client_totals": {"grand_total": total, "rounded_total": total},
		}
		payload.update(overrides)
		return payload

	def push(self, payload):
		event = {
			"event_id": new_id(),
			"aggregate_type": "Sales Invoice",
			"operation": "submit",
			"local_id": payload["local_id"],
			"sequence_no": 1,
			"payload_hash": payload_hash(payload),
			"payload": payload,
		}
		results = sync_api.push_batch(device_id=self.device_id, events=[event])
		self.assertEqual(len(results), 1)
		return results[0]

	def assertOk(self, result):
		self.assertEqual(result["status"], "ok", msg=result.get("error"))
		return frappe.get_doc("Sales Invoice", result["name"])

	def assertValidation(self, result, payload):
		self.assertEqual(result["status"], "error", msg=result)
		self.assertEqual(result["error"]["code"], "validation", msg=result["error"])
		self.assertFalse(frappe.get_all("Sales Invoice", filters={"crenya_local_id": payload["local_id"]}))
		return result["error"]["message"]

	def assertCreditNote(self, doc, payload):
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(doc.is_return, 1)
		self.assertEqual(doc.is_pos, 1)
		self.assertFalse(doc.return_against)
		self.assertEqual(doc.update_stock, 1)
		self.assertEqual(doc.remarks, payload["remarks"])
		self.assertEqual(doc.crenya_local_id, payload["local_id"])
		self.assertAlmostEqual(doc.grand_total, float(payload["client_totals"]["grand_total"]), places=3)
		self.assertTrue(doc.payments)
		self.assertTrue(all(row.amount < 0 for row in doc.payments), [row.amount for row in doc.payments])
		self.assertAlmostEqual(sum(row.amount for row in doc.payments), doc.grand_total, places=3)
		self.assertFalse(any(row.sales_invoice_item for row in doc.items))

	def assertValuedAtValuation(self, doc):
		"""Stock came back at the items' valuation, not at the selling rate."""
		for row in doc.items:
			self.assertAlmostEqual(row.incoming_rate, VALUATION, places=3, msg=row.item_code)
		entries = ledger_entries(doc.name)
		self.assertEqual(len(entries), len(doc.items))
		for entry in entries:
			self.assertGreater(entry.actual_qty, 0)
			self.assertAlmostEqual(entry.incoming_rate, VALUATION, places=3, msg=entry.item_code)
			self.assertAlmostEqual(
				entry.stock_value_difference, VALUATION * entry.actual_qty, places=3, msg=entry.item_code
			)
		return entries

	# bootstrap

	def test_bootstrap_carries_the_flag(self):
		data = device_api.get_bootstrap(device_id=self.device_id)
		self.assertIs(data["profile"]["allow_return_without_invoice"], True)
		self.allow(False)
		data = device_api.get_bootstrap(device_id=self.device_id)
		self.assertIs(data["profile"]["allow_return_without_invoice"], False)

	def test_capabilities_announce_open_returns(self):
		self.assertIs(sync_api.get_sync_capabilities()["features"]["open_returns"], True)

	def test_custom_field_defaults_off(self):
		field = frappe.get_meta("POS Profile").get_field(ALLOW_FIELD)
		self.assertEqual(field.fieldtype, "Check")
		self.assertEqual(str(field.default), "0")

	# the profile flag

	def test_flag_off_is_validation(self):
		self.allow(False)
		before = bin_qty(fixtures.MILK)
		ret = self.payload([self.line(1, "-1")])
		message = self.assertValidation(self.push(ret), ret)
		self.assertIn("does not allow returns without an invoice", message)
		self.assertEqual(bin_qty(fixtures.MILK), before)

		# the same event goes through once the profile allows it (checked at push time)
		self.allow(True)
		self.assertCreditNote(self.assertOk(self.push(ret)), ret)

	def test_return_against_an_invoice_does_not_need_the_flag(self):
		self.allow(False)
		sale = self.payload([self.line(1, "2")], is_return=0)
		sold = self.assertOk(self.push(sale))
		ret = self.payload(
			[self.line(1, "-1", against_line_no=1)], return_against_local_id=sale["local_id"], remarks=None
		)
		doc = self.assertOk(self.push(ret))
		self.assertEqual(doc.return_against, sold.name)
		self.assertEqual(doc.items[0].sales_invoice_item, sold.items[0].name)

	# credit notes

	def test_credit_note_brings_stock_back_at_valuation(self):
		before = bin_qty(fixtures.MILK)
		ret = self.payload([self.line(1, "-2"), self.line(2, "-1", rate="0.500", item_code=fixtures.BREAD)])
		result = self.push(ret)
		doc = self.assertOk(result)
		self.assertCreditNote(doc, ret)
		self.assertEqual(result["totals"]["grand_total"], ret["client_totals"]["grand_total"])
		self.assertEqual([row.qty for row in doc.items], [-2, -1])
		self.assertEqual([row.rate for row in doc.items], [0.6, 0.5])
		self.assertAlmostEqual(bin_qty(fixtures.MILK), before + 2)
		self.assertValuedAtValuation(doc)

	def test_empty_reason_is_validation(self):
		ret = self.payload([self.line(1, "-1")], remarks="  ")
		self.assertIn("reason", self.assertValidation(self.push(ret), ret))

	def test_rows_pointing_at_an_original_are_validation(self):
		ret = self.payload([self.line(1, "-1", against_line_no=1)])
		self.assertIn("original row", self.assertValidation(self.push(ret), ret))

	def test_positive_qty_is_validation(self):
		ret = self.payload([self.line(1, "1")])
		ret["payments"][0]["amount"] = "-0.600"
		self.assertIn("negative on a return", self.assertValidation(self.push(ret), ret))

	# batches

	def test_batched_item_goes_into_the_named_batch(self):
		before = batch_stock(fixtures.BATCH_ONE)
		other = batch_stock(fixtures.BATCH_TWO)
		ret = self.payload(
			[self.line(1, "-2", rate="1.000", item_code=fixtures.BATCH_ITEM, batch_no=fixtures.BATCH_ONE)]
		)
		doc = self.assertOk(self.push(ret))
		self.assertCreditNote(doc, ret)
		row = doc.items[0]
		self.assertEqual(row.batch_no, fixtures.BATCH_ONE)
		self.assertEqual(row.use_serial_batch_fields, 1)
		self.assertEqual(bundle_batches(row.serial_and_batch_bundle), {fixtures.BATCH_ONE: 2})
		self.assertAlmostEqual(batch_stock(fixtures.BATCH_ONE), before + 2)
		self.assertAlmostEqual(batch_stock(fixtures.BATCH_TWO), other)
		self.assertValuedAtValuation(doc)

	def test_expired_batch_takes_the_return(self):
		# ERPNext checks expiry on outward rows only: a credit note may put stock back into an
		# expired batch (it stays unsellable at the till and in ERPNext)
		expiry = frappe.db.get_value("Batch", fixtures.BATCH_EXPIRED, "expiry_date")
		self.assertLess(getdate(expiry), getdate(nowdate()))
		before = batch_stock(fixtures.BATCH_EXPIRED)
		ret = self.payload(
			[self.line(1, "-1", rate="1.000", item_code=fixtures.BATCH_ITEM, batch_no=fixtures.BATCH_EXPIRED)]
		)
		doc = self.assertOk(self.push(ret))
		self.assertCreditNote(doc, ret)
		self.assertEqual(doc.items[0].batch_no, fixtures.BATCH_EXPIRED)
		self.assertEqual(bundle_batches(doc.items[0].serial_and_batch_bundle), {fixtures.BATCH_EXPIRED: 1})
		self.assertAlmostEqual(batch_stock(fixtures.BATCH_EXPIRED), before + 1)
		self.assertValuedAtValuation(doc)

	def test_batched_item_without_batch_is_validation(self):
		before = batch_stock(fixtures.BATCH_ONE)
		ret = self.payload(
			[
				self.line(1, "-1"),
				self.line(2, "-1", rate="1.000", item_code=fixtures.BATCH_ITEM),
			]
		)
		message = self.assertValidation(self.push(ret), ret)
		self.assertIn(f"Line 2 ({fixtures.BATCH_ITEM}): batch_no is required", message)
		self.assertAlmostEqual(batch_stock(fixtures.BATCH_ONE), before)

	def test_batch_of_another_item_is_validation(self):
		fixtures.ensure_item(OTHER_BATCH_ITEM, 1.0, has_batch_no=1, has_expiry_date=1, create_new_batch=0)
		frappe.db.commit()
		ret = self.payload(
			[self.line(1, "-1", rate="1.000", item_code=OTHER_BATCH_ITEM, batch_no=fixtures.BATCH_ONE)]
		)
		message = self.assertValidation(self.push(ret), ret)
		self.assertIn(f"batch {fixtures.BATCH_ONE} belongs to item {fixtures.BATCH_ITEM}", message)
		# a batch on an item that is not batch tracked
		ret = self.payload([self.line(1, "-1", batch_no=fixtures.BATCH_ONE)])
		self.assertIn("not tracked by batch", self.assertValidation(self.push(ret), ret))
		ret = self.payload(
			[self.line(1, "-1", rate="1.000", item_code=fixtures.BATCH_ITEM, batch_no="_TC-NO-SUCH-BATCH")]
		)
		self.assertIn("does not exist", self.assertValidation(self.push(ret), ret))
