"""Payment terminals, payment references and scale barcode rules against a real ERPNext site.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_terminals
push_batch commits per event, so documents created here persist (names start with _TC / _Test Crenya).
"""

import uuid
from decimal import Decimal

import frappe
from frappe.utils import nowdate, nowtime
from frappe.utils.password import get_decrypted_password

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.setup.install import ensure_setup
from crenya_pos_api.sync import errors
from crenya_pos_api.sync.hashing import payload_hash
from crenya_pos_api.sync.scale_rules import RULES_FIELD
from crenya_pos_api.sync.terminals import TERMINAL_DOCTYPE
from crenya_pos_api.tests import fixtures

TOKEN = "pl-security-token-0123456789"
CASHIER = "crenya.terminal.cashier@example.com"


def new_id():
	return str(uuid.uuid4())


def money(value):
	return f"{Decimal(value).quantize(Decimal('0.001')):f}"


def delete_test_terminals():
	for name in frappe.get_all(TERMINAL_DOCTYPE, filters={"label": ["like", "_TC %"]}, pluck="name"):
		frappe.delete_doc(TERMINAL_DOCTYPE, name, ignore_permissions=True, force=True)


def make_terminal(label, provider, mode_of_payment, pos_profile=None, company=fixtures.COMPANY, **values):
	doc = frappe.get_doc(
		{
			"doctype": TERMINAL_DOCTYPE,
			"enabled": 1,
			"label": label,
			"provider": provider,
			"company": company,
			"pos_profile": pos_profile,
			"mode_of_payment": mode_of_payment,
			**values,
		}
	)
	return doc.insert()


def pine_labs_values(**values):
	result = {
		"environment": "UAT",
		"merchant_id": "29610",
		"store_id": "1221258",
		"client_id": "1013483",
		"security_token": TOKEN,
		"allowed_payment_mode": "1",
		"auto_cancel_minutes": 5,
	}
	result.update(values)
	return result


def other_company_cash_account():
	"""Another company of the site and a cash account of it, or (None, None)."""
	other = frappe.db.get_value("Company", {"name": ["!=", fixtures.COMPANY]}, "name")
	if not other:
		return None, None
	account = frappe.db.get_value(
		"Account", {"company": other, "account_type": "Cash", "is_group": 0}, "name"
	)
	return (other, account) if account else (None, None)


class TestCrenyaTerminals(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

		cls._saved_precision = frappe.db.get_single_value("System Settings", "currency_precision")
		frappe.db.set_single_value("System Settings", "currency_precision", "3")
		frappe.db.set_default("currency_precision", "3")
		frappe.clear_cache()
		cls._saved_tolerance = frappe.conf.get("crenya_pos_total_tolerance")
		frappe.conf.crenya_pos_total_tolerance = "0.010"

		cls.main_profile = fixtures.setup_fixtures()
		cls.profile = fixtures.setup_terminal_fixtures()
		cls.clear_rules()
		delete_test_terminals()
		frappe.db.commit()

		cls.device_id = new_id()
		cls.device = device_api.register_device(
			device_id=cls.device_id, device_name="Terminal Till", pos_profile=cls.profile.name
		)
		cls.main_device_id = new_id()
		device_api.register_device(
			device_id=cls.main_device_id, device_name="Main Till", pos_profile=cls.main_profile.name
		)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls.clear_rules()
		delete_test_terminals()
		frappe.db.set_single_value("System Settings", "currency_precision", cls._saved_precision or "")
		frappe.db.set_default("currency_precision", cls._saved_precision or "")
		if cls._saved_tolerance is None:
			frappe.conf.pop("crenya_pos_total_tolerance", None)
		else:
			frappe.conf.crenya_pos_total_tolerance = cls._saved_tolerance
		frappe.db.commit()
		frappe.clear_cache()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		delete_test_terminals()
		self.clear_rules()
		frappe.db.commit()

	# helpers

	@classmethod
	def clear_rules(cls):
		profile = frappe.get_doc("POS Profile", fixtures.TERMINAL_PROFILE)
		if profile.get(RULES_FIELD):
			profile.set(RULES_FIELD, [])
			profile.save(ignore_permissions=True)

	def bootstrap(self, device_id=None):
		return device_api.get_bootstrap(device_id=device_id or self.device_id)

	def terminals_by_name(self, device_id=None):
		return {row["name"]: row for row in self.bootstrap(device_id)["payment_terminals"]}

	def sale_payload(self, payments, qty="1", rate="0.600", **overrides):
		amount = money(Decimal(qty) * Decimal(rate))
		payload = {
			"local_id": new_id(),
			"offline_number": f"OFF-{self.device['device_short']}-{uuid.uuid4().hex[:6]}",
			"pos_profile": self.profile.name,
			"company": fixtures.COMPANY,
			"customer": self.profile.customer,
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
			"items": [
				{
					"line_no": 1,
					"item_code": fixtures.MILK,
					"item_name": fixtures.MILK,
					"qty": qty,
					"uom": "Nos",
					"conversion_factor": "1",
					"price_list_rate": rate,
					"discount_percentage": "0",
					"rate": rate,
					"amount": amount,
					"item_tax_template": None,
					"against_line_no": None,
					"against_row_name": None,
				}
			],
			"payments": payments,
			"client_totals": {"grand_total": amount, "rounded_total": amount},
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
		self.assertEqual(results[0]["status"], "ok", msg=results[0].get("error"))
		return results[0]

	def ensure_cashier(self):
		if not frappe.db.exists("User", CASHIER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": CASHIER,
					"first_name": "Terminal Cashier",
					"send_welcome_email": 0,
					"roles": [{"role": "Crenya POS User"}],
				}
			).insert(ignore_permissions=True)
			frappe.db.commit()
		return CASHIER

	# setup / capabilities

	def test_capabilities_and_custom_field(self):
		features = sync_api.get_sync_capabilities()["features"]
		self.assertTrue(features["payment_terminals"])
		self.assertTrue(features["scale_rules"])
		self.assertIn(RULES_FIELD, ensure_setup()["custom_fields"]["POS Profile"])
		field = frappe.get_meta("POS Profile").get_field(RULES_FIELD)
		self.assertEqual(field.fieldtype, "Table")
		self.assertEqual(field.options, "Crenya POS Scale Barcode Rule")

	# terminal records

	def test_terminal_validation(self):
		with self.assertRaises(frappe.ValidationError):
			make_terminal("_TC UPI bad", "UPI QR", fixtures.UPI, upi_vpa="not-a-vpa")
		with self.assertRaises(frappe.ValidationError):
			make_terminal("_TC UPI missing", "UPI QR", fixtures.UPI)
		with self.assertRaises(frappe.ValidationError):
			# no Pine Labs ids
			make_terminal("_TC PL bad", "Pine Labs Cloud", fixtures.CARD, pos_profile=self.profile.name)
		with self.assertRaises(frappe.ValidationError):
			# the main profile only offers cash
			make_terminal(
				"_TC UPI main", "UPI QR", fixtures.UPI, self.main_profile.name, upi_vpa="shop@okhdfc"
			)
		if not frappe.db.exists("Mode of Payment", "_Test Crenya No Account"):
			frappe.get_doc(
				{"doctype": "Mode of Payment", "mode_of_payment": "_Test Crenya No Account", "type": "Bank"}
			).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			make_terminal("_TC UPI no account", "UPI QR", "_Test Crenya No Account", upi_vpa="shop@okhdfc")

		upi = make_terminal("_TC UPI", "UPI QR", fixtures.UPI, upi_vpa=" shop@okhdfc ")
		self.assertEqual(upi.name, f"_TC UPI - {fixtures.ABBR}")
		self.assertEqual(upi.upi_vpa, "shop@okhdfc")
		again = make_terminal("_TC UPI", "UPI QR", fixtures.UPI, upi_vpa="shop2@okhdfc")
		self.assertEqual(again.name, f"_TC UPI - {fixtures.ABBR}-1")
		pine = make_terminal(
			"_TC Pine", "Pine Labs Cloud", fixtures.CARD, self.profile.name, **pine_labs_values()
		)
		self.assertEqual(pine.name, f"_TC Pine - {self.profile.name}")

	def test_cashier_role_cannot_read_terminals(self):
		user = self.ensure_cashier()
		for ptype in ("read", "write", "create"):
			self.assertFalse(frappe.has_permission(TERMINAL_DOCTYPE, ptype, user=user))
		self.assertTrue(frappe.has_permission(TERMINAL_DOCTYPE, "write", user="Administrator"))

	# bootstrap

	def test_bootstrap_terminals_by_profile_and_company(self):
		upi = make_terminal("_TC UPI", "UPI QR", fixtures.UPI, upi_vpa="shop@okhdfc", upi_payee_name="Crenya")
		pine = make_terminal(
			"_TC Pine", "Pine Labs Cloud", fixtures.CARD, self.profile.name, **pine_labs_values()
		)
		geidea = make_terminal("_TC Geidea", "Geidea", fixtures.CARD, host="10.0.0.5", port=6000)
		bank = make_terminal(
			"_TC Bank ECR", "Bank ECR", fixtures.CASH, self.main_profile.name, host="10.0.0.9"
		)
		disabled = make_terminal("_TC UPI off", "UPI QR", fixtures.UPI, upi_vpa="off@okhdfc", enabled=0)
		other = None
		other_company, account = other_company_cash_account()
		if other_company:
			fixtures.ensure_mode_of_payment(
				fixtures.UPI, "Bank", frappe.get_doc("Company", other_company), account
			)
			other = make_terminal(
				"_TC UPI other", "UPI QR", fixtures.UPI, company=other_company, upi_vpa="other@okhdfc"
			)
		frappe.db.commit()

		rows = self.terminals_by_name()
		self.assertEqual(set(rows), {upi.name, pine.name, geidea.name})
		self.assertNotIn(disabled.name, rows)
		if other:
			self.assertNotIn(other.name, rows)

		self.assertEqual(
			rows[upi.name],
			{
				"name": upi.name,
				"provider": "upi_qr",
				"mode_of_payment": fixtures.UPI,
				"label": "_TC UPI",
				"needs_internet": False,
				"config": {"upi_vpa": "shop@okhdfc", "upi_payee_name": "Crenya"},
			},
		)
		self.assertEqual(rows[pine.name]["provider"], "pinelabs_cloud")
		self.assertTrue(rows[pine.name]["needs_internet"])
		self.assertEqual(rows[pine.name]["config"]["environment"], "uat")
		self.assertEqual(rows[pine.name]["config"]["merchant_id"], "29610")
		self.assertEqual(rows[pine.name]["config"]["auto_cancel_minutes"], 5)
		self.assertEqual(rows[geidea.name]["provider"], "geidea")
		self.assertEqual(rows[geidea.name]["config"], {"host": "10.0.0.5", "port": 6000, "terminal_id": None})

		# the main profile offers cash only: its own Bank ECR terminal, not the card / UPI ones
		main = self.terminals_by_name(self.main_device_id)
		self.assertEqual(set(main), {bank.name})
		self.assertEqual(main[bank.name]["provider"], "bank_ecr")
		self.assertFalse(main[bank.name]["needs_internet"])

		# disabling a terminal takes it off the tills
		frappe.db.set_value(TERMINAL_DOCTYPE, upi.name, "enabled", 0)
		self.assertNotIn(upi.name, self.terminals_by_name())

	def test_security_token_only_in_registered_device_bootstrap(self):
		pine = make_terminal(
			"_TC Pine", "Pine Labs Cloud", fixtures.CARD, self.profile.name, **pine_labs_values()
		)
		frappe.db.commit()

		# stored encrypted: the record itself never shows it
		self.assertNotEqual(frappe.db.get_value(TERMINAL_DOCTYPE, pine.name, "security_token"), TOKEN)
		self.assertNotEqual(frappe.get_doc(TERMINAL_DOCTYPE, pine.name).security_token, TOKEN)
		self.assertEqual(get_decrypted_password(TERMINAL_DOCTYPE, pine.name, "security_token"), TOKEN)

		rows = self.terminals_by_name()
		self.assertEqual(rows[pine.name]["config"]["security_token"], TOKEN)

		# unknown device, disabled device and another user's device get no bootstrap at all
		with self.assertRaises(errors.DeviceNotRegisteredError):
			self.bootstrap(new_id())
		disabled_id = new_id()
		device_api.register_device(
			device_id=disabled_id, device_name="Off Till", pos_profile=self.profile.name
		)
		frappe.db.set_value("Crenya POS Device", disabled_id, "enabled", 0)
		with self.assertRaises(errors.DeviceNotRegisteredError):
			self.bootstrap(disabled_id)
		frappe.set_user(self.ensure_cashier())
		with self.assertRaises(frappe.PermissionError):
			self.bootstrap(self.device_id)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(TERMINAL_DOCTYPE, pine.name).check_permission("read")

	# payments

	def test_reference_no_on_sale_and_return(self):
		sale = self.sale_payload(
			[
				{"mode_of_payment": fixtures.CARD, "amount": "0.400", "reference_no": "RRN-202610020001"},
				{"mode_of_payment": fixtures.CASH, "amount": "0.200"},
			]
		)
		result = self.push(sale)
		doc = frappe.get_doc("Sales Invoice", result["name"])
		references = {row.mode_of_payment: row.reference_no for row in doc.payments}
		self.assertEqual(references[fixtures.CARD], "RRN-202610020001")
		self.assertFalse(references[fixtures.CASH])

		ret = self.sale_payload(
			[{"mode_of_payment": fixtures.CARD, "amount": "-0.600", "reference_no": "UTR-998877"}],
			qty="-1",
			is_return=1,
			return_against_local_id=sale["local_id"],
		)
		ret["items"][0]["against_line_no"] = 1
		returned = frappe.get_doc("Sales Invoice", self.push(ret)["name"])
		self.assertEqual(returned.is_return, 1)
		self.assertEqual(returned.payments[0].mode_of_payment, fixtures.CARD)
		self.assertEqual(returned.payments[0].reference_no, "UTR-998877")

	# scale barcode rules

	def test_scale_rules_bootstrap_and_validation(self):
		self.assertEqual(self.bootstrap()["profile"]["scale_barcode_rules"], [])

		profile = frappe.get_doc("POS Profile", self.profile.name)
		profile.append(RULES_FIELD, {"prefix": "21", "plu_length": 5, "value_type": "Weight"})
		profile.append(
			RULES_FIELD, {"prefix": " 22 ", "plu_length": 6, "value_type": "Price", "value_decimals": "2"}
		)
		profile.save(ignore_permissions=True)
		frappe.db.commit()
		self.assertEqual(
			self.bootstrap()["profile"]["scale_barcode_rules"],
			[
				{"prefix": "21", "plu_length": 5, "value_type": "weight", "value_decimals": None},
				{"prefix": "22", "plu_length": 6, "value_type": "price", "value_decimals": 2},
			],
		)
		# other profiles keep the till's local settings
		self.assertEqual(self.bootstrap(self.main_device_id)["profile"]["scale_barcode_rules"], [])

		for bad in (
			{"prefix": "21", "plu_length": 4, "value_type": "Price"},
			{"prefix": "2", "plu_length": 4, "value_type": "Price"},
			{"prefix": "2345", "plu_length": 4, "value_type": "Price"},
			{"prefix": "23", "plu_length": 10, "value_type": "Weight"},
		):
			with self.subTest(rule=bad):
				profile = frappe.get_doc("POS Profile", self.profile.name)
				profile.append(RULES_FIELD, bad)
				with self.assertRaises(frappe.ValidationError):
					profile.save(ignore_permissions=True)
		frappe.db.rollback()
		self.assertEqual(len(frappe.get_doc("POS Profile", self.profile.name).get(RULES_FIELD)), 2)
