"""Configuration for the ExifTool wrapper layer."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# Fields written by the Lightroom pipeline by default (EXIF:UserComment is the
# one field the Lightroom SDK cannot write itself).
DEFAULT_PIPELINE_FIELDS: tuple[str, ...] = ("EXIF:UserComment",)


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

        Reads ``EXIFTOOL_WRITE_ENABLED`` (default true), ``EXIFTOOL_PATH``,
        and ``EXIFTOOL_FIELDS`` (comma-separated, default
        ``("EXIF:UserComment",)``). Explicit keyword overrides win over the
        environment; overrides with value ``None`` are ignored so callers can
        pass through optional CLI flags directly.
        """
        values: dict[str, Any] = {
            "enabled": _parse_bool_env("EXIFTOOL_WRITE_ENABLED", True),
            "path": os.environ.get("EXIFTOOL_PATH") or None,
            "fields": parse_fields(os.environ.get("EXIFTOOL_FIELDS"))
            or DEFAULT_PIPELINE_FIELDS,
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)
