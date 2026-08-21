"""Download and cache the official ExifTool for this OS.

photokin does not vendor ExifTool in its wheel (that would put a ~34 MB
Windows-only Perl distribution into a ``py3-none-any`` package installed on
every platform). Instead one command provisions it on demand, on every OS:

    python -m photokin.exiftool.fetch            # -> prints the resolved path

- **Windows** downloads the official self-contained ExifTool package (release
  archive hosted on SourceForge, checksums published on exiftool.org) into the
  photokin cache dir (``~/.photokin/bin/exiftool/win``), verified by SHA256,
  laid out so ``resolve_exiftool_path`` finds it.
- **macOS / Linux** first prefer a copy already provisioned here, then a system
  ExifTool on ``PATH`` (Homebrew / apt). With neither, the official pure-Perl
  distribution (``Image-ExifTool-<version>.tar.gz``) is downloaded and verified
  the same way into ``~/.photokin/bin/exiftool/{mac,linux}`` and runs on the
  system ``perl``, which macOS and nearly every Linux ship with.

Everything is best-effort: on any failure ``ensure_exiftool`` returns ``None``
and resolution falls back to the system PATH, so a failed/blocked download never
breaks the app harder than "ExifTool not found".
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import re
import shutil
import sys
import tarfile
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
    # SHA2-256 values from https://exiftool.org/checksums-13.59.txt.
    "exiftool-13.59_64.zip": "44b512b25af500724ba579d0a53c8fc5851628b692dd5e5d94ae4a15c2cba9ec",
    "Image-ExifTool-13.59.tar.gz": "668ea3acececb7235fbd0f4900e72d5f12c9b07e5c778fd36cb1e9b5828fd65a",
}


def _windows_archive_name(version: str) -> str:
    return f"exiftool-{version}_64.zip"


def _posix_archive_name(version: str) -> str:
    return f"Image-ExifTool-{version}.tar.gz"


def _is_windows() -> bool:
    return os.name == "nt" or platform.system().lower() == "windows"


def _posix_subdir() -> str:
    """Return the cache subdir resolve_exiftool_path checks on this POSIX OS."""
    return "mac" if sys.platform == "darwin" else "linux"


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


def _extract_posix_bundle(tar_bytes: bytes, dest_dir: Path) -> Path:
    """Extract the ExifTool perl distribution into `dest_dir` as exiftool + lib/.

    The official ``Image-ExifTool-<version>.tar.gz`` holds an ``exiftool`` perl
    script beside a ``lib`` tree, inside a versioned top-level folder. Locate
    them wherever they land and normalize into the cache layout. The script's
    shebang is rewritten to ``env perl`` — the official one hardcodes
    ``/usr/bin/perl``, which is not where every system keeps it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
            try:
                tf.extractall(tmp_path, filter="data")
            except TypeError:  # Python < 3.11.4: no extraction filters
                tf.extractall(tmp_path)  # noqa: S202 - checksum-verified archive

        pm = next(tmp_path.rglob("lib/Image/ExifTool.pm"), None)
        if pm is None:
            raise RuntimeError("lib/Image/ExifTool.pm not found in the ExifTool archive.")
        src_root = pm.parents[2]
        script = src_root / "exiftool"
        if not script.is_file():
            raise RuntimeError("exiftool script not found in the ExifTool archive.")

        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_lib = dest_dir / "lib"
        if dest_lib.exists():
            shutil.rmtree(dest_lib, ignore_errors=True)
        shutil.copytree(src_root / "lib", dest_lib)

        body = script.read_bytes()
        if body.startswith(b"#!"):
            _, _, rest = body.partition(b"\n")
            body = b"#!/usr/bin/env perl\n" + rest
        dest_script = dest_dir / "exiftool"
        dest_script.write_bytes(body)
        dest_script.chmod(dest_script.stat().st_mode | 0o755)
        return dest_script


def _default_cache_dir() -> Path:
    # Mirrors locate._default_exiftool_cache_dir() so resolve_exiftool_path finds it.
    return Path.home() / ".photokin" / "bin"


def _ensure_windows(root: Path, version: str) -> Optional[str]:
    """Provision the self-contained Windows bundle into the cache (idempotent)."""
    dest_dir = root / "exiftool" / "win"
    dest_exe = dest_dir / "exiftool.exe"
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


def _ensure_posix(root: Path, version: str) -> Optional[str]:
    """Provision the perl distribution on macOS/Linux, preferring what exists.

    Order mirrors ``resolve_exiftool_path``: an already-cached copy first (so
    the answer printed is the answer resolution will use), then a system
    ExifTool on PATH — no download when either exists. The download needs a
    system ``perl`` to be worth anything; without one, point at the system
    package instead.
    """
    dest_dir = root / "exiftool" / _posix_subdir()
    dest_script = dest_dir / "exiftool"
    if dest_script.is_file() and (dest_dir / "lib" / "Image" / "ExifTool.pm").is_file():
        return str(dest_script)

    system_exiftool = shutil.which("exiftool")
    if system_exiftool:
        return system_exiftool

    if not shutil.which("perl"):
        print(
            "[exiftool-fetch] no perl found, and the downloadable ExifTool needs it to run.\n"
            "install ExifTool itself instead: brew install exiftool "
            "(macOS) or apt install libimage-exiftool-perl (Debian/Ubuntu).",
            file=sys.stderr,
        )
        return None

    archive = _posix_archive_name(version)
    try:
        data = _http_get(_SOURCEFORGE_DOWNLOAD_URL.format(archive=archive))
        _verify_sha256(data, archive, version)
        return str(_extract_posix_bundle(data, dest_dir))
    except Exception as exc:  # best-effort: never raise into the caller
        print(f"[exiftool-fetch] could not provision bundled ExifTool: {exc}", file=sys.stderr)
        return None


def ensure_exiftool(cache_dir: Optional[str] = None, *, version: str = EXIFTOOL_VERSION) -> Optional[str]:
    """Ensure a usable ExifTool exists for this OS; return its path or None.

    Windows: download+verify+extract the official self-contained bundle into
    the cache (idempotent). macOS/Linux: prefer an already-cached copy, then a
    system ExifTool on PATH; otherwise download+verify+extract the official
    perl distribution, which runs on the system ``perl``. Any failure returns
    None (best-effort).
    """
    root = Path(cache_dir) if cache_dir else _default_cache_dir()
    if _is_windows():
        return _ensure_windows(root, version)
    return _ensure_posix(root, version)


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cache_dir = argv[0] if argv else None
    path = ensure_exiftool(cache_dir)
    if path:
        print(path)
        return 0
    print(
        "no ExifTool available and the download did not succeed.\n"
        "install it yourself (https://exiftool.org) or set EXIFTOOL_PATH to an existing binary.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
