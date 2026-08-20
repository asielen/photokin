"""Configuration for the ExifTool wrapper layer."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

# Fields written by the Lightroom pipeline by default (EXIF:UserComment is the
# one field the Lightroom SDK cannot write itself).
DEFAULT_PIPELINE_FIELDS: tuple[str, ...] = ("EXIF:UserComment",)

#: The one tag shape ExifTool reliably refuses: ``XMP:<namespace>:<Tag>``, e.g.
#: ``XMP:dc:Description``. It answers "doesn't exist or isn't writable", exits 1
#: and writes nothing. photokin's own canonical constants used this form until
#: it was caught, so a user following older docs or an older changeset will type
#: it; ``suggest_writable_spelling`` turns it into the form that works.
#:
#: The pattern is deliberately narrow, because "a second colon" is NOT itself an
#: error -- ExifTool's ``family0:family1:tag`` syntax is legitimate. Measured
#: against 13.10:
#:
#:     XMP:dc:Description      rejected      -> XMP-dc:Description   works
#:     XMP:xmp:Rating          works         (``xmp`` collides with the family-0
#:                                            group name; ``dc`` collides with
#:                                            nothing -- which is why the two
#:                                            identical-looking spellings differ)
#:     XMP:XMP-dc:Description  works         (a real family-1 group name)
#:     EXIF:IFD0:Model         works         -> EXIF-IFD0:Model is REJECTED
#:
#: That last row is why this is a suggestion keyed on a specific shape rather
#: than a blanket "swap the second colon for a hyphen" rewrite: applied to
#: ``EXIF:IFD0:Model`` such a rewrite would break input that works today. The
#: middle token must therefore be hyphen-free (so a real family-1 group like
#: ``XMP-dc`` is left alone) and the family must be XMP (so ``EXIF:IFD0:`` is).
_XMP_TWO_COLON_RE = re.compile(r"^xmp:([a-z0-9]+):(.+)$", re.IGNORECASE)


def suggest_writable_spelling(tag: str) -> str | None:
    """Return the writable spelling for a known-bad tag, else ``None``.

    Args:
        tag: A tag name as a user typed it, e.g. ``"XMP:dc:Description"``.

    Returns:
        The corrected spelling (``"XMP-dc:Description"``) when ``tag`` uses the
        XMP namespace form ExifTool rejects, otherwise ``None``. ``None`` means
        "nothing known to be wrong", not "verified writable" -- only the real
        binary can say that, which is what
        ``photokin/tests/test_canonical_tags_are_writable.py`` uses it for.
    """
    match = _XMP_TWO_COLON_RE.match(tag.strip())
    if not match:
        return None
    namespace, leaf = match.groups()
    return f"XMP-{namespace}:{leaf}"


def _parse_bool_env(name: str, default: bool) -> bool:
    """Parse an environment variable into a boolean value."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def parse_fields(value: str | None) -> tuple[str, ...] | None:
    """Split a comma-separated tag list into a tuple, dropping blanks."""
    if value is None:
        return None
    parts = [part.strip() for part in value.split(",")]
    return tuple(part for part in parts if part) or None


@dataclass
class ExiftoolConfig:
    """Settings for ExifTool binary discovery, hydration, and changeset apply."""

    path: str | None = None
    cache_dir: str | None = None
    enabled: bool = False
    fields: tuple[str, ...] = (
        "EXIF:DateTimeOriginal",
        "EXIF:CreateDate",
        "EXIF:UserComment",
    )
    dry_run: bool = False
    overwrite_original: bool = True
    write_sidecar_only: bool = False

    @classmethod
    def from_env(cls, **overrides: Any) -> "ExiftoolConfig":
        """Build a pipeline config from environment variables.

        Reads ``EXIFTOOL_WRITE_ENABLED`` (default false), ``EXIFTOOL_PATH``,
        and ``EXIFTOOL_FIELDS`` (comma-separated, default
        ``("EXIF:UserComment",)``). Writing to the user's files requires an
        explicit opt-in, so the default here agrees with the dataclass rather
        than quietly overriding it. Explicit keyword overrides win over the
        environment; overrides with value ``None`` are ignored so callers can
        pass through optional CLI flags directly.
        """
        values: dict[str, Any] = {
            "enabled": _parse_bool_env("EXIFTOOL_WRITE_ENABLED", False),
            "path": os.environ.get("EXIFTOOL_PATH") or None,
            "fields": parse_fields(os.environ.get("EXIFTOOL_FIELDS"))
            or DEFAULT_PIPELINE_FIELDS,
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)
