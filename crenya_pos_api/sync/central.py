"""The central release manifest of a desktop product (retail till, restaurant POS, ...).

A build is published once to a static manifest (README, *Till updates*) instead of to each site;
every workspace reads it beside its own release rows. Everything here takes a `Product`, so the
retail till (`RETAIL`, read by `sync.update`) and other apps' devices (crenya_restaurant passes
its own) share one implementation and never share a cache entry or a disk folder.

- Its releases are shaped like a site's own release rows (named `central:<version>`), so
  `sync.update.select_release` and `updater_document` are reused unchanged; a published local
  release of the same version wins (`merged_releases`: a site-only hotfix or beta).
- The manifest is fetched with a 3 s timeout, kept 10 minutes, and its last good copy 7 days:
  unreachable or invalid, the site's own releases are still offered, and one warning (the URL
  and the reason) goes to the product's logger, at most once per 10 minutes (the failure is
  cached like an answer).
- The till still downloads from its workspace: a central artifact is fetched from the
  manifest's own host only, without following redirects, reading at most its `size` plus one
  byte, and refused unless its sha256 and size are the manifest's. A verified artifact is kept
  on the site by its sha256. The till's minisign check against its built-in key stays the only
  trust anchor: the signature is passed through untouched.
"""

import contextlib
import hashlib
import json
import os
import re
import uuid
from typing import NamedTuple
from urllib.parse import urlsplit

import frappe

from crenya_pos_api.constants import CENTRAL_RELEASES_CONFIG, CENTRAL_RELEASES_URL
from crenya_pos_api.sync.errors import NotFoundError, raise_api_error
from crenya_pos_api.sync.update import (
	ARTIFACT_FIELD,
	CENTRAL_PREFIX,
	CHANNELS,
	STABLE,
	TARGETS,
	parse_release_version,
)


class Product(NamedTuple):
	"""A desktop product with its own central manifest."""

	# short name, in log messages
	key: str
	# the manifest's URL when the site config does not set `config_key`
	manifest_url_default: str
	# site config key overriding the URL; an empty value turns the central source off
	config_key: str
	# frappe.logger name, and the app folder of the verified artifacts on the site
	logger: str
	# prefix of the cache keys (the manifest copy and its last good copy)
	cache_prefix: str


RETAIL = Product(
	"pos", CENTRAL_RELEASES_URL, CENTRAL_RELEASES_CONFIG, "crenya_pos_api", "crenya_pos_api:central"
)

MANIFEST_SCHEMA = 1
# seconds, connect plus read: a till's update check never waits long on the manifest
MANIFEST_TIMEOUT = 3
MANIFEST_CACHE_SECONDS = 10 * 60
LAST_GOOD_SECONDS = 7 * 24 * 3600
LAST_GOOD_SUFFIX = ":last_good"
# a central artifact streamed through the workspace (installers are tens of megabytes)
ARTIFACT_TIMEOUT = 120  # seconds
# a manifest larger than this is refused (a real one: a few kilobytes)
MANIFEST_MAX_BYTES = 1024 * 1024
# verified central artifacts are kept under private/<product.logger>/release_artifacts/<sha256>
ARTIFACT_CACHE_FOLDER = "release_artifacts"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")


# pure helpers (no database, no network)


def on_manifest_host(url, manifest):
	"""True when `url` is on the manifest's own scheme, host and port (no user info)."""
	if not isinstance(url, str) or not isinstance(manifest, str):
		return False
	got, want = urlsplit(url.strip()), urlsplit(manifest)
	return (
		bool(got.hostname)
		and got.scheme == want.scheme
		and got.netloc.lower() == want.netloc.lower()
		and "@" not in got.netloc
	)


def _file(value, manifest):
	"""{url, sha256, size} of a manifest file on the manifest's host; ValueError otherwise."""
	if not isinstance(value, dict) or not on_manifest_host(value.get("url"), manifest):
		raise ValueError("a file must be on the manifest's host")
	sha256, size = value.get("sha256"), value.get("size")
	if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
		raise ValueError("sha256 must be 64 lower-case hex digits")
	if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
		raise ValueError("size must be a positive whole number of bytes")
	return {"url": value["url"].strip(), "sha256": sha256, "size": size}


def _text(value):
	return value.strip() if isinstance(value, str) and value.strip() else None


def _release_entry(value, manifest):
	"""One manifest release, checked; ValueError for an invalid one. An invalid updater target or
	installer is left out."""
	if not isinstance(value, dict):
		raise ValueError("a release must be an object")
	version = str(parse_release_version(value.get("version")))
	channel = value.get("channel")
	if channel not in CHANNELS:
		raise ValueError(f"channel must be one of {', '.join(CHANNELS)}")
	pub_date = value.get("pub_date")
	if not isinstance(pub_date, str) or not _UTC_RE.fullmatch(pub_date):
		raise ValueError("pub_date must be a UTC ISO date (YYYY-MM-DDTHH:MM:SSZ)")
	updater = {}
	targets = value.get("updater") if isinstance(value.get("updater"), dict) else {}
	for target, artifact in targets.items():
		try:
			signature = artifact.get("signature") if isinstance(artifact, dict) else None
			if target not in TARGETS or not _text(signature):
				raise ValueError("a target needs a known name and a signature")
			updater[target] = {**_file(artifact, manifest), "signature": signature}
		except ValueError:
			continue
	installers = []
	for row in value.get("installers") if isinstance(value.get("installers"), list) else ():
		try:
			if not isinstance(row, dict) or not _text(row.get("platform")) or not _text(row.get("label")):
				raise ValueError("an installer needs a platform and a label")
			installers.append(
				{
					"platform": _text(row["platform"]),
					"label": _text(row["label"]),
					"label_ar": _text(row.get("label_ar")),
					**_file(row, manifest),
				}
			)
		except ValueError:
			continue
	return {
		"version": version,
		"channel": channel,
		"pub_date": pub_date,
		"notes": value.get("notes") if isinstance(value.get("notes"), str) else "",
		"updater": updater,
		"installers": installers,
	}


def parse_manifest(data, manifest):
	"""The checked releases of a manifest answer (`manifest`: its URL); ValueError when the answer
	is not a manifest of MANIFEST_SCHEMA. An invalid release is left out, the others are kept."""
	if not isinstance(data, dict) or data.get("schema") != MANIFEST_SCHEMA:
		raise ValueError(f"not a schema {MANIFEST_SCHEMA} release manifest")
	if not isinstance(data.get("releases"), list):
		raise ValueError("the manifest has no releases list")
	entries = []
	for value in data["releases"]:
		try:
			entries.append(_release_entry(value, manifest))
		except ValueError:
			continue
	return entries


def central_rows(entries, channels, target):
	"""Manifest `entries` of `channels` with an artifact for `target`, shaped like a site's
	published release rows (named `central:<version>`, `pub_date` already UTC)."""
	return [
		{
			"name": f"{CENTRAL_PREFIX}{entry['version']}",
			"version": entry["version"],
			"channel": entry["channel"],
			"published": 1,
			"central": True,
			"pub_date": entry["pub_date"],
			"notes": entry["notes"],
			ARTIFACT_FIELD: [
				{
					"target": target,
					"file": entry["updater"][target]["url"],
					"signature": entry["updater"][target]["signature"],
					"sha256": entry["updater"][target]["sha256"],
					"size": entry["updater"][target]["size"],
				}
			],
		}
		for entry in entries
		if entry["channel"] in channels and target in entry["updater"]
	]


def _has_artifact(release, target):
	"""Whether a release row offers `target` (a file and a signature: `select_release`'s rule)."""
	return any(
		row.get("target") == target and row.get("file") and (row.get("signature") or "").strip()
		for row in release.get(ARTIFACT_FIELD) or ()
	)


def merged_releases(local, central, target):
	"""The site's releases and the central ones; a local release of the same version wins when
	it offers `target` (one without that target's file hides nothing)."""
	versions = {release.get("version") for release in local if _has_artifact(release, target)}
	return [*local, *(release for release in central if release["version"] not in versions)]


def verified_artifact(content, sha256, size):
	"""`content` when it is exactly `size` bytes with `sha256`; ValueError otherwise."""
	if len(content) != size:
		raise ValueError(f"got {len(content)} bytes, the manifest says {size}")
	if hashlib.sha256(content).hexdigest() != sha256:
		raise ValueError("its sha256 is not the manifest's")
	return content


def _target_artifact(release, target):
	return next((row for row in release.get(ARTIFACT_FIELD) or () if row.get("target") == target), None)


def artifact_filename(release, target):
	"""The file name of a central release's artifact for `target` (its URL's last part)."""
	artifact = _target_artifact(release, target) or {}
	return os.path.basename(urlsplit(artifact.get("file") or "").path) or f"{release['version']}-{target}"


# the site config, cache, disk and network


def manifest_url(product):
	"""The product's central manifest URL, or None when the site turned it off."""
	value = frappe.conf.get(product.config_key)
	if value is None:
		return product.manifest_url_default
	return _text(value)


def _fetch_bytes(url, limit, timeout=ARTIFACT_TIMEOUT):
	"""At most `limit` bytes of `url`, never following a redirect (the network: patched in the
	unit tests)."""
	import httpx

	chunks, total = [], 0
	with httpx.stream("GET", url, timeout=timeout, follow_redirects=False) as response:
		response.raise_for_status()
		for chunk in response.iter_bytes():
			chunks.append(chunk)
			total += len(chunk)
			if total >= limit:
				break
	return b"".join(chunks)[:limit]


def _fetch_manifest(url):
	"""The manifest's JSON, at most MANIFEST_MAX_BYTES; ValueError for a bigger one."""
	body = _fetch_bytes(url, MANIFEST_MAX_BYTES + 1, MANIFEST_TIMEOUT)
	if len(body) > MANIFEST_MAX_BYTES:
		raise ValueError(f"the manifest is larger than {MANIFEST_MAX_BYTES} bytes")
	return json.loads(body)


def manifest_entries(product):
	"""The product's checked central releases: fetched at most every MANIFEST_CACHE_SECONDS;
	when the manifest is unreachable or invalid, its last good copy (LAST_GOOD_SECONDS), else
	none."""
	url = manifest_url(product)
	if not url:
		return []
	key = f"{product.cache_prefix}:{url}"
	entries = frappe.cache.get_value(key)
	if entries is not None:
		return entries
	try:
		entries = parse_manifest(_fetch_manifest(url), url)
		frappe.cache.set_value(key + LAST_GOOD_SUFFIX, entries, expires_in_sec=LAST_GOOD_SECONDS)
	except Exception as e:
		# down, slow, not JSON or not a manifest: never fail a till's update check for it. The
		# fallback is cached below, so this runs (and warns) at most once per cache period
		frappe.logger(product.logger).warning(
			f"{product.key} release manifest {url} unusable ({type(e).__name__}: {e}); "
			"offering the last good copy or the site's own releases"
		)
		entries = frappe.cache.get_value(key + LAST_GOOD_SUFFIX) or []
	frappe.cache.set_value(key, entries, expires_in_sec=MANIFEST_CACHE_SECONDS)
	return entries


def central_releases(product, channels, target):
	"""The central releases of `channels` with an artifact for `target`, as release rows."""
	return central_rows(manifest_entries(product), channels, target)


def central_release(product, name, channels, target):
	"""The central release row `name` (`central:<version>`) of `channels` with an artifact for
	`target`, or None when the manifest offers no such release."""
	return next((row for row in central_releases(product, channels, target) if row["name"] == name), None)


def downloads(product):
	"""The newest stable central release that has installers: {version, pub_date, notes,
	installers: [{platform, label, label_ar, url, sha256, size}]}, or {installers: []}."""
	best = None
	for entry in manifest_entries(product):
		if entry["channel"] != STABLE or not entry["installers"]:
			continue
		if best is None or parse_release_version(entry["version"]) > parse_release_version(best["version"]):
			best = entry
	if best is None:
		return {"installers": []}
	return {key: best[key] for key in ("version", "pub_date", "notes", "installers")}


def _artifact_cache_path(product, sha256):
	"""Where a verified central artifact is kept on the site, by its sha256 (not a File: never in
	the site's Files or their backups)."""
	if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
		raise ValueError("an artifact is kept by its sha256 (64 lower-case hex digits)")
	return frappe.get_site_path("private", product.logger, ARTIFACT_CACHE_FOLDER, sha256)


def _cached_artifact(product, sha256, size):
	"""The verified bytes kept for `sha256`, or None (none kept, or a damaged copy: removed)."""
	path = _artifact_cache_path(product, sha256)
	try:
		# the name is a checked sha256 under the site's own folder
		with open(path, "rb") as handle:  # nosemgrep: frappe-security-file-traversal
			return verified_artifact(handle.read(), sha256, size)
	except FileNotFoundError:
		return None
	except (OSError, ValueError):
		with contextlib.suppress(OSError):
			os.remove(path)
		return None


def _keep_artifact(product, sha256, content):
	"""Keep verified bytes for `sha256`: written to a temporary file, then renamed into place, so
	a reader never sees half a file. A failure only costs a fetch next time."""
	path = _artifact_cache_path(product, sha256)
	temporary = f"{path}.{uuid.uuid4().hex}.tmp"
	try:
		os.makedirs(os.path.dirname(path), exist_ok=True)
		with open(temporary, "wb") as handle:  # nosemgrep: frappe-security-file-traversal
			handle.write(content)
		os.replace(temporary, path)
	except OSError as e:
		with contextlib.suppress(OSError):
			os.remove(temporary)
		frappe.logger(product.logger).warning(f"Could not keep release artifact {sha256}: {e}")


def central_artifact(product, release, target):
	"""The bytes of central `release`'s (a `central_release` row) artifact for `target`,
	checked against the manifest; NotFoundError when it is missing, off the manifest's host, or
	its download is not the manifest's bytes."""
	artifact = _target_artifact(release, target)
	if artifact is None:
		raise_api_error(NotFoundError, f"Release {release['name']} has no file for {target}")
	if not on_manifest_host(artifact["file"], manifest_url(product)):
		raise_api_error(NotFoundError, f"Release {release['name']} is not on the release manifest's host")
	content = _cached_artifact(product, artifact["sha256"], artifact["size"])
	if content is None:
		try:
			content = verified_artifact(
				_fetch_bytes(artifact["file"], artifact["size"] + 1), artifact["sha256"], artifact["size"]
			)
		except Exception as e:
			raise_api_error(
				NotFoundError, f"Release {release['name']} could not be fetched for {target}: {e}"
			)
		_keep_artifact(product, artifact["sha256"], content)
	return content
