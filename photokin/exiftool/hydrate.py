"""Read-side ExifTool integration: fill manifest gaps Lightroom cannot supply."""

from __future__ import annotations

import logging
from typing import Callable

from ..utils import normalize_path
from .config import ExiftoolConfig
from .locate import resolve_exiftool_path

logger = logging.getLogger(__name__)


def hydrate_user_comments(items: list[dict], cfg: ExiftoolConfig) -> None:
    """Best-effort: read EXIF:UserComment via ExifTool and merge into manifest items.

    Only fills in fields that are missing/null in the existing metadata,
    so Lightroom-provided values are never overridden. Non-fatal: if ExifTool
    can't be found or fails to run, logs a warning and returns rather than
    raising into the manifest pipeline.
    """
    try:
        from photokin.exiftool.manifest import _find_tag_value, run_exiftool_json
    except ImportError:
        return

    try:
        exiftool = resolve_exiftool_path(cfg)
    except (FileNotFoundError, OSError) as exc:
        logger.warning("Skipping EXIF:UserComment hydration: %s", exc)
        return

    paths_needing: list[str] = []
    item_by_path: dict[str, list[dict]] = {}
    for raw in items:
        meta = raw.get("metadata")
        if not isinstance(meta, dict):
            continue
        if (meta.get("userComment") or "").strip():
            continue  # already has a value
        p = normalize_path(raw.get("path") or "")
        if p:
            paths_needing.append(p)
            item_by_path.setdefault(p, []).append(raw)

    if not paths_needing:
        return

    try:
        records = run_exiftool_json(
            exiftool_path=exiftool,
            files=paths_needing,
            fields=["EXIF:UserComment"],
            timeout_sec=max(60, len(paths_needing) * 2),
        )
    except (FileNotFoundError, RuntimeError, OSError) as exc:
        logger.warning("Skipping EXIF:UserComment hydration: ExifTool read failed: %s", exc)
        return

    for rec in records:
        src = rec.get("SourceFile") or ""
        # run_exiftool_json requests -G1, so EXIF:UserComment comes back keyed as
        # ExifIFD:UserComment; _find_tag_value matches on the bare tag name.
        user_comment = str(_find_tag_value(rec, "EXIF:UserComment") or "").strip()
        if not user_comment or not src:
            continue
        # ExifTool emits SourceFile with forward slashes even on Windows, while
        # item_by_path is keyed by normalize_path() output (os.path.normpath, which
        # uses backslashes on Windows). Normalize the record path the same way so
        # the lookup matches on every platform.
        for raw in item_by_path.get(normalize_path(src), []):
            meta = raw.get("metadata")
            if isinstance(meta, dict):
                meta["userComment"] = user_comment


def make_manifest_hydrator(cfg: ExiftoolConfig) -> Callable[[list[dict]], None]:
    """Return a hydrator suitable for ``core.process_manifest_stream(metadata_hydrator=...)``."""

    def _hydrator(items: list[dict]) -> None:
        hydrate_user_comments(items, cfg)

    return _hydrator
