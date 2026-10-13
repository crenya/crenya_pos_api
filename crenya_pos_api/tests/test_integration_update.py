"""Till updates (`api.update.check` / `api.update.download`) against a real ERPNext site.

Run with: bench --site <test-site> run-tests --app crenya_pos_api --module crenya_pos_api.tests.test_integration_update
Releases, devices and test files created here are rolled back after the class
(Frappe removes the files of rolled-back File records from disk).
"""

import hashlib
import json
import os
import uuid
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import frappe
from frappe.utils import get_system_timezone

try:
	from frappe.tests import IntegrationTestCase as FrappeTestCase
except ImportError:
	from frappe.tests.utils import FrappeTestCase

from crenya_pos_api.api import device as device_api
from crenya_pos_api.api import update as update_api
from crenya_pos_api.constants import CENTRAL_RELEASES_CONFIG
from crenya_pos_api.sync import central, errors
from crenya_pos_api.sync.update import RELEASE_DOCTYPE
from crenya_pos_api.tests import fixtures
from crenya_pos_api.utils.dates import site_naive_to_utc_iso

WIN = "windows-x86_64"
MAC = "darwin-aarch64"
VERSIONS = ("0.1.0", "0.2.0", "0.1.5", "0.3.0", "0.9.0")
SIGNATURE = "dW50cnVzdGVkIGNvbW1lbnQ6IHNpZ25hdHVyZSBmcm9tIHRhdXJpIHNlY3JldCBrZXkK"
PUB_DATE = "2026-10-01 12:00:00"


def installer_bytes(version):
	# not valid UTF-8, like a real installer
	return b"MZ\x90\x00\x03\x00\xff\xfe" + f"crenya-pos {version} {uuid.uuid4()}".encode()


def private_file(version, content, is_private=1):
	return frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"_test_crenya-pos_{version}_x64-setup.exe",
			"is_private": is_private,
			"content": content,
		}
	).insert(ignore_permissions=True)


def make_release(version, file_url, channel="stable", published=1, targets=(WIN,), notes=None, pub_date=None):
	return frappe.get_doc(
		{
			"doctype": RELEASE_DOCTYPE,
			"version": version,
			"channel": channel,
			"published": published,
			"pub_date": pub_date,
			"notes": notes,
			"artifacts": [{"target": target, "file": file_url, "signature": SIGNATURE} for target in targets],
		}
	).insert(ignore_permissions=True)


class TestCrenyaUpdates(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.profile = fixtures.setup_fixtures()

		for version in VERSIONS:
			if frappe.db.exists(RELEASE_DOCTYPE, version):
				frappe.delete_doc(RELEASE_DOCTYPE, version, ignore_permissions=True, force=True)

		cls.stable_device = str(uuid.uuid4())
		cls.beta_device = str(uuid.uuid4())
		for device_id, name in ((cls.stable_device, "Update Till"), (cls.beta_device, "Beta Till")):
			device_api.register_device(device_id=device_id, device_name=name, pos_profile=cls.profile.name)
		frappe.db.set_value("Crenya POS Device", cls.beta_device, "update_channel", "beta")

		cls.contents = {version: installer_bytes(version) for version in VERSIONS}
		cls.files = {version: private_file(version, cls.contents[version]) for version in VERSIONS}
		make_release("0.1.0", cls.files["0.1.0"].file_url)
		make_release("0.2.0", cls.files["0.2.0"].file_url, published=0)
		make_release("0.1.5", cls.files["0.1.5"].file_url, notes="Faster sync", pub_date=PUB_DATE)
		make_release("0.3.0", cls.files["0.3.0"].file_url, channel="beta", notes="Beta build")

	def setUp(self):
		# the site's own releases only, unless a case serves a manifest (`central_manifest`)
		no_manifest = patch.dict(frappe.conf, {CENTRAL_RELEASES_CONFIG: ""})
		no_manifest.start()
		self.addCleanup(no_manifest.stop)

	def tearDown(self):
		frappe.set_user("Administrator")

	def central_manifest(self, *versions, target=WIN):
		"""Serve a central manifest of `versions` (stable) at a URL of this case only (its own
		cache keys); returns {version: installer bytes}."""
		host = "https://dl.example.test"
		url = f"{host}/{uuid.uuid4().hex}/pos/releases.json"
		contents = {version: installer_bytes(version) for version in versions}
		releases = []
		for version, content in contents.items():
			file = {
				"url": f"{host}/pos/{version}/crenya-pos_{version}_x64-setup.exe",
				"sha256": hashlib.sha256(content).hexdigest(),
				"size": len(content),
			}
			releases.append(
				{
					"version": version,
					"channel": "stable",
					"pub_date": "2026-10-12T08:00:00Z",
					"notes": f"Central {version}",
					"updater": {target: {**file, "signature": f"central-signature-{version}"}},
					"installers": [{"platform": "windows", "label": "Windows", "label_ar": "ويندوز", **file}],
				}
			)
		conf = patch.dict(frappe.conf, {CENTRAL_RELEASES_CONFIG: url})
		fetch = patch.object(
			central,
			"_fetch_manifest",
			return_value={"schema": 1, "product": "crenya pos", "releases": releases},
		)
		for each in (conf, fetch):
			each.start()
			self.addCleanup(each.stop)
		for content in contents.values():
			path = central._artifact_cache_path(central.RETAIL, hashlib.sha256(content).hexdigest())
			self.addCleanup(lambda path=path: os.path.exists(path) and os.remove(path))
		return contents

	def check(self, current_version, device_id=None, target=WIN):
		return update_api.check(
			device_id=device_id or self.stable_device, target=target, current_version=current_version
		)

	def assertNoUpdate(self, response):
		self.assertEqual(response.status_code, 204)
		self.assertEqual(response.get_data(), b"")

	def test_newer_published_release_is_offered(self):
		response = self.check("0.1.0")
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.mimetype, "application/json")
		body = json.loads(response.get_data())

		self.assertEqual(list(body), ["version", "notes", "pub_date", "url", "signature"])
		self.assertEqual(body["version"], "0.1.5")
		self.assertEqual(body["notes"], "Faster sync")
		self.assertEqual(body["signature"], SIGNATURE)
		self.assertEqual(body["pub_date"], site_naive_to_utc_iso(PUB_DATE, get_system_timezone()))
		self.assertRegex(body["pub_date"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

		parts = urlsplit(body["url"])
		self.assertTrue(body["url"].startswith(frappe.utils.get_url()))
		self.assertEqual(parts.path, "/api/method/crenya_pos_api.api.update.download")
		self.assertEqual(
			parse_qs(parts.query),
			{"release": ["0.1.5"], "target": [WIN], "device_id": [self.stable_device]},
		)

	def test_up_to_date_till_gets_204(self):
		self.assertNoUpdate(self.check("0.1.5"))
		self.assertNoUpdate(self.check("0.2.0"))

	def test_other_target_gets_204(self):
		self.assertNoUpdate(self.check("0.1.0", target=MAC))

	def test_beta_device_sees_beta_release(self):
		body = json.loads(self.check("0.1.0", device_id=self.beta_device).get_data())
		self.assertEqual(body["version"], "0.3.0")
		self.assertEqual(body["notes"], "Beta build")
		self.assertIn(f"device_id={self.beta_device}", body["url"])
		# stable tills never see it
		self.assertNoUpdate(self.check("0.1.5"))

	def test_invalid_requests(self):
		with self.assertRaises(errors.InvalidRequestError):
			self.check("latest")
		with self.assertRaises(errors.InvalidRequestError):
			self.check(None)
		with self.assertRaises(errors.InvalidRequestError):
			self.check("0.1.0", target="windows-i686")

	def test_unknown_or_disabled_device_is_rejected(self):
		with self.assertRaises(errors.DeviceNotRegisteredError):
			self.check("0.1.0", device_id=str(uuid.uuid4()))
		with self.assertRaises(errors.DeviceNotRegisteredError):
			update_api.download(release="0.1.5", target=WIN, device_id=str(uuid.uuid4()))
		with self.assertRaises(errors.DeviceNotRegisteredError):
			update_api.download(release="0.1.5", target=WIN, device_id=None)

		disabled = str(uuid.uuid4())
		device_api.register_device(device_id=disabled, device_name="Off Till", pos_profile=self.profile.name)
		frappe.db.set_value("Crenya POS Device", disabled, "enabled", 0)
		with self.assertRaises(errors.DeviceNotRegisteredError):
			self.check("0.1.0", device_id=disabled)

	def test_download_returns_the_file(self):
		response = update_api.download(release="0.1.5", target=WIN, device_id=self.stable_device)
		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.mimetype, "application/octet-stream")
		self.assertEqual(response.get_data(), self.contents["0.1.5"])
		disposition = response.headers["Content-Disposition"]
		self.assertTrue(disposition.startswith("attachment"))
		self.assertIn(self.files["0.1.5"].file_name, disposition)

		response = update_api.download(release="0.3.0", target=WIN, device_id=self.beta_device)
		self.assertEqual(response.get_data(), self.contents["0.3.0"])

	def test_download_only_serves_available_releases(self):
		cases = [
			("0.2.0", WIN, self.stable_device),  # unpublished
			("0.3.0", WIN, self.stable_device),  # beta release, stable till
			("0.1.5", MAC, self.stable_device),  # no artifact for the target
			("9.9.9", WIN, self.stable_device),  # does not exist
		]
		for release, target, device_id in cases:
			with self.subTest(release=release, target=target), self.assertRaises(errors.NotFoundError):
				update_api.download(release=release, target=target, device_id=device_id)
		with self.assertRaises(errors.InvalidRequestError):
			update_api.download(release="0.1.5", target="plan9", device_id=self.stable_device)
		with self.assertRaises(errors.InvalidRequestError):
			update_api.download(release=None, target=WIN, device_id=self.stable_device)

	def test_publishing_sets_pub_date(self):
		release = frappe.get_doc(RELEASE_DOCTYPE, "0.2.0")
		self.assertFalse(release.pub_date)
		release.published = 1
		release.save(ignore_permissions=True)
		try:
			self.assertTrue(release.pub_date)
			self.assertEqual(json.loads(self.check("0.1.5").get_data())["version"], "0.2.0")
		finally:
			release.reload()
			release.published = 0
			release.save(ignore_permissions=True)

	def test_release_validation(self):
		file_url = self.files["0.9.0"].file_url
		for version in ("1.2", "v1.2.3", "1.2.3-beta.1"):
			with self.subTest(version=version), self.assertRaises(frappe.ValidationError):
				make_release(version, file_url, published=0)

		with self.assertRaises(frappe.ValidationError):
			make_release("0.9.0", file_url, targets=(WIN, WIN))

		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{"doctype": RELEASE_DOCTYPE, "version": "0.9.0", "published": 1, "artifacts": []}
			).insert(ignore_permissions=True)

		public = private_file("0.9.0-public", installer_bytes("public"), is_private=0)
		with self.assertRaises(frappe.ValidationError):
			make_release("0.9.0", public.file_url)

		self.assertFalse(frappe.db.exists(RELEASE_DOCTYPE, "0.9.0"))

	def test_check_offers_central_release_when_site_has_none(self):
		# the site has no release for this target: the central one is offered and downloaded
		contents = self.central_manifest("0.4.0", target=MAC)
		body = json.loads(self.check("0.1.0", target=MAC).get_data())
		self.assertEqual(body["version"], "0.4.0")
		self.assertEqual(body["signature"], "central-signature-0.4.0")
		self.assertEqual(body["pub_date"], "2026-10-12T08:00:00Z")
		parts = urlsplit(body["url"])
		self.assertTrue(body["url"].startswith(frappe.utils.get_url()))
		self.assertEqual(
			parse_qs(parts.query),
			{"release": ["central:0.4.0"], "target": [MAC], "device_id": [self.stable_device]},
		)
		with patch.object(central, "_fetch_bytes", return_value=contents["0.4.0"]):
			response = update_api.download(release="central:0.4.0", target=MAC, device_id=self.stable_device)
		self.assertEqual(response.get_data(), contents["0.4.0"])
		self.assertIn("crenya-pos_0.4.0_x64-setup.exe", response.headers["Content-Disposition"])

	def test_local_release_same_version_wins(self):
		self.central_manifest("0.1.5")
		body = json.loads(self.check("0.1.0").get_data())
		self.assertEqual(body["version"], "0.1.5")
		self.assertEqual(body["signature"], SIGNATURE)
		self.assertEqual(parse_qs(urlsplit(body["url"]).query)["release"], ["0.1.5"])

	def test_newer_central_release_wins_over_local(self):
		self.central_manifest("0.2.5")
		body = json.loads(self.check("0.1.0").get_data())
		self.assertEqual(body["version"], "0.2.5")
		self.assertEqual(parse_qs(urlsplit(body["url"]).query)["release"], ["central:0.2.5"])

	def test_central_release_not_in_manifest_is_not_found(self):
		self.central_manifest("0.4.0")
		with self.assertRaises(errors.NotFoundError):
			update_api.download(release="central:0.9.9", target=WIN, device_id=self.stable_device)

	def test_downloads_lists_newest_stable_installers(self):
		self.central_manifest("0.4.0", "0.5.0")
		answer = update_api.downloads()
		self.assertEqual(answer["version"], "0.5.0")
		self.assertEqual([row["platform"] for row in answer["installers"]], ["windows"])
		# central turned off: nothing to download
		with patch.dict(frappe.conf, {CENTRAL_RELEASES_CONFIG: ""}):
			self.assertEqual(update_api.downloads(), {"installers": []})
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			update_api.downloads()
		# a portal (website) user is refused too
		portal = "crenya.portal.test@example.com"
		frappe.set_user("Administrator")
		if not frappe.db.exists("User", portal):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": portal,
					"first_name": "Portal",
					"send_welcome_email": 0,
					"user_type": "Website User",
				}
			).insert(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("User", portal, "user_type"), "Website User")
		frappe.set_user(portal)
		with self.assertRaises(frappe.PermissionError):
			update_api.downloads()
