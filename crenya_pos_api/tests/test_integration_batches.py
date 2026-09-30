"""Pharmacy batches against a real ERPNext site: the `batch` feed, till sales naming
their batch, and returns going back into the original batches.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_batches
push_batch commits per event, so documents created here persist (names start with _TC).
"""

import uuid
from decimal import Decimal

import frappe
from frappe.utils import add_days, getdate, nowdate, nowtime

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import returns as returns_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.sync.batches import _qty_step, batch_quantities, split_return_qty
from crenya_pos_api.sync.hashing import payload_hash
from crenya_pos_api.tests import fixtures

STOCK_SETTINGS = "Stock Settings"
AUTO_OUTWARD = "auto_create_serial_and_batch_bundle_for_outward"


def new_id():
	return str(uuid.uuid4())


def money(value):
	return f"{Decimal(value).quantize(Decimal('0.001')):f}"


def stock(batch_no):
	return batch_quantities([batch_no], fixtures.WAREHOUSE).get(batch_no, 0.0)


def bundle_batches(bundle):
	rows = frappe.get_all(
		"Serial and Batch Entry", filters={"parent": bundle}, fields=["batch_no", "qty"], order_by="idx"
	)
	return {row.batch_no: row.qty for row in rows}


class TestCrenyaBatches(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

		cls._saved_precision = frappe.db.get_single_value("System Settings", "currency_precision")
		frappe.db.set_single_value("System Settings", "currency_precision", "3")
		frappe.db.set_default("currency_precision", "3")
		cls._saved_auto_outward = frappe.db.get_single_value(STOCK_SETTINGS, AUTO_OUTWARD)
		# tills without batch support rely on ERPNext picking the batch (the default)
		frappe.db.set_single_value(STOCK_SETTINGS, AUTO_OUTWARD, 1)
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
			device_id=cls.device_id, device_name="Pharmacy Till", pos_profile=cls.profile.name
		)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.db.set_single_value("System Settings", "currency_precision", cls._saved_precision or "")
		frappe.db.set_default("currency_precision", cls._saved_precision or "")
		frappe.db.set_single_value(STOCK_SETTINGS, AUTO_OUTWARD, cls._saved_auto_outward)
		for key, value in cls._saved_conf.items():
			if value is None:
				frappe.conf.pop(key, None)
			else:
				frappe.conf[key] = value
		frappe.db.commit()
		frappe.clear_cache()
		super().tearDownClass()

	# helpers

	def line(self, line_no, qty, rate="1.000", item_code=fixtures.BATCH_ITEM, batch_no=None, against=None):
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
			"against_line_no": against,
			"against_row_name": None,
		}
		if batch_no:
			values["batch_no"] = batch_no
		return values

	def payload(self, items, is_return=0, **overrides):
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

	def pull(self, entity, cursor=None, limit=100):
		records, tombstones, pages = {}, [], 0
		while True:
			page = sync_api.pull_changes(device_id=self.device_id, entity=entity, cursor=cursor, limit=limit)
			pages += 1
			for record in page["records"]:
				records[record.get("name") or record.get("item_code")] = record
			tombstones.extend(page["tombstones"])
			cursor = page["next_cursor"]
			if not page["has_more"]:
				return records, tombstones, cursor
			self.assertLess(pages, 500, "pull did not terminate")

	def desk_invoice(self, batches, rate=1.0):
		"""A POS Sales Invoice made in ERPNext whose single row consumed several batches."""
		from erpnext.stock.serial_batch_bundle import SerialBatchCreation

		qty = sum(batches.values())
		bundle = SerialBatchCreation(
			{
				"item_code": fixtures.BATCH_ITEM,
				"warehouse": fixtures.WAREHOUSE,
				"voucher_type": "Sales Invoice",
				"qty": qty,
				"batches": frappe._dict(batches),
				"type_of_transaction": "Outward",
				"company": fixtures.COMPANY,
				"posting_datetime": frappe.utils.get_datetime(f"{nowdate()} {nowtime()}"),
				"do_not_submit": True,
			}
		).make_serial_and_batch_bundle()

		doc = frappe.new_doc("Sales Invoice")
		doc.update(
			{
				"company": fixtures.COMPANY,
				"customer": self.walk_in,
				"is_pos": 1,
				"pos_profile": self.profile.name,
				"update_stock": 1,
				"currency": fixtures.CURRENCY,
				"selling_price_list": fixtures.PRICE_LIST,
				"set_warehouse": fixtures.WAREHOUSE,
				"ignore_pricing_rule": 1,
				"taxes_and_charges": fixtures.TAX_TEMPLATE,
			}
		)
		doc.append(
			"items",
			{
				"item_code": fixtures.BATCH_ITEM,
				"qty": qty,
				"rate": rate,
				"price_list_rate": rate,
				"warehouse": fixtures.WAREHOUSE,
				"cost_center": fixtures.COST_CENTER,
				"serial_and_batch_bundle": bundle.name,
			},
		)
		doc.set_missing_values()
		doc.ignore_pricing_rule = 1
		if not doc.get("taxes"):
			from erpnext.controllers.accounts_controller import get_taxes_and_charges

			for tax in get_taxes_and_charges("Sales Taxes and Charges Template", doc.taxes_and_charges):
				doc.append("taxes", tax)
		doc.calculate_taxes_and_totals()
		if not doc.get("payments"):
			doc.append("payments", {"mode_of_payment": fixtures.CASH, "default": 1})
		payment = doc.payments[0]
		if not payment.account:
			from erpnext.accounts.doctype.sales_invoice.sales_invoice import get_bank_cash_account

			payment.account = get_bank_cash_account(payment.mode_of_payment, fixtures.COMPANY).get("account")
		payment.amount = doc.grand_total
		doc.insert()
		doc.submit()
		frappe.db.commit()
		return doc

	# item records and the batch feed

	def test_item_records_carry_batch_flags(self):
		items, _tombstones, _cursor = self.pull("item", limit=500)
		self.assertEqual(items[fixtures.BATCH_ITEM]["has_batch_no"], 1)
		self.assertEqual(items[fixtures.BATCH_ITEM]["has_expiry_date"], 1)
		self.assertEqual(items[fixtures.MILK]["has_batch_no"], 0)
		self.assertEqual(items[fixtures.MILK]["has_expiry_date"], 0)

	def test_batch_feed_with_quantities(self):
		records, _tombstones, _cursor = self.pull("batch", limit=2)
		for batch_no in (fixtures.BATCH_ONE, fixtures.BATCH_TWO, fixtures.BATCH_EXPIRED):
			record = records[batch_no]
			self.assertEqual(record["item_code"], fixtures.BATCH_ITEM)
			self.assertEqual(Decimal(record["qty"]), Decimal(str(fixtures.batch_stock(batch_no))), batch_no)
			self.assertEqual(record["disabled"], 0)
			self.assertEqual(
				record["expiry_date"], str(frappe.db.get_value("Batch", batch_no, "expiry_date"))
			)
		# expired batches are sent with their stock: the till judges them by date
		expired = records[fixtures.BATCH_EXPIRED]
		self.assertLess(getdate(expired["expiry_date"]), getdate(nowdate()))
		self.assertGreater(Decimal(expired["qty"]), 0)
		self.assertTrue(all(r["item_code"] != fixtures.MILK for r in records.values()))

	def test_sale_resends_batch_with_fresh_qty(self):
		records, _tombstones, cursor = self.pull("batch")
		before = Decimal(records[fixtures.BATCH_ONE]["qty"])
		batch_modified = frappe.db.get_value("Batch", fixtures.BATCH_TWO, "modified")

		sale = self.payload([self.line(1, "2", batch_no=fixtures.BATCH_TWO)])
		self.assertOk(self.push(sale))
		# only the stock moved: the Batch itself looks unchanged
		frappe.db.set_value("Batch", fixtures.BATCH_TWO, "modified", batch_modified, update_modified=False)
		frappe.db.commit()

		records, _tombstones, cursor = self.pull("batch", cursor)
		self.assertIn(fixtures.BATCH_TWO, records)
		self.assertEqual(Decimal(records[fixtures.BATCH_TWO]["qty"]), Decimal(str(stock(fixtures.BATCH_TWO))))
		self.assertNotIn(fixtures.BATCH_ONE, records)
		self.assertEqual(Decimal(str(stock(fixtures.BATCH_ONE))), before)

		# nothing moved since: an empty page
		page = sync_api.pull_changes(device_id=self.device_id, entity="batch", cursor=cursor)
		self.assertEqual(page["records"], [])
		self.assertFalse(page["has_more"])

	def test_deleted_batch_is_a_tombstone(self):
		name = f"_TC-PARA-TMP-{uuid.uuid4().hex[:8]}"
		_records, _tombstones, cursor = self.pull("batch")
		fixtures.ensure_batch(name, fixtures.BATCH_ITEM, add_days(nowdate(), 90))
		frappe.db.commit()
		records, _tombstones, cursor = self.pull("batch", cursor)
		self.assertEqual(records[name]["qty"], "0")

		frappe.delete_doc("Batch", name, force=True)
		frappe.db.commit()
		records, tombstones, _cursor = self.pull("batch", cursor)
		self.assertIn(name, tombstones)
		self.assertNotIn(name, records)

	# sales

	def test_sale_consumes_the_named_batches(self):
		one, two = stock(fixtures.BATCH_ONE), stock(fixtures.BATCH_TWO)
		sale = self.payload(
			[
				self.line(1, "2", batch_no=fixtures.BATCH_TWO),
				self.line(2, "1", batch_no=fixtures.BATCH_ONE),
			]
		)
		doc = self.assertOk(self.push(sale))
		self.assertEqual([row.batch_no for row in doc.items], [fixtures.BATCH_TWO, fixtures.BATCH_ONE])
		self.assertEqual([row.use_serial_batch_fields for row in doc.items], [1, 1])
		self.assertEqual(bundle_batches(doc.items[0].serial_and_batch_bundle), {fixtures.BATCH_TWO: -2})
		self.assertEqual(bundle_batches(doc.items[1].serial_and_batch_bundle), {fixtures.BATCH_ONE: -1})
		self.assertAlmostEqual(stock(fixtures.BATCH_TWO), two - 2)
		self.assertAlmostEqual(stock(fixtures.BATCH_ONE), one - 1)

		lookup = returns_api.get_invoice_for_return(device_id=self.device_id, invoice=doc.name)
		self.assertEqual(lookup["items"][0]["batch_no"], fixtures.BATCH_TWO)
		self.assertEqual(
			lookup["items"][0]["expiry_date"],
			str(frappe.db.get_value("Batch", fixtures.BATCH_TWO, "expiry_date")),
		)
		self.assertEqual(lookup["items"][1]["batch_no"], fixtures.BATCH_ONE)

	def test_expired_batch_is_validation(self):
		before = stock(fixtures.BATCH_EXPIRED)
		sale = self.payload([self.line(1, "1", batch_no=fixtures.BATCH_EXPIRED)])
		message = self.assertValidation(self.push(sale), sale)
		self.assertIn(fixtures.BATCH_EXPIRED, message)
		self.assertEqual(stock(fixtures.BATCH_EXPIRED), before)

	def test_more_than_the_batch_holds_is_validation(self):
		held = stock(fixtures.BATCH_ONE)
		self.assertLess(held + 1, held + stock(fixtures.BATCH_TWO))
		sale = self.payload([self.line(1, str(int(held) + 1), batch_no=fixtures.BATCH_ONE)])
		self.assertValidation(self.push(sale), sale)
		self.assertEqual(stock(fixtures.BATCH_ONE), held)

	def test_batch_of_another_item_is_validation(self):
		sale = self.payload(
			[self.line(1, "1", rate="0.600", item_code=fixtures.MILK, batch_no=fixtures.BATCH_ONE)]
		)
		self.assertIn("batch", self.assertValidation(self.push(sale), sale))
		sale = self.payload([self.line(1, "1", batch_no="_TC-NO-SUCH-BATCH")])
		self.assertIn("does not exist", self.assertValidation(self.push(sale), sale))

	def test_older_payload_without_batch_is_auto_picked(self):
		sale = self.payload([self.line(1, "1")])
		doc = self.assertOk(self.push(sale))
		row = doc.items[0]
		self.assertFalse(row.use_serial_batch_fields)
		picked = bundle_batches(row.serial_and_batch_bundle)
		self.assertEqual(sum(picked.values()), -1)
		self.assertNotIn(fixtures.BATCH_EXPIRED, picked)
		self.assertTrue(set(picked) <= {fixtures.BATCH_ONE, fixtures.BATCH_TWO}, picked)

	# returns

	def test_return_goes_back_into_the_sold_batch(self):
		sale = self.payload([self.line(1, "3", batch_no=fixtures.BATCH_TWO)])
		self.assertOk(self.push(sale))
		before = stock(fixtures.BATCH_TWO)

		ret = self.payload(
			[self.line(1, "-2", against=1)], is_return=1, return_against_local_id=sale["local_id"]
		)
		doc = self.assertOk(self.push(ret))
		self.assertEqual(len(doc.items), 1)
		self.assertEqual(doc.items[0].batch_no, fixtures.BATCH_TWO)
		self.assertEqual(doc.items[0].use_serial_batch_fields, 1)
		self.assertEqual(bundle_batches(doc.items[0].serial_and_batch_bundle), {fixtures.BATCH_TWO: 2})
		self.assertAlmostEqual(stock(fixtures.BATCH_TWO), before + 2)

	def test_return_against_desk_row_of_two_batches_is_split(self):
		sold = {fixtures.BATCH_ONE: 3, fixtures.BATCH_TWO: 2}
		desk = self.desk_invoice(sold)
		self.assertEqual(
			bundle_batches(desk.items[0].serial_and_batch_bundle),
			{batch_no: -qty for batch_no, qty in sold.items()},
		)
		lookup = returns_api.get_invoice_for_return(device_id=self.device_id, invoice=desk.name)
		self.assertIsNone(lookup["items"][0]["batch_no"])
		self.assertIsNone(lookup["items"][0]["expiry_date"])

		step = _qty_step(desk.items[0].uom, 3)
		left = {batch_no: Decimal(qty) for batch_no, qty in sold.items()}
		for qty in ("3", "2"):
			expected = split_return_qty(Decimal(qty), [(b, Decimal(sold[b]), left[b]) for b in sold], step)
			before = {batch_no: stock(batch_no) for batch_no in sold}
			ret = self.payload([self.line(1, f"-{qty}", against=1)], is_return=1, return_against=desk.name)
			doc = self.assertOk(self.push(ret))
			self.assertEqual(
				[(row.batch_no, row.qty) for row in doc.items],
				[(batch_no, -float(part)) for batch_no, part in expected],
			)
			self.assertEqual({row.sales_invoice_item for row in doc.items}, {desk.items[0].name})
			self.assertTrue(all(row.use_serial_batch_fields for row in doc.items))
			for batch_no, part in expected:
				self.assertAlmostEqual(stock(batch_no), before[batch_no] + float(part))
				left[batch_no] -= part

		# everything is back: nothing more can be returned into these batches
		ret = self.payload([self.line(1, "-1", against=1)], is_return=1, return_against=desk.name)
		self.assertValidation(self.push(ret), ret)
