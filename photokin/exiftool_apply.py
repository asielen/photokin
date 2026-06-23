"""Deprecated: moved to photokin.exiftool.apply.

Note: apply_changeset now takes an ExiftoolConfig (photokin.exiftool)
instead of the core Config. Use `python -m photokin.exiftool` for the CLI.
"""

from .exiftool.apply import apply_changeset, main  # noqa: F401

if __name__ == "__main__":
    main()
