"""
photokin.changeset
========================

Utilities for emitting a per-photo changeset NDJSON artifact.

The changeset format is intentionally small and explicit so that it can be
inspected, audited, and replayed later without requiring the Lightroom apply
layer. Keep this schema stable: it is meant to be a portable pre-apply
representation rather than a verbatim copy of internal objects.

Canonical tag keys (XMP/IPTC/EXIF) are the authoritative diff surface for the
changeset; legacy field snapshots are intentionally out of scope here.

Code map:
- make_run_id                generate a run id (UTC timestamp + random token)
- make_photo_id             stable id from sha1 of the normalized absolute path
- select_forwarded_metadata pick which original fields are forwarded to the model
- _normalize_keyword_list   trim/de-dup a keyword list for diffing
- diff_canonical_metadata   compact set/keywords_add/keywords_remove diff
- ordered_group_keys        deterministic ordering of group ids
- emit_changeset_record     PUBLIC: write one photo's changeset NDJSON record
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .canonical import CANONICAL_KEYWORDS_TAG
from .utils import DEFAULT_METADATA_FORWARD_FIELDS, normalize_path, union_keywords

# Bumped whenever a record's shape changes in a way a consumer could care
# about -- a new top-level key, a renamed one, a changed meaning for an
# existing one -- mirroring ``cli._NDJSON_SCHEMA_VERSION``'s rule (the one
# other place this package schema-versions an NDJSON stream), independently:
# the two streams (changeset, results) evolve on their own schedules.
#
# History (why each bump happened, not a full changelog):
#   2 -- a multipage group's per-file ``XMP-dc:Description`` value in
#        ``proposed_changes.set`` changed meaning: it used to be the group's
#        whole transcription, identical across every file; it is now that
#        file's own part. The diff's shape is unchanged, but a consumer that
#        compared or deduped that value across a group's files would have
#        gotten a different answer with no shape change to detect it by. See
#        docs/per-page-captions.md, decision E11.
SCHEMA_VERSION = 2


def make_run_id() -> str:
    """Generate a run id from a UTC timestamp plus a short random token."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    token = secrets.token_hex(4)
    return f"{stamp}_{token}"


def make_photo_id(path: str) -> str:
    """Return a stable ID for a file based on sha1(normalized absolute path)."""
    normalized = normalize_path(path) or path
    normalized = os.path.abspath(os.path.normpath(normalized))
    digest = hashlib.sha1(normalized.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"sha1:{digest}"


def select_forwarded_metadata(forwarded_meta: dict | None, forward_fields: list[str] | None) -> dict:
    """Match prompt forwarding logic, including default field allowlist and state normalization."""
    if not forwarded_meta or not isinstance(forwarded_meta, dict):
        return {}

    effective_fields = list(DEFAULT_METADATA_FORWARD_FIELDS)
    if isinstance(forward_fields, list):
        for field in forward_fields:
            if isinstance(field, str) and field not in effective_fields:
                effective_fields.append(field)

    selected = {
        key: deepcopy(value)
        for key, value in forwarded_meta.items()
        if key in effective_fields and value is not None
    }
    if "state" not in selected and selected.get("stateProvince"):
        selected["state"] = deepcopy(selected["stateProvince"])
    return selected


def _normalize_keyword_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    seen: set[str] = set()
    for val in values or []:
        if not isinstance(val, str):
            continue
        trimmed = val.strip()
        lowered = trimmed.lower()
        if trimmed and lowered not in seen:
            seen.add(lowered)
            out.append(trimmed)
    return out


#: The marker key a write-suppressed changeset record carries inside
#: ``proposed_changes``, beside the empty ``set``/delta fields, valued with
#: the reason (currently only ``"hydration_failed"``). It lets the applier --
#: and any other consumer -- tell "nothing to write" from "unreadable"
#: without a schema change to the record itself; the applier counts these so
#: a run whose every record was suppressed can end loudly instead of as a
#: silent success.
WRITE_SUPPRESSED_KEY = "suppressed"


def diff_canonical_metadata(before: dict, after: dict) -> dict:
    """Compute a compact diff in canonical tag space (set/keywords_add/keywords_remove)."""
    before_keywords = _normalize_keyword_list(before.get(CANONICAL_KEYWORDS_TAG))
    after_keywords = _normalize_keyword_list(after.get(CANONICAL_KEYWORDS_TAG))

    before_set = {kw.lower() for kw in before_keywords}
    after_set = {kw.lower() for kw in after_keywords}

    keywords_add = [kw for kw in after_keywords if kw.lower() not in before_set]
    keywords_remove = [kw for kw in before_keywords if kw.lower() not in after_set]
    keywords_add = union_keywords(keywords_add)
    keywords_remove = union_keywords(keywords_remove)

    set_changes: dict[str, Any] = {}
    for key, after_val in after.items():
        if key == CANONICAL_KEYWORDS_TAG:
            continue
        before_val = before.get(key)
        if before_val != after_val:
            set_changes[key] = deepcopy(after_val)

    return {
        "set": set_changes,
        "keywords_add": keywords_add,
        "keywords_remove": keywords_remove,
    }


def ordered_group_keys(buckets: dict[str, list[dict]]) -> list[str]:
    """Return group ids in a deterministic order."""
    return sorted(buckets.keys(), key=lambda k: (k.lower(), k))


def emit_changeset_record(
    writer,
    *,
    run_id: str,
    group_id: str,
    group_key: str,
    path: str,
    sent_to_model: dict,
    file_metadata: dict,
    proposed_changes: dict,
) -> None:
    """Emit a single changeset NDJSON record for a photo file."""
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sent_block = {
        "metadata": deepcopy(sent_to_model),
    }

    record = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": created_at,
        "group_id": group_id,
        "group_key": group_key,
        "photo_id": make_photo_id(path),
        "path": path,
        "original_data": {
            "file_metadata": deepcopy(file_metadata),
            "sent_to_model": sent_block,
        },
        "proposed_changes": deepcopy(proposed_changes),
    }

    writer(json.dumps(record, ensure_ascii=False))
