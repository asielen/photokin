#!/usr/bin/env python
"""
CLI helper for extracting metadata via ExifTool and emitting a lightweight
manifest compatible with photokin's manifest processor.

Why this exists:
- Lightroom SDK doesn't expose many metadata fields reliably.
- ExifTool can read embedded + sidecar XMP consistently.
- This script is intentionally standalone: it can be used as an optional
  "hydration" step when you don't already have a Lightroom-produced manifest.

Output shape (manifest-like):
{
  "items": [
    {
      "path": "/abs/path/to/file.jpg",
      "metadata": {
        "path": "/abs/path/to/file.jpg",
        "...": "...",
        "exiftool": { "Photoshop:Instructions": "...", ... }
      }
    },
    ...
  ]
}

It also tries to map a few common ExifTool tags into the manifest's expected
top-level metadata keys (e.g. dateTimeOriginal, userComment, caption, title).
Everything is always preserved under metadata["exiftool"] for debugging/audit.

This module is also imported (not just run) by the ExifTool hydration path
(``photokin.exiftool.hydrate``), which reuses :func:`run_exiftool_json`.

Code map:
- _split_fields                       expand comma-separated --field values
- _normalize_paths                    absolutize/clean input file paths
- run_exiftool_json                   PUBLIC: run exiftool -j and parse the JSON
- manifest_value                      PUBLIC: one tag value -> the shape its key expects
- _first_non_empty                    pick the first present value across tag aliases
- exiftool_records_to_manifest_items  PUBLIC: ExifTool records -> manifest items
- build_manifest                      PUBLIC: read + convert in one call -> manifest
- main                               PUBLIC: CLI entry (python -m ...exiftool_manifest)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence



DEFAULT_EXIFTOOL_FIELDS: tuple[str, ...] = (
    "EXIF:DateTimeOriginal",
    "EXIF:UserComment",
    "XMP:Description",
    "XMP:Title",
    # Keywords are not read for their own sake. ``merge._has_date_keyword``
    # treats a ``DATE:`` keyword as the human "hands off the date" signal, and
    # that marker lives here -- it is what photokin's own canonical keyword tag
    # names. Reading DateTimeOriginal without it arms the date-correction
    # heuristic and disables its only interlock, so a hand-dated print would be
    # re-dated from the model's inference. Read as ``XMP:Subject``, the
    # writable family-0 spelling; ExifTool reports it back under ``-G1`` as
    # ``XMP-dc:Subject``, and as a list whenever the file holds more than one.
    "XMP:Subject",
)

def _split_fields(fields: Sequence[str]) -> List[str]:
    out: List[str] = []
    for f in fields:
        if not f:
            continue
        parts = [p.strip() for p in str(f).split(",") if p.strip()]
        out.extend(parts)
    # de-dupe while preserving order
    seen = set()
    deduped: List[str] = []
    for f in out:
        if f in seen:
            continue
        seen.add(f)
        deduped.append(f)
    return deduped


def _normalize_paths(paths: Sequence[str]) -> List[str]:
    return [str(Path(p).expanduser()) for p in paths if p]


#: Command-line budget, in characters, for one ExifTool invocation. Windows caps
#: a command line at 32767 and fails past it with ``[WinError 206] The filename
#: or extension is too long``, which ``subprocess`` raises as
#: ``FileNotFoundError`` -- indistinguishable from a missing binary, and so
#: reported as one. A folder of a few hundred scans reaches that on its own, and
#: the whole read then fails as a single unit. Batching keeps every invocation
#: well inside the limit; the headroom covers the quoting Windows adds. POSIX
#: allows far more, but one rule that holds everywhere is worth more than the
#: subprocess calls it costs on a very large folder.
_ARGV_BUDGET = 30000

#: Floor for the per-batch budget, so a long field list cannot starve it down to
#: nothing. A batch always carries at least one file regardless.
_MIN_ARGV_BUDGET = 4096

#: Per argument: the separating space plus the quotes Windows may add.
_ARGV_OVERHEAD_PER_ARG = 3


def _argv_cost(arg: str) -> int:
    return len(arg) + _ARGV_OVERHEAD_PER_ARG


def _batch_files(files: List[str], prefix_cost: int) -> List[List[str]]:
    """Split ``files`` so each batch plus the fixed prefix fits one command line.

    Args:
        files: Normalized file paths, in the order they should be requested.
        prefix_cost: Command-line cost of the invariant part of the command
            (binary, switches, tag selectors).

    Returns:
        One or more batches, together holding every path exactly once and in the
        original order. A path too long to fit on its own still gets a batch, so
        no input is silently dropped.
    """
    budget = max(_ARGV_BUDGET - prefix_cost, _MIN_ARGV_BUDGET)
    batches: List[List[str]] = []
    current: List[str] = []
    used = 0
    for path in files:
        cost = _argv_cost(path)
        if current and used + cost > budget:
            batches.append(current)
            current, used = [], 0
        current.append(path)
        used += cost
    if current:
        batches.append(current)
    return batches


def _run_exiftool_batch(
    exiftool_path: str, cmd: List[str], timeout_sec: int
) -> List[Dict[str, Any]]:
    """Run one prepared ExifTool command line and return its parsed JSON list."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except FileNotFoundError as e:
        raise FileNotFoundError(
            f"ExifTool not found at: {exiftool_path!r}. "
            f"Provide --exiftool-path or ensure it's on PATH."
        ) from e

    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(
            "ExifTool failed.\n"
            f"cmd: {' '.join(cmd)}\n"
            f"exit: {proc.returncode}\n"
            f"stderr:\n{proc.stderr}"
        )

    raw = proc.stdout.strip()
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except Exception as e:
        raise RuntimeError(
            "Failed to parse ExifTool JSON output.\n"
            f"cmd: {' '.join(cmd)}\n"
            f"stderr:\n{proc.stderr}\n"
            f"stdout (first 1000 chars):\n{raw[:1000]}"
        ) from e

    if not isinstance(data, list):
        raise RuntimeError("Unexpected ExifTool JSON shape (expected list).")

    return data


def run_exiftool_json(
    exiftool_path: str,
    files: Sequence[str],
    fields: Sequence[str],
    *,
    include_all_if_no_fields: bool = False,
    timeout_sec: int = 60,
) -> List[Dict[str, Any]]:
    """
    Run exiftool and return parsed JSON objects.

    If fields is empty and include_all_if_no_fields is False, we still run exiftool but
    only request SourceFile (minimal). If include_all_if_no_fields is True, we omit
    field selectors and ExifTool returns a lot of tags (can be huge).

    Large file lists are split across several invocations (see
    :data:`_ARGV_BUDGET`) and their records concatenated in input order; a list
    that fits one command line is still sent as exactly one.  ``timeout_sec``
    applies per invocation.
    """
    files_n = _normalize_paths(files)
    if not files_n:
        return []

    cmd: List[str] = [exiftool_path, "-json", "-G1"]

    # ExifTool can be finicky about encoding; UTF-8 is a good default.
    cmd += ["-charset", "filename=utf8", "-charset", "utf8"]

    fields_n = _split_fields(fields)
    if fields_n:
        for f in fields_n:
            cmd.append(f"-{f}")
    else:
        if not include_all_if_no_fields:
            # Keep response small; still returns SourceFile plus a minimal set.
            pass
        # else: omit -TAG selectors => lots of tags

    prefix_cost = sum(_argv_cost(part) for part in cmd)
    records: List[Dict[str, Any]] = []
    for batch in _batch_files(files_n, prefix_cost):
        records.extend(_run_exiftool_batch(exiftool_path, cmd + batch, timeout_sec))
    return records


_TAG_TO_MANIFEST_KEY: Dict[str, str] = {
    # capture time
    "EXIF:DateTimeOriginal": "dateTimeOriginal",
    "XMP:DateTimeOriginal": "dateTimeOriginal",
    "XMP-exif:DateTimeOriginal": "dateTimeOriginal",

    # notes/context (canonical source: EXIF UserComment)
    "EXIF:UserComment": "userComment",

    # Lightroom/legacy instruction tags are normalized into userComment
    "Photoshop:Instructions": "userComment",
    "XMP:Instructions": "userComment",
    "XMP-photoshop:Instructions": "userComment",
    "IPTC:SpecialInstructions": "userComment",
    "XMP-iptcCore:Instructions": "userComment",

    # caption/description
    "XMP:Description": "caption",
    "XMP-dc:Description": "caption",
    "IPTC:Caption-Abstract": "caption",

    # title
    "XMP:Title": "title",
    "XMP-dc:Title": "title",
    "IPTC:ObjectName": "title",

    # keywords (multi-valued; carries the reviewed "DATE:" marker)
    "XMP:Subject": "keywords",
    "XMP-dc:Subject": "keywords",
}

#: Manifest keys whose value is a list of strings rather than one string.
_LIST_MANIFEST_KEYS: frozenset[str] = frozenset({"keywords"})

# run_exiftool_json requests -G1, so ExifTool returns fine-grained group names
# (e.g. "ExifIFD:UserComment", "XMP-dc:Description") that do not match the
# family-0 keys above. This mirror, keyed by the bare tag name, lets the mapping
# succeed regardless of the reported group.
_BARE_TAG_TO_MANIFEST_KEY: Dict[str, str] = {
    tag.rsplit(":", 1)[-1]: mk for tag, mk in _TAG_TO_MANIFEST_KEY.items()
}


def _bare_tag(tag: str) -> str:
    """Tag name without its group prefix, e.g. 'ExifIFD:UserComment' -> 'UserComment'."""
    return tag.rsplit(":", 1)[-1]


def _find_tag_value(rec: Dict[str, Any], tag: str) -> Any:
    """Look up ``tag`` in an ExifTool record, tolerant of group mismatches.

    ``run_exiftool_json`` requests ``-G1`` (fine-grained group names), so a
    request for e.g. ``EXIF:UserComment`` comes back keyed as
    ``ExifIFD:UserComment``. Match on the exact key first, then fall back to
    comparing bare tag names.
    """
    if tag in rec:
        return rec[tag]
    bare = _bare_tag(tag)
    for key, value in rec.items():
        if key != "SourceFile" and _bare_tag(key) == bare:
            return value
    return None


def manifest_value(value: Any, manifest_key: str) -> str | List[str] | None:
    """Normalize one ExifTool tag value into the shape its manifest key expects.

    ExifTool returns a multi-valued tag (``XMP:Subject``) as a JSON list when the
    file holds several values and as a bare string when it holds one, so a
    list-valued key has to accept both. Everything else is a single trimmed
    string. ``None`` means "nothing usable here", which is what both callers
    test to decide whether a value is worth storing.

    Args:
        value: The raw value from an ExifTool JSON record.
        manifest_key: The manifest key it maps to, per
            :data:`_TAG_TO_MANIFEST_KEY`.

    Returns:
        A trimmed string, a list of trimmed strings for a list-valued key, or
        ``None`` when nothing non-empty remains.
    """
    if manifest_key in _LIST_MANIFEST_KEYS:
        raw = value if isinstance(value, (list, tuple)) else [value]
        items = [str(v).strip() for v in raw if v is not None and str(v).strip()]
        return items or None
    if isinstance(value, (list, tuple)):
        # A single-valued key that came back multi-valued: take the first
        # usable entry rather than stringifying the list into the metadata.
        value = next((v for v in value if v is not None and str(v).strip()), None)
    if value is None:
        return None
    return str(value).strip() or None


def _first_non_empty(
    values: Sequence[str | List[str] | None],
) -> str | List[str] | None:
    """Return the first already-normalized value that is not ``None``."""
    return next((v for v in values if v is not None), None)


def exiftool_records_to_manifest_items(
    records: List[Dict[str, Any]],
    requested_fields: Sequence[str],
) -> List[Dict[str, Any]]:
    """
    Convert ExifTool JSON records to manifest items.

    - Always stores raw tag/value pairs under metadata["exiftool"].
    - Also maps selected tags into top-level manifest keys (dateTimeOriginal, userComment, etc.).
    - If multiple tags map to the same manifest key, the first non-empty wins.
    """
    fields_n = _split_fields(requested_fields)
    # Records come back from `-G1`, so a requested "EXIF:UserComment" is keyed as
    # "ExifIFD:UserComment". Compare on bare tag names too so requested fields are
    # still retained and mapped.
    requested_bare = {_bare_tag(f) for f in fields_n}

    items: List[Dict[str, Any]] = []
    for rec in records:
        src = rec.get("SourceFile") or rec.get("File:FileName") or rec.get("FileName")
        if not src:
            continue

        raw_tags: Dict[str, Any] = {}
        for k, v in rec.items():
            if k == "SourceFile":
                continue
            if fields_n and k not in fields_n and _bare_tag(k) not in requested_bare:
                continue
            raw_tags[k] = v

        meta: Dict[str, Any] = {"path": str(src), "exiftool": raw_tags}

        candidates_by_key: Dict[str, List[str | List[str] | None]] = {}

        def consider(tag: str, value: Any) -> None:
            mk = _TAG_TO_MANIFEST_KEY.get(tag) or _BARE_TAG_TO_MANIFEST_KEY.get(_bare_tag(tag))
            if not mk:
                return
            candidates_by_key.setdefault(mk, []).append(manifest_value(value, mk))

        source_dict = raw_tags if raw_tags else {k: v for k, v in rec.items() if k != "SourceFile"}
        for tag, value in source_dict.items():
            consider(tag, value)

        for mk, vals in candidates_by_key.items():
            chosen = _first_non_empty(vals)
            if chosen is not None:
                meta[mk] = chosen

        items.append({"path": str(src), "metadata": meta})

    return items


def build_manifest(
    exiftool_path: str,
    files: Sequence[str],
    fields: Sequence[str],
    *,
    include_all_if_no_fields: bool = False,
) -> Dict[str, Any]:
    """Read metadata for ``files`` via ExifTool and return a manifest dict.

    Thin composition of :func:`run_exiftool_json` +
    :func:`exiftool_records_to_manifest_items`; the result (``{"items": [...]}``)
    is directly consumable by the manifest processor.
    """
    records = run_exiftool_json(
        exiftool_path=exiftool_path,
        files=files,
        fields=fields,
        include_all_if_no_fields=include_all_if_no_fields,
    )
    items = exiftool_records_to_manifest_items(records, fields)
    return {"items": items}


def main(argv: List[str]) -> int:
    """CLI entry: extract metadata for the given files and print a manifest JSON.

    Invoked as ``python -m photokin.exiftool.manifest``; writes to
    ``--out`` if given, else stdout. Returns a process exit code.
    """
    p = argparse.ArgumentParser(
        description="Extract metadata with ExifTool and emit a manifest-like JSON for photokin."
    )
    p.add_argument("files", nargs="+", help="One or more image paths to read metadata from.")
    p.add_argument(
        "--field",
        action="append",
        default=[],
        help=(
            "ExifTool tag to request (repeatable). Examples: "
            "Photoshop:Instructions, XMP:Description, EXIF:DateTimeOriginal. "
            "You can also pass a comma-separated list."
        ),
    )
    p.add_argument(
        "--exiftool-path",
        default="exiftool",
        help="Path to the exiftool executable (default: exiftool on PATH).",
    )
    p.add_argument(
        "--include-all-if-no-fields",
        action="store_true",
        help="If no --field is provided, return all ExifTool tags (large output).",
    )
    p.add_argument("--out", default="", help="Optional output file path. If omitted, prints to stdout.")

    args = p.parse_args(argv[1:])
    fields = _split_fields(args.field)
    if not fields:
        fields = list(DEFAULT_EXIFTOOL_FIELDS)

    manifest = build_manifest(
        exiftool_path=args.exiftool_path,
        files=args.files,
        fields=fields,
        include_all_if_no_fields=bool(args.include_all_if_no_fields),
    )

    blob = json.dumps(manifest, ensure_ascii=False, indent=2)


    if args.out:
        Path(args.out).write_text(blob, encoding="utf-8")
    else:
        print(blob)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
