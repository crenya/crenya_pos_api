"""Locale data in bootstrap against a real ERPNext site: currency, dialling codes, cash denominations,
site time zone and the totals tolerance.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_locale
Denominations are labelled _Test Crenya … and removed again after each test.
"""

import uuid
from decimal import Decimal

import frappe
from frappe.geo.country_info import get_all as get_all_country_info
from frappe.utils import cint, flt, get_system_timezone

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.sync.context import get_total_tolerance
from crenya_pos_api.sync.locale_data import DENOMINATION_DOCTYPE
from crenya_pos_api.tests import fixtures

LABEL_PREFIX = "_Test Crenya"


def _remove_test_denominations():
	for name in frappe.get_all(
		DENOMINATION_DOCTYPE, filters={"label": ["like", f"{LABEL_PREFIX}%"]}, pluck="name"
	):
		frappe.delete_doc(DENOMINATION_DOCTYPE, name, force=True, ignore_permissions=True)


def _reset_request_cache():
	"""Tests run in one long "request": drop what `request_cache` kept from earlier bootstraps."""
	cache = getattr(frappe.local, "request_cache", None)
	if cache is not None:
		cache.clear()


def _denomination(currency, value, label, kind="note", enabled=1):
	return frappe.get_doc(
		{
			"doctype": DENOMINATION_DOCTYPE,
			"currency": currency,
			"value": value,
			"label": f"{LABEL_PREFIX} {label}",
			"kind": kind,
			"enabled": enabled,
		}
	).insert(ignore_permissions=True)


class TestCrenyaLocale(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

		cls._saved_precision = frappe.db.get_single_value("System Settings", "currency_precision")
		frappe.db.set_single_value("System Settings", "currency_precision", "3")
		frappe.db.set_default("currency_precision", "3")
		frappe.clear_cache()
		cls._saved_tolerance = frappe.conf.get("crenya_pos_total_tolerance")

		cls.profile = fixtures.setup_fixtures()
		cls.device_id = str(uuid.uuid4())
		device_api.register_device(
			device_id=cls.device_id, device_name="Locale Till", pos_profile=cls.profile.name
		)
		cls.other_currency = frappe.get_all(
			"Currency", filters={"name": ["!=", fixtures.CURRENCY]}, pluck="name", limit=1
		)[0]
		_remove_test_denominations()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		_remove_test_denominations()
		frappe.db.set_single_value("System Settings", "currency_precision", cls._saved_precision or "")
		frappe.db.set_default("currency_precision", cls._saved_precision or "")
		cls._restore_tolerance()
		frappe.db.commit()
		frappe.clear_cache()
		super().tearDownClass()

	@classmethod
	def _restore_tolerance(cls):
		if cls._saved_tolerance is None:
			frappe.conf.pop("crenya_pos_total_tolerance", None)
		else:
			frappe.conf.crenya_pos_total_tolerance = cls._saved_tolerance

	def tearDown(self):
		frappe.set_user("Administrator")
		_remove_test_denominations()
		self._restore_tolerance()
		_reset_request_cache()

	def bootstrap(self):
		_reset_request_cache()
		return device_api.get_bootstrap(device_id=self.device_id)

	def test_bootstrap_currency_matches_currency_record(self):
		data = self.bootstrap()
		record = frappe.get_doc("Currency", fixtures.CURRENCY)
		currency = data["currency"]

		self.assertEqual(currency["code"], fixtures.CURRENCY)
		self.assertEqual(currency["code"], data["profile"]["currency"])
		self.assertEqual(currency["symbol"], (record.symbol or "").strip() or None)
		self.assertIs(currency["symbol_on_right"], bool(cint(record.symbol_on_right)))
		self.assertEqual(currency["fraction"], (record.fraction or "").strip() or None)
		self.assertEqual(currency["fraction_units"], cint(record.fraction_units) or None)
		self.assertEqual(
			currency["number_format"],
			record.number_format or frappe.get_system_settings("number_format"),
		)
		self.assertEqual(currency["precision"], 3)
		self.assertEqual(currency["precision"], data["profile"]["currency_precision"])
		smallest = flt(record.smallest_currency_fraction_value)
		if smallest:
			self.assertEqual(Decimal(currency["smallest_currency_fraction_value"]), Decimal(repr(smallest)))
		else:
			self.assertIsNone(currency["smallest_currency_fraction_value"])

	def test_currency_record_changes_reach_bootstrap(self):
		record = frappe.get_doc("Currency", fixtures.CURRENCY)
		saved = {field: record.get(field) for field in ("symbol", "symbol_on_right", "number_format")}
		try:
			frappe.db.set_value(
				"Currency", fixtures.CURRENCY, {"symbol": "_TC$", "symbol_on_right": 1, "number_format": ""}
			)
			frappe.clear_document_cache("Currency", fixtures.CURRENCY)
			currency = self.bootstrap()["currency"]
			self.assertEqual(currency["symbol"], "_TC$")
			self.assertIs(currency["symbol_on_right"], True)
			# no number format on the currency -> System Settings
			self.assertEqual(currency["number_format"], frappe.get_system_settings("number_format"))
		finally:
			frappe.db.set_value("Currency", fixtures.CURRENCY, saved)
			frappe.clear_document_cache("Currency", fixtures.CURRENCY)

	def test_phone_country_codes_company_country_first(self):
		data = self.bootstrap()
		country = frappe.db.get_value("Company", fixtures.COMPANY, "country")
		info = get_all_country_info()
		codes = data["phone_country_codes"]

		self.assertEqual(
			codes[0],
			{"iso": info[country]["code"].upper(), "name": country, "code": info[country]["isd"]},
		)
		self.assertEqual(data["company"]["phone_country_code"], info[country]["isd"])
		expected = {name for name, row in info.items() if row.get("isd")}
		self.assertEqual({row["name"] for row in codes}, expected)
		self.assertEqual(len(codes), len(expected))
		for row in codes:
			self.assertTrue(row["code"].startswith("+"), row)
			self.assertEqual(row["iso"], info[row["name"]]["code"].upper())

	def test_company_without_country_has_no_phone_code(self):
		country = frappe.db.get_value("Company", fixtures.COMPANY, "country")
		try:
			frappe.db.set_value("Company", fixtures.COMPANY, "country", None)
			data = self.bootstrap()
			self.assertEqual(data["company"]["phone_country_code"], "")
			names = [row["name"] for row in data["phone_country_codes"]]
			self.assertIn(country, names)
		finally:
			frappe.db.set_value("Company", fixtures.COMPANY, "country", country)

	def test_cash_denominations_for_profile_currency_only(self):
		_denomination(fixtures.CURRENCY, 0.1, "small coin", kind="coin")
		_denomination(fixtures.CURRENCY, 20, "twenty")
		_denomination(fixtures.CURRENCY, 0.5, "half")
		_denomination(fixtures.CURRENCY, 50, "disabled fifty", enabled=0)
		_denomination(self.other_currency, 100, "other currency")

		rows = [
			row for row in self.bootstrap()["cash_denominations"] if row["label"].startswith(LABEL_PREFIX)
		]
		self.assertEqual(
			rows,
			[
				{"value": "20", "label": f"{LABEL_PREFIX} twenty", "kind": "note"},
				{"value": "0.5", "label": f"{LABEL_PREFIX} half", "kind": "note"},
				{"value": "0.1", "label": f"{LABEL_PREFIX} small coin", "kind": "coin"},
			],
		)

	def test_no_denominations_configured_is_empty(self):
		configured = frappe.get_all(
			DENOMINATION_DOCTYPE, filters={"currency": fixtures.CURRENCY, "enabled": 1}, pluck="name"
		)
		if configured:
			self.skipTest("the site has denominations configured for the test currency")
		self.assertEqual(self.bootstrap()["cash_denominations"], [])

	def test_duplicate_or_invalid_denomination_is_rejected(self):
		_denomination(fixtures.CURRENCY, 1, "one")
		with self.assertRaises(frappe.UniqueValidationError):
			_denomination(fixtures.CURRENCY, 1, "one again")
		# same value in another currency is fine
		_denomination(self.other_currency, 1, "other one")
		for value in (0, -5):
			with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
				_denomination(fixtures.CURRENCY, value, f"bad {value}")

	def test_total_tolerance_default_follows_precision(self):
		frappe.conf.pop("crenya_pos_total_tolerance", None)
		self.assertEqual(f"{get_total_tolerance(2):f}", "0.10")
		self.assertEqual(f"{get_total_tolerance(3):f}", "0.010")

		frappe.conf.crenya_pos_total_tolerance = "0.05"
		self.assertEqual(get_total_tolerance(3), Decimal("0.05"))
		frappe.conf.crenya_pos_total_tolerance = -0.02
		self.assertEqual(get_total_tolerance(2), Decimal("0.02"))

	def test_site_timezone(self):
		data = self.bootstrap()
		self.assertEqual(data["site_timezone"], get_system_timezone())
		self.assertTrue(data["site_timezone"])
		self.assertEqual(sync_api.get_sync_capabilities()["site_timezone"], data["site_timezone"])
