import unittest
from unittest.mock import patch

import frappe

from crenya_pos_api.utils.session import elevated_as_administrator


def _fake_set_user(user):
	# what frappe.set_user does to the request state, without a database
	frappe.local.session = frappe._dict(user=user, sid="Guest", data=frappe._dict())
	frappe.local.form_dict = frappe._dict()


class TestElevatedAsAdministrator(unittest.TestCase):
	def setUp(self):
		# put back the request state the run had: later unit tests read frappe.session.user
		# (TestStampCreditChange counts on Administrator)
		for name in ("session", "form_dict"):
			saved = getattr(frappe.local, name, None)
			self.addCleanup(setattr, frappe.local, name, saved)
		self.data = frappe._dict(user="manager@x", csrf_token="t")
		self.form = frappe._dict(cmd="x")
		frappe.local.session = frappe._dict(sid="real-sid", user="manager@x", data=self.data)
		frappe.local.form_dict = self.form
		p = patch.object(frappe, "set_user", _fake_set_user)
		p.start()
		self.addCleanup(p.stop)

	def _check_restored(self):
		session = frappe.local.session
		self.assertEqual(session.user, "manager@x")
		self.assertEqual(session.sid, "real-sid")
		self.assertIs(session.data, self.data)
		self.assertEqual(dict(self.data), {"user": "manager@x", "csrf_token": "t"})
		self.assertIs(frappe.local.form_dict, self.form)

	def test_restores_after_block(self):
		with elevated_as_administrator():
			self.assertEqual(frappe.local.session.user, "Administrator")
		self._check_restored()

	def test_restores_when_body_raises(self):
		with self.assertRaises(ValueError):
			with elevated_as_administrator():
				raise ValueError("boom")
		self._check_restored()

	def test_restores_mutated_data_contents(self):
		with elevated_as_administrator():
			self.data["csrf_token"] = "changed"
			self.data.clear()
		self._check_restored()
