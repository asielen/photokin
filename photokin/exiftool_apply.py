"""Deprecated: moved to photo_archiver.exiftool.apply.

Note: apply_changeset now takes an ExiftoolConfig (photo_archiver.exiftool)
instead of the core Config. Use `python -m photo_archiver.exiftool` for the CLI.
"""

from .exiftool.apply import apply_changeset, main  # noqa: F401

if __name__ == "__main__":
    main()
