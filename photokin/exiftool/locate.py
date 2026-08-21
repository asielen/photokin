"""ExifTool binary discovery: configured path, downloaded cache, or system PATH.

photokin does not vendor an ExifTool binary in its wheel. It locates one at
runtime in this order:

1. an explicitly configured path (``ExiftoolConfig.path`` / ``EXIFTOOL_PATH``);
2. a copy previously downloaded by ``photokin.exiftool.fetch`` into the cache
   dir (``~/.photokin/bin/exiftool/<platform>``), which the fetcher can
   provision on any OS;
3. a system ExifTool on ``PATH``.

Code map:
- _default_exiftool_cache_dir      writable cache dir the downloader targets
- _platform_exiftool_relpath       (subdir, filename) for this OS's binary
- _ensure_executable               chmod +x a cached binary (POSIX)
- _try_clear_macos_quarantine      drop the macOS quarantine xattr
- _cached_exiftool_is_complete     the cached binary has its runtime deps alongside
- resolve_exiftool_path            PUBLIC: config path -> cache -> system PATH
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from ..utils import normalize_path
from .config import ExiftoolConfig


def _default_exiftool_cache_dir() -> Path:
    """Return a stable, writable cache location for downloaded binaries."""
    return Path.home() / ".photokin" / "bin"


def _platform_exiftool_relpath() -> tuple[str, str] | None:
    """Return (subdir, filename) for a cached ExifTool on this OS, or None."""
    system = platform.system().lower()
    if os.name == "nt" or system == "windows":
        return ("win", "exiftool.exe")
    if sys.platform == "darwin" or system == "darwin":
        return ("mac", "exiftool")
    if system == "linux":
        return ("linux", "exiftool")
    # Anything else is not auto-provisioned; use system PATH.
    return None


def _ensure_executable(path: Path) -> None:
    """Best-effort: ensure the file is executable (POSIX only)."""
    if os.name == "nt":
        return
    try:
        mode = path.stat().st_mode
        # Add user-execute bit.
        path.chmod(mode | 0o100)
    except Exception:
        # Don't hard fail; subprocess will surface a clearer error later.
        pass


def _cached_exiftool_is_complete(exe_path: Path, subdir: str) -> bool:
    """Return True only if the cached ExifTool has its runtime deps alongside it.

    ExifTool is not a single self-contained file:
    - Windows ``exiftool.exe`` needs a sibling ``exiftool_files`` directory.
    - the macOS/Linux Perl script needs a sibling ``lib/Image/ExifTool.pm`` tree.

    If those are missing the binary can't run, so resolution should skip it and
    fall back to system PATH instead of returning a broken path.
    """
    parent = exe_path.parent
    if subdir == "win":
        return (parent / "exiftool_files").is_dir()
    if subdir in ("mac", "linux"):
        return (parent / "lib" / "Image" / "ExifTool.pm").is_file()
    return True


def _try_clear_macos_quarantine(path: Path) -> None:
    """Best-effort removal of macOS quarantine attribute."""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            ["xattr", "-d", "com.apple.quarantine", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        pass


def _cache_root(cfg: ExiftoolConfig | None) -> Path:
    if cfg is not None:
        override = normalize_path(getattr(cfg, "cache_dir", None))
        if override:
            return Path(override)
    return _default_exiftool_cache_dir()


def resolve_exiftool_path(cfg: ExiftoolConfig | None = None) -> str:
    """Resolve ExifTool executable path.

    Resolution order:
    1) cfg.path (if provided; errors if set but missing)
    2) a downloaded copy in the cache dir (see photokin.exiftool.fetch)
    3) system PATH
    """
    # 1) Explicit override.
    if cfg is not None:
        p = normalize_path(getattr(cfg, "path", None))
        if p:
            if os.path.isfile(p):
                return p
            raise FileNotFoundError(f"Configured exiftool path not found: {p}")

    # 2) Downloaded copy in the cache dir.
    relpath = _platform_exiftool_relpath()
    if relpath is not None:
        subdir, fname = relpath
        cached = _cache_root(cfg) / "exiftool" / subdir / fname
        try:
            if cached.is_file() and _cached_exiftool_is_complete(cached, subdir):
                _ensure_executable(cached)
                _try_clear_macos_quarantine(cached)
                return str(cached)
        except Exception:
            pass

    # 3) Fall back to system PATH.
    system_exiftool = shutil.which("exiftool")
    if system_exiftool:
        return system_exiftool

    raise FileNotFoundError(
        "ExifTool not found.\n"
        "run `python -m photokin.exiftool.fetch` to download it, or install it yourself "
        "(https://exiftool.org) and retry."
    )
