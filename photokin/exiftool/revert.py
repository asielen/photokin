"""
photokin.exiftool.revert
==============================

Put files back the way a changeset found them.

A bad run can write wrong metadata to hundreds of files, and re-running cannot
undo it: ``-r`` reads the bad values as the files' originals. This reads a
changeset back and restores, per file, every tag the run wrote to its value
before the run, deleting a tag the file did not have. A tag the run never
touched is never written.

The before-values come from ``original_data.written_tags``, which the applier
records just before each write (changeset schema 5). An older changeset has
only ``original_data.file_metadata``, the canonical values ``-r`` read; the
revert falls back to it and lists, per file, the tags whose before-value it
had to infer.

Before restoring a file the revert checks that the file still holds what the
run wrote. A tag that changed since was edited after the run, so the file is
reported as a conflict and left alone unless ``force`` is set.

Code map:
- RevertOptions          which files to revert, and whether to override conflicts
- _first_records         the first applied record per path, and how many there were
- _plan_file             before-values and expected current values for one file
- _same                  do two tag values match, as ExifTool prints them?
- _restore_write         the ExifTool write that puts one file back
- revert_changeset       PUBLIC: revert a changeset, returning one record per file
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from json import JSONDecodeError
from typing import Any

from ..canonical import (
    CANONICAL_HIERARCHICAL_KEYWORDS_TAG,
    CANONICAL_IPTC_KEYWORDS_TAG,
    CANONICAL_KEYWORDS_TAG,
    iim_holds_keyword,
)
from ..changeset import WRITE_SUPPRESSED_KEY
from .apply import (
    _KEYWORD_WRITE_TAGS,
    _TMP_DIR_PREFIX,
    _companion_tags,
    _datfile_name,
    _FileWrite,
    _normalize_tag_value,
    _path_key,
    _run_file_write,
    _snapshot_value,
)
from .config import ExiftoolConfig
from .locate import resolve_exiftool_path
from .manifest import run_exiftool_json

#: Every status a per-file record can carry.
REVERT_STATUSES: tuple[str, ...] = ("reverted", "unchanged", "conflict", "missing", "error")

_DIGITS_RE = re.compile(r"\D")
#: A trailing zone, which ExifTool adds to an IPTC time it reads back.
_ZONE_RE = re.compile(r"(?:[+-]\d{2}:?\d{2}|Z)$")
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class RevertOptions:
    """Which files to revert, and how hard.

    Attributes:
        only_paths: Revert only these files; empty means every file.
        match: Revert only files whose path or basename matches this glob.
        force: Restore a file even when a tag changed after the run.
    """

    only_paths: frozenset[str] = frozenset()
    match: str | None = None
    force: bool = False

    def wants(self, path: str) -> bool:
        """Return whether *path* is in scope.

        Args:
            path: A record's path.

        Returns:
            True when no filter excludes it.
        """
        if self.only_paths and _path_key(path) not in self.only_paths:
            return False
        if self.match is None:
            return True
        return fnmatch.fnmatch(path, self.match) or fnmatch.fnmatch(
            os.path.basename(path), self.match
        )


@dataclass
class _FilePlan:
    """One file's revert, before its current values are known.

    Attributes:
        path: The file.
        entries: How many changeset records named it; the first one is used.
        before: ``{tag: value before the run}`` for each scalar tag, ``None``
            where the file had none.
        before_keywords: ``{keyword tag: list or None}`` from the snapshot;
            empty for an old changeset, which restores keywords from
            ``keyword_delta`` instead.
        expected: ``{tag: value the run wrote}``, what the file should still
            hold if nothing edited it since.
        keyword_delta: ``(added, removed)`` keywords, for an old changeset.
        inferred: Tags whose before-value was inferred, for an old changeset.
    """

    path: str
    entries: int
    before: dict[str, Any] = field(default_factory=dict)
    before_keywords: dict[str, list[str] | None] = field(default_factory=dict)
    expected: dict[str, Any] = field(default_factory=dict)
    keyword_delta: tuple[list[str], list[str]] | None = None
    inferred: list[str] = field(default_factory=list)

    @property
    def tags(self) -> list[str]:
        """Every tag this revert reads or writes."""
        return list(dict.fromkeys((*self.before, *self.before_keywords, *self.expected)))


def _first_records(changeset_path: str) -> tuple[list[tuple[dict, int]], list[dict]]:
    """Return the first applied record per path, and the lines that could not be read.

    Records marked ``write: false`` were never written by ExifTool and are
    skipped. Virtual copies of one file share one original, so the first
    record per path stands for all of them.

    Args:
        changeset_path: The changeset to read.

    Returns:
        ``([(record, entries for its path)], [error record])``, records in
        first-seen order.
    """
    first: dict[str, dict] = {}
    counts: dict[str, int] = {}
    errors: list[dict] = []
    with open(changeset_path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except JSONDecodeError as exc:
                errors.append({"line": line_number, "status": "error", "error": str(exc)})
                continue
            path = record.get("path") if isinstance(record, dict) else None
            if not isinstance(path, str) or not path or record.get("write") is False:
                continue
            key = _path_key(path)
            first.setdefault(key, record)
            counts[key] = counts.get(key, 0) + 1
    return [(record, counts[key]) for key, record in first.items()], errors


def _expected_values(proposed: dict[str, Any]) -> dict[str, Any]:
    """Return the scalar values a record's write put on the file, as the applier wrote them."""
    set_changes = proposed.get("set") or {}
    warnings: list[dict[str, Any]] = []
    written = {
        tag: value
        for tag, raw in set_changes.items()
        if (value := _normalize_tag_value(tag, raw, warnings)) is not None
    }
    return {**written, **_companion_tags(written, set_changes)}


def _plan_file(record: dict, entries: int) -> _FilePlan:
    """Work out one file's before-values and the values the run left on it.

    Args:
        record: The file's first changeset record.
        entries: How many records named the file.

    Returns:
        The plan; empty ``before`` and ``before_keywords`` when the run wrote
        nothing to the file.
    """
    plan = _FilePlan(record["path"], entries)
    proposed = record.get("proposed_changes") or {}
    if not isinstance(proposed, dict) or proposed.get(WRITE_SUPPRESSED_KEY):
        return plan
    original = record.get("original_data") or {}
    snapshot = original.get("written_tags")
    added = [k for k in proposed.get("keywords_add") or [] if isinstance(k, str)]
    removed = [k for k in proposed.get("keywords_remove") or [] if isinstance(k, str)]
    expected = _expected_values(proposed)

    if isinstance(snapshot, dict):
        for tag, value in snapshot.items():
            if tag in _KEYWORD_WRITE_TAGS:
                plan.before_keywords[tag] = value
            else:
                plan.before[tag] = value
        plan.expected = {tag: expected[tag] for tag in plan.before if tag in expected}
        before_subject = snapshot.get(CANONICAL_KEYWORDS_TAG)
    else:
        file_metadata = original.get("file_metadata") or {}
        for tag in expected:
            plan.before[tag] = file_metadata.get(tag)
            if tag not in file_metadata:
                plan.inferred.append(tag)
        plan.expected = expected
        if added or removed:
            plan.keyword_delta = (added, removed)
        before_subject = file_metadata.get(CANONICAL_KEYWORDS_TAG)

    if added or removed:
        held = {k.casefold() for k in _as_list(before_subject)}
        kept = [
            k
            for k in _as_list(before_subject)
            if k.casefold() not in {r.casefold() for r in removed}
        ]
        plan.expected[CANONICAL_KEYWORDS_TAG] = kept + [
            k for k in added if k.casefold() not in held
        ]
    return plan


def _as_list(value: Any) -> list[str]:
    """A keyword tag's value as a list of strings."""
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _same(tag: str, current: Any, wanted: Any) -> bool:
    """Return whether a tag's current value matches *wanted*, as ExifTool prints values.

    Keywords compare as case-insensitive sets. A date or time compares by its
    digits without a trailing zone, since ExifTool prints ``1948-06-01`` back as
    ``1948:06:01`` and ``00:00:00`` as ``00:00:00-08:00``. Text
    compares with runs of whitespace folded.

    Args:
        tag: The tag, which decides the comparison.
        current: The value read from the file, or ``None``.
        wanted: The value to compare against, or ``None`` for absent.

    Returns:
        True when the two are the same value.
    """
    if tag in _KEYWORD_WRITE_TAGS or isinstance(current, list) or isinstance(wanted, list):
        return {k.casefold() for k in _as_list(current)} == {k.casefold() for k in _as_list(wanted)}
    if current is None or wanted is None:
        return (current is None or str(current).strip() == "") == (
            wanted is None or str(wanted).strip() == ""
        )
    if "Date" in tag or "Time" in tag:
        return _date_digits(current) == _date_digits(wanted)
    return _SPACE_RE.sub(" ", str(current)).strip() == _SPACE_RE.sub(" ", str(wanted)).strip()


def _date_digits(value: Any) -> str:
    """A date or time value's digits, without a trailing zone."""
    return _DIGITS_RE.sub("", _ZONE_RE.sub("", str(value).strip()))


def _restore_write(plan: _FilePlan, current: dict[str, Any]) -> tuple[_FileWrite, list[str]]:
    """Build the write that puts one file back, against what it holds now.

    A scalar tag already at its before-value is left out. A tag with no
    before-value is deleted (``-TAG=``). Keywords are restored as deltas
    against the file's current list, because ExifTool does not clear a list
    tag and refill it within one command.

    Args:
        plan: The file's plan.
        current: The file's current values for ``plan.tags``.

    Returns:
        The write, and the tags it touches; both empty when nothing differs.
    """
    tags: dict[str, str] = {}
    for tag, before in plan.before.items():
        if _same(tag, current.get(tag), before):
            continue
        if before is None:
            tags[tag] = ""
        else:
            tags[tag] = _normalize_tag_value(tag, before, []) or str(before)
    keyword_args: list[str] = []
    touched_keywords: list[str] = []
    for tag, before in plan.before_keywords.items():
        now = _as_list(current.get(tag))
        if before is None:
            if now:
                keyword_args.append(f"-{tag}=")
                touched_keywords.append(tag)
            continue
        wanted = _as_list(before)
        wanted_cf = {k.casefold() for k in wanted}
        now_cf = {k.casefold() for k in now}
        ops = [f"-{tag}-={k}" for k in now if k.casefold() not in wanted_cf]
        ops += [f"-{tag}+={k}" for k in wanted if k.casefold() not in now_cf]
        if ops:
            keyword_args.extend(ops)
            touched_keywords.append(tag)
    if plan.keyword_delta is not None:
        added, removed = plan.keyword_delta
        before_cf = {k.casefold() for k in _as_list(plan.before.get(CANONICAL_KEYWORDS_TAG))}
        for keyword in (*added, *removed):
            targets = [CANONICAL_KEYWORDS_TAG, CANONICAL_HIERARCHICAL_KEYWORDS_TAG]
            if iim_holds_keyword(keyword):
                targets.append(CANONICAL_IPTC_KEYWORDS_TAG)
            for tag in targets:
                if keyword in removed:
                    keyword_args += [f"-{tag}-={keyword}", f"-{tag}+={keyword}"]
                elif keyword.casefold() not in before_cf:
                    keyword_args.append(f"-{tag}-={keyword}")
            touched_keywords.extend(t for t in targets if t not in touched_keywords)
    via_argfile = not all(
        text.isascii() for text in (plan.path, tempfile.gettempdir(), *tags.values(), *keyword_args)
    )
    return _FileWrite(tags, keyword_args, via_argfile), [*tags, *touched_keywords]


def revert_changeset(
    changeset_path: str, cfg: ExiftoolConfig, options: RevertOptions
) -> list[dict[str, Any]]:
    """Restore the files a changeset wrote to their values before the run.

    Every file in scope is read in one batched ExifTool call, then each is
    judged: ``missing`` when it is gone, ``conflict`` when a tag no longer holds
    what the run wrote (left alone unless ``options.force``), ``unchanged`` when
    it already holds its before-values, else restored and ``reverted``.
    ``cfg.dry_run`` judges and measures every file but writes nothing.

    Args:
        changeset_path: The changeset of the run to undo.
        cfg: ExifTool configuration; ``dry_run`` and ``path`` are read.
        options: Which files, and whether to override conflicts.

    Returns:
        One record per file (``path``, ``status``, ``tags``, ``entries``, and
        ``conflicts``, ``inferred`` or ``error`` where they apply), then a
        summary record ``{"summary": {status: count}, "dry_run": bool}``.

    Raises:
        FileNotFoundError: When ExifTool or the changeset cannot be found.
        OSError, RuntimeError, subprocess.TimeoutExpired: When the files'
            current values cannot be read.
    """
    exiftool = resolve_exiftool_path(cfg)
    first, out = _first_records(changeset_path)
    plans = [
        _plan_file(record, entries) for record, entries in first if options.wants(record["path"])
    ]
    present = [plan for plan in plans if os.path.isfile(plan.path)]
    tags = list(dict.fromkeys(tag for plan in present for tag in plan.tags))
    read = (
        run_exiftool_json(exiftool, [plan.path for plan in present], tags)
        if present and tags
        else []
    )
    current_by_path = {
        _path_key(rec["SourceFile"]): rec for rec in read if isinstance(rec.get("SourceFile"), str)
    }

    with ExitStack() as stack:
        datfile_dir: list[str] = []

        def _datfile_path_for(tag: str, index: int) -> str:
            if not datfile_dir:
                datfile_dir.append(
                    stack.enter_context(
                        tempfile.TemporaryDirectory(
                            prefix=_TMP_DIR_PREFIX, ignore_cleanup_errors=True
                        )
                    )
                )
            return os.path.join(datfile_dir[0], _datfile_name(index, tag))

        for index, plan in enumerate(plans, start=1):
            out.append(
                _revert_one(
                    plan, current_by_path, (exiftool, cfg, options), _datfile_path_for, index
                )
            )

    counts = {status: 0 for status in REVERT_STATUSES}
    for record in out:
        counts[record["status"]] += 1
    out.append({"summary": counts, "dry_run": cfg.dry_run})
    return out


def _revert_one(
    plan: _FilePlan,
    current_by_path: dict[str, dict[str, Any]],
    run: tuple[str, ExiftoolConfig, RevertOptions],
    datfile_path_for: Callable[[str, int], str],
    index: int,
) -> dict[str, Any]:
    """Judge and, unless it conflicts, restore one file.

    Args:
        plan: The file's plan.
        current_by_path: Every file's ExifTool record, by path key.
        run: The ExifTool path, its configuration and the revert options.
        datfile_path_for: ``(tag, index) -> path`` for a value too long to inline.
        index: The file's position, which names its DATFILEs.

    Returns:
        The file's record.
    """
    exiftool, cfg, options = run
    record: dict[str, Any] = {"path": plan.path, "entries": plan.entries}
    if plan.inferred:
        record["inferred"] = plan.inferred
    if cfg.dry_run:
        record["dry_run"] = True
    if not os.path.isfile(plan.path):
        return {**record, "status": "missing", "tags": []}
    file_values = current_by_path.get(_path_key(plan.path))
    if file_values is None:
        return {
            **record,
            "status": "error",
            "tags": [],
            "error": "ExifTool did not read this file.",
        }
    current = {tag: _snapshot_value(file_values, tag) for tag in plan.tags}
    write, touched = _restore_write(plan, current)
    if not touched:
        return {**record, "status": "unchanged", "tags": []}
    conflicts = [
        tag for tag, wrote in plan.expected.items() if not _same(tag, current.get(tag), wrote)
    ]
    if conflicts:
        record["conflicts"] = conflicts
        if not options.force:
            return {**record, "status": "conflict", "tags": []}
    error = _run_file_write(
        exiftool, cfg, write, plan.path, lambda tag: datfile_path_for(tag, index)
    )
    if error is not None:
        return {**record, "status": "error", "tags": touched, "error": error.get("error")}
    return {**record, "status": "reverted", "tags": touched}
