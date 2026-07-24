import hashlib
import io
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from photokin.exiftool import fetch


def _make_win_zip(*, wrap_dir: str = "exiftool-13.47_64", exe_name: str = "exiftool(-k).exe") -> bytes:
    """Build a synthetic ExifTool Windows zip: <wrap>/<exe> + <wrap>/exiftool_files/..."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{wrap_dir}/{exe_name}", "MZ-fake-exe")
        zf.writestr(f"{wrap_dir}/exiftool_files/exiftool.pl", "#!/usr/bin/perl\n")
        zf.writestr(f"{wrap_dir}/exiftool_files/lib/Image/ExifTool.pm", "package Image::ExifTool;\n")
    return buf.getvalue()


class TestExtractWindowsBundle(unittest.TestCase):
    def test_extract_normalizes_layout(self):
        data = _make_win_zip(exe_name="exiftool(-k).exe")
        with TemporaryDirectory() as d:
            dest = Path(d) / "win"
            exe = fetch._extract_windows_bundle(data, dest)
            self.assertEqual(exe, dest / "exiftool.exe")
            self.assertTrue(exe.is_file())
            self.assertTrue((dest / "exiftool_files").is_dir())
            self.assertTrue((dest / "exiftool_files" / "exiftool.pl").is_file())

    def test_extract_accepts_plain_exiftool_exe(self):
        data = _make_win_zip(wrap_dir=".", exe_name="exiftool.exe")
        with TemporaryDirectory() as d:
            dest = Path(d) / "win"
            exe = fetch._extract_windows_bundle(data, dest)
            self.assertTrue(exe.is_file())
            self.assertTrue((dest / "exiftool_files").is_dir())

    def test_extract_missing_files_dir_raises(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("exiftool.exe", "x")
        with TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                fetch._extract_windows_bundle(buf.getvalue(), Path(d) / "win")


class TestVerifySha256(unittest.TestCase):
    def test_offline_pin_match(self):
        data = b"payload"
        digest = hashlib.sha256(data).hexdigest()
        with patch.dict(fetch.KNOWN_SHA256, {"a.zip": digest}, clear=False):
            fetch._verify_sha256(data, "a.zip")  # no raise

    def test_mismatch_raises(self):
        with patch.dict(fetch.KNOWN_SHA256, {"a.zip": "00" * 32}, clear=False):
            with self.assertRaises(RuntimeError):
                fetch._verify_sha256(b"payload", "a.zip")

    def test_uses_published_checksum_when_no_pin(self):
        data = b"payload"
        digest = hashlib.sha256(data).hexdigest()
        with patch.object(fetch, "_published_sha256", return_value=digest):
            fetch._verify_sha256(data, "b.zip")  # no raise

    def test_fails_closed_without_any_checksum(self):
        with patch.object(fetch, "_published_sha256", return_value=None):
            with self.assertRaises(RuntimeError):
                fetch._verify_sha256(b"payload", "b.zip")


class TestEnsureExiftool(unittest.TestCase):
    def test_non_windows_returns_none(self):
        with patch.object(fetch, "_is_windows", return_value=False):
            self.assertIsNone(fetch.ensure_exiftool("/tmp/whatever"))

    def test_downloads_and_extracts_on_windows(self):
        data = _make_win_zip()
        with TemporaryDirectory() as cache:
            with patch.object(fetch, "_is_windows", return_value=True), patch.object(
                fetch, "_http_get", return_value=data
            ), patch.object(fetch, "_verify_sha256", return_value=None):
                path = fetch.ensure_exiftool(cache)
            self.assertIsNotNone(path)
            exe = Path(path)
            self.assertEqual(exe, Path(cache) / "exiftool" / "win" / "exiftool.exe")
            self.assertTrue(exe.is_file())
            self.assertTrue((exe.parent / "exiftool_files").is_dir())

    def test_idempotent_skips_download_when_present(self):
        with TemporaryDirectory() as cache:
            win = Path(cache) / "exiftool" / "win"
            win.mkdir(parents=True)
            (win / "exiftool.exe").write_text("x")
            (win / "exiftool_files").mkdir()

            def _boom(*a, **k):
                raise AssertionError("should not download when already cached")

            with patch.object(fetch, "_is_windows", return_value=True), patch.object(
                fetch, "_http_get", side_effect=_boom
            ):
                path = fetch.ensure_exiftool(cache)
            self.assertEqual(Path(path), win / "exiftool.exe")

    def test_returns_none_on_download_failure(self):
        with TemporaryDirectory() as cache:
            with patch.object(fetch, "_is_windows", return_value=True), patch.object(
                fetch, "_http_get", side_effect=OSError("network blocked")
            ):
                self.assertIsNone(fetch.ensure_exiftool(cache))


if __name__ == "__main__":
    unittest.main()
