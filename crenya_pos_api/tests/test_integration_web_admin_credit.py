"""Shops web admin: credit customers, the credit limit edit and the Credit sales / Expenses settings
(crenya_pos_api/api/web_admin/credit.py, settings.py), retail P1 task 12, and the rule that every
Shops web-admin method needs a browser session. Restaurant credit 0.14.0 (Task 4): the credit pages
are product-aware (a test "restaurant" product stands in for crenya_restaurant's), and a shop's
answers are 0.13.2's plus the new keys only.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_web_admin_credit
"""

import inspect
from decimal import Decimal
from unittest import mock

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from crenya_pos_api.api import web_admin
from crenya_pos_api.api.web_admin import credit as web_credit
from crenya_pos_api.api.web_admin import scope
from crenya_pos_api.expenses import booking
from crenya_pos_api.sync.approvals import APPROVER_ROLE
from crenya_pos_api.sync.context import POS_USER_ROLE, profile_users
from crenya_pos_api.sync.credit import CREDIT_ENABLED_FIELD, CREDIT_SALES_FIELD
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.sync_case import new_id
from crenya_pos_api.tests.test_integration_credit import CreditCase, make_customer
from crenya_pos_api.tests.web_admin_case import (
	CASHIER,
	MANAGER,
	SHOP2,
	SHOP2_CASHIER,
	SHOP2_MANAGER,
	ensure_shop2,
	ensure_user,
)
from crenya_pos_api.utils.dates import format_db_datetime

ACCOUNTS = "_test_crenya_accounts_manager@example.com"

# a restaurant product as crenya_restaurant registers it (Task 22): its outlets are the POS Profiles
# in restaurant mode, managed by a Restaurant Manager the outlet lists (or every one when it lists
# nobody)
RESTAURANT_MANAGER_ROLE = "Restaurant Manager"
RESTAURANT_A = "_Test Crenya Credit Restaurant A"
RESTAURANT_B = "_Test Crenya Credit Restaurant B"
REST_MANAGER = "crenya.rest.manager@example.com"
REST_MANAGER_B = "crenya.rest.manager.b@example.com"
# a Bank-type mode of payment both restaurants take (beside Cash)
SETTLE_BANK = "_Test Crenya Settle Bank"


def restaurant_outlets_of(outlet=None):
	filters = {scope.RESTAURANT_MODE_FIELD: 1}
	if outlet:
		filters["name"] = outlet
	return frappe.get_all("POS Profile", filters=filters, fields=["name", "company"], order_by="name asc")


def manages_test_restaurant(roles, user, profile):
	if "System Manager" in roles:
		return True
	users = set(profile_users(profile))
	return RESTAURANT_MANAGER_ROLE in roles and (not users or user in users)


TEST_RESTAURANT = web_credit.CreditProduct(
	key="restaurant", outlets=restaurant_outlets_of, manages=manages_test_restaurant
)


def ensure_restaurant(name, users, walk_in):
	"""A restaurant outlet of the test company (crenya_restaurant's field set) taking Cash and
	SETTLE_BANK, whose Applicable for Users lists `users`."""
	company = frappe.get_doc("Company", fixtures.COMPANY)
	profile = fixtures.ensure_pos_profile(
		company,
		walk_in,
		name=name,
		payments=[{"mode_of_payment": fixtures.CASH, "default": 1}, {"mode_of_payment": SETTLE_BANK}],
	)
	if {row.user for row in profile.applicable_for_users} != set(users):
		profile.set("applicable_for_users", [{"user": user} for user in users])
		profile.save(ignore_permissions=True)
	frappe.db.set_value("POS Profile", name, scope.RESTAURANT_MODE_FIELD, 1, update_modified=False)
	frappe.clear_document_cache("POS Profile", name)
	return name


def setup_restaurant_credit(walk_in):
	"""crenya_restaurant's POS Profile field, its manager role and users, the bank mode and the two
	restaurants (A lists REST_MANAGER, B lists REST_MANAGER_B); the test restaurant product
	registered. Answers the product it replaced (None), for `restore_restaurant_product`."""
	create_custom_fields(
		{
			"POS Profile": [
				{
					"fieldname": scope.RESTAURANT_MODE_FIELD,
					"fieldtype": "Check",
					"label": "Restaurant mode",
					"insert_after": "company",
				}
			]
		},
		ignore_validate=True,
	)
	if not frappe.db.exists("Role", RESTAURANT_MANAGER_ROLE):
		frappe.get_doc({"doctype": "Role", "role_name": RESTAURANT_MANAGER_ROLE, "desk_access": 1}).insert(
			ignore_permissions=True
		)
	ensure_user(MANAGER, [POS_USER_ROLE, APPROVER_ROLE])
	ensure_user(CASHIER, [POS_USER_ROLE])
	ensure_user(ACCOUNTS, [POS_USER_ROLE, "Accounts Manager"])
	ensure_user(REST_MANAGER, [POS_USER_ROLE, RESTAURANT_MANAGER_ROLE])
	ensure_user(REST_MANAGER_B, [POS_USER_ROLE, RESTAURANT_MANAGER_ROLE])
	fixtures.ensure_mode_of_payment(SETTLE_BANK, "Bank", frappe.get_doc("Company", fixtures.COMPANY))
	ensure_restaurant(RESTAURANT_A, [REST_MANAGER], walk_in)
	ensure_restaurant(RESTAURANT_B, [REST_MANAGER_B], walk_in)
	frappe.db.commit()  # nosemgrep
	previous = web_credit.credit_product("restaurant")
	web_credit.register_credit_product(TEST_RESTAURANT)
	return previous


def restore_restaurant_product(previous):
	if previous is None:
		web_credit._PRODUCTS.pop("restaurant", None)
	else:
		web_credit.register_credit_product(previous)


def credit_limit_rows(customer):
	return frappe.get_all(
		"Customer Credit Limit",
		filters={"parent": customer, "parenttype": "Customer", "company": fixtures.COMPANY},
		fields=["credit_limit", "bypass_credit_limit_check"],
	)


def rows_by_customer(answer):
	return {row["customer"]: row for row in answer["rows"]}


class WebCreditCase(CreditCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_user(MANAGER, [POS_USER_ROLE, APPROVER_ROLE])
		ensure_user(CASHIER, [POS_USER_ROLE])
		# an accounts manager who is no till manager
		ensure_user(ACCOUNTS, [POS_USER_ROLE, "Accounts Manager"])
		ensure_user(SHOP2_CASHIER, [POS_USER_ROLE])
		ensure_user(SHOP2_MANAGER, [POS_USER_ROLE, APPROVER_ROLE])
		ensure_shop2([SHOP2_CASHIER, SHOP2_MANAGER])
		frappe.db.commit()  # nosemgrep

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def sell(self, customer, total):
		return self.post(self.on_account_sale(customer, total=total))["name"]

	def as_manager(self):
		frappe.set_user(MANAGER)

	def row_of(self, customer, user=None):
		frappe.set_user(user or "Administrator")
		return rows_by_customer(web_admin.credit_customers(self.profile.name))[customer]


class TestCreditCustomers(WebCreditCase):
	def test_credit_customers_flags_over_limit(self):
		over = make_customer("webover", limit=100)
		under = make_customer("webunder", limit=500)
		free = make_customer("webfree", limit=None)
		idle = make_customer("webidle", limit=250)
		for customer, total in ((over, "150.000"), (under, "120.000"), (free, "80.000")):
			self.sell(customer, total)
		self.sell(under, "30.000")
		self.as_manager()
		answer = web_admin.credit_customers(self.profile.name)
		self.assertEqual(answer["outlet"], self.profile.name)
		self.assertEqual(answer["currency"]["precision"], 3)
		# a shop manager reads; only a System Manager or an Accounts Manager changes
		self.assertIs(answer["can_edit"], False)
		rows = rows_by_customer(answer)
		self.assertEqual(
			(rows[over]["credit_limit"], rows[over]["outstanding"], rows[over]["available"]),
			("100.000", "150.000", "0.000"),
		)
		self.assertIs(rows[over]["over_limit"], True)
		self.assertEqual(rows[over]["limit_source"], "customer")
		self.assertEqual(
			(rows[under]["outstanding"], rows[under]["available"], rows[under]["open_invoices"]),
			("150.000", "350.000", 2),
		)
		self.assertIs(rows[under]["over_limit"], False)
		# no limit: "No limit" on the web, with no source
		self.assertEqual(
			(rows[free]["credit_limit"], rows[free]["available"], rows[free]["over_limit"]),
			(None, None, False),
		)
		self.assertIsNone(rows[free]["limit_source"])
		# a limit and no balance is listed too
		self.assertEqual((rows[idle]["credit_limit"], rows[idle]["outstanding"]), ("250.000", "0.000"))
		# over the limit first
		names = [row["customer"] for row in answer["rows"]]
		self.assertLess(names.index(over), names.index(under))
		self.assertLess(names.index(over), names.index(free))
		self.assertIs(web_admin.credit_customers(self.profile.name)["more"], False)

	def test_the_source_of_an_inherited_limit_is_named(self):
		group_customer = make_customer("websrcgroup", limit=None)
		company_customer = make_customer("websrccompany", limit=None)
		self.sell(group_customer, "10.000")
		self.sell(company_customer, "10.000")
		group = frappe.get_doc(
			"Customer Group", frappe.db.get_value("Customer", group_customer, "customer_group")
		)
		saved_rows = [row.as_dict() for row in group.credit_limits]
		saved_company = frappe.db.get_value("Company", fixtures.COMPANY, "credit_limit")
		try:
			group.set("credit_limits", [{"company": fixtures.COMPANY, "credit_limit": 300}])
			group.save(ignore_permissions=True)
			self.as_manager()
			row = self.row_of(group_customer, MANAGER)
			self.assertEqual((row["credit_limit"], row["limit_source"]), ("300.000", "group"))
			frappe.set_user("Administrator")
			group.reload()
			group.set("credit_limits", [])
			group.save(ignore_permissions=True)
			frappe.db.set_value("Company", fixtures.COMPANY, "credit_limit", 900)
			frappe.clear_document_cache("Company", fixtures.COMPANY)
			row = self.row_of(company_customer, MANAGER)
			self.assertEqual((row["credit_limit"], row["limit_source"]), ("900.000", "company"))
		finally:
			frappe.set_user("Administrator")
			group.reload()
			group.set("credit_limits", [])
			for old in saved_rows:
				group.append(
					"credit_limits", {"company": old["company"], "credit_limit": old["credit_limit"]}
				)
			group.save(ignore_permissions=True)
			frappe.db.set_value("Company", fixtures.COMPANY, "credit_limit", saved_company or 0)
			frappe.clear_document_cache("Company", fixtures.COMPANY)
			frappe.db.commit()  # nosemgrep

	def test_cashier_and_other_shops_manager_refused(self):
		frappe.set_user(CASHIER)
		with self.assertRaises(frappe.PermissionError):
			web_admin.credit_customers(self.profile.name)
		# shop 2 lists its own users: this shop's manager does not manage it
		self.as_manager()
		with self.assertRaises(frappe.PermissionError):
			web_admin.credit_customers(SHOP2)

	def test_the_users_company_permission_is_honoured(self):
		def allow(company):
			doc = frappe.get_doc(
				{"doctype": "User Permission", "user": MANAGER, "allow": "Company", "for_value": company}
			)
			doc.flags.ignore_links = True
			doc.insert(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep

		allow("_Test Crenya Other Company")
		try:
			self.as_manager()
			with self.assertRaises(frappe.PermissionError):
				web_admin.credit_customers(self.profile.name)
			with self.assertRaises(frappe.PermissionError):
				web_admin.expenses_not_booked(self.profile.name)
			# not among the companies listed: no shop's expenses either
			self.assertEqual(web_admin.expenses_not_booked()["rows"], [])
			frappe.set_user("Administrator")
			allow(fixtures.COMPANY)
			self.as_manager()
			self.assertEqual(web_admin.credit_customers(self.profile.name)["outlet"], self.profile.name)
		finally:
			frappe.set_user("Administrator")
			for name in frappe.get_all(
				"User Permission", filters={"user": MANAGER, "allow": "Company"}, pluck="name"
			):
				frappe.delete_doc("User Permission", name, ignore_permissions=True, force=True)
			frappe.db.commit()  # nosemgrep


class TestCreditPageForAccountants(WebCreditCase):
	"""A System Manager or an Accounts Manager opens the Credit page of the shops of the companies they
	may use, without being a Crenya POS Manager of the shop; nothing else of the shop opens to them."""

	def allow_company(self, user, company):
		doc = frappe.get_doc(
			{"doctype": "User Permission", "user": user, "allow": "Company", "for_value": company}
		)
		doc.flags.ignore_links = True
		doc.insert(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep
		self.addCleanup(self.drop_permissions, user)

	def drop_permissions(self, user):
		frappe.set_user("Administrator")
		for name in frappe.get_all(
			"User Permission", filters={"user": user, "allow": "Company"}, pluck="name"
		):
			frappe.delete_doc("User Permission", name, ignore_permissions=True, force=True)
		frappe.db.commit()  # nosemgrep

	def test_an_accounts_manager_reads_credit_without_being_a_till_manager(self):
		customer = make_customer("acctread", limit=100)
		self.sell(customer, "150.000")
		frappe.set_user(ACCOUNTS)
		self.assertNotIn(APPROVER_ROLE, frappe.get_roles())
		answer = web_admin.credit_customers(self.profile.name)
		self.assertEqual(answer["outlet"], self.profile.name)
		self.assertIs(answer["can_edit"], True)
		row = rows_by_customer(answer)[customer]
		self.assertEqual(
			(row["credit_limit"], row["outstanding"], row["over_limit"]), ("100.000", "150.000", True)
		)
		# the lists a shop manager opens stay closed: manage() still refuses
		for call in (
			lambda: web_admin.outlet_settings(self.profile.name),
			lambda: web_admin.devices(self.profile.name),
			lambda: web_admin.refused_sales(self.profile.name),
			lambda: web_admin.expenses_not_booked(self.profile.name),
			lambda: web_admin.day_closes("2026-10-01", "2026-10-02", self.profile.name),
			lambda: web_admin.terminals(self.profile.name),
		):
			with self.assertRaises(frappe.PermissionError):
				call()
		self.assertEqual(web_admin.outlets()["outlets"], [])

	def test_the_company_is_the_boundary(self):
		self.allow_company(ACCOUNTS, "_Test Crenya Other Company")
		frappe.set_user(ACCOUNTS)
		with self.assertRaises(frappe.PermissionError):
			web_admin.credit_customers(self.profile.name)
		self.assertEqual(web_admin.credit_shops(), {"shops": []})
		frappe.set_user("Administrator")
		self.allow_company(ACCOUNTS, fixtures.COMPANY)
		frappe.set_user(ACCOUNTS)
		self.assertEqual(web_admin.credit_customers(self.profile.name)["outlet"], self.profile.name)
		self.assertIn(self.profile.name, [shop["name"] for shop in web_admin.credit_shops()["shops"]])

	def test_credit_shops_lists_what_each_user_may_open(self):
		names = lambda answer: [shop["name"] for shop in answer["shops"]]  # noqa: E731
		frappe.set_user(ACCOUNTS)
		answer = web_admin.credit_shops()
		self.assertLessEqual({self.profile.name, SHOP2}, set(names(answer)))
		shop = next(s for s in answer["shops"] if s["name"] == self.profile.name)
		self.assertEqual(
			(shop["credit_view"], shop["can_edit"], shop["company"], shop["currency"]),
			(True, True, fixtures.COMPANY, fixtures.CURRENCY),
		)
		self.assertIsInstance(shop["credit_enabled"], bool)
		self.assertEqual(shop["outlet_name"], self.profile.name)
		# a shop manager: the shops they manage, read only (shop 2 lists its own users)
		self.as_manager()
		answer = web_admin.credit_shops()
		self.assertIn(self.profile.name, names(answer))
		self.assertNotIn(SHOP2, names(answer))
		self.assertTrue(all(s["can_edit"] is False for s in answer["shops"]))
		# a System Manager sees them all and edits
		frappe.set_user("Administrator")
		answer = web_admin.credit_shops()
		self.assertLessEqual({self.profile.name, SHOP2}, set(names(answer)))
		self.assertTrue(all(s["can_edit"] for s in answer["shops"]))
		# a cashier none
		frappe.set_user(CASHIER)
		self.assertEqual(web_admin.credit_shops(), {"shops": []})
		with self.assertRaises(frappe.PermissionError):
			web_admin.credit_customers(self.profile.name)

	def test_an_accounts_manager_reads_loyalty_only_for_a_company_they_may_use(self):
		self.allow_company(ACCOUNTS, "_Test Crenya Other Company")
		frappe.set_user(ACCOUNTS)
		with self.assertRaises(frappe.PermissionError):
			web_admin.loyalty_programs(self.profile.name)
		frappe.set_user("Administrator")
		self.allow_company(ACCOUNTS, fixtures.COMPANY)
		frappe.set_user(ACCOUNTS)
		self.assertIn("rows", web_admin.loyalty_programs(self.profile.name))

	def test_a_restaurant_outlet_is_not_a_shop(self):
		frappe.set_user(ACCOUNTS)
		with self.assertRaises(frappe.PermissionError):
			web_admin.credit_customers("No such outlet")
		with self.assertRaises(frappe.PermissionError):
			web_admin.credit_customers("")

	def test_credit_shops_needs_a_session(self):
		frappe.set_user(ACCOUNTS)
		with mock.patch("crenya_pos_api.web.context.request_uses_api_key", return_value=True):
			with self.assertRaises(frappe.PermissionError):
				web_admin.credit_shops()


class TestEveryShopsMethodNeedsASession(WebCreditCase):
	def dummy_arguments(self, function):
		kwargs = {}
		for name, parameter in inspect.signature(function).parameters.items():
			if parameter.default is not inspect.Parameter.empty:
				continue
			annotation = parameter.annotation
			if name == "outlet":
				kwargs[name] = self.profile.name
			elif annotation is dict:
				kwargs[name] = {}
			elif annotation is list:
				kwargs[name] = []
			elif annotation is bool:
				kwargs[name] = False
			elif annotation is int:
				kwargs[name] = 1
			else:
				kwargs[name] = "x"
		return kwargs

	def test_the_loop_covers_every_whitelisted_shops_method(self):
		"""`__all__` is kept by hand: a whitelisted method of a web_admin module that is left out of it
		would escape the loop below. The set Frappe registered must be exactly `__all__`."""
		registered = {
			function.__name__
			for function in frappe.whitelisted
			if getattr(function, "__module__", "").startswith("crenya_pos_api.api.web_admin.")
		}
		self.assertEqual(registered, set(web_admin.__all__))
		for name in web_admin.__all__:
			self.assertTrue(callable(getattr(web_admin, name)), name)

	def test_an_api_key_reaches_no_shops_method(self):
		customer = make_customer("webloop", limit=100)
		names = sorted(web_admin.__all__)
		self.assertGreater(len(names), 30)
		for user in (MANAGER, "Administrator"):
			frappe.set_user(user)
			with mock.patch("crenya_pos_api.web.context.request_uses_api_key", return_value=True):
				for name in names:
					function = getattr(web_admin, name)
					with self.assertRaises(frappe.PermissionError, msg=f"{name} as {user}"):
						function(**self.dummy_arguments(function))
		self.assertEqual(credit_limit_rows(customer)[0]["credit_limit"], 100)

	def test_a_browser_session_still_reaches_them(self):
		self.as_manager()
		self.assertEqual(web_admin.credit_customers(self.profile.name)["outlet"], self.profile.name)
		self.assertTrue(web_admin.outlets()["outlets"])
		self.assertEqual(web_admin.outlet_settings(self.profile.name)["reaches_tills"], "next_sync")


class TestSaveCreditLimit(WebCreditCase):
	def modified(self, customer, user="Administrator"):
		return self.row_of(customer, user)["modified"]

	def test_a_shop_manager_cannot_change_a_limit(self):
		customer = make_customer("webmgr", limit=75)
		modified = self.modified(customer)
		self.as_manager()
		with self.assertRaises(frappe.PermissionError):
			web_admin.save_credit_limit(self.profile.name, customer, "5000", modified)
		self.assertEqual(credit_limit_rows(customer)[0]["credit_limit"], 75)

	def test_save_credit_limit_writes_company_row_and_comments(self):
		customer = make_customer("webset", limit=None)
		self.sell(customer, "40.000")
		modified = self.modified(customer)
		self.assertEqual(credit_limit_rows(customer), [])
		frappe.set_user("Administrator")
		row = web_admin.save_credit_limit(self.profile.name, customer, "250.500", modified)
		self.assertEqual(
			[Decimal(str(r["credit_limit"])) for r in credit_limit_rows(customer)], [Decimal("250.5")]
		)
		self.assertEqual(
			(row["customer"], row["credit_limit"], row["limit_source"], row["outstanding"], row["available"]),
			(customer, "250.500", "customer", "40.000", "210.500"),
		)
		comments = frappe.get_all(
			"Comment",
			filters={
				"reference_doctype": "Customer",
				"reference_name": customer,
				"content": ["like", "%app.crenya.com by Administrator%"],
			},
			pluck="content",
		)
		self.assertTrue(comments)
		self.assertIn("from no limit of its own to 250.500", comments[0])
		again = web_admin.save_credit_limit(self.profile.name, customer, "100", row["modified"])
		self.assertEqual(again["credit_limit"], "100.000")
		self.assertEqual(len(credit_limit_rows(customer)), 1)
		cleared = web_admin.save_credit_limit(self.profile.name, customer, "0", again["modified"])
		self.assertEqual(credit_limit_rows(customer), [])
		self.assertEqual(
			(cleared["credit_limit"], cleared["available"], cleared["limit_source"]), (None, None, None)
		)
		last = frappe.get_all(
			"Comment",
			filters={
				"reference_name": customer,
				"content": ["like", "%from 100.000 to no limit of its own%"],
			},
		)
		self.assertTrue(last)

	def test_an_accounts_manager_may_change_a_limit(self):
		customer = make_customer("webaccounts", limit=75)
		modified = self.modified(customer)
		frappe.set_user(ACCOUNTS)
		row = web_admin.save_credit_limit(self.profile.name, customer, "90", modified)
		self.assertEqual(row["credit_limit"], "90.000")
		# and opens the Credit page without being a till manager
		self.assertEqual(web_admin.credit_customers(self.profile.name)["can_edit"], True)

	def test_clearing_an_own_limit_shows_the_group_limit_that_applies(self):
		customer = make_customer("webclear", limit=75)
		group = frappe.get_doc("Customer Group", frappe.db.get_value("Customer", customer, "customer_group"))
		saved = [row.as_dict() for row in group.credit_limits]
		try:
			group.set("credit_limits", [{"company": fixtures.COMPANY, "credit_limit": 400}])
			group.save(ignore_permissions=True)
			row = web_admin.save_credit_limit(self.profile.name, customer, "", self.modified(customer))
			self.assertEqual(credit_limit_rows(customer), [])
			# not "no limit": the group's applies, and the answer says so
			self.assertEqual((row["credit_limit"], row["limit_source"]), ("400.000", "group"))
		finally:
			group.reload()
			group.set("credit_limits", [])
			for old in saved:
				group.append(
					"credit_limits", {"company": old["company"], "credit_limit": old["credit_limit"]}
				)
			group.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep

	def test_a_bypass_flag_on_the_row_is_kept(self):
		customer = make_customer("webbypass", limit=75)
		doc = frappe.get_doc("Customer", customer)
		doc.credit_limits[0].bypass_credit_limit_check = 1
		doc.save(ignore_permissions=True)
		modified = self.modified(customer)
		row = web_admin.save_credit_limit(self.profile.name, customer, "", modified)
		rows = credit_limit_rows(customer)
		self.assertEqual([(r["credit_limit"], r["bypass_credit_limit_check"]) for r in rows], [(0, 1)])
		self.assertIsNone(row["credit_limit"])
		changed = web_admin.save_credit_limit(self.profile.name, customer, "60", row["modified"])
		self.assertEqual(changed["credit_limit"], "60.000")
		self.assertEqual(credit_limit_rows(customer)[0]["bypass_credit_limit_check"], 1)

	def test_blank_is_no_limit_of_its_own(self):
		customer = make_customer("webblank", limit=75)
		row = web_admin.save_credit_limit(self.profile.name, customer, "", self.modified(customer))
		self.assertIsNone(row["credit_limit"])
		self.assertEqual(credit_limit_rows(customer), [])

	def test_save_credit_limit_needs_web_session(self):
		customer = make_customer("websession", limit=75)
		modified = self.modified(customer)
		with mock.patch("crenya_pos_api.web.context.request_uses_api_key", return_value=True):
			with self.assertRaises(frappe.PermissionError):
				web_admin.save_credit_limit(self.profile.name, customer, "10", modified)
		self.assertEqual(credit_limit_rows(customer)[0]["credit_limit"], 75)

	def test_save_credit_limit_stale_modified_refused(self):
		customer = make_customer("webstale", limit=75)
		with self.assertRaises(frappe.TimestampMismatchError):
			web_admin.save_credit_limit(self.profile.name, customer, "10", "2001-01-01 00:00:00.000000")
		self.assertEqual(credit_limit_rows(customer)[0]["credit_limit"], 75)

	def test_credit_limit_precision_and_size(self):
		customer = make_customer("webprec", limit=75)
		modified = self.modified(customer)
		for value in ("10.0001", "-1", "ten", "1000000000000"):
			with self.assertRaises(frappe.ValidationError, msg=value):
				web_admin.save_credit_limit(self.profile.name, customer, value, modified)
		row = web_admin.save_credit_limit(self.profile.name, customer, "10.125", modified)
		self.assertEqual(row["credit_limit"], "10.125")

	def test_only_a_customer_of_the_shop(self):
		frappe.set_user("Administrator")
		outside = fixtures.make_customer(f"_TC web outside {new_id()[:6]}")
		other_group = "_Test Crenya Other Web Customers"
		if not frappe.db.exists("Customer Group", other_group):
			from frappe.utils.nestedset import get_root_of

			fixtures._insert(
				{
					"doctype": "Customer Group",
					"customer_group_name": other_group,
					"parent_customer_group": get_root_of("Customer Group"),
				}
			)
		saved = [row.customer_group for row in self.profile.customer_groups]
		profile = frappe.get_doc("POS Profile", self.profile.name)
		profile.set("customer_groups", [{"customer_group": other_group}])
		profile.flags.ignore_validate = True
		profile.save(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep
		frappe.clear_document_cache("POS Profile", self.profile.name)
		try:
			with self.assertRaises(frappe.PermissionError):
				web_admin.save_credit_limit(
					self.profile.name,
					outside,
					"10",
					format_db_datetime(frappe.db.get_value("Customer", outside, "modified")),
				)
		finally:
			profile = frappe.get_doc("POS Profile", self.profile.name)
			profile.set("customer_groups", [{"customer_group": g} for g in saved])
			profile.flags.ignore_validate = True
			profile.save(ignore_permissions=True)
			frappe.db.commit()  # nosemgrep
			frappe.clear_document_cache("POS Profile", self.profile.name)


class TestCreditAndExpenseSettings(WebCreditCase):
	def fields(self, answer):
		return {f["fieldname"]: f for section in answer["sections"] for f in section["fields"]}

	def test_settings_form_has_credit_and_expenses_sections(self):
		frappe.set_user("Administrator")
		answer = web_admin.outlet_settings(self.profile.name)
		titles = [section["title"] for section in answer["sections"]]
		self.assertIn("Expenses", titles)
		self.assertIn("Credit sales", titles)
		fields = self.fields(answer)
		for name in (
			booking.ENABLED_FIELD,
			booking.CASH_SUPPLIER_FIELD,
			booking.SUPPLIER_GROUP_FIELD,
			booking.TAX_TEMPLATE_FIELD,
			booking.ADD_PIN_FIELD,
			CREDIT_SALES_FIELD,
		):
			self.assertIn(name, fields, msg=name)
		# a System Manager is offered the site's suppliers, groups and purchase templates
		for name in (booking.CASH_SUPPLIER_FIELD, booking.SUPPLIER_GROUP_FIELD, booking.TAX_TEMPLATE_FIELD):
			self.assertIn("linkOptions", fields[name], msg=name)
		self.assertTrue(answer["expense_setup_checks"])
		self.assertIn("module", {row["key"] for row in answer["expense_setup_checks"]})
		self.assertIn("credit_setup_checks", answer)

	def test_a_manager_is_offered_no_site_wide_supplier_names(self):
		self.as_manager()
		fields = self.fields(web_admin.outlet_settings(self.profile.name))
		# the accounting links are a System Manager's: read only, and no list of the site's suppliers
		for name in (booking.CASH_SUPPLIER_FIELD, booking.SUPPLIER_GROUP_FIELD, booking.TAX_TEMPLATE_FIELD):
			self.assertTrue(fields[name]["read_only"], msg=name)
			self.assertNotIn("linkOptions", fields[name], msg=name)
		for name in (booking.ENABLED_FIELD, booking.ADD_PIN_FIELD, CREDIT_SALES_FIELD):
			self.assertFalse(fields[name]["read_only"], msg=name)

	def test_manager_saves_the_switches_but_not_the_links(self):
		self.keep_profile(CREDIT_SALES_FIELD, booking.ENABLED_FIELD, booking.CASH_SUPPLIER_FIELD)
		self.as_manager()
		answer = web_admin.outlet_settings(self.profile.name)
		saved = web_admin.save_outlet_settings(
			self.profile.name, {CREDIT_SALES_FIELD: 1, booking.ENABLED_FIELD: 0}, answer["modified"]
		)
		fields = self.fields(saved)
		self.assertEqual(
			(fields[CREDIT_SALES_FIELD]["value"], fields[booking.ENABLED_FIELD]["value"]), (1, 0)
		)
		with self.assertRaises(frappe.PermissionError):
			web_admin.save_outlet_settings(
				self.profile.name, {booking.CASH_SUPPLIER_FIELD: ""}, saved["modified"]
			)
		# a System Manager sets the links
		frappe.set_user("Administrator")
		again = web_admin.outlet_settings(self.profile.name)
		done = web_admin.save_outlet_settings(
			self.profile.name, {booking.TAX_TEMPLATE_FIELD: ""}, again["modified"]
		)
		self.assertEqual(self.fields(done)[booking.TAX_TEMPLATE_FIELD]["value"], "")

	def test_credit_setup_check_follows_the_switch(self):
		self.keep_profile(CREDIT_SALES_FIELD)
		self.as_manager()
		answer = web_admin.outlet_settings(self.profile.name)
		on = web_admin.save_outlet_settings(self.profile.name, {CREDIT_SALES_FIELD: 1}, answer["modified"])
		self.assertEqual([row["key"] for row in on["credit_setup_checks"]], ["credit_receivable"])
		off = web_admin.save_outlet_settings(self.profile.name, {CREDIT_SALES_FIELD: 0}, on["modified"])
		self.assertEqual(off["credit_setup_checks"], [])

	def keep_profile(self, *fields):
		saved = frappe.db.get_value("POS Profile", self.profile.name, list(fields), as_dict=True)

		def restore():
			frappe.db.set_value("POS Profile", self.profile.name, saved, update_modified=False)
			frappe.clear_document_cache("POS Profile", self.profile.name)
			frappe.db.commit()  # nosemgrep

		self.addCleanup(restore)


# the keys a shop's answers had in 0.13.2, and the ones 0.14.0 adds
ROW_KEYS_0132 = {
	"customer",
	"name",
	"modified",
	"credit_limit",
	"limit_source",
	"outstanding",
	"available",
	"over_limit",
	"open_invoices",
}
ANSWER_KEYS_0132 = {"outlet", "currency", "rows", "more", "can_edit"}
SHOP_KEYS_0132 = {"name", "outlet_name", "company", "currency", "credit_view", "credit_enabled", "can_edit"}


class TestShopAnswersUnchanged(WebCreditCase):
	def test_shop_credit_answers_unchanged(self):
		"""A shop's credit_customers, credit_shops and save_credit_limit answer 0.13.2's values with the
		new keys added only; the optional range arguments change no 0.13.2 key; the shop still lists by
		limit or balance (a House Account flag alone lists nobody on a shop)."""
		over = make_customer("unchanged", limit=100)
		self.sell(over, "150.000")
		flagged = make_customer("unchangedflag", limit=None)
		frappe.db.set_value("Customer", flagged, CREDIT_ENABLED_FIELD, 1)
		frappe.db.commit()  # nosemgrep
		self.as_manager()
		answer = web_admin.credit_customers(self.profile.name)
		self.assertEqual(set(answer), ANSWER_KEYS_0132 | {"totals"})
		rows = rows_by_customer(answer)
		self.assertNotIn(flagged, rows)
		row = rows[over]
		self.assertEqual(
			set(row),
			ROW_KEYS_0132 | {"credit_enabled", "mobile_no", "credit_sales", "payments", "location"},
		)
		self.assertEqual(
			(row["credit_limit"], row["limit_source"], row["outstanding"], row["available"]),
			("100.000", "customer", "150.000", "0.000"),
		)
		self.assertEqual((row["over_limit"], row["open_invoices"], row["credit_enabled"]), (True, 1, False))
		today = frappe.utils.nowdate()
		ranged = web_admin.credit_customers(self.profile.name, from_date=today, to_date=today)
		self.assertEqual(set(ranged), set(answer))
		ranged_row = rows_by_customer(ranged)[over]
		self.assertEqual({k: ranged_row[k] for k in ROW_KEYS_0132}, {k: row[k] for k in ROW_KEYS_0132})
		with self.assertRaises(frappe.ValidationError):
			web_admin.credit_customers(self.profile.name, from_date=today, to_date="2001-01-01")
		with self.assertRaises(frappe.ValidationError):
			web_admin.credit_customers(self.profile.name, from_date="19-10-2026")
		shop = next(s for s in web_admin.credit_shops()["shops"] if s["name"] == self.profile.name)
		self.assertEqual(set(shop), SHOP_KEYS_0132 | {"credit_on", "owing"})
		self.assertEqual((shop["credit_enabled"], shop["credit_on"], shop["owing"]), (True, True, True))
		self.assertEqual(web_admin.credit_shops(), web_admin.credit_shops("shop"))
		frappe.set_user("Administrator")
		saved = web_admin.save_credit_limit(self.profile.name, over, "200", row["modified"])
		self.assertEqual(set(saved), ROW_KEYS_0132 | {"credit_enabled", "mobile_no", "location"})
		self.assertEqual((saved["credit_limit"], saved["available"]), ("200.000", "50.000"))


class RestaurantCreditCase(WebCreditCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.previous_product = setup_restaurant_credit(cls.profile.customer)

	@classmethod
	def tearDownClass(cls):
		restore_restaurant_product(cls.previous_product)
		super().tearDownClass()

	def set_restaurant_switch(self, outlet, value):
		saved = frappe.db.get_value("POS Profile", outlet, CREDIT_SALES_FIELD)

		def restore():
			frappe.db.set_value("POS Profile", outlet, CREDIT_SALES_FIELD, saved or 0, update_modified=False)
			frappe.clear_document_cache("POS Profile", outlet)
			frappe.db.commit()  # nosemgrep

		self.addCleanup(restore)
		frappe.db.set_value("POS Profile", outlet, CREDIT_SALES_FIELD, value, update_modified=False)
		frappe.clear_document_cache("POS Profile", outlet)
		frappe.db.commit()  # nosemgrep


def shop_names(answer):
	return [shop["name"] for shop in answer["shops"]]


class TestRestaurantCreditPages(RestaurantCreditCase):
	def test_restaurant_outlet_resolves_restaurant_product(self):
		self.assertEqual(scope.outlet_product(RESTAURANT_A), "restaurant")
		self.assertEqual(scope.outlet_product(self.profile.name), "shop")
		self.assertIsNone(scope.outlet_product("No such outlet"))
		self.assertIsNone(scope.outlet_product(None))
		self.assertEqual(web_credit.outlet_credit_product(RESTAURANT_A).key, "restaurant")
		self.assertEqual(web_credit.outlet_credit_product(self.profile.name).key, "shop")
		answer = web_admin.credit_customers(RESTAURANT_A)
		self.assertEqual(answer["outlet"], RESTAURANT_A)
		# a restaurant outlet is no shop: the shop pages that are not credit still refuse it
		with self.assertRaises(frappe.PermissionError):
			web_admin.outlet_settings(RESTAURANT_A)
		# until crenya_restaurant registers its product, a restaurant outlet opens nothing (0.13.2)
		web_credit._PRODUCTS.pop("restaurant")
		try:
			with self.assertRaises(frappe.PermissionError):
				web_admin.credit_customers(RESTAURANT_A)
			self.assertEqual(web_admin.credit_shops("restaurant"), {"shops": []})
		finally:
			web_credit.register_credit_product(TEST_RESTAURANT)

	def test_restaurant_manager_reads_own_outlet_only(self):
		self.sell(make_customer("restown", limit=None), "5.000")
		self.set_restaurant_switch(RESTAURANT_A, 1)
		frappe.set_user(REST_MANAGER)
		answer = web_admin.credit_customers(RESTAURANT_A)
		self.assertIs(answer["can_edit"], False)
		for outlet in (RESTAURANT_B, self.profile.name):
			with self.assertRaises(frappe.PermissionError, msg=outlet):
				web_admin.credit_customers(outlet)
		listed = shop_names(web_admin.credit_shops("restaurant"))
		self.assertIn(RESTAURANT_A, listed)
		self.assertNotIn(RESTAURANT_B, listed)
		self.assertEqual(web_admin.credit_shops(), {"shops": []})
		# a shop's manager is no restaurant manager
		self.as_manager()
		with self.assertRaises(frappe.PermissionError):
			web_admin.credit_customers(RESTAURANT_A)
		self.assertNotIn(RESTAURANT_A, shop_names(web_admin.credit_shops("restaurant")))
		frappe.set_user(CASHIER)
		with self.assertRaises(frappe.PermissionError):
			web_admin.credit_customers(RESTAURANT_A)

	def test_accountant_reads_every_outlet_of_permitted_company(self):
		self.sell(make_customer("restacct", limit=None), "5.000")
		frappe.set_user(ACCOUNTS)
		for outlet in (RESTAURANT_A, RESTAURANT_B):
			answer = web_admin.credit_customers(outlet)
			self.assertEqual((answer["outlet"], answer["can_edit"]), (outlet, True))
		shops = {shop["name"]: shop for shop in web_admin.credit_shops("restaurant")["shops"]}
		self.assertLessEqual({RESTAURANT_A, RESTAURANT_B}, set(shops))
		self.assertIs(shops[RESTAURANT_B]["owing"], True)
		self.assertIs(shops[RESTAURANT_B]["can_edit"], True)
		frappe.set_user("Administrator")
		permission = frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": ACCOUNTS,
				"allow": "Company",
				"for_value": "_Test Crenya Other Company",
			}
		)
		permission.flags.ignore_links = True
		permission.insert(ignore_permissions=True)
		frappe.db.commit()  # nosemgrep
		try:
			frappe.set_user(ACCOUNTS)
			with self.assertRaises(frappe.PermissionError):
				web_admin.credit_customers(RESTAURANT_A)
			self.assertNotIn(RESTAURANT_A, shop_names(web_admin.credit_shops("restaurant")))
		finally:
			frappe.set_user("Administrator")
			frappe.delete_doc("User Permission", permission.name, ignore_permissions=True, force=True)
			frappe.db.commit()  # nosemgrep

	def test_limits_editable_by_accountants_only(self):
		"""P4: a restaurant's manager reads limits and facilities, an accountant changes them."""
		customer = make_customer("restlimit", limit=50)
		self.sell(customer, "10.000")
		frappe.set_user(REST_MANAGER)
		row = rows_by_customer(web_admin.credit_customers(RESTAURANT_A))[customer]
		with self.assertRaises(frappe.PermissionError):
			web_admin.save_credit_limit(RESTAURANT_A, customer, "500", row["modified"])
		with self.assertRaises(frappe.PermissionError):
			web_admin.save_credit_facility(RESTAURANT_A, customer, 1)
		self.assertEqual(credit_limit_rows(customer)[0]["credit_limit"], 50)
		frappe.set_user(ACCOUNTS)
		saved = web_admin.save_credit_limit(RESTAURANT_A, customer, "500", row["modified"])
		self.assertEqual(saved["credit_limit"], "500.000")
		self.assertIs(web_admin.save_credit_facility(RESTAURANT_A, customer, 1)["credit_enabled"], True)

	def test_restaurant_listed_when_on_or_owing(self):
		"""RC16: a restaurant outlet is listed while its switch is on or a customer of its company
		owes; switched off with nobody owing, it is not (a shop is listed either way)."""
		frappe.set_user(ACCOUNTS)
		self.set_restaurant_switch(RESTAURANT_A, 0)
		with mock.patch.object(web_credit, "owing_companies", return_value=set()):
			answer = web_admin.credit_shops("restaurant")
			self.assertNotIn(RESTAURANT_A, shop_names(answer))
			self.assertIn(self.profile.name, shop_names(web_admin.credit_shops("shop")))
			self.set_restaurant_switch(RESTAURANT_A, 1)
			shop = next(s for s in web_admin.credit_shops("restaurant")["shops"] if s["name"] == RESTAURANT_A)
			self.assertEqual((shop["credit_on"], shop["owing"]), (True, False))
			self.set_restaurant_switch(RESTAURANT_A, 0)
		with mock.patch.object(web_credit, "owing_companies", return_value={fixtures.COMPANY}):
			shop = next(s for s in web_admin.credit_shops("restaurant")["shops"] if s["name"] == RESTAURANT_A)
			self.assertEqual((shop["credit_on"], shop["owing"]), (False, True))
		self.assertEqual(web_admin.credit_shops("no such product"), {"shops": []})
