"""Till auto-update: release selection and the Tauri updater documents.

The pure helpers at the top take plain dicts and do no database access; the
functions below them load Crenya POS Release rows for an authorized device.
"""

import os
import re
from urllib.parse import urlencode

import frappe
from packaging.version import InvalidVersion, Version

from crenya_pos_api.sync.context import DEVICE_DOCTYPE
from crenya_pos_api.sync.errors import InvalidRequestError, NotFoundError, raise_api_error
from crenya_pos_api.utils.dates import site_naive_to_utc_iso

RELEASE_DOCTYPE = "Crenya POS Release"
ARTIFACT_DOCTYPE = "Crenya POS Release Artifact"
ARTIFACT_FIELD = "artifacts"

STABLE = "stable"
BETA = "beta"
CHANNELS = (STABLE, BETA)

# the updater's {{target}}-{{arch}}
TARGETS = (
	"windows-x86_64",
	"windows-aarch64",
	"darwin-aarch64",
	"darwin-x86_64",
	"linux-x86_64",
)

DOWNLOAD_PATH = "/api/method/crenya_pos_api.api.update.download"
PRIVATE_FILES_PREFIX = "/private/files/"
MAX_VERSION_LENGTH = 64

_RELEASE_VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def parse_release_version(value):
	"""A release version must be plain semver `X.Y.Z` (no leading zeros, no pre-release)."""
	if not isinstance(value, str) or not _RELEASE_VERSION_RE.match(value.strip()):
		raise ValueError(f"version must be X.Y.Z (for example 0.2.0), got {value!r}")
	return Version(value.strip())


def parse_client_version(value):
	"""The version the till runs now, as sent by the updater."""
	if isinstance(value, Version):
		return value
	if not isinstance(value, str) or not value.strip():
		raise ValueError("current_version is required")
	value = value.strip()
	if len(value) > MAX_VERSION_LENGTH:
		raise ValueError(f"current_version is longer than {MAX_VERSION_LENGTH} characters")
	try:
		return Version(value)
	except InvalidVersion:
		raise ValueError(f"current_version {value!r} is not a valid version")


def validate_target(value):
	if not isinstance(value, str) or value.strip() not in TARGETS:
		raise ValueError(f"target must be one of {', '.join(TARGETS)}, got {value!r}")
	return value.strip()


def channels_for(device_channel):
	"""Stable tills only see stable releases; beta tills see beta and stable releases."""
	return (STABLE, BETA) if device_channel == BETA else (STABLE,)


def duplicate_targets(rows):
	"""Targets that appear on more than one artifact row, in first-seen order."""
	seen, duplicates = set(), []
	for row in rows:
		target = row.get("target")
		if not target:
			continue
		if target in seen and target not in duplicates:
			duplicates.append(target)
		seen.add(target)
	return duplicates


def _artifact_for(release, target):
	for row in release.get(ARTIFACT_FIELD) or []:
		if row.get("target") == target and row.get("file") and (row.get("signature") or "").strip():
			return row
	return None


def select_release(releases, current_version, target, channels=(STABLE,)):
	"""Newest published release of `channels` newer than `current_version` with an artifact for `target`.

	`releases` are dicts with `version`, `channel`, `published` and `artifacts`
	(dicts with `target`, `file`, `signature`). Returns `(release, artifact)` or
	None. Releases with an invalid version are ignored.
	"""
	current = parse_client_version(current_version)
	best = None
	for release in releases:
		if not release.get("published") or release.get("channel") not in channels:
			continue
		try:
			version = parse_release_version(release.get("version"))
		except ValueError:
			continue
		if version <= current:
			continue
		artifact = _artifact_for(release, target)
		if artifact and (best is None or version > best[0]):
			best = (version, release, artifact)
	return (best[1], best[2]) if best else None


def download_url(site_url, release, target, device_id):
	query = urlencode({"release": release, "target": target, "device_id": device_id})
	return f"{site_url.rstrip('/')}{DOWNLOAD_PATH}?{query}"


def updater_document(release, artifact, url, pub_date):
	"""The raw Tauri updater response body."""
	return {
		"version": release["version"],
		"notes": release.get("notes") or "",
		"pub_date": pub_date,
		"url": url,
		"signature": (artifact.get("signature") or "").strip(),
	}


# database access (device already authorized by get_device_context)


def _valid_or_raise(fn, value):
	try:
		return fn(value)
	except ValueError as e:
		raise_api_error(InvalidRequestError, str(e))


def device_channel(ctx):
	channel = frappe.db.get_value(DEVICE_DOCTYPE, ctx.device_id, "update_channel")
	return channel if channel in CHANNELS else STABLE


def _published_releases(channels, target):
	releases = frappe.get_all(
		RELEASE_DOCTYPE,
		filters={"published": 1, "channel": ["in", list(channels)]},
		fields=["name", "version", "channel", "published", "pub_date", "notes", "modified"],
	)
	if not releases:
		return []
	artifacts = {}
	for row in frappe.get_all(
		ARTIFACT_DOCTYPE,
		filters={
			"parenttype": RELEASE_DOCTYPE,
			"parentfield": ARTIFACT_FIELD,
			"parent": ["in", [release.name for release in releases]],
			"target": target,
		},
		fields=["parent", "target", "file", "signature"],
	):
		artifacts.setdefault(row.parent, []).append(row)
	for release in releases:
		release[ARTIFACT_FIELD] = artifacts.get(release.name, [])
	return releases


def check_update(ctx, target, current_version):
	"""Updater document for the device, or None when nothing newer is published."""
	from frappe.utils import get_system_timezone, get_url

	target = _valid_or_raise(validate_target, target)
	current = _valid_or_raise(parse_client_version, current_version)
	channels = channels_for(device_channel(ctx))

	match = select_release(_published_releases(channels, target), current, target, channels)
	if not match:
		return None
	release, artifact = match
	url = download_url(get_url(), release["name"], target, ctx.device_id)
	pub_date = site_naive_to_utc_iso(
		release.get("pub_date") or release.get("modified"), get_system_timezone()
	)
	return updater_document(release, artifact, url, pub_date)


def get_artifact(ctx, release, target):
	"""(filename, bytes) of a published release's artifact the device may install."""
	target = _valid_or_raise(validate_target, target)
	if not isinstance(release, str) or not release.strip():
		raise_api_error(InvalidRequestError, "release is required")
	release = release.strip()

	row = frappe.db.get_value(RELEASE_DOCTYPE, release, ["name", "channel", "published"], as_dict=True)
	if not row or not row.published or row.channel not in channels_for(device_channel(ctx)):
		raise_api_error(NotFoundError, f"Release {release} is not available to this device")

	file_url = frappe.db.get_value(
		ARTIFACT_DOCTYPE,
		{"parenttype": RELEASE_DOCTYPE, "parentfield": ARTIFACT_FIELD, "parent": row.name, "target": target},
		"file",
	)
	file_name = file_url and frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not file_name:
		raise_api_error(NotFoundError, f"Release {release} has no file for {target}")

	file = frappe.get_doc("File", file_name)
	content = file.get_content()
	if isinstance(content, str):
		# File.get_content decodes files that happen to be valid UTF-8
		content = content.encode("utf-8")
	return file.file_name or os.path.basename(file_url), content
