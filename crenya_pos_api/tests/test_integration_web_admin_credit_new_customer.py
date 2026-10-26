"""The House Account page's product seams and its New customer (provider settlement 0.15.0, Task 4;
spec PS8, NC5, 7.5): a product may leave customers out of its credit page (`excluded_customers`,
the restaurant's service providers) and say who may switch the House Account on for a customer
created there (`may_enable_new`); `create_credit_customer` creates one from app.crenya.com.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module
crenya_pos_api.tests.test_integration_web_admin_credit_new_customer
"""

import random
import uuid
from unittest import mock

import frappe
from frappe.utils import cint

from crenya_pos_api.api import web_admin
from crenya_pos_api.api.web_admin import credit as web_credit
from crenya_pos_api.sync.credit import CREDIT_CHANGED_AT_FIELD, CREDIT_ENABLED_FIELD
from crenya_pos_api.tests import fixtures
from crenya_pos_api.tests.test_integration_credit import make_customer
from crenya_pos_api.tests.test_integration_web_admin_credit import (
	ACCOUNTS,
	REST_MANAGER,
	REST_MANAGER_B,
	RESTAURANT_A,
	ROW_KEYS_0132,
	SETTLE_BANK,
	SHOP_KEYS_0132,
	TEST_RESTAURANT,
	RestaurantCreditCase,
	credit_limit_rows,
	manages_test_restaurant,
	rows_by_customer,
)
from crenya_pos_api.tests.web_admin_case import CASHIER, MANAGER
from crenya_pos_api.web.context import WebAdminContext

PROVIDER_TEXT = "{name} is a service provider: record its payment on Service providers → Settle"
# the keys a shop row had in 0.14.0 (0.13.2's and the four added since)
ROW_KEYS_0140 = ROW_KEYS_0132 | {"credit_enabled", "mobile_no", "credit_sales", "payments"}
SHOP_KEYS_0140 = SHOP_KEYS_0132 | {"credit_on", "owing"}


def mobile():
	"""A mobile number no other test customer has."""
	return f"+968 9{random.randint(1000000, 9999999)} {random.randint(10, 99)}"


def new_request():
	return str(uuid.uuid4())


def customers_with_mobile(number):
	digits = "".join(c for c in number if c.isdigit())
	return [
		row.name
		for row in frappe.get_all(
			"Customer", filters={"mobile_no": ["is", "set"]}, fields=["name", "mobile_no"]
		)
		if "".join(c for c in row.mobile_no if c.isdigit()) == digits
	]


def comments_of(customer):
	return frappe.get_all(
		"Comment",
		filters={"reference_doctype": "Customer", "reference_name": customer, "comment_type": "Info"},
		pluck="content",
		order_by="creation asc",
	)


class NewCustomerCase(RestaurantCreditCase):
	def use_product(self, **seams):
		"""Register the test restaurant product with `seams` for this test only."""
		web_credit.register_credit_product(TEST_RESTAURANT._replace(**seams))
		self.addCleanup(web_credit.register_credit_product, TEST_RESTAURANT)

	def full_list(self):
		"""The listing is cut at MAX_CREDIT_ROWS and this site keeps every test's customers: list them
		all so a test's own customers are in it."""
		patch = mock.patch.object(web_credit, "MAX_CREDIT_ROWS", 100000)
		patch.start()
		self.addCleanup(patch.stop)

	def create(self, outlet=RESTAURANT_A, user=REST_MANAGER, enable=0, limit=None, request_id=None, **fields):
		frappe.set_user(user)
		customer = {
			"customer_name": fields.pop("customer_name", f"Web Guest {uuid.uuid4().hex[:8]}"),
			"mobile_no": mobile(),
		}
		customer.update(fields)
		return web_admin.create_credit_customer(
			outlet, customer, enable, credit_limit=limit, request_id=request_id or new_request()
		)


class TestProductSeams(NewCustomerCase):
	def test_shop_answers_unchanged(self):
		"""A shop has no excluded customers: its rows keep 0.14.0's keys and values (plus `location`),
		and its credit_shops answer is the same whatever a restaurant product excludes."""
		self.full_list()
		customer = make_customer("seamshop", limit=100)
		self.sell(customer, "150.000")
		self.as_manager()
		before_shops = web_admin.credit_shops()
		row = rows_by_customer(web_admin.credit_customers(self.profile.name))[customer]
		excluder = mock.Mock(return_value={customer})
		self.use_product(excluded_customers=excluder, excluded_refusal=PROVIDER_TEXT)
		self.as_manager()
		self.assertEqual(web_admin.credit_shops(), before_shops)
		for shop in before_shops["shops"]:
			self.assertEqual(set(shop), SHOP_KEYS_0140)
		after = rows_by_customer(web_admin.credit_customers(self.profile.name))[customer]
		self.assertEqual(after, row)
		self.assertEqual(set(row), ROW_KEYS_0140 | {"location"})
		self.assertEqual(
			(row["credit_limit"], row["outstanding"], row["over_limit"], row["location"]),
			("100.000", "150.000", True, None),
		)
		# a shop is never asked who to leave out
		excluder.assert_not_called()
		self.assertEqual(
			web_credit.owing_companies({fixtures.COMPANY}),
			web_credit.owing_companies({fixtures.COMPANY}, excluded=None),
		)

	def test_excluded_customers_left_out_of_rows_and_owing(self):
		self.full_list()
		provider = make_customer("seamprovider", limit=None)
		guest = make_customer("seamguest", limit=None)
		self.sell(provider, "40.000")
		self.sell(guest, "40.000")
		frappe.set_user(ACCOUNTS)
		self.use_product(excluded_customers=lambda ctx: {provider}, excluded_refusal=PROVIDER_TEXT)
		rows = rows_by_customer(web_admin.credit_customers(RESTAURANT_A))
		self.assertNotIn(provider, rows)
		self.assertIn(guest, rows)
		# the product's own seam is given the outlet's context
		seen = []
		self.use_product(excluded_customers=lambda ctx: seen.append(ctx.outlet) or set())
		self.assertIn(provider, rows_by_customer(web_admin.credit_customers(RESTAURANT_A)))
		self.assertEqual(set(seen), {RESTAURANT_A})

		# "a customer still owes": everyone else who could make the company owe is left out too, so
		# the provider's own bill decides
		others = set(frappe.get_all("Customer", filters={CREDIT_ENABLED_FIELD: 1}, pluck="name"))
		others |= set(
			frappe.get_all(
				"Sales Invoice",
				filters={
					"company": fixtures.COMPANY,
					"docstatus": 1,
					"outstanding_amount": [">", 0],
					"pos_profile": ["is", "set"],
				},
				pluck="customer",
				distinct=True,
			)
		)
		others.discard(provider)
		self.assertEqual(
			web_credit.owing_companies({fixtures.COMPANY}, excluded={fixtures.COMPANY: others}),
			{fixtures.COMPANY},
		)
		self.assertEqual(
			web_credit.owing_companies({fixtures.COMPANY}, excluded={fixtures.COMPANY: others | {provider}}),
			set(),
		)
		self.set_restaurant_switch(RESTAURANT_A, 0)
		self.use_product(excluded_customers=lambda ctx: others | {provider})
		self.assertNotIn(RESTAURANT_A, [s["name"] for s in web_admin.credit_shops("restaurant")["shops"]])
		self.use_product(excluded_customers=lambda ctx: others)
		shop = next(s for s in web_admin.credit_shops("restaurant")["shops"] if s["name"] == RESTAURANT_A)
		self.assertEqual((shop["credit_on"], shop["owing"]), (False, True))

	def test_settle_excluded_customer_refused_with_product_text(self):
		provider = make_customer("seamsettle", limit=None)
		self.sell(provider, "25.000")
		self.use_product(excluded_customers=lambda ctx: {provider}, excluded_refusal=PROVIDER_TEXT)
		frappe.set_user(ACCOUNTS)
		with self.assertRaises(frappe.PermissionError) as refused:
			web_admin.settle_customer(
				RESTAURANT_A, provider, SETTLE_BANK, "5.000", reference="SLIP1", request_id=new_request()
			)
		name = frappe.db.get_value("Customer", provider, "customer_name")
		self.assertIn(PROVIDER_TEXT.format(name=name), str(refused.exception))
		self.assertFalse(
			frappe.db.exists("Payment Entry", {"party": provider, "docstatus": ["!=", 2]}),
			"no payment booked",
		)
		# the shop keeps settling everyone
		self.assertEqual(web_credit.excluded_customers(WebAdminContext(ACCOUNTS, self.profile.name)), set())

	def test_limit_and_facility_refuse_excluded_customer(self):
		"""Controller ruling (review round 1): a provider's limit and House Account are not changed on
		the House Account page either; the refusal is the product's words, the name escaped."""
		provider = make_customer("seamtoggle", limit=50)
		frappe.db.set_value("Customer", provider, "customer_name", "Talabat <b>Bold</b>")
		frappe.db.commit()  # nosemgrep
		self.use_product(excluded_customers=lambda ctx: {provider}, excluded_refusal=PROVIDER_TEXT)
		frappe.set_user(ACCOUNTS)
		for call in (
			lambda: web_admin.save_credit_facility(RESTAURANT_A, provider, 1),
			lambda: web_admin.save_credit_limit(RESTAURANT_A, provider, "500", "2000-01-01 00:00:00"),
			lambda: web_admin.settle_customer(
				RESTAURANT_A, provider, SETTLE_BANK, "5.000", reference="S1", request_id=new_request()
			),
		):
			with self.assertRaises(frappe.PermissionError) as refused:
				call()
			text = str(refused.exception)
			self.assertIn("Talabat &lt;b&gt;Bold&lt;/b&gt; is a service provider", text)
			self.assertNotIn("<b>", text)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Customer", provider, CREDIT_ENABLED_FIELD), 0)
		self.assertEqual(credit_limit_rows(provider)[0]["credit_limit"], 50)
		# the shop still changes the same customer
		frappe.set_user(ACCOUNTS)
		self.assertIs(web_admin.save_credit_facility(self.profile.name, provider, 1)["credit_enabled"], True)


class TestCreateCreditCustomer(NewCustomerCase):
	def test_create_without_enable_by_manager(self):
		"""TEST_RESTAURANT says nothing of who may enable (P4 only): its manager still creates a
		customer without the House Account."""
		request = new_request()
		number = mobile()
		guest = f"Walk Up Guest {uuid.uuid4().hex[:6]}"
		row = self.create(
			customer_name=guest,
			mobile_no=number,
			email_id="guest@example.com",
			location="Al Khuwair,   Way 3021",
			request_id=request,
		)
		self.assertEqual(set(row), ROW_KEYS_0140 - {"credit_sales", "payments"} | {"location"})
		self.assertEqual(
			(row["name"], row["mobile_no"], row["location"], row["credit_enabled"], row["credit_limit"]),
			(guest, number, "Al Khuwair, Way 3021", False, None),
		)
		frappe.set_user("Administrator")
		doc = frappe.get_doc("Customer", row["customer"])
		self.assertEqual(
			(doc.crenya_local_id, doc.email_id, cint(doc.get(CREDIT_ENABLED_FIELD))),
			(request, "guest@example.com", 0),
		)
		self.assertIn(f"Created on app.crenya.com by {REST_MANAGER}", comments_of(doc.name))
		self.assertFalse(any("House Account on" in text for text in comments_of(doc.name)))
		# asking to enable is refused for the manager, and nothing is created
		number = mobile()
		with self.assertRaises(frappe.PermissionError):
			self.create(enable=1, mobile_no=number)
		self.assertEqual(customers_with_mobile(number), [])

	def test_create_and_enable_by_restaurant_seam(self):
		self.use_product(may_enable_new=manages_test_restaurant)
		row = self.create(enable="true", customer_name=f"Regular Guest {uuid.uuid4().hex[:6]}")
		self.assertIs(row["credit_enabled"], True)
		frappe.set_user("Administrator")
		doc = frappe.get_doc("Customer", row["customer"])
		self.assertEqual(cint(doc.get(CREDIT_ENABLED_FIELD)), 1)
		self.assertTrue(doc.get(CREDIT_CHANGED_AT_FIELD))
		self.assertIn(f"Created on app.crenya.com by {REST_MANAGER}; House Account on", comments_of(doc.name))
		# the other restaurant's manager opens nothing of this outlet
		with self.assertRaises(frappe.PermissionError):
			self.create(user=REST_MANAGER_B, enable=1)
		# a product whose seam says no leaves it to P4
		self.use_product(may_enable_new=lambda roles, user, profile: False)
		with self.assertRaises(frappe.PermissionError):
			self.create(enable=1)
		self.assertIs(self.create(user=ACCOUNTS, enable=1)["credit_enabled"], True)

	def test_shop_manager_cannot_enable_new(self):
		number = mobile()
		with self.assertRaises(frappe.PermissionError):
			self.create(outlet=self.profile.name, user=MANAGER, enable=1, mobile_no=number)
		self.assertEqual(customers_with_mobile(number), [])
		row = self.create(outlet=self.profile.name, user=MANAGER, enable=0, mobile_no=number)
		self.assertIs(row["credit_enabled"], False)
		self.assertIs(self.create(outlet=self.profile.name, user=ACCOUNTS, enable=1)["credit_enabled"], True)

	def test_limit_only_for_p4(self):
		self.use_product(may_enable_new=manages_test_restaurant)
		number = mobile()
		with self.assertRaises(frappe.PermissionError):
			self.create(enable=1, limit="100", mobile_no=number)
		self.assertEqual(customers_with_mobile(number), [])
		# blank is no limit, for anyone
		self.assertIsNone(self.create(enable=1, limit="")["credit_limit"])
		with self.assertRaises(frappe.ValidationError):
			self.create(user=ACCOUNTS, limit="-1")
		with self.assertRaises(frappe.ValidationError):
			self.create(user=ACCOUNTS, limit="1.2345")
		row = self.create(user=ACCOUNTS, enable=1, limit="100")
		self.assertEqual((row["credit_limit"], row["limit_source"]), ("100.000", "customer"))
		frappe.set_user("Administrator")
		self.assertEqual(credit_limit_rows(row["customer"])[0]["credit_limit"], 100)

	def test_duplicate_mobile_refused_use_instead(self):
		digits = f"9689{random.randint(1000000, 9999999)}"
		typed = f"+{digits[:3]} ({digits[3:7]}) {digits[7:9]}-{digits[9:]}"
		owner = f"Mobile Owner {uuid.uuid4().hex[:6]}"
		first = self.create(customer_name=owner, mobile_no=typed)
		self.assertEqual(frappe.db.get_value("Customer", first["customer"], "mobile_no"), typed)
		with self.assertRaises(frappe.ValidationError) as refused:
			self.create(mobile_no=digits)
		self.assertIn(f"Use {owner} instead", str(refused.exception))
		self.assertEqual(customers_with_mobile(digits), [first["customer"]])
		frappe.set_user("Administrator")
		frappe.db.set_value("Customer", first["customer"], "mobile_no", "", update_modified=False)
		frappe.db.commit()  # nosemgrep

	def test_request_id_idempotent(self):
		request = new_request()
		once = f"Once Only {uuid.uuid4().hex[:6]}"
		first = self.create(customer_name=once, request_id=request)
		again = self.create(customer_name="Another Name", mobile_no=mobile(), enable=0, request_id=request)
		self.assertEqual(again["customer"], first["customer"])
		self.assertEqual(again["name"], once)
		self.assertEqual(
			frappe.get_all("Customer", filters={"crenya_local_id": request}, pluck="name"),
			[first["customer"]],
		)
		# its canonical form is the same request
		self.assertEqual(self.create(request_id=request.upper())["customer"], first["customer"])
		# another user cannot read a customer back through its request id
		with self.assertRaises(frappe.PermissionError):
			self.create(user=ACCOUNTS, request_id=request)
		with self.assertRaises(frappe.ValidationError):
			self.create(request_id="not-a-uuid")
		frappe.set_user(REST_MANAGER)
		with self.assertRaises(frappe.ValidationError):
			web_admin.create_credit_customer(RESTAURANT_A, {"customer_name": "X", "mobile_no": mobile()}, 0)

	def test_fields_validated(self):
		bad = [
			{"customer_name": ""},
			{"customer_name": "x" * 141},
			{"mobile_no": ""},
			{"mobile_no": "12345"},
			{"mobile_no": "9123abc4567"},
			{"mobile_no": "9" * 21},
			{"email_id": "not an email"},
			{"location": "line one\nline two"},
			{"location": "x" * 141},
		]
		for fields in bad:
			with self.assertRaises(frappe.ValidationError, msg=str(fields)):
				self.create(**fields)
		frappe.set_user(REST_MANAGER)
		for value in ("[]", "not json", "5"):
			with self.assertRaises(frappe.ValidationError, msg=str(value)):
				web_admin.create_credit_customer(RESTAURANT_A, value, 0, request_id=new_request())
		with self.assertRaises(frappe.ValidationError):
			web_admin.create_credit_customer(
				RESTAURANT_A, {"customer_name": "X", "mobile_no": mobile()}, "maybe", request_id=new_request()
			)
		# a form post sends the customer as JSON; unknown keys never reach the Customer
		row = web_admin.create_credit_customer(
			RESTAURANT_A,
			frappe.as_json(
				{
					"customer_name": "From Json " + uuid.uuid4().hex[:6],
					"mobile_no": mobile(),
					CREDIT_ENABLED_FIELD: 1,
					"flags": {"crenya_credit_grant": "web"},
				}
			),
			0,
			request_id=new_request(),
		)
		self.assertEqual((row["name"][:9], row["credit_enabled"]), ("From Json", False))
		with self.assertRaises(frappe.PermissionError):
			self.create(user=CASHIER)

	def test_existing_customer_toggle_still_p4(self):
		self.use_product(may_enable_new=manages_test_restaurant)
		row = self.create(enable=0)
		frappe.set_user(REST_MANAGER)
		with self.assertRaises(frappe.PermissionError):
			web_admin.save_credit_facility(RESTAURANT_A, row["customer"], 1)
		self.assertEqual(frappe.db.get_value("Customer", row["customer"], CREDIT_ENABLED_FIELD), 0)
		frappe.set_user(ACCOUNTS)
		self.assertIs(
			web_admin.save_credit_facility(RESTAURANT_A, row["customer"], 1)["credit_enabled"], True
		)

	def use_outlet_groups(self, groups):
		"""RESTAURANT_A limited to `groups` for this test only."""
		profile = frappe.get_doc("POS Profile", RESTAURANT_A)
		saved = [row.customer_group for row in profile.get("customer_groups") or []]

		def put(names):
			doc = frappe.get_doc("POS Profile", RESTAURANT_A)
			doc.set("customer_groups", [{"customer_group": name} for name in names])
			doc.save(ignore_permissions=True)
			frappe.clear_document_cache("POS Profile", RESTAURANT_A)
			frappe.db.commit()  # nosemgrep

		self.addCleanup(put, saved)
		put(groups)

	def test_new_customer_group_inside_outlet(self):
		"""Review round 1: the customer is put inside the outlet's groups (a leaf under them), so it
		is listed and a resend finds it; groups with no leaf are refused before anything is made."""
		root = frappe.db.get_value(
			"Customer Group", {"is_group": 1, "parent_customer_group": ["in", ["", None]]}
		)
		parent = ensure_group("_Test Crenya NC Parent", root, is_group=1)
		leaf = ensure_group("_Test Crenya NC Leaf", parent)
		empty = ensure_group("_Test Crenya NC Empty", root, is_group=1)
		frappe.db.commit()  # nosemgrep
		self.use_outlet_groups([parent])
		request = new_request()
		row = self.create(request_id=request)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Customer", row["customer"], "customer_group"), leaf)
		self.assertEqual(self.create(request_id=request)["customer"], row["customer"])
		self.use_outlet_groups([empty])
		number = mobile()
		with self.assertRaises(frappe.ValidationError):
			self.create(mobile_no=number)
		self.assertEqual(customers_with_mobile(number), [])

	def test_duplicate_mobile_found_whatever_the_separators(self):
		"""Review round 1: the SQL narrowing keeps every number with the same digits."""
		digits = f"9689{random.randint(1000000, 9999999)}"
		owner = f"Sep Owner {uuid.uuid4().hex[:6]}"
		self.create(customer_name=owner, mobile_no=f"{digits[:4]} {digits[4:6]}-{digits[6:8]} {digits[8:]}")
		for typed in (digits, f"+{digits}", f"({digits[:3]}) {digits[3:]}"):
			with self.assertRaises(frappe.ValidationError, msg=typed) as refused:
				self.create(mobile_no=typed)
			self.assertIn(f"Use {owner} instead", str(refused.exception))
		# the same last six digits with another start is no duplicate
		self.assertTrue(self.create(mobile_no="955" + digits[-8:])["customer"])

	def test_rows_have_location(self):
		self.full_list()
		self.use_product(may_enable_new=manages_test_restaurant)
		placed = self.create(enable=1, location="Ruwi High Street")
		plain = self.create(enable=1)
		frappe.set_user(ACCOUNTS)
		rows = rows_by_customer(web_admin.credit_customers(RESTAURANT_A))
		self.assertEqual(rows[placed["customer"]]["location"], "Ruwi High Street")
		self.assertIsNone(rows[plain["customer"]]["location"])


def ensure_group(name, parent, is_group=0):
	if not frappe.db.exists("Customer Group", name):
		frappe.get_doc(
			{
				"doctype": "Customer Group",
				"customer_group_name": name,
				"parent_customer_group": parent,
				"is_group": is_group,
			}
		).insert(ignore_permissions=True)
	return name
