"""Credit days as a House Account payment terms template on an ERPNext site: the template made once
and refused when a same-named one is not what it says, the days read back, the days a customer gets
(own, group, company), a customer document changed in memory (limit rows of one company, days) and
a till's cash, split and wholly-on-account sales to a customer with terms.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_credit_terms
"""

from decimal import Decimal

import frappe
from frappe.utils import add_days, getdate

from crenya_pos_api.sync.credit_terms import (
	DAYS_BASIS,
	apply_customer_terms,
	customer_terms,
	days_template,
	template_days,
)
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.test_integration_credit import CreditCase, make_customer

TEMPLATE = "Payment Terms Template"
OTHER_COMPANY = "_Test Crenya Other Company"


def delete_template(name):
	if frappe.db.exists(TEMPLATE, name):
		frappe.delete_doc(TEMPLATE, name, force=True, ignore_permissions=True)


def make_template(name, rows):
	"""A template of `name` with detail rows (invoice_portion, due_date_based_on, credit_days)."""
	delete_template(name)
	doc = frappe.new_doc(TEMPLATE)
	doc.template_name = name
	for portion, basis, days in rows:
		doc.append(
			"terms",
			{"invoice_portion": portion, "due_date_based_on": basis, "credit_days": days},
		)
	doc.insert(ignore_permissions=True)
	return doc.name


class TermsCase(CreditCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(
			self.restore_company_terms, frappe.db.get_value("Company", fixtures.COMPANY, "payment_terms")
		)

	def restore_company_terms(self, value):
		frappe.db.set_value("Company", fixtures.COMPANY, "payment_terms", value)
		frappe.clear_document_cache("Company", fixtures.COMPANY)

	def set_group_terms(self, customer, template):
		"""Give the customer a Customer Group of its own with `template` as payment terms."""
		name = f"_Test Crenya Terms Group {frappe.generate_hash(length=6)}"
		parent = frappe.db.get_value("Customer", customer, "customer_group")
		group = frappe.get_doc(
			{
				"doctype": "Customer Group",
				"customer_group_name": name,
				"parent_customer_group": frappe.db.get_value(
					"Customer Group", parent, "parent_customer_group"
				)
				or parent,
				"payment_terms": template,
			}
		).insert(ignore_permissions=True)
		original = frappe.db.get_value("Customer", customer, "customer_group")
		frappe.db.set_value("Customer", customer, "customer_group", group.name)
		self.addCleanup(self.drop_group, customer, original, group.name)
		return group.name

	def drop_group(self, customer, original, group):
		# a group of ours must not stay: `fixtures.leaf_customer_group` hands out the first leaf
		frappe.db.set_value("Customer", customer, "customer_group", original)
		frappe.delete_doc("Customer Group", group, force=True, ignore_permissions=True)

	def set_own_days(self, customer, days):
		doc = frappe.get_doc("Customer", customer)
		apply_customer_terms(doc, fixtures.COMPANY, days=days)
		doc.save(ignore_permissions=True)
		return doc


class TestDaysTemplate(TermsCase):
	def test_days_template_created_once(self):
		delete_template("House Account 31 days")
		first = days_template(31)
		second = days_template(31)
		self.assertEqual((first, second), ("House Account 31 days", "House Account 31 days"))
		self.assertEqual(frappe.db.count(TEMPLATE, {"name": "House Account 31 days"}), 1)
		doc = frappe.get_doc(TEMPLATE, "House Account 31 days")
		self.assertEqual(len(doc.terms), 1)
		row = doc.terms[0]
		self.assertEqual(
			(row.description, row.invoice_portion, row.due_date_based_on, row.credit_days),
			("House Account 31 days", 100, DAYS_BASIS, 31),
		)
		self.assertIsNone(row.payment_term)

	def test_days_template_mismatch_refused(self):
		name = make_template("House Account 32 days", [(50, DAYS_BASIS, 32), (50, DAYS_BASIS, 60)])
		with self.assertRaises(frappe.ValidationError) as raised:
			days_template(32)
		self.assertIn(f"Payment Terms Template {name} is not 32 days", str(raised.exception))
		# one row, other days
		make_template("House Account 32 days", [(100, DAYS_BASIS, 45)])
		with self.assertRaises(frappe.ValidationError):
			days_template(32)
		delete_template("House Account 32 days")

	def test_days_template_duplicate_race_reads_back(self):
		delete_template("House Account 33 days")
		real_insert = frappe.model.document.Document.insert
		raced = []

		def racing_insert(doc, *args, **kwargs):
			if doc.doctype == TEMPLATE and doc.template_name == "House Account 33 days" and not raced:
				# another worker wins the race just before our insert
				raced.append(True)
				make_template("House Account 33 days", [(100, DAYS_BASIS, 33)])
			return real_insert(doc, *args, **kwargs)

		from unittest import mock

		with mock.patch.object(frappe.model.document.Document, "insert", racing_insert):
			self.assertEqual(days_template(33), "House Account 33 days")
		self.assertEqual(frappe.db.count(TEMPLATE, {"name": "House Account 33 days"}), 1)

	def test_days_template_race_leaves_no_already_exists_message(self):
		"""M6: the duplicate Frappe's insert reports is handled here; it must not reach the caller's
		message log (it would show on the screen of a request that succeeded)."""
		from unittest import mock

		delete_template("House Account 35 days")
		days_template(35)
		frappe.local.message_log = []
		with mock.patch.object(frappe.db, "exists", return_value=False):
			self.assertEqual(days_template(35), "House Account 35 days")
		self.assertEqual(frappe.local.message_log, [])
		delete_template("House Account 35 days")

	def test_days_template_read_back_when_exists_missed_it(self):
		# `exists` answers False (the snapshot predates the winner's commit): the insert hits the
		# duplicate and the template is read back, locked
		from unittest import mock

		delete_template("House Account 34 days")
		days_template(34)
		with mock.patch.object(frappe.db, "exists", return_value=False):
			self.assertEqual(days_template(34), "House Account 34 days")
		make_template("House Account 34 days", [(100, DAYS_BASIS, 99)])
		with mock.patch.object(frappe.db, "exists", return_value=False):
			with self.assertRaises(frappe.ValidationError):
				days_template(34)
		delete_template("House Account 34 days")

	def test_days_template_rejects_bad_days(self):
		for bad in (-1, 366, True, None):
			with self.assertRaises(ValueError):
				days_template(bad)

	def test_template_days(self):
		one = make_template("_Test Crenya Terms one row", [(100, DAYS_BASIS, 30)])
		two = make_template("_Test Crenya Terms two rows", [(50, DAYS_BASIS, 30), (50, DAYS_BASIS, 60)])
		month = make_template(
			"_Test Crenya Terms month end", [(100, "Day(s) after the end of the invoice month", 30)]
		)
		zero = make_template("_Test Crenya Terms zero", [(100, DAYS_BASIS, 0)])
		self.assertEqual(template_days(one), 30)
		self.assertEqual(template_days(zero), 0)
		for other in (two, month, None, "", "_Test Crenya Terms missing"):
			self.assertIsNone(template_days(other), other)
		for name in (one, two, month, zero):
			delete_template(name)


class TestCustomerTerms(TermsCase):
	def test_customer_terms_sources(self):
		# made first: `make_customer` puts a customer in the first leaf group, ours included
		own = make_customer("terms own")
		grouped = make_customer("terms group")
		company_only = make_customer("terms company")
		self.set_own_days(own, 30)
		self.assertEqual(
			customer_terms(own, fixtures.COMPANY),
			{"payment_terms": "House Account 30 days", "credit_days": 30, "credit_days_source": "customer"},
		)
		self.set_group_terms(grouped, days_template(45))
		self.assertEqual(
			customer_terms(grouped, fixtures.COMPANY),
			{"payment_terms": "House Account 45 days", "credit_days": 45, "credit_days_source": "group"},
		)
		frappe.db.set_value("Company", fixtures.COMPANY, "payment_terms", days_template(10))
		self.assertEqual(
			customer_terms(company_only, fixtures.COMPANY),
			{"payment_terms": "House Account 10 days", "credit_days": 10, "credit_days_source": "company"},
		)
		frappe.db.set_value("Company", fixtures.COMPANY, "payment_terms", None)
		self.assertEqual(
			customer_terms(company_only, fixtures.COMPANY),
			{"payment_terms": None, "credit_days": None, "credit_days_source": None},
		)

	def test_customer_terms_other_template_has_no_days(self):
		customer = make_customer("terms other")
		template = make_template("_Test Crenya Terms split", [(50, DAYS_BASIS, 30), (50, DAYS_BASIS, 60)])
		frappe.db.set_value("Customer", customer, "payment_terms", template)
		answer = customer_terms(customer, fixtures.COMPANY)
		self.assertEqual(answer["payment_terms"], template)
		self.assertEqual((answer["credit_days"], answer["credit_days_source"]), (None, None))
		frappe.db.set_value("Customer", customer, "payment_terms", None)
		delete_template(template)

	def test_days_none_clears_own_terms_and_reports_group_source(self):
		customer = make_customer("terms clear")
		self.set_group_terms(customer, days_template(45))
		doc = self.set_own_days(customer, 30)
		self.assertEqual(doc.payment_terms, "House Account 30 days")
		change = apply_customer_terms(doc, fixtures.COMPANY, days=None)
		doc.save(ignore_permissions=True)
		self.assertEqual((change["old_days"], change["new_days"]), (30, None))
		self.assertFalse(frappe.db.get_value("Customer", customer, "payment_terms"))
		self.assertEqual(
			customer_terms(customer, fixtures.COMPANY),
			{"payment_terms": "House Account 45 days", "credit_days": 45, "credit_days_source": "group"},
		)

	def test_days_zero_uses_zero_day_template_over_group(self):
		customer = make_customer("terms zero")
		self.set_group_terms(customer, days_template(45))
		doc = self.set_own_days(customer, 0)
		self.assertEqual(doc.payment_terms, "House Account 0 days")
		self.assertEqual(customer_terms(customer, fixtures.COMPANY)["credit_days_source"], "customer")
		frappe.db.set_value("Customer", customer, "crenya_credit_enabled", 1)
		payload = self.on_account_sale(customer, total="10.000")
		invoice = frappe.get_doc("Sales Invoice", self.post(payload)["name"])
		self.assertEqual(getdate(invoice.due_date), getdate(invoice.posting_date))


class TestApplyCustomerTerms(TermsCase):
	def test_other_company_limit_row_untouched(self):
		customer = make_customer("terms rows")
		doc = frappe.get_doc("Customer", customer)
		doc.append("credit_limits", {"company": OTHER_COMPANY, "credit_limit": 100})
		doc.append("credit_limits", {"company": fixtures.COMPANY, "credit_limit": 200})
		other = Decimal("100")
		change = apply_customer_terms(doc, fixtures.COMPANY, limit=Decimal("50.000"))
		self.assertEqual((change["old_limit"], change["new_limit"]), (Decimal("200"), Decimal("50.000")))
		rows = {row.company: row.credit_limit for row in doc.credit_limits}
		self.assertEqual(rows, {OTHER_COMPANY: float(other), fixtures.COMPANY: 50.0})
		change = apply_customer_terms(doc, fixtures.COMPANY, limit=None)
		self.assertEqual((change["old_limit"], change["new_limit"]), (Decimal("50"), None))
		self.assertEqual(
			[(row.company, row.credit_limit) for row in doc.credit_limits], [(OTHER_COMPANY, 100.0)]
		)

	def test_limit_row_added_and_bypass_kept(self):
		doc = frappe.new_doc("Customer")
		change = apply_customer_terms(doc, fixtures.COMPANY, limit=Decimal("7.500"))
		self.assertEqual((change["old_limit"], change["new_limit"]), (None, Decimal("7.500")))
		self.assertEqual([(r.company, r.credit_limit) for r in doc.credit_limits], [(fixtures.COMPANY, 7.5)])
		doc.credit_limits[0].bypass_credit_limit_check = 1
		apply_customer_terms(doc, fixtures.COMPANY, limit=None)
		self.assertEqual([(r.company, r.credit_limit) for r in doc.credit_limits], [(fixtures.COMPANY, 0)])

	def test_unset_leaves_parts_alone(self):
		doc = frappe.new_doc("Customer")
		doc.payment_terms = days_template(30)
		doc.append("credit_limits", {"company": fixtures.COMPANY, "credit_limit": 9})
		change = apply_customer_terms(doc, fixtures.COMPANY)
		self.assertEqual(
			change, {"old_limit": Decimal("9"), "new_limit": Decimal("9"), "old_days": 30, "new_days": 30}
		)
		self.assertEqual(doc.payment_terms, "House Account 30 days")
		self.assertEqual(len(doc.credit_limits), 1)

	def test_apply_does_not_save(self):
		customer = make_customer("terms nosave")
		doc = frappe.get_doc("Customer", customer)
		apply_customer_terms(doc, fixtures.COMPANY, limit=Decimal("5.000"), days=15)
		self.assertFalse(frappe.db.get_value("Customer", customer, "payment_terms"))
		self.assertFalse(frappe.db.exists("Customer Credit Limit", {"parent": customer}))


class TestTillSalesWithTerms(TermsCase):
	def test_till_cash_and_split_sale_with_terms_post(self):
		customer = make_customer("terms till")
		self.set_own_days(customer, 30)
		frappe.db.set_value("Customer", customer, "crenya_credit_enabled", 1)

		cash = frappe.get_doc("Sales Invoice", self.post(self.sale_payload(customer=customer))["name"])
		self.assertEqual(cash.customer, customer)
		self.assertEqual(getdate(cash.due_date), add_days(getdate(cash.posting_date), 30))
		self.assertFalse(cash.payment_schedule)

		split = frappe.get_doc(
			"Sales Invoice",
			self.post(self.on_account_sale(customer, total="100.000", cash="40.000"))["name"],
		)
		self.assertEqual(split.docstatus, 1)
		self.assertEqual(getdate(split.due_date), add_days(getdate(split.posting_date), 30))

		whole = frappe.get_doc(
			"Sales Invoice", self.post(self.on_account_sale(customer, total="60.000"))["name"]
		)
		self.assertEqual(whole.docstatus, 1)
		self.assertEqual(whole.outstanding_amount, 60.0)
		self.assertEqual(getdate(whole.due_date), add_days(getdate(whole.posting_date), 30))
