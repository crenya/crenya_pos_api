"""Shops web admin: outlet settings, setup checks and the list editors
(crenya_pos_api/api/web_admin/settings.py).

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_web_admin_settings
"""

import frappe

from crenya_pos_api.api import web_admin
from crenya_pos_api.sync.locale_data import DENOMINATION_DOCTYPE
from crenya_pos_api.sync.scale_rules import RULES_FIELD
from crenya_pos_api.sync.terminals import TERMINAL_DOCTYPE
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.web_admin_case import CASHIER, MANAGER, SHOP2, WebAdminCase


def fields_of(answer):
	return {field["fieldname"]: field for section in answer["sections"] for field in section["fields"]}


def checks_of(answer):
	return {row["key"]: row for row in answer["setup_checks"]}


class TestWebAdminSettings(WebAdminCase):
	def test_form_is_built_from_the_definitions(self):
		answer = web_admin.outlet_settings(self.profile.name)
		fields = fields_of(answer)
		for name in ("selling_price_list", "allow_discount_change", "crenya_allow_return_without_invoice"):
			self.assertIn(name, fields)
		self.assertIn("crenya_max_discount_percent", fields)
		self.assertIn("crenya_approve_no_sale", fields)
		self.assertEqual(fields["item_groups"]["value"], [fixtures.ITEM_GROUP_ROOT])
		self.assertTrue(fields["item_groups"]["read_only"])
		self.assertIn(fixtures.PRICE_LIST, fields["selling_price_list"]["linkOptions"])
		self.assertEqual(answer["reaches_tills"], "next_sync")
		self.assertTrue(answer["can_edit_owner_fields"])
		# money and retail checks in one list
		keys = set(checks_of(answer))
		self.assertTrue({"rounding_method", "price_list", "approver"} <= keys)
		self.assertNotIn("money_setup_checks", answer)

	def test_manager_saves_allow_discount_change(self):
		self.keep_profile(self.profile.name, "allow_discount_change")
		self.as_user(MANAGER)
		answer = web_admin.outlet_settings(self.profile.name)
		self.assertFalse(answer["can_edit_owner_fields"])
		self.assertTrue(fields_of(answer)["taxes_and_charges"]["read_only"])
		before = fields_of(answer)["allow_discount_change"]["value"]
		saved = web_admin.save_outlet_settings(
			self.profile.name, {"allow_discount_change": 0 if before else 1}, answer["modified"]
		)
		self.assertEqual(fields_of(saved)["allow_discount_change"]["value"], 0 if before else 1)
		self.assertNotEqual(saved["modified"], answer["modified"])
		self.assertTrue(
			frappe.get_all(
				"Comment",
				filters={
					"reference_name": self.profile.name,
					"content": ["like", f"%app.crenya.com by {MANAGER}%"],
				},
			)
		)

	def test_manager_refused_owner_only_tax_template(self):
		self.as_user(MANAGER)
		answer = web_admin.outlet_settings(self.profile.name)
		with self.assertRaises(frappe.PermissionError):
			web_admin.save_outlet_settings(
				self.profile.name, {"taxes_and_charges": fixtures.ZERO_SALES_TEMPLATE}, answer["modified"]
			)

	def test_read_only_and_unknown_fields_refused(self):
		answer = web_admin.outlet_settings(self.profile.name)
		for values in ({"item_groups": []}, {"company": "x"}, {"allow_discount_change": "yes"}):
			with self.assertRaises(frappe.ValidationError, msg=values):
				web_admin.save_outlet_settings(self.profile.name, values, answer["modified"])

	def test_stale_modified_refused(self):
		with self.assertRaises(frappe.TimestampMismatchError):
			web_admin.save_outlet_settings(
				self.profile.name, {"allow_rate_change": 1}, "2001-01-01 00:00:00.000000"
			)

	def test_cashier_refused(self):
		self.as_user(CASHIER)
		for call in (
			lambda: web_admin.outlet_settings(self.profile.name),
			lambda: web_admin.scale_rules(self.profile.name),
			lambda: web_admin.denominations(self.profile.name),
			lambda: web_admin.terminals(self.profile.name),
			lambda: web_admin.company_wording(self.profile.name),
		):
			with self.assertRaises(frappe.PermissionError):
				call()

	def test_setup_checks_missing_approver(self):
		# shop 2 lists a cashier with a PIN and a manager without one, and requires approvals
		checks = checks_of(web_admin.outlet_settings(SHOP2))
		self.assertEqual(checks["cashiers"]["status"], "ok")
		self.assertEqual(checks["approver"]["status"], "missing")
		self.assertTrue(checks["approver"]["fix"])
		self.assertEqual(checks["price_list"]["status"], "ok")
		self.assertEqual(checks["warehouse"]["status"], "ok")

	def test_scale_rule_bad_prefix_refused(self):
		self.addCleanup(self._clear_scale_rules)
		self.as_user(MANAGER)
		answer = web_admin.scale_rules(self.profile.name)
		bad = [{"prefix": "2a", "plu_length": 5, "value_type": "weight", "value_decimals": 3}]
		with self.assertRaises(frappe.ValidationError):
			web_admin.save_scale_rules(self.profile.name, bad, answer["modified"])
		overlap = [
			{"prefix": "2", "plu_length": 5, "value_type": "weight", "value_decimals": 3},
			{"prefix": "21", "plu_length": 5, "value_type": "price", "value_decimals": None},
		]
		with self.assertRaises(frappe.ValidationError):
			web_admin.save_scale_rules(self.profile.name, overlap, answer["modified"])
		good = [{"prefix": "21", "plu_length": 5, "value_type": "price", "value_decimals": 3}]
		saved = web_admin.save_scale_rules(self.profile.name, good, answer["modified"])
		self.assertEqual(saved["rows"], good)
		with self.assertRaises(frappe.TimestampMismatchError):
			web_admin.save_scale_rules(self.profile.name, [], answer["modified"])

	def _clear_scale_rules(self):
		frappe.db.delete(
			"Crenya POS Scale Barcode Rule", {"parent": self.profile.name, "parentfield": RULES_FIELD}
		)
		frappe.clear_document_cache("POS Profile", self.profile.name)
		frappe.db.commit()

	def test_denominations_system_manager_only_at_currency_precision(self):
		self.addCleanup(self._delete_denominations)
		self.as_user(MANAGER)
		self.assertFalse(web_admin.denominations(self.profile.name)["can_edit"])
		row = {"value": "0.005", "label": "_TC 5 baisa", "kind": "coin", "enabled": 1}
		with self.assertRaises(frappe.PermissionError):
			web_admin.save_denominations(self.profile.name, [row])
		self.as_user("Administrator")
		with self.assertRaises(frappe.ValidationError):
			web_admin.save_denominations(self.profile.name, [{**row, "value": "0.0005"}])
		answer = web_admin.save_denominations(self.profile.name, [row])
		self.assertEqual(answer["currency"], fixtures.CURRENCY)
		saved = next(r for r in answer["rows"] if r["label"] == "_TC 5 baisa")
		self.assertEqual(saved["value"], "0.005")
		answer = web_admin.save_denominations(self.profile.name, [{**saved, "enabled": 0}])
		self.assertEqual(next(r for r in answer["rows"] if r["name"] == saved["name"])["enabled"], 0)

	def _delete_denominations(self):
		frappe.db.delete(DENOMINATION_DOCTYPE, {"label": ["like", "_TC %"]})
		frappe.db.commit()

	def test_terminals_outlet_ones_for_managers_secret_never_answered(self):
		self.addCleanup(self._delete_terminals)
		self.as_user(MANAGER)
		terminal = {
			"label": "_TC Web UPI",
			"provider": "UPI QR",
			"pos_profile": self.profile.name,
			"mode_of_payment": fixtures.CASH,
			"upi_vpa": "shop@okbank",
			"upi_payee_name": "Shop",
			"enabled": 1,
		}
		answer = web_admin.save_terminal(self.profile.name, terminal)
		row = next(r for r in answer["rows"] if r["label"] == "_TC Web UPI")
		self.assertEqual(row["company"], fixtures.COMPANY)
		self.assertTrue(row["can_edit"])
		self.assertNotIn("security_token", row)
		self.assertIn("UPI QR", answer["choices"]["providers"])
		# a terminal of every outlet of the company is a System Manager's
		with self.assertRaises(frappe.PermissionError):
			web_admin.save_terminal(
				self.profile.name, {**terminal, "label": "_TC Shared", "pos_profile": None}
			)
		# and one of another outlet is not this outlet's
		with self.assertRaises(frappe.PermissionError):
			web_admin.save_terminal(self.profile.name, {**terminal, "pos_profile": SHOP2})
		changed = web_admin.save_terminal(
			self.profile.name, {"name": row["name"], "modified": row["modified"], "upi_payee_name": "Shop 1"}
		)
		self.assertEqual(
			next(r for r in changed["rows"] if r["name"] == row["name"])["upi_payee_name"], "Shop 1"
		)

	def _delete_terminals(self):
		frappe.set_user("Administrator")
		for name in frappe.get_all(TERMINAL_DOCTYPE, filters={"label": ["like", "_TC %"]}, pluck="name"):
			frappe.delete_doc(TERMINAL_DOCTYPE, name, ignore_permissions=True, force=True)
		frappe.db.commit()

	def test_company_wording_system_manager_only(self):
		self.as_user(MANAGER)
		answer = web_admin.company_wording(self.profile.name)
		fields = fields_of(answer)
		self.assertIn("crenya_receipt_title_ar", fields)
		self.assertTrue(all(field["read_only"] for field in fields.values()))
		self.assertNotIn("setup_checks", answer)
		with self.assertRaises(frappe.PermissionError):
			web_admin.save_company_wording(self.profile.name, {"crenya_tax_name": "VAT"}, answer["modified"])
		self.as_user("Administrator")
		answer = web_admin.company_wording(self.profile.name)
		before = fields_of(answer)["crenya_tax_name"]["value"]
		self.addCleanup(lambda: frappe.db.set_value("Company", fixtures.COMPANY, "crenya_tax_name", before))
		saved = web_admin.save_company_wording(
			self.profile.name, {"crenya_tax_name": "_TC VAT"}, answer["modified"]
		)
		self.assertEqual(fields_of(saved)["crenya_tax_name"]["value"], "_TC VAT")
