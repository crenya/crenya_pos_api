"""Tax and invoice wording defaults, receipt QR mapping and the GSTIN / HSN fallbacks (no site needed)."""

import unittest
from types import SimpleNamespace

from crenya_pos_api.setup.install import CUSTOM_FIELDS
from crenya_pos_api.sync.tax_wording import (
	COMPANY_WORDING_FIELDS,
	DEFAULT_CREDIT_NOTE_TITLE,
	DEFAULT_INVOICE_TITLE,
	DEFAULT_RECEIPT_QR_OPTION,
	RECEIPT_QR_MODES,
	RECEIPT_QR_OPTIONS,
	company_wording,
	company_wording_fields,
	default_tax_name,
	item_tax_code_field,
	receipt_qr_mode,
	tax_id_label,
	tax_id_value,
	tax_words,
)


class FakeMeta:
	"""The part of a DocType meta the wording code reads: has_field / get_field(...).label."""

	def __init__(self, **labels):
		self.labels = labels

	def has_field(self, fieldname):
		return fieldname in self.labels

	def get_field(self, fieldname):
		return SimpleNamespace(fieldname=fieldname, label=self.labels[fieldname])


ERPNEXT_COMPANY = FakeMeta(tax_id="Tax ID")
INDIA_COMPANY = FakeMeta(tax_id="Tax ID", gstin="GSTIN / UIN")
ERPNEXT_ITEM = FakeMeta(item_name="Item Name")
INDIA_ITEM = FakeMeta(item_name="Item Name", gst_hsn_code="HSN/SAC")


def row(description=None, account_head=None):
	return {"description": description, "account_head": account_head}


class TestTaxWords(unittest.TestCase):
	def test_words_before_rate_or_abbreviation(self):
		cases = {
			"VAT 5% - CR": "VAT",
			"VAT 5%": "VAT",
			"VAT - _TCP": "VAT",
			"Output Tax CGST @ 9.0": "Output Tax CGST",
			"CGST @ 9": "CGST",
			"SGST": "SGST",
			"Value Added Tax": "Value Added Tax",
			"  VAT   15%  ": "VAT",
			"5% VAT": "",
			"VAT5": "",
			"": "",
			None: "",
		}
		for text, expected in cases.items():
			with self.subTest(text=text):
				self.assertEqual(tax_words(text), expected)

	def test_hyphenated_word_is_kept(self):
		# only a lone "-" separates the company abbreviation
		self.assertEqual(tax_words("Non-Resident Tax - CR"), "Non-Resident Tax")

	def test_non_ascii_digits_are_words(self):
		# the till stops only at ASCII digits; Arabic-Indic digits stay part of the name
		arabic_five = "\u0665"
		self.assertEqual(tax_words(f"ضريبة {arabic_five} - CR"), f"ضريبة {arabic_five}")


class TestDefaultTaxName(unittest.TestCase):
	def test_first_row_description(self):
		rows = [row("VAT 5%", "VAT 5% - CR"), row("Excise 50%", "Excise - CR")]
		self.assertEqual(default_tax_name(rows), "VAT")

	def test_account_head_when_description_has_no_words(self):
		self.assertEqual(default_tax_name([row("5%", "Output VAT 5% - CR")]), "Output VAT")
		self.assertEqual(default_tax_name([row(None, "VAT - CR")]), "VAT")

	def test_next_row_when_first_has_no_words(self):
		rows = [row("18%", "18% - IN"), row("CGST @ 9", "CGST - IN")]
		self.assertEqual(default_tax_name(rows), "CGST")

	def test_none_without_rows_or_words(self):
		self.assertIsNone(default_tax_name([]))
		self.assertIsNone(default_tax_name(None))
		self.assertIsNone(default_tax_name([row("5%", "5% - CR")]))


class TestReceiptQr(unittest.TestCase):
	def test_mapping(self):
		self.assertEqual(receipt_qr_mode("Verification link"), "verify_link")
		self.assertEqual(receipt_qr_mode("ZATCA (KSA)"), "zatca_tlv")
		self.assertEqual(receipt_qr_mode("None"), "none")
		self.assertEqual(receipt_qr_mode(" ZATCA (KSA) "), "zatca_tlv")

	def test_blank_or_unknown_is_verify_link(self):
		for value in (None, "", "  ", "QR", "none"):
			with self.subTest(value=value):
				self.assertEqual(receipt_qr_mode(value), "verify_link")

	def test_field_options_match_mapping(self):
		self.assertEqual(RECEIPT_QR_OPTIONS, "Verification link\nZATCA (KSA)\nNone")
		self.assertEqual(RECEIPT_QR_MODES[DEFAULT_RECEIPT_QR_OPTION], "verify_link")
		self.assertEqual(set(RECEIPT_QR_MODES.values()), {"verify_link", "zatca_tlv", "none"})


class TestGstinFallbacks(unittest.TestCase):
	def test_tax_id_value(self):
		self.assertEqual(tax_id_value({"tax_id": "OM123", "gstin": "29ABCDE1234F1Z5"}, True), "OM123")
		self.assertEqual(tax_id_value({"tax_id": "", "gstin": "29ABCDE1234F1Z5"}, True), "29ABCDE1234F1Z5")
		self.assertEqual(tax_id_value({"tax_id": None, "gstin": " 29AB "}, True), "29AB")
		# a site without the gstin field never reads it
		self.assertIsNone(tax_id_value({"tax_id": "", "gstin": "29ABCDE1234F1Z5"}, False))
		self.assertIsNone(tax_id_value({}, True))

	def test_tax_id_label(self):
		self.assertEqual(tax_id_label(ERPNEXT_COMPANY), "Tax ID")
		self.assertEqual(tax_id_label(INDIA_COMPANY), "GSTIN / UIN")
		self.assertEqual(tax_id_label(FakeMeta(tax_id="VAT Number")), "VAT Number")
		self.assertIsNone(tax_id_label(FakeMeta()))
		self.assertIsNone(tax_id_label(None))

	def test_item_tax_code_field(self):
		self.assertIsNone(item_tax_code_field(ERPNEXT_ITEM))
		self.assertEqual(item_tax_code_field(INDIA_ITEM), "gst_hsn_code")
		self.assertIsNone(item_tax_code_field(None))

	def test_company_fields_read_only_when_present(self):
		self.assertEqual(company_wording_fields(ERPNEXT_COMPANY), [])
		self.assertEqual(company_wording_fields(INDIA_COMPANY), ["gstin"])
		full = FakeMeta(**dict.fromkeys(COMPANY_WORDING_FIELDS, "x"), gstin="GSTIN")
		self.assertEqual(company_wording_fields(full), [*COMPANY_WORDING_FIELDS, "gstin"])


VAT_TAXES = (row("VAT 5%", "VAT 5% - CR"),)


class TestCompanyWording(unittest.TestCase):
	def test_defaults(self):
		wording = company_wording({"country": "Oman", "tax_id": ""}, ERPNEXT_COMPANY, ERPNEXT_ITEM, VAT_TAXES)
		self.assertEqual(
			wording,
			{
				"country": "Oman",
				"tax_name": "VAT",
				"tax_name_ar": None,
				"tax_id_label": "Tax ID",
				"tax_id_label_ar": None,
				"invoice_title": DEFAULT_INVOICE_TITLE,
				"invoice_title_ar": None,
				"credit_note_title": DEFAULT_CREDIT_NOTE_TITLE,
				"credit_note_title_ar": None,
				"receipt_qr": "verify_link",
				"tax_code_label": None,
				"tax_id": None,
			},
		)
		self.assertEqual(DEFAULT_INVOICE_TITLE, "Tax Invoice")
		self.assertEqual(DEFAULT_CREDIT_NOTE_TITLE, "Credit Note")

	def test_no_country_and_no_taxes(self):
		wording = company_wording({}, ERPNEXT_COMPANY, ERPNEXT_ITEM, [])
		self.assertIsNone(wording["country"])
		self.assertIsNone(wording["tax_name"])

	def test_company_values_win(self):
		values = {
			"country": "Saudi Arabia",
			"tax_id": "300000000000003",
			"crenya_tax_name": "VAT",
			"crenya_tax_name_ar": "ضريبة القيمة المضافة",
			"crenya_tax_id_label": "VAT No.",
			"crenya_tax_id_label_ar": "الرقم الضريبي",
			"crenya_invoice_title": "Simplified Tax Invoice",
			"crenya_invoice_title_ar": "فاتورة ضريبية مبسطة",
			"crenya_credit_note_title": "Credit Note",
			"crenya_credit_note_title_ar": "إشعار دائن",
			"crenya_receipt_qr": "ZATCA (KSA)",
		}
		wording = company_wording(values, ERPNEXT_COMPANY, ERPNEXT_ITEM, [row("Output 15%", "Output - KSA")])
		self.assertEqual(wording["tax_name"], "VAT")
		self.assertEqual(wording["tax_name_ar"], "ضريبة القيمة المضافة")
		self.assertEqual(wording["tax_id_label"], "VAT No.")
		self.assertEqual(wording["tax_id_label_ar"], "الرقم الضريبي")
		self.assertEqual(wording["invoice_title"], "Simplified Tax Invoice")
		self.assertEqual(wording["invoice_title_ar"], "فاتورة ضريبية مبسطة")
		self.assertEqual(wording["credit_note_title_ar"], "إشعار دائن")
		self.assertEqual(wording["receipt_qr"], "zatca_tlv")
		self.assertEqual(wording["tax_id"], "300000000000003")
		self.assertEqual(wording["country"], "Saudi Arabia")

	def test_blank_company_values_fall_back(self):
		values = {"crenya_tax_name": "  ", "crenya_invoice_title": "", "crenya_receipt_qr": ""}
		wording = company_wording(values, ERPNEXT_COMPANY, ERPNEXT_ITEM, VAT_TAXES)
		self.assertEqual(wording["tax_name"], "VAT")
		self.assertEqual(wording["invoice_title"], "Tax Invoice")
		self.assertEqual(wording["receipt_qr"], "verify_link")

	def test_india_compliance_site(self):
		taxes = [row("CGST @ 9.0", "Output Tax CGST - IN"), row("SGST @ 9.0", "Output Tax SGST - IN")]
		values = {"country": "India", "tax_id": "", "gstin": "29ABCDE1234F1Z5"}
		wording = company_wording(values, INDIA_COMPANY, INDIA_ITEM, taxes)
		self.assertEqual(wording["tax_name"], "CGST")
		self.assertEqual(wording["tax_id_label"], "GSTIN / UIN")
		self.assertEqual(wording["tax_id"], "29ABCDE1234F1Z5")
		self.assertEqual(wording["tax_code_label"], "HSN/SAC")


class TestCompanyCustomFields(unittest.TestCase):
	def test_fields_defined(self):
		fields = {field["fieldname"]: field for field in CUSTOM_FIELDS["Company"]}
		for fieldname in COMPANY_WORDING_FIELDS:
			with self.subTest(fieldname=fieldname):
				self.assertIn(fieldname, fields)
		select = fields["crenya_receipt_qr"]
		self.assertEqual(select["fieldtype"], "Select")
		self.assertEqual(select["options"], "Verification link\nZATCA (KSA)\nNone")
		self.assertEqual(select["default"], "Verification link")
		for fieldname in COMPANY_WORDING_FIELDS:
			if fieldname != "crenya_receipt_qr":
				self.assertEqual(fields[fieldname]["fieldtype"], "Data", fieldname)

	def test_insert_after_chain_is_resolvable(self):
		# every custom field is placed after a standard field or one defined before it
		for doctype, fields in CUSTOM_FIELDS.items():
			seen = set()
			for field in fields:
				anchor = field["insert_after"]
				if anchor.startswith("crenya_"):
					self.assertIn(anchor, seen, f"{doctype}.{field['fieldname']}")
				seen.add(field["fieldname"])


if __name__ == "__main__":
	unittest.main()
