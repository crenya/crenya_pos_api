"""The bootstrap's `approvals` and the manager role at a retail till (audit P0 #5): the actions a
profile switches on, its discount limit, and the Crenya POS Manager role pulled to tills.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_bootstrap
"""

import frappe

from crenya_pos_api.api import device as device_api
from crenya_pos_api.sync.approvals import APPROVAL_ACTIONS, APPROVER_ROLE, approvals_bootstrap
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import SyncCase, new_id

MANAGER = "crenya.manager.test@example.com"


class TestApprovalsBootstrap(SyncCase):
	def set_profile(self, **values):
		saved = frappe.db.get_value("POS Profile", self.profile.name, list(values), as_dict=True)
		frappe.db.set_value("POS Profile", self.profile.name, values)
		frappe.db.commit()
		frappe.clear_document_cache("POS Profile", self.profile.name)

		def restore():
			frappe.db.set_value("POS Profile", self.profile.name, saved)
			frappe.db.commit()
			frappe.clear_document_cache("POS Profile", self.profile.name)

		self.addCleanup(restore)

	def approvals(self, device_id=None):
		return device_api.get_bootstrap(device_id=device_id or self.device_id).get("approvals")

	def test_role_and_fields_exist(self):
		self.assertTrue(frappe.db.exists("Role", APPROVER_ROLE))
		self.assertEqual(frappe.db.get_value("Role", APPROVER_ROLE, "desk_access"), 0)
		meta = frappe.get_meta("POS Profile")
		for _key, field, *_ in APPROVAL_ACTIONS:
			self.assertTrue(meta.has_field(field), field)
		self.assertTrue(meta.has_field("crenya_max_discount_percent"))

	def test_bootstrap_lists_required_actions_in_order(self):
		every = {field: 1 for _key, field, *_ in APPROVAL_ACTIONS}
		self.set_profile(**{**every, "crenya_approve_line_void": 0, "crenya_approve_settings": 0})
		answer = self.approvals()
		self.assertEqual(answer["approver_roles"], ["Crenya POS Manager"])
		self.assertEqual(
			answer["required"],
			[
				key
				for key, field, *_ in APPROVAL_ACTIONS
				if field not in ("crenya_approve_line_void", "crenya_approve_settings")
			],
		)

	def test_blank_max_discount_is_null(self):
		self.set_profile(crenya_max_discount_percent=0)
		self.assertIsNone(self.approvals()["max_discount_percent"])
		self.set_profile(crenya_max_discount_percent=10)
		self.assertEqual(self.approvals()["max_discount_percent"], "10")

	def test_only_tills_get_approvals(self):
		kds = new_id()
		device_api.register_device(
			device_id=kds, device_name="Kitchen", pos_profile=self.profile.name, device_type="kds"
		)
		self.assertNotIn("approvals", device_api.get_bootstrap(device_id=kds))

	def test_cashier_pull_sends_manager_role_to_till(self):
		if not frappe.db.exists("User", MANAGER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": MANAGER,
					"first_name": "Crenya Manager",
					"send_welcome_email": 0,
					"roles": [{"role": "Crenya POS User"}, {"role": APPROVER_ROLE}],
				}
			).insert(ignore_permissions=True)
			frappe.db.commit()
		cashier = self._cashier()
		records, _cursor, _pages = self._pull_all("cashier", limit=500)
		manager = next(row for row in records if row["name"] == MANAGER)
		self.assertEqual(manager["roles"], ["Crenya POS User", "Crenya POS Manager"])
		self.assertTrue(manager["enabled"])
		# a cashier holds Crenya POS User alone
		self.assertEqual(next(row for row in records if row["name"] == cashier)["roles"], ["Crenya POS User"])

	def test_patch_leaves_existing_profiles_without_approvals(self):
		"""An existing profile keeps today's behaviour: the patch switches every approval off, so
		an updated till asks for nothing new until the owner turns actions on."""
		from crenya_pos_api.patches.v0_11 import store_controls

		self.set_profile(**{field: 1 for _key, field, *_ in APPROVAL_ACTIONS})
		self.assertEqual(len(self.approvals()["required"]), len(APPROVAL_ACTIONS))
		store_controls.execute()
		store_controls.execute()  # idempotent
		frappe.clear_document_cache("POS Profile", self.profile.name)
		self.assertEqual(self.approvals()["required"], [])

	def test_new_profile_has_every_approval_on(self):
		"""A POS Profile created after this version starts with every approval on (the default)."""
		name = f"_Test Crenya POS New {new_id()[:8]}"
		created = fixtures.ensure_pos_profile(fixtures.ensure_company(), self.walk_in, name=name)
		self.addCleanup(frappe.delete_doc, "POS Profile", name, force=True, ignore_permissions=True)
		stored = frappe.db.get_value(
			"POS Profile", created.name, [f for _k, f, *_ in APPROVAL_ACTIONS], as_dict=True
		)
		self.assertEqual(set(stored.values()), {1})
		self.assertEqual(
			approvals_bootstrap(frappe.get_doc("POS Profile", created.name))["required"],
			[key for key, *_ in APPROVAL_ACTIONS],
		)
