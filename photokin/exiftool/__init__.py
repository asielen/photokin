"""ExifTool wrapper layer for photo_archiver.

The core package never touches ExifTool; this subpackage provides the optional
read (manifest hydration) and write (changeset apply) integration plus binary
discovery. See README.md in this directory.
"""

from .apply import apply_changeset
from .config import ExiftoolConfig
from .hydrate import hydrate_user_comments, make_manifest_hydrator
from .locate import resolve_exiftool_path

__all__ = [
    "ExiftoolConfig",
    "apply_changeset",
    "hydrate_user_comments",
    "make_manifest_hydrator",
    "resolve_exiftool_path",
]
