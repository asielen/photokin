import hashlib
import io
import tarfile
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


def _make_posix_tar(
    *, wrap_dir: str = "Image-ExifTool-13.47", shebang: bytes = b"#!/usr/bin/perl\n", with_lib: bool = True
) -> bytes:
    """Build a synthetic ExifTool perl tarball: <wrap>/exiftool + <wrap>/lib/Image/ExifTool.pm."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(f"{wrap_dir}/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))

        add("exiftool", shebang + b"print 'exiftool';\n")
        if with_lib:
            add("lib/Image/ExifTool.pm", b"package Image::ExifTool;\n")
            add("lib/Image/ExifTool/XMP.pm", b"package Image::ExifTool::XMP;\n")
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


class TestExtractPosixBundle(unittest.TestCase):
    def test_extract_normalizes_layout_and_rewrites_shebang(self):
        data = _make_posix_tar()
        with TemporaryDirectory() as d:
            dest = Path(d) / "mac"
            script = fetch._extract_posix_bundle(data, dest)
            self.assertEqual(script, dest / "exiftool")
            self.assertTrue(script.is_file())
            self.assertTrue((dest / "lib" / "Image" / "ExifTool.pm").is_file())
            self.assertTrue((dest / "lib" / "Image" / "ExifTool" / "XMP.pm").is_file())
            body = script.read_bytes()
            self.assertTrue(body.startswith(b"#!/usr/bin/env perl\n"))
            self.assertIn(b"print 'exiftool';", body)

    def test_extract_missing_lib_raises(self):
        data = _make_posix_tar(with_lib=False)
        with TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                fetch._extract_posix_bundle(data, Path(d) / "mac")


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


class TestPublishedChecksum(unittest.TestCase):
    def test_prefers_versioned_file_and_parses_openssl_format(self):
        def fake_get(url, **kw):
            if url.endswith("checksums-13.47.txt"):
                return (
                    "SHA1(exiftool-13.47_64.zip)= " + "a" * 40 + "\n"
                    "SHA256(exiftool-13.47_64.zip)= " + "b" * 64 + "\n"
                ).encode()
            raise OSError("versioned file should be tried first")

        with patch.object(fetch, "_http_get", side_effect=fake_get):
            got = fetch._published_sha256("exiftool-13.47_64.zip", "13.47")
        self.assertEqual(got, "b" * 64)

    def test_falls_back_to_unversioned_sha256sum_format(self):
        def fake_get(url, **kw):
            if url.endswith("checksums-13.47.txt"):
                raise OSError("404")
            if url.endswith("checksums.txt"):
                return (("c" * 64) + "  exiftool-13.47_64.zip\n").encode()
            raise OSError("unexpected url")

        with patch.object(fetch, "_http_get", side_effect=fake_get):
            got = fetch._published_sha256("exiftool-13.47_64.zip", "13.47")
        self.assertEqual(got, "c" * 64)

    def test_returns_none_when_unreachable(self):
        with patch.object(fetch, "_http_get", side_effect=OSError("blocked")):
            self.assertIsNone(fetch._published_sha256("exiftool-13.47_64.zip", "13.47"))


class TestEnsureExiftoolPosix(unittest.TestCase):
    def test_cached_copy_wins_without_download(self):
        with TemporaryDirectory() as cache:
            sub = Path(cache) / "exiftool" / fetch._posix_subdir()
            (sub / "lib" / "Image").mkdir(parents=True)
            (sub / "exiftool").write_text("#!/usr/bin/env perl\n")
            (sub / "lib" / "Image" / "ExifTool.pm").write_text("package Image::ExifTool;\n")

            def _boom(*a, **k):
                raise AssertionError("should not download when already cached")

            with patch.object(fetch, "_is_windows", return_value=False), patch.object(
                fetch, "_http_get", side_effect=_boom
            ), patch.object(fetch.shutil, "which", return_value=None):
                path = fetch.ensure_exiftool(cache)
            self.assertEqual(Path(path), sub / "exiftool")

    def test_system_exiftool_wins_without_download(self):
        def fake_which(name):
            return "/usr/local/bin/exiftool" if name == "exiftool" else None

        def _boom(*a, **k):
            raise AssertionError("should not download when a system ExifTool exists")

        with TemporaryDirectory() as cache:
            with patch.object(fetch, "_is_windows", return_value=False), patch.object(
                fetch, "_http_get", side_effect=_boom
            ), patch.object(fetch.shutil, "which", side_effect=fake_which):
                path = fetch.ensure_exiftool(cache)
        self.assertEqual(path, "/usr/local/bin/exiftool")

    def test_downloads_perl_distribution_when_nothing_exists(self):
        def fake_which(name):
            return "/usr/bin/perl" if name == "perl" else None

        data = _make_posix_tar()
        with TemporaryDirectory() as cache:
            with patch.object(fetch, "_is_windows", return_value=False), patch.object(
                fetch, "_http_get", return_value=data
            ), patch.object(fetch, "_verify_sha256", return_value=None), patch.object(
                fetch.shutil, "which", side_effect=fake_which
            ):
                path = fetch.ensure_exiftool(cache)
            self.assertIsNotNone(path)
            script = Path(path)
            self.assertEqual(script, Path(cache) / "exiftool" / fetch._posix_subdir() / "exiftool")
            self.assertTrue((script.parent / "lib" / "Image" / "ExifTool.pm").is_file())

    def test_returns_none_without_perl(self):
        def _boom(*a, **k):
            raise AssertionError("should not download without a perl to run it")

        with TemporaryDirectory() as cache:
            with patch.object(fetch, "_is_windows", return_value=False), patch.object(
                fetch, "_http_get", side_effect=_boom
            ), patch.object(fetch.shutil, "which", return_value=None):
                self.assertIsNone(fetch.ensure_exiftool(cache))

    def test_returns_none_on_download_failure(self):
        def fake_which(name):
            return "/usr/bin/perl" if name == "perl" else None

        with TemporaryDirectory() as cache:
            with patch.object(fetch, "_is_windows", return_value=False), patch.object(
                fetch, "_http_get", side_effect=OSError("network blocked")
            ), patch.object(fetch.shutil, "which", side_effect=fake_which):
                self.assertIsNone(fetch.ensure_exiftool(cache))


class TestEnsureExiftool(unittest.TestCase):
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
