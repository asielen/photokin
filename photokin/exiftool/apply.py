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
- _datfile_name               deterministic ASCII filename for one (file, tag) DATFILE
- _select_datfile_routing     decide which tag values must move off the command line
- _build_exiftool_command     assemble the ExifTool argv for one file's writes
- apply_changeset             PUBLIC: read a changeset NDJSON and write via ExifTool
- main                        PUBLIC: CLI entry (python -m photokin.exiftool)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
from datetime import date, datetime
from json import JSONDecodeError
from typing import Any, Iterable

from .config import ExiftoolConfig, parse_fields, suggest_writable_spelling
from .locate import resolve_exiftool_path

logger = logging.getLogger(__name__)

EXIF_DATE_TAGS = {"EXIF:DateTimeOriginal", "EXIF:CreateDate"}
EXIF_DATE_FORMAT = "%Y:%m:%d %H:%M:%S"
_EXIF_DT_RE = re.compile(r"^\d{4}:\d{2}:\d{2}( \d{2}:\d{2}:\d{2})?$")

# Windows' CreateProcess caps a full command line at 32,767 characters. Measured
# against the real binary in this checkout: a 32,000-character inline tag value
# writes fine, a 40,000-character one raises FileNotFoundError [WinError 206],
# which the per-file handler below catches as an OSError -- so the write simply
# never happens, for every tag on that file, and looks like a missing-binary
# failure rather than what it is. At roughly 1,500 characters per handwritten
# page that ceiling arrives around 20 pages of transcription.
#
# _INLINE_VALUE_MAX gates individual values: anything longer always moves off
# the command line into a DATFILE ExifTool reads directly (`-TAG<=path`), per
# the ``-@ ARGFILE`` vs. ``-TAG<=DATFILE`` decision recorded in
# docs/per-page-captions.md (E2) -- an argfile holds one argument per line and
# silently truncates a multi-line value, which a DATFILE does not.
#
# _COMMAND_LENGTH_BUDGET gates the *whole* command: ``tags_to_write`` can hold
# several fields, so several values individually under the per-value threshold
# can still sum past the OS ceiling. The budget sits well below 32,767 so the
# approximate length check in `_select_datfile_routing` (argv joined by single
# spaces, which slightly under-counts real CreateProcess quoting overhead) still
# leaves margin, and so the exe path and target file path have room too.
_INLINE_VALUE_MAX = 4_000
_COMMAND_LENGTH_BUDGET = 30_000

# ExifTool's own docs warn that ``-TAG<=DATFILE`` looks like shell redirection.
# It is not: the command below is built as an argv list and executed by
# subprocess.run with no shell involved, so the literal ``<`` character never
# reaches a shell to misinterpret. Do not add quoting around it -- a literal
# quote character would become part of the path ExifTool tries to open.
#
# ExifTool's default value charset is already UTF-8, so `-charset` is not
# needed here. `-charset filename=utf8` would matter only if a DATFILE *path*
# itself were non-ASCII; `_datfile_name` keeps every path ASCII, so that never
# arises.
_DATFILE_READ_FAILURE_MARKER = "Error opening file"


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


_TAG_FILENAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _datfile_name(file_index: int, tag: str) -> str:
    """Build a deterministic, ASCII-safe DATFILE basename for one write.

    Args:
        file_index: The changeset line number this write belongs to (1-based).
        tag: The ExifTool tag being routed off the command line, e.g.
            ``"XMP-dc:Description"``.

    Returns:
        A filename unique within one ``apply_changeset`` run and reproducible
        across runs of the same changeset, e.g.
        ``"000007_XMP-dc_Description_5f2a91c4.txt"``. Tag punctuation such as
        ``:`` is replaced because it is not a legal filename character on
        Windows, and a digest of the tag as written follows it because that
        replacement is lossy: every character outside the safe set folds to the
        same underscore, so ``XMP-dc:Description`` and ``XMP-dc/Description``
        -- both reachable through ``--fields``, which validates neither
        uniqueness nor filename-safety -- would otherwise name one file. Two
        tags routed to one path means the second write overwrites the first and
        ExifTool reads the wrong value for one of them, with nothing anywhere
        reporting it: the run succeeds and a field is silently written with
        another field's content.
    """
    safe_tag = _TAG_FILENAME_UNSAFE_RE.sub("_", tag)
    digest = hashlib.sha256(tag.encode("utf-8")).hexdigest()[:8]
    return f"{file_index:06d}_{safe_tag}_{digest}.txt"


def _select_datfile_routing(
    exiftool: str,
    cfg: ExiftoolConfig,
    tags: dict[str, str],
    path: str,
    candidate_paths: dict[str, str],
) -> dict[str, str]:
    """Decide which of one file's tag values must route through a DATFILE.

    Two passes, per decision E3 in docs/per-page-captions.md: first, any value
    longer than ``_INLINE_VALUE_MAX`` always routes. Then the whole command is
    assembled and measured, and -- because the OS limit is on the whole
    command line, not on any single value -- the next-longest still-inline
    value is routed and the command re-measured, repeating until it fits
    ``_COMMAND_LENGTH_BUDGET`` or nothing inline is left to route.

    Args:
        exiftool: Path to the ExifTool executable (needed to measure the
            actual assembled command, not just the tag arguments).
        cfg: Wrapper config, for the same reason.
        tags: Ordered tag -> value mapping for one file's write.
        path: The file path the command targets.
        candidate_paths: Tag -> DATFILE path, precomputed for every tag in
            ``tags`` regardless of whether it ends up routed.

    Returns:
        A ``{tag: path}`` mapping -- a subset of ``candidate_paths`` -- for
        the tags that must be written to a DATFILE and rendered as
        ``-TAG<=path`` rather than inlined.
    """
    routed = {tag for tag, value in tags.items() if len(value) > _INLINE_VALUE_MAX}

    def _command_length() -> int:
        routed_paths = {tag: candidate_paths[tag] for tag in routed}
        cmd = _build_exiftool_command(exiftool, cfg, tags, path, routed_paths)
        # ``subprocess.list2cmdline`` and not ``" ".join``: it is the exact
        # quoting ``subprocess`` applies before handing the string to
        # CreateProcess, so it is what the 32,767-character cap is actually
        # measured against. Joining on spaces looks close enough and is not:
        # a value dense in quotes and backslashes gets every one of them
        # escaped, and the two measures diverge by nearly 2x. Seven tags of
        # 3,998 characters of `\"` pairs -- each under the per-value threshold,
        # so none is force-routed -- measure 28,253 joined and 56,219 as
        # Windows will really see them, which is over the cap this whole
        # function exists to stay under. The function is pure Python and
        # importable everywhere; POSIX limits are far higher, so measuring the
        # Windows way on every platform is conservative, not wrong.
        return len(subprocess.list2cmdline(cmd))

    remaining_by_length = sorted(
        (tag for tag in tags if tag not in routed), key=lambda t: len(tags[t]), reverse=True
    )
    for tag in remaining_by_length:
        if _command_length() <= _COMMAND_LENGTH_BUDGET:
            break
        routed.add(tag)

    return {tag: candidate_paths[tag] for tag in routed}


def _build_exiftool_command(
    exiftool: str,
    cfg: ExiftoolConfig,
    tags: dict[str, str],
    path: str,
    datfile_paths: dict[str, str] | None = None,
) -> list[str]:
    """Assemble the ExifTool argv for one file's tag writes.

    Args:
        exiftool: Path to the ExifTool executable.
        cfg: Wrapper config; controls ``-overwrite_original`` vs. the sidecar
            ``-o`` form.
        tags: Ordered tag -> value mapping to write.
        path: Path to the file the command targets.
        datfile_paths: Tag -> DATFILE path for tags rendered as
            ``-TAG<=path`` instead of inlined as ``-TAG=value`` (E3). A tag
            absent from this mapping is inlined; ``tags[tag]`` is unused for a
            routed tag here -- the caller must already have written that
            value to the given path before running this command.

    Returns:
        The argv list to hand to ``subprocess.run`` (no shell involved, so
        the ``<`` in a routed tag's ``-TAG<=path`` needs no quoting).
    """
    datfile_paths = datfile_paths or {}
    cmd = [exiftool]
    if cfg.write_sidecar_only:
        cmd.extend(["-o", "%d%f.xmp"])
    elif cfg.overwrite_original:
        cmd.append("-overwrite_original")
    for tag, value in tags.items():
        if tag in datfile_paths:
            cmd.append(f"-{tag}<={datfile_paths[tag]}")
        else:
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
    # The tag is also dropped from ``allowed_fields`` below: the warning says
    # "nothing will be written for this tag", so leaving it in would still
    # hand it to ExifTool once per file and reproduce, per file, the very
    # message this warning exists to say once instead.
    unwritable_fields = set()
    for field in allowed_fields:
        better = suggest_writable_spelling(field)
        if better:
            unwritable_fields.add(field)
            summary["warnings"].append(
                {
                    "tag": field,
                    "warning": (
                        f"{field!r} is not a writable ExifTool tag name; use "
                        f"{better!r} instead. Nothing will be written for this tag."
                    ),
                }
            )
    if unwritable_fields:
        allowed_fields = tuple(f for f in allowed_fields if f not in unwritable_fields)

    # One temp directory for the whole batch (E4), not a file per write: a
    # per-write file would need its own cleanup that a crash could skip, and
    # could collide across concurrent runs. This one directory, opened as a
    # context manager around the loop below, is removed on the way out
    # (including on an unhandled exception) and every DATFILE this run writes
    # lives inside it, named deterministically by (line number, tag) via
    # `_datfile_name` so a 500-file batch is reproducible.
    with (
        tempfile.TemporaryDirectory(prefix="photokin-exiftool-") as tmp_dir,
        open(changeset_path, "r", encoding="utf-8") as handle,
    ):
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

            candidate_paths = {
                tag: os.path.join(tmp_dir, _datfile_name(line_number, tag)) for tag in tags_to_write
            }
            datfile_paths = _select_datfile_routing(
                exiftool, cfg, tags_to_write, path, candidate_paths
            )

            try:
                for tag, datfile_path in datfile_paths.items():
                    # newline="" is load-bearing: Python's text-mode write
                    # translates a bare "\n" to "\r\n" on Windows, which
                    # would corrupt every transcription's line breaks
                    # before ExifTool ever reads the file. No BOM either --
                    # ExifTool's default value charset is already UTF-8.
                    with open(datfile_path, "w", encoding="utf-8", newline="") as datfile:
                        datfile.write(tags_to_write[tag])
            except OSError as exc:
                summary["errors"].append(
                    {"path": path, "error": f"Failed to write DATFILE for ExifTool: {exc}"}
                )
                continue

            cmd = _build_exiftool_command(exiftool, cfg, tags_to_write, path, datfile_paths)
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            except (OSError, ValueError) as exc:
                # OSError covers FileNotFoundError (binary went missing
                # mid-batch) and PermissionError (an unexecutable binary, a
                # locked file) alike; ValueError covers subprocess's own
                # argument validation (an embedded NUL byte in a bad path).
                # Per-file, like every other failure in this loop: a batch of
                # 500 does not abort on file 3, and the files already written
                # before it stay written and stay reported.
                summary["errors"].append({"path": path, "error": f"ExifTool failed to start: {exc}"})
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

            if datfile_paths and _DATFILE_READ_FAILURE_MARKER in result.stderr:
                # E5: a corrupted or dropped write must never pass as
                # success just because the process exited 0. Measured
                # against the real binary -- when one DATFILE among
                # several tag writes cannot be opened (e.g. it vanished
                # mid-batch), ExifTool still reports "N image files
                # updated" and exits 0, because the *other* tags on that
                # same command did get written; only stderr names the
                # miss. Trusting the exit code alone would let that file's
                # skipped tag pass silently as written.
                summary["errors"].append(
                    {
                        "path": path,
                        "error": "ExifTool could not read a DATFILE for this write; nothing on this "
                        "file was confirmed written.",
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
