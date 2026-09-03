"""Read-side ExifTool integration: read what a file already holds into its item."""

from __future__ import annotations

import logging
from typing import Callable

from ..utils import HYDRATION_FAILED_KEY, normalize_path
from .config import ExiftoolConfig
from .locate import resolve_exiftool_path
from .manifest import _TAG_TO_MANIFEST_KEY, DEFAULT_EXIFTOOL_FIELDS

logger = logging.getLogger(__name__)

#: The tags read, and the manifest key each fills. Derived rather than
#: re-declared: ``DEFAULT_EXIFTOOL_FIELDS`` is already this subpackage's
#: statement of "what photokin reads from a file", every member already has a
#: mapping, and every one is in ``utils.DEFAULT_METADATA_FORWARD_FIELDS`` so
#: every one can actually reach the model. Iteration order is the tuple's, so a
#: hydrated ``metadata`` object has a stable key order and a generated manifest
#: is byte-identical across runs.
_HYDRATED_TAGS: tuple[tuple[str, str], ...] = tuple(
    (tag, _TAG_TO_MANIFEST_KEY[tag]) for tag in DEFAULT_EXIFTOOL_FIELDS
)


def _has_date_marker(keywords: list[str]) -> bool:
    """Whether any keyword looks like a reviewed ``DATE:`` marker.

    Mirrors :func:`photokin.merge._has_date_keyword`'s predicate exactly; kept
    local rather than imported so this read-side module does not reach into
    the merge step's internals for one line of string matching.
    """
    return any(kw.strip().upper().startswith("DATE:") for kw in keywords if isinstance(kw, str))


def hydrate_item_metadata(
    items: list[dict],
    cfg: ExiftoolConfig,
    *,
    title_from_file: set[int] | None = None,
) -> None:
    """Best-effort: read each file's own metadata via ExifTool into its item.

    Reads the tags in :data:`_HYDRATED_TAGS` -- ``EXIF:DateTimeOriginal``,
    ``EXIF:UserComment``, ``XMP:Description``, ``XMP:Title`` and ``XMP:Subject``
    -- and fills only the keys an item's metadata is missing or holds empty, so a
    Lightroom- or ``--meta``-supplied value is never overridden. Values are
    stored verbatim: ``dateTimeOriginal`` stays in EXIF colon form, which is what
    ``merge`` and ``canonical`` both read, and ``keywords`` stays a list.

    Args:
        items: Manifest items, mutated in place.
        cfg: ExifTool configuration used to locate the binary.
        title_from_file: When given, receives ``id(item["metadata"])`` for every
            item whose title this call actually filled from the file. This is
            the precise, per-item version of "title may be from file": an item
            already carrying a manifest/``--meta`` title never has its title
            key filled here (that is the whole point of "missing or empty"
            above), so it never lands in this set, however many *other* items
            in the same run genuinely got theirs read off the print. Callers
            that only have a single run-wide "was ``-r`` given" bit have to
            treat every item's title as possibly-from-file; this lets the one
            caller who actually knows say so precisely instead.

    An item's ``metadata`` object is created only when a value is actually
    written, so a file ExifTool reads nothing from is left exactly as it arrived.
    An item naming a ``metadata_path`` is skipped entirely: ``load_item_metadata``
    prefers an inline dict over the path, so seeding one would shadow the sidecar
    the caller named. So is an item whose ``metadata`` is present but not a dict.

    Non-fatal by design: the CLI pre-flights the binary when ``-r`` is given, so
    a failure here is a mid-run one -- a locked file, a corrupt image, a timeout
    -- which warns and continues rather than costing the whole batch. What a
    failure does leave behind is a mark: every item whose requested read could
    not be confirmed gets :data:`photokin.utils.HYDRATION_FAILED_KEY` set, so
    the changeset emitter can decline to propose writes for a file whose
    "before" it never saw. Unread is not empty -- a diff taken against
    emptiness would overwrite whatever the file really holds.
    """
    # Imported here rather than at module scope so the run_exiftool_json a test
    # patches onto photokin.exiftool.manifest is the one this call reaches.
    from photokin.exiftool.manifest import (
        _find_tag_value,
        manifest_value,
        run_exiftool_json,
    )

    paths_needing: list[str] = []
    wanted_by_path: dict[str, list[tuple[dict, set[str], bool]]] = {}
    for raw in items:
        meta = raw.get("metadata")
        if not isinstance(meta, dict):
            if raw.get("metadata_path"):
                continue
            if meta is not None:
                # Present but not a dict, so it is something the caller put
                # there and not ours to replace. photokin itself reads nothing
                # from it -- load_item_metadata returns None -- but the item is
                # the caller's object, and the write below would silently swap
                # their value for a dict of tags. The pre-C3 read skipped every
                # non-dict; only the absent case had to be opened up, for folder
                # items that arrive carrying nothing but a path.
                continue
            meta = {}  # nothing yet, and not attached to raw until something is read
        missing = [
            (tag, key)
            for tag, key in _HYDRATED_TAGS
            if manifest_value(meta.get(key), key) is None
        ]
        missing_keys = {key for _tag, key in missing}
        # Keywords are the only carrier of the DATE: interlock (see
        # DEFAULT_EXIFTOOL_FIELDS's comment), so an item whose dateTimeOriginal
        # is about to arm the date-correction heuristic still needs its file's
        # XMP:Subject checked for that marker even when the item already has
        # *other* keywords of its own -- "missing or empty" above is about not
        # clobbering a caller's keyword list, not about skipping the read that
        # protects a human-reviewed date. That case is handled as a rescue
        # rather than a normal fill: see the assignment loop below.
        rescue_date_marker = "dateTimeOriginal" in missing_keys and "keywords" not in missing_keys
        if not missing and not rescue_date_marker:
            continue
        p = normalize_path(raw.get("path") or "")
        if p:
            paths_needing.append(p)
            wanted = missing_keys | ({"keywords"} if rescue_date_marker else set())
            wanted_by_path.setdefault(p, []).append((raw, wanted, rescue_date_marker))

    if not paths_needing:
        return

    def _mark_failed(paths: list[str]) -> None:
        for p in paths:
            for raw, _wanted, _rescue in wanted_by_path.get(p, []):
                raw[HYDRATION_FAILED_KEY] = True

    try:
        exiftool = resolve_exiftool_path(cfg)
    except (FileNotFoundError, OSError) as exc:
        _mark_failed(paths_needing)
        logger.warning("Skipping metadata hydration: %s", exc)
        return

    try:
        records = run_exiftool_json(
            exiftool_path=exiftool,
            files=paths_needing,
            fields=list(DEFAULT_EXIFTOOL_FIELDS),
            timeout_sec=max(60, len(paths_needing) * 2),
        )
    except (FileNotFoundError, RuntimeError, OSError) as exc:
        _mark_failed(paths_needing)
        logger.warning("Skipping metadata hydration: ExifTool read failed: %s", exc)
        return

    unseen = set(wanted_by_path)
    for rec in records:
        src = rec.get("SourceFile") or ""
        if not src:
            continue
        # ExifTool emits SourceFile with forward slashes even on Windows, while
        # wanted_by_path is keyed by normalize_path() output (os.path.normpath,
        # which uses backslashes on Windows). Normalize the record path the same
        # way so the lookup matches on every platform.
        unseen.discard(normalize_path(src))
        for raw, wanted, rescue_date_marker in wanted_by_path.get(normalize_path(src), []):
            for tag, key in _HYDRATED_TAGS:
                if key not in wanted:
                    continue
                # run_exiftool_json requests -G1, so EXIF:UserComment comes back
                # keyed as ExifIFD:UserComment; _find_tag_value matches on the
                # bare tag name.
                value = manifest_value(_find_tag_value(rec, tag), key)
                if value is None:
                    continue
                if key == "keywords" and rescue_date_marker:
                    # The item already has its own keyword list -- this is not
                    # a fill, so that list is never replaced. Only a DATE:
                    # marker found on the file is rescued, and only if the
                    # item doesn't already carry one of its own.
                    existing = raw.get("metadata", {}).get("keywords") or []
                    if _has_date_marker(existing):
                        continue
                    marker = next((kw for kw in value if _has_date_marker([kw])), None)
                    if marker is None:
                        continue
                    raw["metadata"]["keywords"] = [*existing, marker]
                    continue
                target = raw.get("metadata")
                if not isinstance(target, dict):
                    target = raw["metadata"] = {}
                target[key] = value
                if key == "title" and title_from_file is not None:
                    title_from_file.add(id(target))

    if unseen:
        # A record came back for every readable file; one missing means
        # ExifTool could not read that file at all, which is a failed read,
        # not an empty one.
        _mark_failed(sorted(unseen))
        logger.warning(
            "-r could not read %d file(s); no writes will be proposed for them: %s",
            len(unseen),
            ", ".join(sorted(unseen)),
        )


class _ManifestHydrator:
    """A hydrator with a ``title_from_file`` attribute ``process_manifest_stream`` can read.

    A plain closure could carry the same set as a bolted-on attribute, but not
    in a shape a type checker can verify; a small callable class gives
    ``title_from_file`` a real, declared type instead.
    """

    def __init__(self, cfg: ExiftoolConfig) -> None:
        self._cfg = cfg
        self.title_from_file: set[int] = set()

    def __call__(self, items: list[dict]) -> None:
        hydrate_item_metadata(items, self._cfg, title_from_file=self.title_from_file)


def make_manifest_hydrator(cfg: ExiftoolConfig) -> Callable[[list[dict]], None]:
    """Return a hydrator suitable for ``core.process_manifest_stream(metadata_hydrator=...)``.

    The returned callable also carries a ``title_from_file`` attribute -- a
    ``set[int]`` of ``id(item["metadata"])`` for items whose title this
    hydrator actually filled from the file, populated as a side effect of each
    call. ``process_manifest_stream`` reads it, when present, to narrow the
    title-precedence rule to the items it actually applies to instead of
    guessing from "was -r given" alone; see :func:`hydrate_item_metadata`.
    """
    return _ManifestHydrator(cfg)
