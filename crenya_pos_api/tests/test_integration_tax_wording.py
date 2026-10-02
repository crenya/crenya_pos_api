"""Tax and invoice wording against a real ERPNext site: bootstrap company block, item `tax_code`,
customer / company GSTIN fallbacks and `api.setup.ensure_custom_fields`.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_tax_wording
India Compliance's `gstin` / `gst_hsn_code` fields are imitated with plain Data custom fields when the
site does not have them, and removed again afterwards.
"""

import uuid

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_field

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import setup as setup_api
from crenya_pos_api.api import sync as sync_api
from crenya_pos_api.setup.install import CUSTOM_FIELDS, POS_USER_ROLE
from crenya_pos_api.sync.tax_wording import COMPANY_WORDING_FIELDS, GSTIN_FIELD, TAX_CODE_FIELD
from crenya_pos_api.tests import fixtures

CLERK = "_test_crenya_setup_clerk@example.com"
WORDING_KEYS = (
	"country",
	"tax_name",
	"tax_name_ar",
	"tax_id_label",
	"tax_id_label_ar",
	"invoice_title",
	"invoice_title_ar",
	"credit_note_title",
	"credit_note_title_ar",
	"receipt_qr",
	"tax_code_label",
	"tax_id",
)


def _reset_request_cache():
	cache = getattr(frappe.local, "request_cache", None)
	if cache is not None:
		cache.clear()


def _ensure_clerk():
	if not frappe.db.exists("User", CLERK):
		frappe.get_doc(
			{"doctype": "User", "email": CLERK, "first_name": "Setup Clerk", "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	user = frappe.get_doc("User", CLERK)
	user.set("roles", [])
	user.save(ignore_permissions=True)
	user.add_roles(POS_USER_ROLE, "Sales User")


class TestCrenyaTaxWording(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		setup_api.ensure_custom_fields()

		cls._saved_lag = frappe.conf.get("crenya_pos_pull_lag_seconds")
		frappe.conf.crenya_pos_pull_lag_seconds = 0

		cls.profile = fixtures.setup_fixtures()
		cls.device_id = str(uuid.uuid4())
		device_api.register_device(
			device_id=cls.device_id, device_name="Wording Till", pos_profile=cls.profile.name
		)
		fields = [*COMPANY_WORDING_FIELDS, "tax_id"]
		cls._saved_company = frappe.db.get_value("Company", fixtures.COMPANY, fields, as_dict=True)
		_ensure_clerk()
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.db.set_value("Company", fixtures.COMPANY, cls._saved_company)
		if cls._saved_lag is None:
			frappe.conf.pop("crenya_pos_pull_lag_seconds", None)
		else:
			frappe.conf.crenya_pos_pull_lag_seconds = cls._saved_lag
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.set_user("Administrator")
		# a company with nothing configured: every wording key takes its default
		blank = dict.fromkeys(COMPANY_WORDING_FIELDS)
		blank["tax_id"] = None
		frappe.db.set_value("Company", fixtures.COMPANY, blank)

	def tearDown(self):
		frappe.set_user("Administrator")
		_reset_request_cache()

	def bootstrap_company(self):
		_reset_request_cache()
		return device_api.get_bootstrap(device_id=self.device_id)["company"]

	def pull_all(self, entity, cursor=None):
		records = []
		while True:
			page = sync_api.pull_changes(device_id=self.device_id, entity=entity, cursor=cursor, limit=1000)
			records.extend(page["records"])
			cursor = page["next_cursor"]
			if not page["has_more"]:
				return records, cursor

	def imitate_field(self, doctype, fieldname, label, insert_after):
		"""Plain Data field standing in for an India Compliance field (removed after the test)."""
		if frappe.get_meta(doctype).has_field(fieldname):
			return False
		create_custom_field(
			doctype,
			{"fieldname": fieldname, "fieldtype": "Data", "label": label, "insert_after": insert_after},
		)
		frappe.clear_cache(doctype=doctype)

		def remove():
			name = frappe.db.get_value("Custom Field", {"dt": doctype, "fieldname": fieldname})
			if name:
				frappe.delete_doc("Custom Field", name, force=True, ignore_permissions=True)
			frappe.clear_cache(doctype=doctype)
			frappe.db.commit()

		self.addCleanup(remove)
		return True

	# bootstrap

	def test_bootstrap_defaults(self):
		company = self.bootstrap_company()
		for key in WORDING_KEYS:
			self.assertIn(key, company)

		company_meta = frappe.get_meta("Company")
		id_field = GSTIN_FIELD if company_meta.has_field(GSTIN_FIELD) else "tax_id"
		item_meta = frappe.get_meta("Item")
		self.assertEqual(company["country"], frappe.db.get_value("Company", fixtures.COMPANY, "country"))
		# words of the POS Profile template's first row ("VAT 5%")
		self.assertEqual(company["tax_name"], "VAT")
		self.assertEqual(company["tax_id_label"], company_meta.get_field(id_field).label)
		self.assertEqual(company["invoice_title"], "Tax Invoice")
		self.assertEqual(company["credit_note_title"], "Credit Note")
		self.assertEqual(company["receipt_qr"], "verify_link")
		self.assertIsNone(company["tax_id"])
		for key in ("tax_name_ar", "tax_id_label_ar", "invoice_title_ar", "credit_note_title_ar"):
			self.assertIsNone(company[key], key)
		if item_meta.has_field(TAX_CODE_FIELD):
			self.assertEqual(company["tax_code_label"], item_meta.get_field(TAX_CODE_FIELD).label)
		else:
			self.assertIsNone(company["tax_code_label"])

	def test_bootstrap_returns_company_values(self):
		values = {
			"tax_id": "OM1100223344",
			"crenya_tax_name": "Value Added Tax",
			"crenya_tax_name_ar": "ضريبة القيمة المضافة",
			"crenya_tax_id_label": "VATIN",
			"crenya_tax_id_label_ar": "الرقم الضريبي",
			"crenya_invoice_title": "Simplified Tax Invoice",
			"crenya_invoice_title_ar": "فاتورة ضريبية مبسطة",
			"crenya_credit_note_title": "Refund Note",
			"crenya_credit_note_title_ar": "إشعار دائن",
			"crenya_receipt_qr": "ZATCA (KSA)",
		}
		frappe.db.set_value("Company", fixtures.COMPANY, values)
		company = self.bootstrap_company()
		self.assertEqual(company["tax_id"], "OM1100223344")
		self.assertEqual(company["tax_name"], "Value Added Tax")
		self.assertEqual(company["tax_name_ar"], "ضريبة القيمة المضافة")
		self.assertEqual(company["tax_id_label"], "VATIN")
		self.assertEqual(company["tax_id_label_ar"], "الرقم الضريبي")
		self.assertEqual(company["invoice_title"], "Simplified Tax Invoice")
		self.assertEqual(company["invoice_title_ar"], "فاتورة ضريبية مبسطة")
		self.assertEqual(company["credit_note_title"], "Refund Note")
		self.assertEqual(company["credit_note_title_ar"], "إشعار دائن")
		self.assertEqual(company["receipt_qr"], "zatca_tlv")

		frappe.db.set_value("Company", fixtures.COMPANY, "crenya_receipt_qr", "None")
		self.assertEqual(self.bootstrap_company()["receipt_qr"], "none")
		frappe.db.set_value("Company", fixtures.COMPANY, "crenya_receipt_qr", "Verification link")
		self.assertEqual(self.bootstrap_company()["receipt_qr"], "verify_link")

	def test_company_gstin_fallback(self):
		self.imitate_field("Company", GSTIN_FIELD, "GSTIN / UIN", "tax_id")
		frappe.db.set_value("Company", fixtures.COMPANY, GSTIN_FIELD, "29ABCDE1234F1Z5")
		try:
			company = self.bootstrap_company()
			self.assertEqual(company["tax_id"], "29ABCDE1234F1Z5")
			self.assertEqual(company["tax_id_label"], frappe.get_meta("Company").get_field(GSTIN_FIELD).label)
			# ERPNext's own tax_id wins when set
			frappe.db.set_value("Company", fixtures.COMPANY, "tax_id", "TAX-1")
			self.assertEqual(self.bootstrap_company()["tax_id"], "TAX-1")
		finally:
			frappe.db.set_value("Company", fixtures.COMPANY, GSTIN_FIELD, None)

	# pull

	def test_item_tax_code(self):
		items, cursor = self.pull_all("item")
		milk = next(record for record in items if record["item_code"] == fixtures.MILK)
		self.assertIn("tax_code", milk)
		if not frappe.get_meta("Item").has_field(TAX_CODE_FIELD):
			self.assertIsNone(milk["tax_code"])

		imitated = self.imitate_field("Item", TAX_CODE_FIELD, "HSN/SAC", "item_name")
		saved = frappe.db.get_value("Item", fixtures.MILK, TAX_CODE_FIELD)
		self.addCleanup(frappe.db.set_value, "Item", fixtures.MILK, TAX_CODE_FIELD, saved)
		frappe.db.set_value("Item", fixtures.MILK, TAX_CODE_FIELD, "04012000")
		frappe.db.commit()

		# the existing cursor keeps working and delivers the changed item with its code
		changed, _cursor = self.pull_all("item", cursor)
		milk = next(record for record in changed if record["item_code"] == fixtures.MILK)
		self.assertEqual(milk["tax_code"], "04012000")

		if imitated:
			# items without a code (a new column is empty)
			items, _cursor = self.pull_all("item")
			bread = next(record for record in items if record["item_code"] == fixtures.BREAD)
			self.assertIsNone(bread["tax_code"])
		self.assertEqual(
			self.bootstrap_company()["tax_code_label"],
			frappe.get_meta("Item").get_field(TAX_CODE_FIELD).label,
		)

	def test_customer_gstin_fallback(self):
		self.imitate_field("Customer", GSTIN_FIELD, "GSTIN / UIN", "tax_id")
		with_gstin = fixtures.make_customer(f"_Test Crenya GST {uuid.uuid4().hex[:6]}")
		with_both = fixtures.make_customer(f"_Test Crenya GST {uuid.uuid4().hex[:6]}")
		frappe.db.set_value("Customer", with_gstin, {"tax_id": None, GSTIN_FIELD: "27AAAAA0000A1Z5"})
		frappe.db.set_value("Customer", with_both, {"tax_id": "TAX-9", GSTIN_FIELD: "27BBBBB0000B1Z5"})
		frappe.db.commit()

		customers, _cursor = self.pull_all("customer")
		by_name = {record["name"]: record for record in customers}
		self.assertEqual(by_name[with_gstin]["tax_id"], "27AAAAA0000A1Z5")
		self.assertEqual(by_name[with_both]["tax_id"], "TAX-9")

	# setup

	def test_ensure_custom_fields_requires_system_manager(self):
		frappe.set_user(CLERK)
		with self.assertRaises(frappe.PermissionError):
			setup_api.ensure_custom_fields()
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			setup_api.ensure_custom_fields()

	def test_ensure_custom_fields_is_idempotent(self):
		def crenya_fields():
			return sorted(
				frappe.get_all(
					"Custom Field",
					filters={"fieldname": ["like", "crenya_%"]},
					fields=["dt", "fieldname", "fieldtype", "insert_after", "options", "default"],
					order_by="dt asc, fieldname asc",
				),
				key=lambda row: (row.dt, row.fieldname),
			)

		first = setup_api.ensure_custom_fields()
		before = crenya_fields()
		second = setup_api.ensure_custom_fields()
		self.assertEqual(first, second)
		self.assertEqual(crenya_fields(), before)
		self.assertEqual(second["role"], POS_USER_ROLE)
		self.assertTrue(frappe.db.exists("Role", POS_USER_ROLE))

		for doctype, fields in CUSTOM_FIELDS.items():
			meta = frappe.get_meta(doctype)
			self.assertEqual(second["custom_fields"][doctype], [field["fieldname"] for field in fields])
			for field in fields:
				self.assertTrue(meta.has_field(field["fieldname"]), f"{doctype}.{field['fieldname']}")

		select = frappe.get_meta("Company").get_field("crenya_receipt_qr")
		self.assertEqual(select.fieldtype, "Select")
		self.assertEqual(select.options, "Verification link\nZATCA (KSA)\nNone")
		self.assertEqual(select.default, "Verification link")

	def test_ensure_custom_fields_restores_a_missing_field(self):
		name = frappe.db.get_value("Custom Field", {"dt": "Company", "fieldname": "crenya_invoice_title_ar"})
		frappe.delete_doc("Custom Field", name, force=True, ignore_permissions=True)
		frappe.clear_cache(doctype="Company")
		self.assertFalse(frappe.get_meta("Company").has_field("crenya_invoice_title_ar"))

		setup_api.ensure_custom_fields()
		self.assertTrue(frappe.get_meta("Company").has_field("crenya_invoice_title_ar"))

	def test_capabilities_announce_tax_wording(self):
		self.assertIs(sync_api.get_sync_capabilities()["features"]["tax_wording"], True)
