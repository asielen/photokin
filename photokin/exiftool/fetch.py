"""Download and cache the official ExifTool binary for this OS.

photokin does not vendor ExifTool in its wheel (that would put a ~34 MB
Windows-only Perl distribution into a ``py3-none-any`` package installed on
every platform). Instead the plugin's setup flow downloads it once, on demand:

- **Windows** — where a Lightroom user is least likely to have ExifTool on PATH
  — downloads the official self-contained ExifTool package (release archive
  hosted on SourceForge, checksums published on exiftool.org) into the
  photokin cache dir (``~/.photokin/bin/exiftool/win``), verified by SHA256,
  laid out so ``resolve_exiftool_path`` finds it.
- **macOS / Linux** rely on a system ExifTool (Homebrew / apt / winget), which
  is the norm there; this module is a no-op and returns ``None``.

Everything is best-effort: on any failure ``ensure_exiftool`` returns ``None``
and resolution falls back to the system PATH, so a failed/blocked download never
breaks the app harder than "ExifTool not found".

Run standalone (used by the plugin's "Install/Update Requirements" flow):

    python -m photokin.exiftool.fetch            # -> prints the resolved path
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

# Pinned ExifTool release. Bump together with a fresh checksums verification.
EXIFTOOL_VERSION = "13.59"
_BASE_URL = "https://exiftool.org"
# exiftool.org no longer hosts the Windows zip at its own root (only checksums
# and HTML live there now); the archive itself redirects through SourceForge.
# urlopen follows the redirect chain transparently, so this is a plain GET.
_SOURCEFORGE_DOWNLOAD_URL = "https://sourceforge.net/projects/exiftool/files/{archive}/download"

# Optional reproducible pin: map an archive filename to its known SHA256 so the
# download is verified offline against a value the maintainer vetted. When empty
# for the pinned version, we fall back to the checksums.txt published alongside
# the archive on exiftool.org.
KNOWN_SHA256: dict[str, str] = {
    # "exiftool-13.47_64.zip": "<sha256>",
}


def _windows_archive_name(version: str) -> str:
    return f"exiftool-{version}_64.zip"


def _is_windows() -> bool:
    return os.name == "nt" or platform.system().lower() == "windows"


def _http_get(url: str, *, timeout: int = 120) -> bytes:
    req = Request(url, headers={"User-Agent": "photokin-exiftool-fetch"})
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https host)
        return resp.read()


_SHA256_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")


def _published_sha256(filename: str, version: str = EXIFTOOL_VERSION, *, timeout: int = 30) -> Optional[str]:
    """Return the published SHA256 for `filename` from exiftool.org, or None.

    ExifTool publishes a per-release checksums file (``checksums-<version>.txt``);
    the unversioned ``checksums.txt`` only ever covers the *current* release, so a
    pinned older version won't be listed there. Try the versioned file first, then
    the unversioned one. Parse robustly: pick the 64-hex token off any line that
    names the archive (handles both ``SHA256(file)= <hex>`` and ``<hex>  file``
    layouts; the 64-hex width naturally excludes the SHA1/MD5 lines).
    """
    for url in (f"{_BASE_URL}/checksums-{version}.txt", f"{_BASE_URL}/checksums.txt"):
        try:
            text = _http_get(url, timeout=timeout).decode("utf-8", "replace")
        except Exception:
            continue
        for line in text.splitlines():
            if filename in line:
                m = _SHA256_RE.search(line)
                if m:
                    return m.group(0).lower()
    return None


def _verify_sha256(data: bytes, filename: str, version: str = EXIFTOOL_VERSION) -> None:
    """Raise if `data` does not match a trusted SHA256 for `filename`.

    Prefers the offline KNOWN_SHA256 pin; otherwise uses exiftool.org's published
    per-release checksums. Fails closed when no trusted checksum is available.
    """
    expected = KNOWN_SHA256.get(filename) or _published_sha256(filename, version)
    if not expected:
        raise RuntimeError(
            f"No trusted SHA256 available for {filename} (checksums.txt unreachable "
            "and no offline pin); refusing to use an unverified download."
        )
    actual = hashlib.sha256(data).hexdigest().lower()
    if actual != expected.lower():
        raise RuntimeError(f"SHA256 mismatch for {filename}: expected {expected}, got {actual}")


def _extract_windows_bundle(zip_bytes: bytes, dest_dir: Path) -> Path:
    """Extract the ExifTool Windows zip into `dest_dir` as exiftool.exe + exiftool_files/.

    The official archive contains an ``exiftool(-k).exe`` (or ``exiftool.exe``)
    next to an ``exiftool_files`` directory, sometimes inside a top-level folder.
    Locate them wherever they land and normalize into the cache layout.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(tmp_path)

        files_dir = next((p for p in tmp_path.rglob("exiftool_files") if p.is_dir()), None)
        if files_dir is None:
            raise RuntimeError("exiftool_files/ not found in the ExifTool archive.")
        src_root = files_dir.parent
        exe = next(
            (src_root / n for n in ("exiftool.exe", "exiftool(-k).exe") if (src_root / n).is_file()),
            None,
        )
        if exe is None:
            exe = next((p for p in src_root.glob("exiftool*.exe") if p.is_file()), None)
        if exe is None:
            raise RuntimeError("exiftool executable not found in the ExifTool archive.")

        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_files = dest_dir / "exiftool_files"
        if dest_files.exists():
            shutil.rmtree(dest_files, ignore_errors=True)
        shutil.copytree(files_dir, dest_files)
        dest_exe = dest_dir / "exiftool.exe"
        shutil.copyfile(exe, dest_exe)
        return dest_exe


def _default_cache_dir() -> Path:
    # Mirrors locate._default_exiftool_cache_dir() so resolve_exiftool_path finds it.
    return Path.home() / ".photokin" / "bin"


def ensure_exiftool(cache_dir: Optional[str] = None, *, version: str = EXIFTOOL_VERSION) -> Optional[str]:
    """Ensure a usable ExifTool exists for this OS; return its path or None.

    Windows: download+verify+extract into the cache (idempotent). Other OSes:
    return None (use system ExifTool). Any failure returns None (best-effort).
    """
    if not _is_windows():
        return None

    root = Path(cache_dir) if cache_dir else _default_cache_dir()
    dest_dir = root / "exiftool" / "win"
    dest_exe = dest_dir / "exiftool.exe"
    # Idempotent: already downloaded and complete.
    if dest_exe.is_file() and (dest_dir / "exiftool_files").is_dir():
        return str(dest_exe)

    archive = _windows_archive_name(version)
    try:
        data = _http_get(_SOURCEFORGE_DOWNLOAD_URL.format(archive=archive))
        _verify_sha256(data, archive, version)
        return str(_extract_windows_bundle(data, dest_dir))
    except Exception as exc:  # best-effort: never raise into the caller
        print(f"[exiftool-fetch] could not provision bundled ExifTool: {exc}", file=sys.stderr)
        return None


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cache_dir = argv[0] if argv else None
    path = ensure_exiftool(cache_dir)
    if path:
        print(path)
        return 0
    if not _is_windows():
        print("ExifTool is not auto-downloaded on this OS; using system ExifTool (PATH).")
        return 0
    print("ExifTool download unavailable; install ExifTool or set the path in settings.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
