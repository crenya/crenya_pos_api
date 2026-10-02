"""Payment terminals, scale barcode rules and payment references (no site needed)."""

import copy
import unittest
from decimal import Decimal
from unittest import mock

import frappe

from crenya_pos_api.sync import scale_rules, terminals
from crenya_pos_api.sync.errors import SyncError
from crenya_pos_api.sync.scale_rules import format_rules, validate_rules
from crenya_pos_api.sync.terminals import (
	NEEDS_INTERNET,
	PROVIDERS,
	format_terminals,
	is_valid_vpa,
	provider_config,
	validate_links,
	validate_settings,
)
from crenya_pos_api.sync.validation import validate_invoice_payload
from crenya_pos_api.tests.test_unit_validation import SALE, make_return


class ThrowCalled(Exception):
	pass


def fake_throw(message, *args, **kwargs):
	raise ThrowCalled(message)


def patch_throw(test, module):
	"""frappe.throw / _ without a site: throw raises ThrowCalled with the message."""
	for patch in (
		mock.patch.object(module, "_", side_effect=lambda text: text),
		mock.patch.object(module.frappe, "throw", side_effect=fake_throw),
	):
		patch.start()
		test.addCleanup(patch.stop)


def terminal(**values):
	doc = frappe._dict(
		{
			"name": "UPI - CR",
			"enabled": 1,
			"label": "UPI",
			"provider": "UPI QR",
			"company": "Crenya LLC",
			"pos_profile": None,
			"mode_of_payment": "UPI",
			"upi_vpa": "shop@okhdfc",
		}
	)
	doc.update(values)
	return doc


def pine_labs(**values):
	settings = {
		"name": "Pine Labs - CR",
		"label": "Pine Labs",
		"provider": "Pine Labs Cloud",
		"mode_of_payment": "Card",
		"upi_vpa": None,
		"environment": "Production",
		"merchant_id": "29610",
		"store_id": "1221258",
		"client_id": "1013483",
		"user_id": None,
		"security_token": "*****",
		"allowed_payment_mode": "1",
		"auto_cancel_minutes": 5,
	}
	settings.update(values)
	return terminal(**settings)


class TestTerminalSettings(unittest.TestCase):
	def setUp(self):
		patch_throw(self, terminals)

	def assertRejected(self, doc, fragment):
		with self.assertRaises(ThrowCalled) as ctx:
			validate_settings(doc)
		self.assertIn(fragment, ctx.exception.args[0])

	def test_vpa_format(self):
		for vpa in ("shop@okhdfc", "crenya.store-1@ybl", "9876543210@paytm", "a_b@upi"):
			with self.subTest(vpa=vpa):
				self.assertTrue(is_valid_vpa(vpa))
		for vpa in (
			None,
			"",
			"shop",
			"@okhdfc",
			"shop@",
			"s@okhdfc",
			"shop@@bank",
			"shop @bank",
			"shop@1bank",
			7,
		):
			with self.subTest(vpa=vpa):
				self.assertFalse(is_valid_vpa(vpa))

	def test_upi_terminal(self):
		doc = terminal(label="  UPI  ", upi_vpa=" shop@okhdfc ", upi_payee_name="  ")
		validate_settings(doc)
		self.assertEqual(doc.label, "UPI")
		self.assertEqual(doc.upi_vpa, "shop@okhdfc")
		self.assertIsNone(doc.upi_payee_name)
		self.assertRejected(terminal(upi_vpa=None), "UPI ID (VPA) is required")
		self.assertRejected(terminal(upi_vpa="shop.okhdfc"), "name@bank")

	def test_label_and_provider_required(self):
		self.assertRejected(terminal(label=" "), "Label is required")
		self.assertRejected(terminal(provider="Stripe"), "Provider must be one of")

	def test_pine_labs_ids(self):
		doc = pine_labs(environment=None, merchant_id=" 29610 ")
		validate_settings(doc)
		self.assertEqual(doc.environment, "UAT")
		self.assertEqual(doc.merchant_id, "29610")
		for fieldname, label in (
			("merchant_id", "Merchant ID"),
			("store_id", "Store ID"),
			("client_id", "Client ID"),
		):
			with self.subTest(field=fieldname):
				self.assertRejected(pine_labs(**{fieldname: ""}), f"{label} is required")
		self.assertRejected(pine_labs(security_token=None), "Security Token is required")
		self.assertRejected(pine_labs(store_id="12 34"), "Store ID must be a single word")
		self.assertRejected(pine_labs(client_id="9" * 141), "Client ID must be a single word")
		self.assertRejected(pine_labs(allowed_payment_mode="card"), "Allowed Payment Mode")
		validate_settings(pine_labs(allowed_payment_mode="1,10"))
		self.assertRejected(pine_labs(auto_cancel_minutes=-1), "Auto Cancel")
		self.assertRejected(pine_labs(auto_cancel_minutes=1441), "Auto Cancel")

	def test_ecr_settings(self):
		for provider in ("Geidea", "Network International ECR", "Bank ECR"):
			with self.subTest(provider=provider):
				validate_settings(terminal(provider=provider, upi_vpa=None, host="10.0.0.5", port=6000))
				validate_settings(terminal(provider=provider, upi_vpa=None))
		self.assertRejected(terminal(provider="Bank ECR", port=70000), "Port must be between")
		self.assertRejected(terminal(provider="Geidea", host="10.0.0.5 x"), "Host must be a single word")


class TestTerminalLinks(unittest.TestCase):
	def setUp(self):
		patch_throw(self, terminals)
		self.profile = frappe._dict(
			{
				"name": "Muscat Main",
				"company": "Crenya LLC",
				"payments": [frappe._dict(mode_of_payment="Cash"), frappe._dict(mode_of_payment="UPI")],
			}
		)
		for patch in (
			mock.patch.object(terminals.frappe, "get_cached_doc", return_value=self.profile),
			mock.patch.object(terminals, "mode_has_company_account", side_effect=self.has_account),
		):
			patch.start()
			self.addCleanup(patch.stop)

	@staticmethod
	def has_account(mode, company):
		return company == "Crenya LLC" and mode in ("Cash", "UPI", "Card")

	def test_mode_of_company(self):
		validate_links(terminal())
		with self.assertRaises(ThrowCalled) as ctx:
			validate_links(terminal(mode_of_payment="Cheque"))
		self.assertIn("has no account for company Crenya LLC", ctx.exception.args[0])
		with self.assertRaises(ThrowCalled):
			validate_links(terminal(company="Other LLC"))

	def test_profile_company_and_payments(self):
		validate_links(terminal(pos_profile="Muscat Main"))
		with self.assertRaises(ThrowCalled) as ctx:
			validate_links(terminal(pos_profile="Muscat Main", mode_of_payment="Card"))
		self.assertIn("not in the payment methods of POS Profile Muscat Main", ctx.exception.args[0])
		self.profile.company = "Other LLC"
		with self.assertRaises(ThrowCalled) as ctx:
			validate_links(terminal(pos_profile="Muscat Main"))
		self.assertIn("belongs to company Other LLC", ctx.exception.args[0])


class TestTerminalBootstrap(unittest.TestCase):
	def setUp(self):
		self.secrets = []

	def get_secret(self, name):
		self.secrets.append(name)
		return "s3cret"

	def test_provider_keys_and_internet(self):
		self.assertEqual(
			sorted(PROVIDERS.values()), ["bank_ecr", "geidea", "network_ecr", "pinelabs_cloud", "upi_qr"]
		)
		self.assertEqual(set(NEEDS_INTERNET), set(PROVIDERS.values()))
		self.assertFalse(NEEDS_INTERNET["upi_qr"])
		self.assertFalse(NEEDS_INTERNET["bank_ecr"])
		for key in ("pinelabs_cloud", "geidea", "network_ecr"):
			self.assertTrue(NEEDS_INTERNET[key])

	def test_shapes_rows_for_the_profile(self):
		rows = [
			terminal(upi_payee_name="Crenya Store"),
			pine_labs(pos_profile="Muscat Main", user_id="cashier1", auto_cancel_minutes=0),
			# another profile of the company
			terminal(name="UPI - Sohar", pos_profile="Sohar"),
			# mode of payment not offered by this profile
			terminal(name="Cheque", mode_of_payment="Cheque"),
			terminal(
				name="Bank ECR - CR",
				label="",
				provider="Bank ECR",
				host="10.0.0.9",
				port="0",
				mode_of_payment="Cash",
			),
			terminal(name="Unknown", provider="Stripe"),
		]
		result = format_terminals(rows, "Muscat Main", ["Cash", "UPI", "Card"], self.get_secret)
		self.assertEqual([row["name"] for row in result], ["UPI - CR", "Pine Labs - CR", "Bank ECR - CR"])
		upi, pine, ecr = result
		self.assertEqual(
			upi,
			{
				"name": "UPI - CR",
				"provider": "upi_qr",
				"mode_of_payment": "UPI",
				"label": "UPI",
				"needs_internet": False,
				"config": {"upi_vpa": "shop@okhdfc", "upi_payee_name": "Crenya Store"},
			},
		)
		self.assertEqual(pine["provider"], "pinelabs_cloud")
		self.assertTrue(pine["needs_internet"])
		self.assertEqual(
			pine["config"],
			{
				"environment": "production",
				"merchant_id": "29610",
				"store_id": "1221258",
				"client_id": "1013483",
				"user_id": "cashier1",
				"security_token": "s3cret",
				"allowed_payment_mode": "1",
				"auto_cancel_minutes": None,
			},
		)
		self.assertEqual(ecr["label"], "Bank ECR - CR")
		self.assertEqual(ecr["config"], {"host": "10.0.0.9", "port": None, "terminal_id": None})
		self.assertFalse(ecr["needs_internet"])
		# only the Pine Labs row's secret is read
		self.assertEqual(self.secrets, ["Pine Labs - CR"])

	def test_config_holds_only_the_providers_settings(self):
		row = pine_labs(upi_vpa="shop@okhdfc", host="10.0.0.5")
		self.assertNotIn("upi_vpa", provider_config("pinelabs_cloud", row, "x"))
		self.assertNotIn("security_token", provider_config("upi_qr", row))
		self.assertNotIn("security_token", provider_config("geidea", row))
		self.assertIsNone(provider_config("pinelabs_cloud", row)["security_token"])

	def test_empty(self):
		self.assertEqual(format_terminals([], "Muscat Main", ["Cash"], self.get_secret), [])


def rule(idx, prefix, plu_length=5, value_type="Weight", value_decimals=""):
	return frappe._dict(
		{
			"idx": idx,
			"prefix": prefix,
			"plu_length": plu_length,
			"value_type": value_type,
			"value_decimals": value_decimals,
		}
	)


class TestScaleRules(unittest.TestCase):
	def setUp(self):
		patch_throw(self, scale_rules)

	def assertRejected(self, rows, fragment):
		with self.assertRaises(ThrowCalled) as ctx:
			validate_rules(rows)
		self.assertIn(fragment, ctx.exception.args[0])

	def test_valid_rules(self):
		rows = [
			rule(1, " 21 "),
			rule(2, "22", 6, "Price", "3"),
			rule(3, "3", 10),
			rule(4, "20", 5, "Weight", "0"),
		]
		validate_rules(rows)
		self.assertEqual(rows[0].prefix, "21")
		validate_rules([])

	def test_prefix(self):
		for prefix in ("", "1234", "2a", None):
			with self.subTest(prefix=prefix):
				self.assertRejected([rule(1, prefix)], "prefix must be 1 to 3 digits")

	def test_plu_length(self):
		for length in (0, 11, -1, None):
			with self.subTest(length=length):
				self.assertRejected([rule(1, "21", length)], "PLU length must be between 1 and 10")
		# 3 + 9 leaves no digit for the value before the check digit
		self.assertRejected([rule(1, "210", 9)], "less than 12 digits")
		self.assertRejected([rule(1, "21", 10)], "less than 12 digits")
		validate_rules([rule(1, "210", 8), rule(2, "3", 10)])

	def test_value_type_and_decimals(self):
		self.assertRejected([rule(1, "21", value_type="weight")], "value type must be Weight or Price")
		for decimals in ("7", "-1", "x"):
			with self.subTest(decimals=decimals):
				self.assertRejected([rule(1, "21", value_decimals=decimals)], "value decimals")
		validate_rules([rule(1, "21", value_decimals=None)])
		validate_rules([rule(1, "21", value_decimals=6)])

	def test_unique_prefixes(self):
		self.assertRejected(
			[rule(1, "21"), rule(2, "21", 6, "Price")], "rules 1 and 2 have the same prefix 21"
		)
		self.assertRejected([rule(1, "21"), rule(2, " 21")], "same prefix")
		self.assertRejected([rule(1, "2"), rule(2, "21")], "prefix 2 overlaps prefix 21")
		self.assertRejected([rule(1, "210"), rule(2, "21")], "overlaps")

	def test_bootstrap_shape(self):
		rows = [
			rule(2, "22", 6, "Price", "2"),
			rule(1, "21", "5", "Weight", ""),
			rule(3, "23", 5, "Weight", "0"),
			# no longer valid (saved before validation existed): skipped
			rule(4, "2x"),
			rule(5, "24", 0),
			rule(6, "25", value_type="Volume"),
			rule(7, "26", value_decimals="9"),
		]
		self.assertEqual(
			format_rules(rows),
			[
				{"prefix": "21", "plu_length": 5, "value_type": "weight", "value_decimals": None},
				{"prefix": "22", "plu_length": 6, "value_type": "price", "value_decimals": 2},
				{"prefix": "23", "plu_length": 5, "value_type": "weight", "value_decimals": 0},
			],
		)
		self.assertEqual(format_rules([]), [])

	def test_profile_hook(self):
		profile = frappe._dict({scale_rules.RULES_FIELD: [rule(1, "21"), rule(2, "21")]})
		with self.assertRaises(ThrowCalled):
			scale_rules.pos_profile_validate(profile)
		scale_rules.pos_profile_validate(frappe._dict())


class TestPaymentReference(unittest.TestCase):
	def assertInvalid(self, payload, fragment):
		with self.assertRaises(SyncError) as ctx:
			validate_invoice_payload(payload)
		self.assertEqual(ctx.exception.code, "validation")
		self.assertIn(fragment, ctx.exception.message)

	def test_reference_no_is_kept(self):
		payload = copy.deepcopy(SALE)
		payload["payments"] = [
			{"mode_of_payment": "Card", "amount": "1.200", "reference_no": " 123456789012 "}
		]
		data = validate_invoice_payload(payload)
		self.assertEqual(
			data["payments"],
			[{"mode_of_payment": "Card", "amount": Decimal("1.200"), "reference_no": "123456789012"}],
		)

	def test_blank_or_missing_reference_is_none(self):
		for value in (None, "", "  "):
			with self.subTest(value=value):
				payload = copy.deepcopy(SALE)
				payload["payments"][0]["reference_no"] = value
				self.assertIsNone(validate_invoice_payload(payload)["payments"][0]["reference_no"])

	def test_references_of_one_mode_are_joined(self):
		payload = copy.deepcopy(SALE)
		payload["payments"] = [
			{"mode_of_payment": "Card", "amount": "0.700", "reference_no": "RRN1"},
			{"mode_of_payment": "Cash", "amount": "0.100"},
			{"mode_of_payment": "Card", "amount": "0.300", "reference_no": "RRN2"},
			{"mode_of_payment": "Card", "amount": "0.100", "reference_no": "RRN1"},
		]
		data = validate_invoice_payload(payload)
		self.assertEqual(
			data["payments"],
			[
				{"mode_of_payment": "Card", "amount": Decimal("1.100"), "reference_no": "RRN1, RRN2"},
				{"mode_of_payment": "Cash", "amount": Decimal("0.100"), "reference_no": None},
			],
		)

	def test_reference_rules(self):
		payload = copy.deepcopy(SALE)
		payload["payments"][0]["reference_no"] = "R" * 141
		self.assertInvalid(payload, "payments[0].reference_no is longer than 140 characters")
		payload["payments"][0]["reference_no"] = 123456
		self.assertInvalid(payload, "payments[0].reference_no must be a string")
		payload["payments"] = [
			{"mode_of_payment": "Card", "amount": "0.600", "reference_no": "A" * 80},
			{"mode_of_payment": "Card", "amount": "0.600", "reference_no": "B" * 80},
		]
		self.assertInvalid(payload, "payments of Card: reference numbers are longer than 140")
		payload["payments"][1]["reference_no"] = "B" * 50
		validate_invoice_payload(payload)

	def test_return_keeps_reference(self):
		payload = make_return()
		payload["payments"][0]["reference_no"] = "UTR-998877"
		data = validate_invoice_payload(payload)
		self.assertEqual(data["payments"][0]["reference_no"], "UTR-998877")
		self.assertEqual(data["payments"][0]["amount"], Decimal("-0.600"))
