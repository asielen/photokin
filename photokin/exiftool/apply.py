"""
photokin.exiftool.apply
=============================

Apply a changeset NDJSON to files using ExifTool for limited, archival fields.

This module is intentionally small: it reads changeset records, filters allowed
tags, normalizes values (especially dates), and invokes ExifTool.

Why this exists separately from the Lightroom SDK: a few archival fields (notably
``EXIF:UserComment`` and date tags) are either unwritable or awkward via the SDK,
so the plugin routes them here. Date normalization is the fiddly part — ExifTool
wants ``YYYY:MM:DD HH:MM:SS``, but changesets carry ISO-ish strings, so most of
the private helpers below exist to coerce values into ExifTool's expected shape.

Code map:
- _normalize_exif_datetime    coerce a datetime/ISO value into EXIF date format
- _looks_like_exif_datetime   cheap check for already-EXIF-formatted strings
- _normalize_exif_string      strip/clean a scalar string value
- _parse_fallback_datetime    last-ditch parse of loose date text
- _normalize_tag_value        dispatch a tag+value to the right normalizer
- _build_exiftool_command     assemble the ExifTool argv for one file's writes
- apply_changeset             PUBLIC: read a changeset NDJSON and write via ExifTool
- main                        PUBLIC: CLI entry (python -m photokin.exiftool)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
from datetime import date, datetime
from json import JSONDecodeError
from typing import Any, Iterable

from .config import ExiftoolConfig, parse_fields, suggest_writable_spelling
from .locate import resolve_exiftool_path

logger = logging.getLogger(__name__)

EXIF_DATE_TAGS = {"EXIF:DateTimeOriginal", "EXIF:CreateDate"}
EXIF_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"
_EXIF_DT_RE = re.compile(r"^\d{4}:\d{2}:\d{2}( \d{2}:\d{2}:\d{2})?$")


def _normalize_exif_datetime(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None, "Empty date string."
        if _looks_like_exif_datetime(raw):
            normalized = _normalize_exif_string(raw)
            return normalized, None
        candidate = raw
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            dt = _parse_fallback_datetime(candidate)
            if dt is None:
                return None, f"Unrecognized date format: {raw!r}."
    else:
        return None, f"Unsupported date type: {type(value).__name__}."

    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt.strftime(EXIF_DATE_FORMAT), None


def _looks_like_exif_datetime(value: str) -> bool:
    return bool(_EXIF_DT_RE.match(value.strip()))


def _normalize_exif_string(value: str) -> str:
    raw = value.strip()
    if len(raw) == 10 and raw.count(":") == 2:
        return f"{raw} 00:00:00"
    if len(raw) == 19 and raw.count(":") == 4:
        return raw
    return raw


def _parse_fallback_datetime(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _normalize_tag_value(tag: str, value: Any, warnings: list[dict[str, Any]]) -> str | None:
    if value is None:
        return None
    if tag in EXIF_DATE_TAGS:
        normalized, warning = _normalize_exif_datetime(value)
        if warning:
            warnings.append({"tag": tag, "warning": warning})
            return None
        return normalized
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    warnings.append({"tag": tag, "warning": f"Unsupported value type: {type(value).__name__}."})
    return None


def _build_exiftool_command(exiftool: str, cfg: ExiftoolConfig, tags: dict[str, str], path: str) -> list[str]:
    cmd = [exiftool]
    if cfg.write_sidecar_only:
        cmd.extend(["-o", "%d%f.xmp"])
    elif cfg.overwrite_original:
        cmd.append("-overwrite_original")
    for tag, value in tags.items():
        cmd.append(f"-{tag}={value}")
    cmd.append(path)
    return cmd


def apply_changeset(
    changeset_path: str,
    cfg: ExiftoolConfig,
    *,
    enabled: bool | None = None,
    fields: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Apply a changeset NDJSON file to photos via ExifTool, returning a summary.

    Each line is one photo's changeset record; only tags in ``allowed_fields``
    (the explicit ``fields`` arg, else ``cfg.fields``) are written, so this stays
    a deliberately narrow archival-write path. Failures are collected per-file
    into ``summary["errors"]`` rather than raised, so one bad file never aborts a
    batch. Honors ``cfg.dry_run`` (preview without writing).

    Returns a summary dict: ``files_seen``, ``files_written``, ``tags_written``,
    ``errors``, ``warnings``, ``dry_run``.
    """
    summary: dict[str, Any] = {
        "files_seen": 0,
        "files_written": 0,
        "tags_written": 0,
        "errors": [],
        "warnings": [],
        "dry_run": cfg.dry_run,
    }

    is_enabled = cfg.enabled if enabled is None else enabled
    if not is_enabled:
        summary["warnings"].append(
            {
                "warning": "ExifTool apply disabled (enabled=False).",
                "changeset": changeset_path,
            }
        )
        return summary

    try:
        exiftool = resolve_exiftool_path(cfg)
    except FileNotFoundError as exc:
        summary["errors"].append({"error": str(exc), "changeset": changeset_path})
        return summary

    logger.debug("Using ExifTool at: %s", exiftool)

    allowed_fields = tuple(fields) if fields is not None else cfg.fields
    allowed_fields = tuple(field.strip() for field in allowed_fields if field.strip())

    # Say once, up front, what ExifTool would otherwise say once per file as
    # "Sorry, ... doesn't exist or isn't writable / Nothing to do" -- a message
    # that names no remedy and, repeated across a batch, reads like a problem
    # with the files rather than with the tag name. The CLI rejects this
    # spelling in pre-flight; this is the same guard for direct library callers
    # (the Lightroom plugin among them), which never pass through that path.
    for field in allowed_fields:
        better = suggest_writable_spelling(field)
        if better:
            summary["warnings"].append(
                {
                    "tag": field,
                    "warning": (
                        f"{field!r} is not a writable ExifTool tag name; use "
                        f"{better!r} instead. Nothing will be written for this tag."
                    ),
                }
            )

    with open(changeset_path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except JSONDecodeError as exc:
                summary["errors"].append(
                    {
                        "line": line_number,
                        "error": f"JSON parse failed: {exc}",
                        "changeset": changeset_path,
                    }
                )
                continue

            summary["files_seen"] += 1
            path = record.get("path")
            if not isinstance(path, str) or not path:
                summary["warnings"].append({"line": line_number, "warning": "Missing path in record."})
                continue

            proposed = record.get("proposed_changes") or {}
            if not isinstance(proposed, dict):
                summary["warnings"].append({"path": path, "warning": "Invalid proposed_changes payload."})
                continue

            set_changes = proposed.get("set") or {}
            if not isinstance(set_changes, dict):
                summary["warnings"].append({"path": path, "warning": "Invalid proposed_changes.set payload."})
                continue

            tags_to_write: dict[str, str] = {}
            for tag in allowed_fields:
                if tag not in set_changes:
                    continue
                value = _normalize_tag_value(tag, set_changes.get(tag), summary["warnings"])
                if value is None:
                    continue
                tags_to_write[tag] = value

            if not tags_to_write:
                continue

            if cfg.dry_run:
                summary["files_written"] += 1
                summary["tags_written"] += len(tags_to_write)
                continue

            cmd = _build_exiftool_command(exiftool, cfg, tags_to_write, path)
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            except FileNotFoundError as exc:
                summary["errors"].append({"path": path, "error": f"ExifTool not found: {exc}"})
                continue

            if result.returncode != 0:
                summary["errors"].append(
                    {
                        "path": path,
                        "error": f"ExifTool failed with code {result.returncode}.",
                        "stderr": result.stderr.strip(),
                    }
                )
                continue

            summary["files_written"] += 1
            summary["tags_written"] += len(tags_to_write)

    return summary


def main() -> None:
    """CLI entry point: apply a changeset NDJSON from the command line.

    Parses ExifTool-apply flags into an :class:`ExiftoolConfig`, runs
    :func:`apply_changeset`, and prints the JSON summary. Invoked as
    ``python -m photokin.exiftool``.
    """
    defaults = ExiftoolConfig()
    parser = argparse.ArgumentParser(description="Apply an ExifTool changeset NDJSON.")
    parser.add_argument("--changeset", required=True, help="Path to a changeset NDJSON file.")
    parser.add_argument(
        "--fields",
        help="Comma-separated list of ExifTool tags to allow (defaults to ExiftoolConfig.fields).",
    )
    parser.add_argument("--enabled", action="store_true", default=defaults.enabled)
    parser.add_argument("--dry-run", action="store_true", default=defaults.dry_run)
    parser.add_argument("--exiftool-path", default=None)
    parser.add_argument(
        "--overwrite-original",
        dest="overwrite_original",
        action="store_true",
        default=defaults.overwrite_original,
    )
    parser.add_argument(
        "--no-overwrite-original",
        dest="overwrite_original",
        action="store_false",
        help="Do not pass -overwrite_original to ExifTool.",
    )
    parser.add_argument(
        "--write-sidecar-only",
        action="store_true",
        default=defaults.write_sidecar_only,
        help="Write .xmp sidecars instead of modifying the original file.",
    )
    parser.add_argument("--output", help="Optional path for JSON summary output.")

    args = parser.parse_args()

    cfg = ExiftoolConfig(
        enabled=args.enabled,
        path=args.exiftool_path or os.environ.get("EXIFTOOL_PATH") or None,
        dry_run=args.dry_run,
        overwrite_original=args.overwrite_original,
        write_sidecar_only=args.write_sidecar_only,
        fields=defaults.fields,
    )

    fields = parse_fields(args.fields)
    summary = apply_changeset(args.changeset, cfg, fields=fields)

    output = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(output)
    else:
        print(output)


if __name__ == "__main__":
    main()
