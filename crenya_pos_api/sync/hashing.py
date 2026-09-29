"""Canonical payload JSON and hash, identical to what the till computes."""

import hashlib
import json


def canonical_json(payload):
	return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(payload):
	return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
