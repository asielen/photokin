"""ExifTool binary discovery: configured path, bundled resource, or system PATH.

The library does not ship an ExifTool binary; it locates one at runtime. The
bundled-resource tier exists only so a downstream distribution (e.g. the
Lightroom plugin) *could* vendor one — in the plain PyPI install it finds
nothing there and falls through to the system PATH.

Code map:
- _default_exiftool_cache_dir         writable cache dir for extracted binaries
- _platform_exiftool_resource_relpath (subdir, filename) for this OS's binary
- _ensure_executable                  chmod +x an extracted binary (POSIX)
- _try_clear_macos_quarantine         drop the macOS quarantine xattr
- resolve_exiftool_path               PUBLIC: config path -> bundled -> system PATH
"""

from __future__ import annotations

import importlib.resources as _res
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ..utils import normalize_path
from .config import ExiftoolConfig


def _default_exiftool_cache_dir() -> Path:
    """Return a stable, writable cache location for extracted binaries."""
    return Path.home() / ".photokin" / "bin"


def _platform_exiftool_resource_relpath() -> tuple[str, str]:
    """Return (subdir, filename) for the bundled ExifTool binary."""
    system = platform.system().lower()
    if os.name == "nt" or system == "windows":
        return ("win", "exiftool.exe")
    if sys.platform == "darwin" or system == "darwin":
        return ("mac", "exiftool")
    if sys.platform.startswith("linux") or system == "linux":
        raise RuntimeError("ExifTool is not bundled for Linux; trying system PATH.")
    raise RuntimeError(f"ExifTool is not bundled for this platform: {platform.system()}.")


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


def resolve_exiftool_path(cfg: ExiftoolConfig | None = None) -> str:
    """Resolve ExifTool executable path.

    Resolution order:
    1) cfg.path (if provided and exists)
    2) bundled resource under photokin/tools/exiftool/<platform>/
       - if resource isn't a real file path, extract to cache and run from there
    3) system PATH
    """
    # 1) Explicit override.
    if cfg is not None:
        p = normalize_path(getattr(cfg, "path", None))
        if p:
            if os.path.isfile(p):
                return p
            raise FileNotFoundError(f"Configured exiftool path not found: {p}")

    # 2) Bundled resource.
    bundle_resolution_error: Exception | None = None
    try:
        subdir, fname = _platform_exiftool_resource_relpath()
        rel = Path("tools") / "exiftool" / subdir / fname
        res = _res.files("photokin") / rel.as_posix()

    except Exception as exc:
        bundle_resolution_error = exc
        rel = None
        res = None

    if res is not None and rel is not None:
        # 2a) Try direct filesystem path first (editable installs).
        try:
            res_path = Path(str(res))
            if res_path.exists() and res_path.is_file():
                _ensure_executable(res_path)
                _try_clear_macos_quarantine(res_path)
                return str(res_path)
        except Exception:
            pass

        # 2b) Extract to a stable cache dir for wheels/zip installs.
        cache_root: Path
        if cfg is not None:
            override = normalize_path(getattr(cfg, "cache_dir", None))
            cache_root = Path(override) if override else _default_exiftool_cache_dir()
        else:
            cache_root = _default_exiftool_cache_dir()

        out_dir = cache_root / "exiftool" / subdir
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / fname

        try:
            data = res.read_bytes()
        except Exception as exc:
            bundle_resolution_error = exc
        else:
            # Only rewrite if missing or size mismatch.
            if (not out_path.exists()) or (out_path.stat().st_size != len(data)):
                with tempfile.NamedTemporaryFile(dir=out_dir, delete=False) as tmp:
                    tmp.write(data)
                    tmp_path = Path(tmp.name)
                if out_path.exists():
                    try:
                        out_path.unlink()
                    except Exception:
                        pass
                shutil.move(str(tmp_path), out_path)

            _ensure_executable(out_path)
            _try_clear_macos_quarantine(out_path)
            return str(out_path)

    # 3) Fall back to system PATH.
    system_exiftool = shutil.which("exiftool")
    if system_exiftool:
        return system_exiftool

    error_hint = f" (bundled lookup error: {bundle_resolution_error})" if bundle_resolution_error else ""
    raise FileNotFoundError(
        "ExifTool not found. Install it or set the ExifTool path in plugin settings.\n"
        "Download: https://exiftool.org"
        f"{error_hint}"
    )
