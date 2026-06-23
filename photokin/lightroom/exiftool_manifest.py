#!/usr/bin/env python
"""
CLI helper for extracting metadata via ExifTool and emitting a lightweight
manifest compatible with photo_archiver's manifest processor.

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
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence



DEFAULT_EXIFTOOL_FIELDS: tuple[str, ...] = (
    "EXIF:DateTimeOriginal",
    "EXIF:UserComment",
    "XMP:Description",
    "XMP:Title",
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

    cmd += list(files_n)

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
}


def _first_non_empty(values: Sequence[Optional[str]]) -> Optional[str]:
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


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

    items: List[Dict[str, Any]] = []
    for rec in records:
        src = rec.get("SourceFile") or rec.get("File:FileName") or rec.get("FileName")
        if not src:
            continue

        raw_tags: Dict[str, Any] = {}
        for k, v in rec.items():
            if k == "SourceFile":
                continue
            if fields_n and k not in fields_n:
                continue
            raw_tags[k] = v

        meta: Dict[str, Any] = {"path": str(src), "exiftool": raw_tags}

        candidates_by_key: Dict[str, List[Optional[str]]] = {}

        def consider(tag: str, value: Any) -> None:
            mk = _TAG_TO_MANIFEST_KEY.get(tag)
            if not mk:
                return
            candidates_by_key.setdefault(mk, []).append(None if value is None else str(value))

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
    records = run_exiftool_json(
        exiftool_path=exiftool_path,
        files=files,
        fields=fields,
        include_all_if_no_fields=include_all_if_no_fields,
    )
    items = exiftool_records_to_manifest_items(records, fields)
    return {"items": items}


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(
        description="Extract metadata with ExifTool and emit a manifest-like JSON for photo_archiver."
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
