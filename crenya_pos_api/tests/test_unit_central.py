"""The central release manifest (dl.crenya.com), product-neutral (`sync.central`), and the retail
till's update check merging it with the site's own Crenya POS Release rows, on the pure code with
the manifest fetch mocked: the signature passes through untouched, a download is checked against
the manifest's sha256 and size and fetched only from the manifest's host, the site's own
releases are offered when the manifest is down, and only retail tills are offered retail builds.

Every manifest case runs for retail (`central.RETAIL`) and for a second product, so nothing in
`sync.central` belongs to one product."""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from crenya_pos_api.constants import CENTRAL_RELEASES_CONFIG, CENTRAL_RELEASES_URL
from crenya_pos_api.sync import central, update
from crenya_pos_api.sync.context import DeviceContext
from crenya_pos_api.sync.errors import NotFoundError

WIN = "windows-x86_64"
MAC = "darwin-aarch64"
HOST = "https://dl.example.test"
SITE = "https://erp.example.om"
PUB_DATE = "2026-10-12T08:00:00Z"
LOCAL_SIGNATURE = "bG9jYWwgc2lnbmF0dXJl"
# the real fetch (the cases patch it)
FETCH_MANIFEST = central._fetch_manifest

OTHER = central.Product(
	"restaurant",
	"https://dl.example.test/restaurant/releases.json",
	"other_product_releases_url",
	"other_product",
	"other_product:central_releases",
)


class Refused(Exception):
	pass


def raise_refused(exc_class, message):
	raise Refused(exc_class, message)


class FakeCache:
	"""frappe.cache's get_value / set_value / delete_value, with expiry left out."""

	def __init__(self):
		self.values = {}
		self.ttl = {}

	def get_value(self, key, *args, **kwargs):
		return self.values.get(key)

	def set_value(self, key, value, expires_in_sec=None, **kwargs):
		self.values[key] = value
		self.ttl[key] = expires_in_sec

	def delete_value(self, key, *args, **kwargs):
		self.values.pop(key, None)

	def expire_fresh(self):
		"""The 10-minute copy is gone; the last-good one stays."""
		for key in [key for key in self.values if not key.endswith(central.LAST_GOOD_SUFFIX)]:
			del self.values[key]


def installer(version):
	# not valid UTF-8, like a real installer
	return b"MZ\x90\x00\xff\xfe" + f"crenya pos {version}".encode()


def sha(content):
	return hashlib.sha256(content).hexdigest()


def signature(version):
	return f"dW50cnVzdGVkIGNvbW1lbnQ6IHNpZ25hdHVyZQo{version.replace('.', '')}\nsecond line=="


def entry(version, channel="stable", host=HOST, targets=(WIN,)):
	content = installer(version)
	return {
		"version": version,
		"channel": channel,
		"pub_date": PUB_DATE,
		"notes": f"Notes of {version}",
		"updater": {
			target: {
				"url": f"{host}/pos/{version}/crenya-pos_{version}_x64-setup.exe",
				"signature": signature(version),
				"sha256": sha(content),
				"size": len(content),
			}
			for target in targets
		},
		"installers": [
			{
				"platform": "windows",
				"label": "Windows",
				"label_ar": "ويندوز",
				"url": f"{host}/pos/{version}/crenya-pos_{version}_x64-setup.exe",
				"sha256": sha(content),
				"size": len(content),
			}
		],
	}


def manifest(*entries):
	return {"schema": 1, "product": "crenya pos", "releases": list(entries)}


def local_release(version, channel="stable"):
	return {
		"name": version,
		"version": version,
		"channel": channel,
		"published": 1,
		"pub_date": "2026-10-01 12:00:00",
		"notes": f"Local {version}",
		"modified": "2026-10-01 12:00:00",
		"artifacts": [{"target": WIN, "file": f"/private/files/{version}.exe", "signature": LOCAL_SIGNATURE}],
	}


def context(device_type="till"):
	device = {"name": "dev-1234", "device_id": "dev-1234", "enabled": 1, "device_type": device_type}
	return DeviceContext(device=device, profile=None)


class CentralCase(unittest.TestCase):
	"""Patches the site away: cache, site config, logger, disk folder and the manifest fetch."""

	product = central.RETAIL
	manifest_url = f"{HOST}/pos/releases.json"

	def setUp(self):
		self.cache = FakeCache()
		self.conf = {self.product.config_key: self.manifest_url}
		self.fetch = mock.Mock(return_value=manifest())
		# the site's folder (the verified artifacts' disk cache)
		self.site = tempfile.mkdtemp()
		self.addCleanup(shutil.rmtree, self.site, True)
		patches = [
			mock.patch.object(
				central.frappe, "get_site_path", lambda *parts: os.path.join(self.site, *parts), create=True
			),
			mock.patch.object(central.frappe, "cache", self.cache, create=True),
			mock.patch.object(central.frappe, "conf", self.conf, create=True),
			# no site, so no log files: the warning test checks the logger itself
			mock.patch.object(central.frappe, "logger", create=True),
			mock.patch.object(central, "_fetch_manifest", self.fetch),
			mock.patch.object(central, "raise_api_error", side_effect=raise_refused),
		]
		for patch in patches:
			patch.start()
			self.addCleanup(patch.stop)

	def offered(self, channels=("stable",), target=WIN):
		return [row["version"] for row in central.central_releases(self.product, channels, target)]

	def artifact(self, version, channels=("stable",), target=WIN):
		release = central.central_release(self.product, f"central:{version}", channels, target)
		if release is None:
			raise Refused(NotFoundError, "not offered")
		return central.central_artifact(self.product, release, target)


class ManifestCases:
	"""Run for every product (the subclasses set `product` and `manifest_url`)."""

	def test_manifest_url_override_and_off(self):
		self.assertEqual(central.manifest_url(self.product), self.manifest_url)
		del self.conf[self.product.config_key]
		self.assertEqual(central.manifest_url(self.product), self.product.manifest_url_default)
		# an empty value turns the central source off: nothing is fetched
		self.conf[self.product.config_key] = ""
		self.assertIsNone(central.manifest_url(self.product))
		self.assertEqual(central.manifest_entries(self.product), [])
		self.assertEqual(central.downloads(self.product), {"installers": []})
		self.fetch.assert_not_called()

	def test_central_release_keeps_signature_and_checks_digest(self):
		self.fetch.return_value = manifest(entry("0.7.0"), entry("0.6.0"))
		rows = central.central_releases(self.product, ("stable",), WIN)
		self.assertEqual([row["name"] for row in rows], ["central:0.7.0", "central:0.6.0"])
		artifact = rows[0][update.ARTIFACT_FIELD][0]
		# the manifest's signature byte for byte
		self.assertEqual(artifact["signature"], signature("0.7.0"))
		self.assertEqual(rows[0]["pub_date"], PUB_DATE)

		good = installer("0.7.0")
		with mock.patch.object(central, "_fetch_bytes", return_value=good) as fetch_bytes:
			self.assertEqual(self.artifact("0.7.0"), good)
		url, limit = fetch_bytes.call_args.args
		self.assertEqual(urlsplit(url).netloc, "dl.example.test")
		self.assertEqual(limit, len(good) + 1)
		self.assertEqual(central.artifact_filename(rows[0], WIN), "crenya-pos_0.7.0_x64-setup.exe")

		# same size, other bytes; one byte more; one byte less: refused (nothing cached yet)
		shutil.rmtree(self.site)
		for wrong in (good[:-1] + b"X", good + b"X", good[:-1]):
			with self.subTest(wrong=wrong), mock.patch.object(central, "_fetch_bytes", return_value=wrong):
				with self.assertRaises(Refused) as caught:
					self.artifact("0.7.0")
				self.assertIs(caught.exception.args[0], NotFoundError)

	def test_verified_artifact_cached_on_disk_by_sha256(self):
		self.fetch.return_value = manifest(entry("0.7.0"))
		good = installer("0.7.0")
		path = central._artifact_cache_path(self.product, sha(good))
		self.assertTrue(path.startswith(os.path.join(self.site, "private", self.product.logger)))
		with mock.patch.object(central, "_fetch_bytes", return_value=good) as fetch_bytes:
			self.assertEqual(self.artifact("0.7.0"), good)
			# served from the disk afterwards: a hash already held is never fetched again
			self.assertEqual(self.artifact("0.7.0"), good)
		self.assertEqual(fetch_bytes.call_count, 1)
		with open(path, "rb") as handle:
			self.assertEqual(handle.read(), good)
		# written atomically: no temporary file is left
		self.assertEqual(os.listdir(os.path.dirname(path)), [os.path.basename(path)])
		# a damaged copy is never served: fetched and verified again
		with open(path, "wb") as handle:
			handle.write(b"damaged")
		with mock.patch.object(central, "_fetch_bytes", return_value=good) as fetch_bytes:
			self.assertEqual(self.artifact("0.7.0"), good)
		self.assertEqual(fetch_bytes.call_count, 1)
		# a refused download is not cached
		shutil.rmtree(self.site)
		with (
			mock.patch.object(central, "_fetch_bytes", return_value=b"x" * len(good)),
			self.assertRaises(Refused),
		):
			self.artifact("0.7.0")
		self.assertFalse(os.path.exists(path))

	def test_unknown_central_release_is_not_offered(self):
		self.fetch.return_value = manifest(entry("0.7.0"), entry("0.8.0", channel="beta"))
		for release in ("central:0.9.9", "central:0.8.0", "central:", "0.7.0"):
			with self.subTest(release=release):
				self.assertIsNone(central.central_release(self.product, release, ("stable",), WIN))
		# no artifact for the target
		self.assertIsNone(central.central_release(self.product, "central:0.7.0", ("stable",), MAC))

	def test_beta_channel_sees_central_beta(self):
		self.fetch.return_value = manifest(entry("0.7.0"), entry("0.8.0", channel="beta"))
		self.assertEqual(self.offered(), ["0.7.0"])
		self.assertEqual(self.offered(("stable", "beta")), ["0.7.0", "0.8.0"])

	def test_manifest_down_offers_nothing_central(self):
		for failure in (ConnectionError("down"), TimeoutError("slow"), ValueError("not JSON")):
			with self.subTest(failure=failure):
				self.cache.values.clear()
				self.fetch.side_effect = failure
				self.assertEqual(self.offered(), [])
		self.fetch.side_effect = None
		# an answer that is not the manifest's shape is ignored as a whole
		for bad in ([], {"schema": 2, "releases": []}, {"schema": 1}, {"schema": 1, "releases": {}}):
			with self.subTest(bad=bad):
				self.cache.values.clear()
				self.fetch.return_value = bad
				self.assertEqual(self.offered(), [])

	def test_last_good_manifest_is_kept_and_fetches_are_cached(self):
		self.fetch.return_value = manifest(entry("0.7.0"))
		self.assertEqual(self.offered(), ["0.7.0"])
		self.assertEqual(self.offered(), ["0.7.0"])
		self.assertEqual(self.fetch.call_count, 1)  # the 10-minute copy
		self.assertIn(central.MANIFEST_CACHE_SECONDS, self.cache.ttl.values())
		self.assertIn(central.LAST_GOOD_SECONDS, self.cache.ttl.values())
		self.assertEqual(central.MANIFEST_CACHE_SECONDS, 10 * 60)
		self.assertEqual(central.LAST_GOOD_SECONDS, 7 * 24 * 3600)
		# the manifest goes down after the copy expired: the last good one is offered
		self.cache.expire_fresh()
		self.fetch.side_effect = ConnectionError("down")
		self.assertEqual(self.offered(), ["0.7.0"])
		self.assertEqual(self.fetch.call_count, 2)
		# and the failure is not retried on every check
		self.assertEqual(self.offered(), ["0.7.0"])
		self.assertEqual(self.fetch.call_count, 2)

	def test_a_failed_fetch_is_logged_once_per_cache_period(self):
		self.fetch.side_effect = ConnectionError("Name or service not known")
		logger = mock.Mock()
		with mock.patch.object(central.frappe, "logger", return_value=logger, create=True) as get_logger:
			self.offered()
			self.offered()
			self.assertEqual(logger.warning.call_count, 1)
			get_logger.assert_called_with(self.product.logger)
			message = logger.warning.call_args.args[0]
			self.assertIn(self.manifest_url, message)
			self.assertIn("Name or service not known", message)
			# the next period: one more
			self.cache.expire_fresh()
			self.offered()
			self.assertEqual(logger.warning.call_count, 2)
			# an invalid manifest is logged with its reason too; a good one is not
			self.cache.values.clear()
			self.fetch.side_effect = None
			self.fetch.return_value = {"schema": 2}
			self.offered()
			self.assertEqual(logger.warning.call_count, 3)
			self.assertIn("schema", logger.warning.call_args.args[0])
			self.cache.values.clear()
			self.fetch.return_value = manifest(entry("0.7.0"))
			self.offered()
			self.assertEqual(logger.warning.call_count, 3)

	def test_download_only_from_manifest_host(self):
		self.assertTrue(central.on_manifest_host(f"{HOST}/pos/a.exe", self.manifest_url))
		for url in (
			"https://evil.example.test/pos/a.exe",
			"http://dl.example.test/pos/a.exe",
			"https://dl.example.test:8443/a.exe",
			"https://dl.example.test.evil.test/a.exe",
			"https://user@evil.test/a.exe",
			"file:///etc/passwd",
			"",
			None,
		):
			with self.subTest(url=url):
				self.assertFalse(central.on_manifest_host(url, self.manifest_url))
		# an entry elsewhere is never offered; the rest of the manifest is
		self.fetch.return_value = manifest(entry("0.8.0", host="https://evil.example.test"), entry("0.7.0"))
		self.assertEqual(self.offered(), ["0.7.0"])
		# a row whose host no longer is the manifest's (the site config changed) is never fetched
		self.fetch.return_value = manifest(entry("0.7.0"))
		release = central.central_release(self.product, "central:0.7.0", ("stable",), WIN)
		self.conf[self.product.config_key] = "https://other.example.test/pos/releases.json"
		with mock.patch.object(central, "_fetch_bytes") as fetch_bytes, self.assertRaises(Refused):
			central.central_artifact(self.product, release, WIN)
		fetch_bytes.assert_not_called()

	def test_invalid_entries_are_skipped(self):
		broken = [
			{**entry("0.9.0"), "version": "0.9"},
			{**entry("0.9.1"), "channel": "nightly"},
			{**entry("0.9.2"), "pub_date": "yesterday"},
		]
		for key, value in (
			("signature", " "),
			("sha256", "abc"),
			("size", 0),
			("size", True),
			("size", "12"),
			("url", None),
		):
			bad = entry("0.9.3")
			bad["updater"][WIN][key] = value
			broken.append(bad)
		rows = central.parse_manifest(manifest(*broken, entry("0.7.0")), self.manifest_url)
		# a broken release is left out; a broken updater target leaves its release without it
		self.assertEqual([row["version"] for row in rows], ["0.9.3"] * 6 + ["0.7.0"])
		self.assertTrue(all(row["updater"] == {} for row in rows[:6]))
		offered = central.central_rows(rows, ("stable",), WIN)
		self.assertEqual([row["version"] for row in offered], ["0.7.0"])

	def test_newest_stable_installers(self):
		self.fetch.return_value = manifest(entry("0.6.0"), entry("0.7.0"), entry("0.8.0", channel="beta"))
		answer = central.downloads(self.product)
		self.assertEqual(answer["version"], "0.7.0")
		self.assertEqual(answer["pub_date"], PUB_DATE)
		self.assertEqual(answer["notes"], "Notes of 0.7.0")
		content = installer("0.7.0")
		self.assertEqual(
			answer["installers"],
			[
				{
					"platform": "windows",
					"label": "Windows",
					"label_ar": "ويندوز",
					"url": f"{HOST}/pos/0.7.0/crenya-pos_0.7.0_x64-setup.exe",
					"sha256": sha(content),
					"size": len(content),
				}
			],
		)

	def test_nothing_published_centrally(self):
		self.fetch.side_effect = ConnectionError("down")
		self.assertEqual(central.downloads(self.product), {"installers": []})


class TestCentralRetail(ManifestCases, CentralCase):
	product = central.RETAIL

	def test_retail_default_url(self):
		self.assertEqual(central.manifest_url(central.RETAIL), self.manifest_url)
		del self.conf["crenya_pos_releases_url"]
		self.assertEqual(central.manifest_url(central.RETAIL), "https://dl.crenya.com/pos/releases.json")

	def test_retail_product(self):
		self.assertEqual(CENTRAL_RELEASES_URL, "https://dl.crenya.com/pos/releases.json")
		self.assertEqual(CENTRAL_RELEASES_CONFIG, "crenya_pos_releases_url")
		self.assertEqual(
			central.RETAIL,
			central.Product(
				"pos",
				CENTRAL_RELEASES_URL,
				"crenya_pos_releases_url",
				"crenya_pos_api",
				"crenya_pos_api:central",
			),
		)

	def test_empty_site_config_turns_central_off(self):
		self.conf["crenya_pos_releases_url"] = ""
		self.assertIsNone(central.manifest_url(central.RETAIL))

	def test_cache_keys_do_not_collide_between_products(self):
		# both products read the same URL: each keeps its own copy, last good copy and disk folder
		self.conf[OTHER.config_key] = self.manifest_url
		self.fetch.return_value = manifest(entry("0.7.0"))
		self.assertEqual(self.offered(), ["0.7.0"])
		self.fetch.return_value = manifest(entry("0.9.0"))
		self.assertEqual(
			[row["version"] for row in central.central_releases(OTHER, ("stable",), WIN)], ["0.9.0"]
		)
		self.assertEqual(self.offered(), ["0.7.0"])
		self.assertEqual(self.fetch.call_count, 2)
		retail = {key for key in self.cache.values if key.startswith(central.RETAIL.cache_prefix)}
		other = {key for key in self.cache.values if key.startswith(OTHER.cache_prefix)}
		self.assertEqual(len(retail), 2)
		self.assertEqual(len(other), 2)
		self.assertFalse(retail & other)
		digest = "0" * 64
		self.assertNotEqual(
			central._artifact_cache_path(central.RETAIL, digest), central._artifact_cache_path(OTHER, digest)
		)

	def test_a_till_never_waits_long_on_the_manifest(self):
		# connect plus read: a till's update check waits at most 3 s on dl.crenya.com
		self.assertEqual(central.MANIFEST_TIMEOUT, 3)

	def fake_httpx(self, chunks):
		"""httpx stands in: the unit tests need no network."""
		stream = mock.MagicMock()
		stream.__enter__.return_value.iter_bytes.return_value = iter(chunks)
		# httpx.Timeout(seconds) stands in as the seconds themselves
		return types.SimpleNamespace(stream=mock.Mock(return_value=stream), Timeout=lambda seconds: seconds)

	def test_fetches_follow_no_redirect(self):
		body = json.dumps(manifest()).encode()
		httpx = self.fake_httpx([body[:10], body[10:]])
		with mock.patch.dict(sys.modules, {"httpx": httpx}):
			self.assertEqual(FETCH_MANIFEST(self.manifest_url), manifest())
		self.assertEqual(httpx.stream.call_args.kwargs["follow_redirects"], False)
		self.assertAlmostEqual(httpx.stream.call_args.kwargs["timeout"], central.MANIFEST_TIMEOUT, places=1)
		httpx = self.fake_httpx([b"a" * 4, b"b" * 4, b"c" * 4])
		with mock.patch.dict(sys.modules, {"httpx": httpx}):
			# reading stops at the limit
			self.assertEqual(central._fetch_bytes(f"{HOST}/a.exe", 6), b"aaaabb")
		self.assertEqual(httpx.stream.call_args.kwargs["follow_redirects"], False)
		self.assertAlmostEqual(httpx.stream.call_args.kwargs["timeout"], central.ARTIFACT_TIMEOUT, places=1)

	def test_one_overall_deadline(self):
		httpx = self.fake_httpx([b"a" * 4, b"b" * 4, b"c" * 4])
		# 0 s at the start, then 2 s per read: the third read is past a 5 s deadline
		clock = iter([0, 0, 2, 4, 6, 8])
		with (
			mock.patch.dict(sys.modules, {"httpx": httpx}),
			mock.patch.object(central.time, "monotonic", lambda: next(clock)),
			self.assertRaises(TimeoutError),
		):
			central._fetch_bytes(f"{HOST}/a.exe", 100, timeout=5)
		# connecting gets what is left of the deadline
		self.assertEqual(httpx.stream.call_args.kwargs["timeout"], 5)

	def test_manifest_capped_at_one_megabyte(self):
		self.assertEqual(central.MANIFEST_MAX_BYTES, 1024 * 1024)
		chunk = b" " * (256 * 1024)
		httpx = self.fake_httpx([b"{}", *[chunk] * 8])
		with mock.patch.dict(sys.modules, {"httpx": httpx}), self.assertRaises(ValueError):
			FETCH_MANIFEST(self.manifest_url)

	def test_verified_artifact(self):
		content = installer("0.7.0")
		self.assertEqual(central.verified_artifact(content, sha(content), len(content)), content)
		with self.assertRaises(ValueError):
			central.verified_artifact(content, sha(b"other"), len(content))
		with self.assertRaises(ValueError):
			central.verified_artifact(content + b"x", sha(content), len(content))


class TestCentralOtherProduct(ManifestCases, CentralCase):
	product = OTHER
	manifest_url = OTHER.manifest_url_default


class TestRetailUpdateCheck(CentralCase):
	"""`update.check_update` / `get_artifact` of a retail till: the site's own releases merged
	with the central ones; other device types never see a central retail build."""

	def setUp(self):
		super().setUp()
		self.local = []
		patches = [
			mock.patch.object(update, "_published_releases", side_effect=lambda channels, target: self.local),
			mock.patch.object(update, "device_channel", return_value="stable"),
			mock.patch.object(update, "raise_api_error", side_effect=raise_refused),
			mock.patch("frappe.utils.get_url", return_value=SITE),
			mock.patch("frappe.utils.get_system_timezone", return_value="Asia/Muscat"),
		]
		for patch in patches:
			patch.start()
			self.addCleanup(patch.stop)

	def check(self, current="0.5.0", ctx=None, target=WIN):
		return update.check_update(ctx or context(), target, current)

	def test_check_offers_central_release_when_site_has_none(self):
		self.fetch.return_value = manifest(entry("0.7.0"), entry("0.6.0"))
		document = self.check()
		self.assertEqual(list(document), ["version", "notes", "pub_date", "url", "signature"])
		self.assertEqual(document["version"], "0.7.0")
		self.assertEqual(document["signature"], signature("0.7.0").strip())
		self.assertEqual(document["pub_date"], PUB_DATE)
		# the till downloads through its own workspace (its Authorization header stays there)
		parts = urlsplit(document["url"])
		self.assertEqual(f"{parts.scheme}://{parts.netloc}", SITE)
		self.assertEqual(parts.path, update.DOWNLOAD_PATH)
		self.assertEqual(
			parse_qs(parts.query), {"release": ["central:0.7.0"], "target": [WIN], "device_id": ["dev-1234"]}
		)
		good = installer("0.7.0")
		with mock.patch.object(central, "_fetch_bytes", return_value=good):
			self.assertEqual(
				update.get_artifact(context(), "central:0.7.0", WIN), ("crenya-pos_0.7.0_x64-setup.exe", good)
			)

	def test_local_release_same_version_wins(self):
		self.local = [local_release("0.7.0")]
		self.fetch.return_value = manifest(entry("0.7.0"))
		document = self.check()
		self.assertEqual(document["signature"], LOCAL_SIGNATURE)
		self.assertEqual(parse_qs(urlsplit(document["url"]).query)["release"], ["0.7.0"])
		# a local release without an artifact for the target hides nothing
		self.local = [{**local_release("0.7.0"), "artifacts": []}]
		document = self.check()
		self.assertEqual(parse_qs(urlsplit(document["url"]).query)["release"], ["central:0.7.0"])
		# a newer central one still wins over an older local one
		self.local = [local_release("0.7.0")]
		self.cache.values.clear()
		self.fetch.return_value = manifest(entry("0.7.0"), entry("0.8.0"))
		self.assertEqual(self.check()["version"], "0.8.0")

	def test_manifest_down_serves_local_releases(self):
		self.local = [local_release("0.6.0")]
		self.fetch.side_effect = ConnectionError("down")
		document = self.check()
		self.assertEqual(document["version"], "0.6.0")
		self.assertEqual(document["signature"], LOCAL_SIGNATURE)

	def test_central_off_serves_local_releases(self):
		self.conf["crenya_pos_releases_url"] = ""
		self.local = [local_release("0.6.0")]
		self.assertEqual(self.check()["version"], "0.6.0")
		self.fetch.assert_not_called()

	def test_beta_till_sees_central_beta(self):
		self.fetch.return_value = manifest(entry("0.7.0"), entry("0.8.0", channel="beta"))
		self.assertEqual(self.check()["version"], "0.7.0")
		with mock.patch.object(update, "device_channel", return_value="beta"):
			self.assertEqual(self.check()["version"], "0.8.0")
		with self.assertRaises(Refused) as caught:
			update.get_artifact(context(), "central:0.8.0", WIN)
		self.assertIs(caught.exception.args[0], NotFoundError)

	def test_other_device_types_get_no_central_retail_build(self):
		self.fetch.return_value = manifest(entry("0.7.0"))
		for device_type in ("restaurant_pos", "kds", "waiter"):
			with self.subTest(device_type=device_type):
				self.assertIsNone(self.check(ctx=context(device_type)))
				with self.assertRaises(Refused) as caught:
					update.get_artifact(context(device_type), "central:0.7.0", WIN)
				self.assertIs(caught.exception.args[0], NotFoundError)
		self.fetch.assert_not_called()

	def test_unknown_central_release_is_not_found(self):
		self.fetch.return_value = manifest(entry("0.7.0"))
		for release in ("central:0.9.9", "central:"):
			with self.subTest(release=release), self.assertRaises(Refused) as caught:
				update.get_artifact(context(), release, WIN)
			self.assertIs(caught.exception.args[0], NotFoundError)
		with self.assertRaises(Refused):
			update.get_artifact(context(), "central:0.7.0", MAC)


if __name__ == "__main__":
	unittest.main()
