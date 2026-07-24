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


def _bundled_exiftool_is_complete(exe_path: Path, subdir: str) -> bool:
    """Return True only if the bundled ExifTool has its runtime dependencies alongside it.

    The bundled binaries are not self-contained:
    - macOS ships the ExifTool Perl script, which loads ``Image::ExifTool`` from a
      sibling ``lib/`` directory (``lib/Image/ExifTool.pm``).
    - Windows ships ``exiftool.exe`` next to an ``exiftool_files`` directory.

    If those are missing, running the bundled binary fails, so the caller must fall
    back to a system ExifTool on PATH instead of returning a broken path.
    """
    parent = exe_path.parent
    if subdir == "mac":
        return (parent / "lib" / "Image" / "ExifTool.pm").is_file()
    if subdir == "win":
        return (parent / "exiftool_files").is_dir()
    return True


def _iter_bundle_files(root):
    """Yield (relative_posix_path, traversable_file) for every file under `root`.

    Works for both real-directory installs and zip/wheel-backed resources.
    """
    stack = [(root, "")]
    while stack:
        node, prefix = stack.pop()
        for child in node.iterdir():
            rel = f"{prefix}{child.name}"
            if child.is_dir():
                stack.append((child, rel + "/"))
            else:
                yield rel, child


def _materialize_file(data: bytes, dest: Path) -> None:
    """Write `data` to `dest` atomically, only when missing or changed."""
    if dest.exists() and dest.stat().st_size == len(data):
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    if dest.exists():
        try:
            dest.unlink()
        except Exception:
            pass
    shutil.move(str(tmp_path), dest)


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
                # Only use the bundled binary if its runtime dependencies (mac
                # lib/, win exiftool_files) are actually present; otherwise it
                # can't run and we must fall back to system PATH below.
                if _bundled_exiftool_is_complete(res_path, subdir):
                    _ensure_executable(res_path)
                    _try_clear_macos_quarantine(res_path)
                    return str(res_path)
                bundle_resolution_error = FileNotFoundError(
                    f"Bundled ExifTool at {res_path} is missing its runtime library; "
                    "falling back to system PATH."
                )
        except Exception:
            pass

        # 2b) Extract the whole bundle tree to a stable cache dir for wheels/zip
        # installs. The binaries are not self-contained, so we must copy the
        # sibling lib/ (mac) or exiftool_files/ (win) alongside the executable,
        # then verify completeness before returning.
        cache_root: Path
        if cfg is not None:
            override = normalize_path(getattr(cfg, "cache_dir", None))
            cache_root = Path(override) if override else _default_exiftool_cache_dir()
        else:
            cache_root = _default_exiftool_cache_dir()

        out_dir = cache_root / "exiftool" / subdir
        out_path = out_dir / fname

        try:
            src_dir = _res.files("photokin") / (Path("tools") / "exiftool" / subdir).as_posix()
            out_dir.mkdir(parents=True, exist_ok=True)
            for relpath, child in _iter_bundle_files(src_dir):
                _materialize_file(child.read_bytes(), out_dir / relpath)
        except Exception as exc:
            bundle_resolution_error = exc
        else:
            if out_path.is_file() and _bundled_exiftool_is_complete(out_path, subdir):
                _ensure_executable(out_path)
                _try_clear_macos_quarantine(out_path)
                return str(out_path)
            bundle_resolution_error = FileNotFoundError(
                f"Extracted ExifTool at {out_path} is incomplete; falling back to system PATH."
            )

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
