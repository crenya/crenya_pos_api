import unittest
from unittest import mock

from crenya_pos_api import compat


class TestFrappeMajor(unittest.TestCase):
	def test_reads_the_major_number(self):
		for version, major in (("15.50.1", 15), ("16.0.0-dev", 16), ("17.1.0", 17)):
			with mock.patch.object(compat.frappe, "__version__", version):
				self.assertEqual(compat.frappe_major(), major)


class TestErpnextBeforeTests(unittest.TestCase):
	def test_v15_calls_the_erpnext_function_unchanged(self):
		with (
			mock.patch.object(compat, "frappe_major", return_value=15),
			mock.patch("erpnext.setup.utils.before_tests", create=True) as erp,
			mock.patch.object(compat.frappe, "clear_cache") as clear_cache,
		):
			compat.erpnext_before_tests()
		erp.assert_called_once_with()
		clear_cache.assert_not_called()

	def test_v16_runs_the_wizard_when_no_company(self):
		with (
			mock.patch.object(compat, "frappe_major", return_value=16),
			mock.patch.object(compat.frappe, "clear_cache"),
			mock.patch.object(compat.frappe, "db") as db,
			mock.patch("frappe.desk.page.setup_wizard.setup_wizard.setup_complete") as wizard,
			mock.patch("erpnext.setup.utils._enable_all_roles_for_admin") as roles,
			mock.patch("erpnext.setup.utils.set_defaults_for_tests") as defaults,
		):
			db.a_row_exists.return_value = False
			compat.erpnext_before_tests()
		wizard.assert_called_once()
		self.assertEqual(wizard.call_args.args[0]["company_name"], "Wind Power LLC")
		roles.assert_called_once_with()
		defaults.assert_called_once_with()
		db.commit.assert_called_once_with()

	def test_v16_skips_the_wizard_when_a_company_exists(self):
		with (
			mock.patch.object(compat, "frappe_major", return_value=16),
			mock.patch.object(compat.frappe, "clear_cache"),
			mock.patch.object(compat.frappe, "db") as db,
			mock.patch("frappe.desk.page.setup_wizard.setup_wizard.setup_complete") as wizard,
			mock.patch("erpnext.setup.utils._enable_all_roles_for_admin"),
			mock.patch("erpnext.setup.utils.set_defaults_for_tests"),
		):
			db.a_row_exists.return_value = True
			compat.erpnext_before_tests()
		wizard.assert_not_called()
