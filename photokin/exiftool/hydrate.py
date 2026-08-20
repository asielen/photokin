"""Read-side ExifTool integration: read what a file already holds into its item."""

from __future__ import annotations

import logging
from typing import Callable

from ..utils import normalize_path
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


def hydrate_item_metadata(items: list[dict], cfg: ExiftoolConfig) -> None:
    """Best-effort: read each file's own metadata via ExifTool into its item.

    Reads the tags in :data:`_HYDRATED_TAGS` -- ``EXIF:DateTimeOriginal``,
    ``EXIF:UserComment``, ``XMP:Description``, ``XMP:Title`` and ``XMP:Subject``
    -- and fills only the keys an item's metadata is missing or holds empty, so a
    Lightroom- or ``--meta``-supplied value is never overridden. Values are
    stored verbatim: ``dateTimeOriginal`` stays in EXIF colon form, which is what
    ``merge`` and ``canonical`` both read, and ``keywords`` stays a list.

    An item's ``metadata`` object is created only when a value is actually
    written, so a file ExifTool reads nothing from is left exactly as it arrived.
    An item naming a ``metadata_path`` is skipped entirely: ``load_item_metadata``
    prefers an inline dict over the path, so seeding one would shadow the sidecar
    the caller named. So is an item whose ``metadata`` is present but not a dict.

    Non-fatal by design: the CLI pre-flights the binary when ``-r`` is given, so
    a failure here is a mid-run one -- a locked file, a corrupt image, a timeout
    -- which warns and continues rather than costing the whole batch.

    Args:
        items: Manifest items, mutated in place.
        cfg: ExifTool configuration used to locate the binary.
    """
    # Imported here rather than at module scope so the run_exiftool_json a test
    # patches onto photokin.exiftool.manifest is the one this call reaches.
    from photokin.exiftool.manifest import (
        _find_tag_value,
        manifest_value,
        run_exiftool_json,
    )

    try:
        exiftool = resolve_exiftool_path(cfg)
    except (FileNotFoundError, OSError) as exc:
        logger.warning("Skipping metadata hydration: %s", exc)
        return

    paths_needing: list[str] = []
    wanted_by_path: dict[str, list[tuple[dict, set[str]]]] = {}
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
        if not missing:
            continue
        p = normalize_path(raw.get("path") or "")
        if p:
            paths_needing.append(p)
            wanted_by_path.setdefault(p, []).append((raw, {key for _tag, key in missing}))

    if not paths_needing:
        return

    try:
        records = run_exiftool_json(
            exiftool_path=exiftool,
            files=paths_needing,
            fields=list(DEFAULT_EXIFTOOL_FIELDS),
            timeout_sec=max(60, len(paths_needing) * 2),
        )
    except (FileNotFoundError, RuntimeError, OSError) as exc:
        logger.warning("Skipping metadata hydration: ExifTool read failed: %s", exc)
        return

    for rec in records:
        src = rec.get("SourceFile") or ""
        if not src:
            continue
        # ExifTool emits SourceFile with forward slashes even on Windows, while
        # wanted_by_path is keyed by normalize_path() output (os.path.normpath,
        # which uses backslashes on Windows). Normalize the record path the same
        # way so the lookup matches on every platform.
        for raw, wanted in wanted_by_path.get(normalize_path(src), []):
            for tag, key in _HYDRATED_TAGS:
                if key not in wanted:
                    continue
                # run_exiftool_json requests -G1, so EXIF:UserComment comes back
                # keyed as ExifIFD:UserComment; _find_tag_value matches on the
                # bare tag name.
                value = manifest_value(_find_tag_value(rec, tag), key)
                if value is None:
                    continue
                target = raw.get("metadata")
                if not isinstance(target, dict):
                    target = raw["metadata"] = {}
                target[key] = value


def make_manifest_hydrator(cfg: ExiftoolConfig) -> Callable[[list[dict]], None]:
    """Return a hydrator suitable for ``core.process_manifest_stream(metadata_hydrator=...)``."""

    def _hydrator(items: list[dict]) -> None:
        hydrate_item_metadata(items, cfg)

    return _hydrator
