import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from packaging.version import Version

from crenya_pos_api.sync.update import (
	BETA,
	STABLE,
	TARGETS,
	channels_for,
	download_url,
	duplicate_targets,
	parse_client_version,
	parse_release_version,
	select_release,
	updater_document,
	validate_target,
)
from crenya_pos_api.utils.dates import site_naive_to_utc_iso

WIN = "windows-x86_64"
MAC = "darwin-aarch64"


def release(version, channel=STABLE, published=1, targets=(WIN,), notes=None):
	return {
		"name": version,
		"version": version,
		"channel": channel,
		"published": published,
		"notes": notes,
		"artifacts": [
			{
				"target": target,
				"file": f"/private/files/crenya-pos_{version}_{target}.exe",
				"signature": "c2ln",
			}
			for target in targets
		],
	}


RELEASES = [
	release("0.1.0"),
	release("0.2.0", published=0),
	release("0.1.5", notes="Faster sync"),
	release("0.1.10", targets=(MAC,)),
	release("0.3.0", channel=BETA),
]


def picked(releases, current, target=WIN, channels=(STABLE,)):
	match = select_release(releases, current, target, channels)
	return match and match[0]["version"]


class TestVersions(unittest.TestCase):
	def test_release_versions_are_plain_semver(self):
		self.assertEqual(parse_release_version("0.2.0"), Version("0.2.0"))
		self.assertEqual(parse_release_version(" 10.0.12 "), Version("10.0.12"))
		for value in (None, "", "1.2", "1.2.3.4", "v1.2.3", "01.2.3", "1.2.3-beta.1", "1.2.x", 123):
			with self.subTest(value=value), self.assertRaises(ValueError):
				parse_release_version(value)

	def test_comparison_is_semantic(self):
		self.assertGreater(parse_release_version("0.1.10"), parse_release_version("0.1.9"))
		self.assertGreater(parse_release_version("1.0.0"), parse_release_version("0.99.99"))

	def test_client_version(self):
		self.assertEqual(parse_client_version("0.1.0"), Version("0.1.0"))
		self.assertEqual(parse_client_version(" 0.2.0-beta.1 "), Version("0.2.0b1"))
		self.assertLess(parse_client_version("0.2.0-beta.1"), parse_release_version("0.2.0"))
		for value in (None, "", "  ", "not-a-version", "1" * 65, 1):
			with self.subTest(value=value), self.assertRaises(ValueError):
				parse_client_version(value)


class TestTargets(unittest.TestCase):
	def test_known_targets(self):
		for target in TARGETS:
			self.assertEqual(validate_target(target), target)
		self.assertEqual(validate_target(" linux-x86_64 "), "linux-x86_64")

	def test_unknown_targets(self):
		for value in (None, "", "windows", "windows-i686", "Windows-x86_64", ["windows-x86_64"]):
			with self.subTest(value=value), self.assertRaises(ValueError):
				validate_target(value)

	def test_duplicate_targets(self):
		rows = [{"target": WIN}, {"target": MAC}, {"target": WIN}, {"target": None}, {"target": WIN}]
		self.assertEqual(duplicate_targets(rows), [WIN])
		self.assertEqual(duplicate_targets([{"target": WIN}, {"target": MAC}]), [])


class TestSelection(unittest.TestCase):
	def test_newest_published_newer_release(self):
		self.assertEqual(picked(RELEASES, "0.1.0"), "0.1.5")
		self.assertEqual(picked(RELEASES, Version("0.1.0")), "0.1.5")

	def test_nothing_newer(self):
		self.assertIsNone(picked(RELEASES, "0.1.5"))
		self.assertIsNone(picked(RELEASES, "0.2.0"))
		self.assertIsNone(picked([], "0.1.0"))

	def test_unpublished_is_ignored(self):
		self.assertIsNone(picked([release("0.2.0", published=0)], "0.1.0"))

	def test_target_must_have_an_artifact(self):
		self.assertEqual(picked(RELEASES, "0.1.0", target=MAC), "0.1.10")
		self.assertIsNone(picked(RELEASES, "0.1.0", target="linux-x86_64"))
		unsigned = release("0.4.0")
		unsigned["artifacts"][0]["signature"] = "  "
		self.assertIsNone(picked([unsigned], "0.1.0"))

	def test_channels(self):
		self.assertEqual(picked(RELEASES, "0.1.0", channels=channels_for(BETA)), "0.3.0")
		self.assertEqual(picked(RELEASES, "0.1.0", channels=channels_for(STABLE)), "0.1.5")
		self.assertEqual(picked(RELEASES, "0.1.0", channels=channels_for(None)), "0.1.5")
		# a beta till is offered a stable release newer than the latest beta
		self.assertEqual(picked([*RELEASES, release("0.4.0")], "0.3.0", channels=channels_for(BETA)), "0.4.0")

	def test_order_of_rows_does_not_matter(self):
		self.assertEqual(picked(list(reversed(RELEASES)), "0.1.0"), "0.1.5")

	def test_invalid_release_versions_are_skipped(self):
		self.assertEqual(picked([release("0.9"), release("0.1.5")], "0.1.0"), "0.1.5")

	def test_pre_release_client_gets_final(self):
		self.assertEqual(picked([release("0.2.0")], "0.2.0-beta.1"), "0.2.0")

	def test_invalid_current_version(self):
		with self.assertRaises(ValueError):
			select_release(RELEASES, "latest", WIN)


class TestUpdaterDocument(unittest.TestCase):
	def test_body_shape(self):
		rel, artifact = select_release(RELEASES, "0.1.0", WIN)
		artifact = dict(artifact, signature="  dW50cnVzdGVkIGNvbW1lbnQ=\n")
		url = download_url("https://erp.example.om/", rel["name"], WIN, "dev-1234-abcd")
		body = updater_document(rel, artifact, url, "2026-10-01T08:00:00Z")
		self.assertEqual(list(body), ["version", "notes", "pub_date", "url", "signature"])
		self.assertEqual(body["version"], "0.1.5")
		self.assertEqual(body["notes"], "Faster sync")
		self.assertEqual(body["pub_date"], "2026-10-01T08:00:00Z")
		self.assertEqual(body["signature"], "dW50cnVzdGVkIGNvbW1lbnQ=")

		parts = urlsplit(body["url"])
		self.assertEqual(f"{parts.scheme}://{parts.netloc}", "https://erp.example.om")
		self.assertEqual(parts.path, "/api/method/crenya_pos_api.api.update.download")
		self.assertEqual(
			parse_qs(parts.query),
			{"release": ["0.1.5"], "target": [WIN], "device_id": ["dev-1234-abcd"]},
		)

	def test_empty_notes(self):
		rel, artifact = select_release([release("0.2.0")], "0.1.0", WIN)
		self.assertEqual(updater_document(rel, artifact, "u", None)["notes"], "")

	def test_query_is_encoded(self):
		url = download_url("https://erp.example.om", "0.1.5", WIN, "a b&c")
		self.assertTrue(url.endswith("device_id=a+b%26c"))


class TestPubDate(unittest.TestCase):
	def test_site_time_to_utc(self):
		self.assertEqual(
			site_naive_to_utc_iso(datetime(2026, 10, 1, 12, 0, 0, 500000), "Asia/Muscat"),
			"2026-10-01T08:00:00Z",
		)
		self.assertEqual(
			site_naive_to_utc_iso("2026-10-01 12:00:00.000000", "Asia/Muscat"), "2026-10-01T08:00:00Z"
		)
		self.assertEqual(site_naive_to_utc_iso("2026-10-01 02:30:00", "Asia/Muscat"), "2026-09-30T22:30:00Z")
		self.assertEqual(site_naive_to_utc_iso("2026-10-01 12:00:00", "UTC"), "2026-10-01T12:00:00Z")
		self.assertEqual(site_naive_to_utc_iso("2026-10-01 12:00:00", "Not/AZone"), "2026-10-01T12:00:00Z")
		self.assertEqual(site_naive_to_utc_iso("2026-10-01 12:00:00", None), "2026-10-01T12:00:00Z")
		aware = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
		self.assertEqual(site_naive_to_utc_iso(aware, "Asia/Muscat"), "2026-10-01T12:00:00Z")
		self.assertIsNone(site_naive_to_utc_iso(None, "Asia/Muscat"))
		self.assertIsNone(site_naive_to_utc_iso("", "Asia/Muscat"))


if __name__ == "__main__":
	unittest.main()
