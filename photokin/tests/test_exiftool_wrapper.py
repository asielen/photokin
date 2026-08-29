import argparse
import base64
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from photokin import cli
from photokin.exiftool import ExiftoolConfig, apply_changeset
from photokin.exiftool import locate
from photokin.exiftool.apply import _COMMAND_LENGTH_BUDGET, _INLINE_VALUE_MAX, _normalize_exif_datetime
from photokin.exiftool.config import parse_fields

#: A 283-byte 4x4 baseline JPEG -- the same minimal fixture
#: test_canonical_tags_are_writable.py uses, duplicated here rather than
#: imported so this file stays self-contained and a missing Pillow can't
#: become a second, quieter reason for the round-trip test not to run.
_MINIMAL_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABsSFBcUERsXFhceHBsgKEIrKCUlKFE6PTBCYFVlZF9V"
    "XVtqeJmBanGQc1tdhbWGkJ6jq62rZ4C8ybqmx5moq6T/2wBDARweHigjKE4rK06kbl1upKSkpKSk"
    "pKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKT/wAARCAAEAAQDASIA"
    "AhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAAAP/EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAUAQEA"
    "AAAAAAAAAAAAAAAAAAAA/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAwDAQACEQMRAD8AAA//2Q=="
)


class TestExiftoolConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = ExiftoolConfig()
        self.assertIsNone(cfg.path)
        self.assertIsNone(cfg.cache_dir)
        self.assertFalse(cfg.enabled)
        self.assertEqual(
            cfg.fields,
            ("EXIF:DateTimeOriginal", "EXIF:CreateDate", "EXIF:UserComment"),
        )
        self.assertFalse(cfg.dry_run)
        self.assertTrue(cfg.overwrite_original)
        self.assertFalse(cfg.write_sidecar_only)

    def test_from_env_reads_environment(self):
        env = {
            "EXIFTOOL_WRITE_ENABLED": "false",
            "EXIFTOOL_FIELDS": "EXIF:UserComment, XMP-dc:Description",
            "EXIFTOOL_PATH": "/opt/exiftool",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = ExiftoolConfig.from_env()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.fields, ("EXIF:UserComment", "XMP-dc:Description"))
        self.assertEqual(cfg.path, "/opt/exiftool")

    def test_from_env_defaults_when_unset(self):
        # Writes are off unless they are asked for. ``enabled`` used to default
        # to True here while the dataclass declared False, so the line anyone
        # would read to learn the default was the one the CLI never reached.
        with patch.dict(os.environ, {}, clear=False):
            for name in ("EXIFTOOL_WRITE_ENABLED", "EXIFTOOL_FIELDS", "EXIFTOOL_PATH"):
                os.environ.pop(name, None)
            cfg = ExiftoolConfig.from_env()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.fields, ("EXIF:UserComment",))
        self.assertIsNone(cfg.path)

    def test_from_env_overrides_win(self):
        env = {"EXIFTOOL_WRITE_ENABLED": "true", "EXIFTOOL_FIELDS": "EXIF:UserComment"}
        with patch.dict(os.environ, env, clear=False):
            cfg = ExiftoolConfig.from_env(enabled=False, fields=("EXIF:CreateDate",))
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.fields, ("EXIF:CreateDate",))

    def test_from_env_ignores_none_overrides(self):
        env = {"EXIFTOOL_WRITE_ENABLED": "false"}
        with patch.dict(os.environ, env, clear=False):
            cfg = ExiftoolConfig.from_env(enabled=None, path=None)
        self.assertFalse(cfg.enabled)

    def test_parse_fields(self):
        self.assertIsNone(parse_fields(None))
        self.assertIsNone(parse_fields(" , "))
        self.assertEqual(parse_fields("A, B,,C "), ("A", "B", "C"))


class _ChangesetFileMixin:
    """Shared helper: write an NDJSON changeset file, cleaned up after the test."""

    def _write_changeset(self, records) -> str:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".ndjson", delete=False, encoding="utf-8"
        )
        with handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        # This mixin has no TestCase base of its own -- that's what lets two
        # datfile-routing test classes share it alongside TestApplyChangeset
        # without a diamond -- so mypy can't see that addCleanup resolves; it
        # only does once a concrete subclass mixes in unittest.TestCase too.
        self.addCleanup(os.unlink, handle.name)  # type: ignore[attr-defined]
        return handle.name


class TestApplyChangeset(_ChangesetFileMixin, unittest.TestCase):
    def test_disabled_config_returns_warning_summary(self):
        path = self._write_changeset([])
        cfg = ExiftoolConfig(enabled=False)
        summary = apply_changeset(path, cfg)
        self.assertEqual(summary["files_written"], 0)
        self.assertEqual(len(summary["warnings"]), 1)

    def test_dry_run_counts_without_subprocess(self):
        path = self._write_changeset(
            [
                {
                    "path": "/photos/a.jpg",
                    "proposed_changes": {
                        "set": {
                            "EXIF:UserComment": "Hello",
                            "EXIF:DateTimeOriginal": "1968-07-04",
                        }
                    },
                },
                {"path": "/photos/b.jpg", "proposed_changes": {"set": {}}},
            ]
        )
        cfg = ExiftoolConfig(enabled=True, dry_run=True)
        with patch(
            "photokin.exiftool.apply.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch("photokin.exiftool.apply.subprocess.run") as run_mock:
            summary = apply_changeset(path, cfg)
        run_mock.assert_not_called()
        self.assertEqual(summary["files_seen"], 2)
        self.assertEqual(summary["files_written"], 1)
        self.assertEqual(summary["tags_written"], 2)
        self.assertTrue(summary["dry_run"])

    def test_missing_binary_reports_error(self):
        path = self._write_changeset([])
        cfg = ExiftoolConfig(enabled=True)
        with patch(
            "photokin.exiftool.apply.resolve_exiftool_path",
            side_effect=FileNotFoundError("ExifTool not found."),
        ):
            summary = apply_changeset(path, cfg)
        self.assertEqual(len(summary["errors"]), 1)

    def test_an_unwritable_field_is_warned_about_and_never_attempted(self):
        """"Nothing will be written for this tag" has to be true, not aspirational.

        Direct library callers (the Lightroom plugin among them) never pass
        through the CLI's own pre-flight refusal, so this warning is their only
        guard against the old ``XMP:dc:`` colon form. Warning once and then
        still handing the tag to ExifTool per file would reproduce, once per
        file, the exact "doesn't exist or isn't writable" noise the warning
        exists to replace -- so the field must actually drop out of what gets
        written, alongside any field that is fine.
        """
        path = self._write_changeset(
            [
                {
                    "path": "/photos/a.jpg",
                    "proposed_changes": {
                        "set": {
                            "EXIF:UserComment": "Hello",
                            "XMP:dc:Description": "A caption",
                        }
                    },
                }
            ]
        )
        cfg = ExiftoolConfig(enabled=True, fields=("EXIF:UserComment", "XMP:dc:Description"))
        with patch(
            "photokin.exiftool.apply.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch("photokin.exiftool.apply.subprocess.run") as run_mock:
            run_mock.return_value = type(
                "Result", (), {"returncode": 0, "stdout": "", "stderr": ""}
            )()
            summary = apply_changeset(path, cfg)
        self.assertTrue(
            any("XMP:dc:Description" in w.get("tag", "") for w in summary["warnings"])
        )
        self.assertEqual(summary["tags_written"], 1)
        written_cmd = run_mock.call_args[0][0]
        self.assertTrue(any(arg.startswith("-EXIF:UserComment=") for arg in written_cmd))
        self.assertFalse(any(arg.startswith("-XMP:dc:Description=") for arg in written_cmd))

    def test_a_subprocess_error_on_one_file_does_not_abort_the_batch(self):
        """One bad file is a per-file error, not a reason to lose the rest.

        ``subprocess.run`` can fail to even start for reasons beyond a missing
        binary -- a non-executable file, a permissions error -- and those must
        be caught exactly like ``FileNotFoundError`` is: recorded against that
        file, with the files before and after it still written and reported.
        """
        path = self._write_changeset(
            [
                {"path": "/photos/a.jpg", "proposed_changes": {"set": {"EXIF:UserComment": "A"}}},
                {"path": "/photos/b.jpg", "proposed_changes": {"set": {"EXIF:UserComment": "B"}}},
                {"path": "/photos/c.jpg", "proposed_changes": {"set": {"EXIF:UserComment": "C"}}},
            ]
        )
        cfg = ExiftoolConfig(enabled=True, fields=("EXIF:UserComment",))
        ok_result = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        def _run(cmd, **_kw):
            if "/photos/b.jpg" in cmd:
                raise PermissionError("not executable")
            return ok_result

        with patch(
            "photokin.exiftool.apply.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch("photokin.exiftool.apply.subprocess.run", side_effect=_run):
            summary = apply_changeset(path, cfg)
        self.assertEqual(summary["files_seen"], 3)
        self.assertEqual(summary["files_written"], 2)
        self.assertEqual(len(summary["errors"]), 1)
        self.assertEqual(summary["errors"][0]["path"], "/photos/b.jpg")


class TestDatfileRoutingForLongValues(_ChangesetFileMixin, unittest.TestCase):
    """Part B: values too long for the command line move into a DATFILE.

    The round-trip case exercises the real ExifTool binary (skipped cleanly
    when none is on PATH, matching test_canonical_tags_are_writable.py's
    convention) because the hazard it guards against -- a value silently
    truncated or dropped -- is a property of what ExifTool actually does with
    the file it is handed, not something a mock can demonstrate. The routing,
    budget and cleanup behavior below it only need to observe what argv
    ``apply_changeset`` assembles, so those are exercised at the mocked
    ``subprocess.run`` boundary, matching this file's existing style.
    """

    def test_a_long_description_round_trips_byte_identical_via_the_real_binary(self):
        """The E2 exit criterion: a ~40,000-char value survives write+read whole.

        Covers newlines, non-ASCII, and the marks the 0.4.0 conventions
        produce (``~~struck~~``, ``_underlined_``, a ``> [margin note]`` line,
        a bare ``---`` rule) -- and asserts content equality, not just that
        the run reported success, per E5.
        """
        exiftool = shutil.which("exiftool")
        if not exiftool:
            self.skipTest("no exiftool binary on PATH")

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        image = Path(tmp.name) / "probe.jpg"
        image.write_bytes(base64.b64decode(_MINIMAL_JPEG_B64))

        marks = (
            "Café résumé — naïve façade\n"
            "~~struck~~ and _underlined_\n"
            "> a margin note\n"
            "---\n"
        )
        body = ("Some handwritten transcription text. " * 50 + "\n") * 20
        description = marks + body
        # Pad/trim to exactly 40,000 characters -- the size the plan measured
        # a plain inline value failing at (WinError 206 past ~32,767).
        description = (
            description[:40_000]
            if len(description) > 40_000
            else description + "x" * (40_000 - len(description))
        )
        self.assertEqual(len(description), 40_000)

        changeset = self._write_changeset(
            [
                {
                    "path": str(image),
                    "proposed_changes": {"set": {"XMP-dc:Description": description}},
                }
            ]
        )
        cfg = ExiftoolConfig(enabled=True, path=exiftool, fields=("XMP-dc:Description",))
        summary = apply_changeset(changeset, cfg)

        self.assertEqual(summary["errors"], [])
        self.assertEqual(summary["files_written"], 1)

        readback = subprocess.run(
            [exiftool, "-b", "-XMP-dc:Description", str(image)],
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(readback.stdout.decode("utf-8"), description)

    def test_a_short_value_still_takes_the_inline_path(self):
        """Threshold-gating (E3) must leave the ordinary case alone."""
        changeset = self._write_changeset(
            [
                {
                    "path": "/photos/a.jpg",
                    "proposed_changes": {"set": {"EXIF:UserComment": "A short caption."}},
                }
            ]
        )
        cfg = ExiftoolConfig(enabled=True, fields=("EXIF:UserComment",))
        ok_result = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch(
            "photokin.exiftool.apply.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch(
            "photokin.exiftool.apply.subprocess.run", return_value=ok_result
        ) as run_mock:
            summary = apply_changeset(changeset, cfg)
        self.assertEqual(summary["errors"], [])
        self.assertEqual(summary["files_written"], 1)
        cmd = run_mock.call_args[0][0]
        self.assertTrue(any(arg.startswith("-EXIF:UserComment=") for arg in cmd))
        self.assertFalse(any("<=" in arg for arg in cmd))

    def test_medium_values_that_together_exceed_the_budget_get_routed(self):
        """Several values individually under the per-value threshold can still
        sum past the whole-command budget (E3) -- the OS limit is on the
        whole command line, not on any one value. Enough of them must be
        routed for the assembled command to fit the budget.
        """
        tags = tuple(f"XMP-dc:Field{i}" for i in range(10))
        value = "m" * 3_500
        self.assertLess(len(value), _INLINE_VALUE_MAX)  # under threshold alone
        changeset = self._write_changeset(
            [{"path": "/photos/a.jpg", "proposed_changes": {"set": dict.fromkeys(tags, value)}}]
        )
        cfg = ExiftoolConfig(enabled=True, fields=tags)
        ok_result = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch(
            "photokin.exiftool.apply.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch(
            "photokin.exiftool.apply.subprocess.run", return_value=ok_result
        ) as run_mock:
            summary = apply_changeset(changeset, cfg)
        self.assertEqual(summary["errors"], [])
        cmd = run_mock.call_args[0][0]
        self.assertLessEqual(len(" ".join(cmd)), _COMMAND_LENGTH_BUDGET)
        routed = [arg for arg in cmd if "<=" in arg]
        self.assertGreater(len(routed), 0, "expected at least one value routed to a DATFILE")
        self.assertLess(len(routed), len(tags), "expected some values to stay inline")

    def test_a_datfile_read_failure_is_reported_even_when_exiftool_exits_zero(self):
        """E5: do not trust the exit code alone.

        Measured against the real binary: when one tag's DATFILE cannot be
        opened but other tags on the same command succeed, ExifTool still
        reports "N image files updated" and exits 0 -- only stderr names the
        miss. Trusting the exit code alone would let that silently-dropped
        write pass as success.
        """
        changeset = self._write_changeset(
            [
                {
                    "path": "/photos/a.jpg",
                    "proposed_changes": {"set": {"EXIF:UserComment": "z" * 5_000}},
                }
            ]
        )
        cfg = ExiftoolConfig(enabled=True, fields=("EXIF:UserComment",))
        partial_result = type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": "    1 image files updated\n",
                "stderr": "Error opening file 000001_EXIF-UserComment.txt\n",
            },
        )()
        with patch(
            "photokin.exiftool.apply.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch("photokin.exiftool.apply.subprocess.run", return_value=partial_result):
            summary = apply_changeset(changeset, cfg)
        self.assertEqual(summary["files_written"], 0)
        self.assertEqual(len(summary["errors"]), 1)
        self.assertIn("DATFILE", summary["errors"][0]["error"])

    def test_no_temp_files_survive_the_batch_including_a_failed_file(self):
        """E4: one TemporaryDirectory for the whole batch, cleaned up on exit
        even when a file in the middle of the batch fails.
        """
        long_value = "y" * 5_000  # over _INLINE_VALUE_MAX so a DATFILE is actually written
        changeset = self._write_changeset(
            [
                {"path": "/photos/a.jpg", "proposed_changes": {"set": {"EXIF:UserComment": long_value}}},
                {"path": "/photos/b.jpg", "proposed_changes": {"set": {"EXIF:UserComment": long_value}}},
            ]
        )
        cfg = ExiftoolConfig(enabled=True, fields=("EXIF:UserComment",))
        ok_result = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        def _run(cmd, **_kw):
            if "/photos/b.jpg" in cmd:
                raise PermissionError("not executable")
            return ok_result

        real_temporary_directory = tempfile.TemporaryDirectory
        created: list[str] = []

        def _spy_temporary_directory(*args, **kwargs):
            instance = real_temporary_directory(*args, **kwargs)
            created.append(instance.name)
            return instance

        with patch(
            "photokin.exiftool.apply.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch(
            "photokin.exiftool.apply.subprocess.run", side_effect=_run
        ), patch(
            "photokin.exiftool.apply.tempfile.TemporaryDirectory",
            side_effect=_spy_temporary_directory,
        ):
            summary = apply_changeset(changeset, cfg)

        self.assertEqual(summary["files_written"], 1)
        self.assertEqual(len(summary["errors"]), 1)
        self.assertEqual(len(created), 1)
        self.assertFalse(os.path.exists(created[0]))


class TestExifDatetimeNormalization(unittest.TestCase):
    def test_iso_datetime(self):
        value, warning = _normalize_exif_datetime("1968-07-04T12:30:00")
        self.assertEqual(value, "1968:07:04 12:30:00")
        self.assertIsNone(warning)

    def test_zulu_suffix(self):
        value, warning = _normalize_exif_datetime("1968-07-04T12:30:00Z")
        self.assertEqual(value, "1968:07:04 12:30:00")
        self.assertIsNone(warning)

    def test_date_only(self):
        value, warning = _normalize_exif_datetime("1968-07-04")
        self.assertEqual(value, "1968:07:04 00:00:00")
        self.assertIsNone(warning)

    def test_already_exif_format(self):
        value, warning = _normalize_exif_datetime("1968:07:04")
        self.assertEqual(value, "1968:07:04 00:00:00")
        self.assertIsNone(warning)

    def test_garbage_returns_warning(self):
        value, warning = _normalize_exif_datetime("around july, maybe 1968")
        self.assertIsNone(value)
        self.assertIsNotNone(warning)


class TestCliExiftoolConfigResolution(unittest.TestCase):
    def _args(self, **overrides) -> argparse.Namespace:
        values = {"exiftool_write": None, "exiftool_fields": None, "exiftool_path": None}
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_flag_beats_env(self):
        env = {"EXIFTOOL_WRITE_ENABLED": "true", "EXIFTOOL_FIELDS": "EXIF:CreateDate"}
        with patch.dict(os.environ, env, clear=False):
            cfg = cli._resolve_exiftool_config(
                self._args(exiftool_write="false", exiftool_fields="EXIF:UserComment"),
                dry_run=False,
            )
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.fields, ("EXIF:UserComment",))

    def test_env_beats_default(self):
        env = {"EXIFTOOL_WRITE_ENABLED": "false", "EXIFTOOL_FIELDS": "EXIF:CreateDate"}
        with patch.dict(os.environ, env, clear=False):
            cfg = cli._resolve_exiftool_config(self._args(), dry_run=False)
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.fields, ("EXIF:CreateDate",))

    def test_defaults_when_nothing_set(self):
        # No flag and no environment means no writing: the CLI has to be told.
        with patch.dict(os.environ, {}, clear=False):
            for name in ("EXIFTOOL_WRITE_ENABLED", "EXIFTOOL_FIELDS", "EXIFTOOL_PATH"):
                os.environ.pop(name, None)
            cfg = cli._resolve_exiftool_config(self._args(), dry_run=True)
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.fields, ("EXIF:UserComment",))
        self.assertTrue(cfg.dry_run)
        self.assertTrue(cfg.overwrite_original)


class TestResolveExiftoolPath(unittest.TestCase):
    def test_completeness_check_mac(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "exiftool"
            exe.write_text("#!/usr/bin/env perl\n")
            # No sibling lib/ => incomplete, must not be used.
            self.assertFalse(locate._cached_exiftool_is_complete(exe, "mac"))
            libpm = Path(d) / "lib" / "Image" / "ExifTool.pm"
            libpm.parent.mkdir(parents=True)
            libpm.write_text("package Image::ExifTool;\n")
            self.assertTrue(locate._cached_exiftool_is_complete(exe, "mac"))

    def test_completeness_check_win(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d) / "exiftool.exe"
            exe.write_text("binary")
            self.assertFalse(locate._cached_exiftool_is_complete(exe, "win"))
            (Path(d) / "exiftool_files").mkdir()
            self.assertTrue(locate._cached_exiftool_is_complete(exe, "win"))

    def _make_cached(self, cache: str, *, complete: bool) -> Path:
        """Create a downloaded-cache layout: <cache>/exiftool/win/exiftool.exe[+exiftool_files]."""
        win = Path(cache) / "exiftool" / "win"
        win.mkdir(parents=True)
        exe = win / "exiftool.exe"
        exe.write_text("binary")
        if complete:
            (win / "exiftool_files").mkdir()
        return exe

    def test_cached_copy_is_used_when_complete(self):
        with tempfile.TemporaryDirectory() as cache:
            exe = self._make_cached(cache, complete=True)
            cfg = ExiftoolConfig(cache_dir=cache)
            with patch.object(
                locate, "_platform_exiftool_relpath", return_value=("win", "exiftool.exe")
            ), patch.object(locate.shutil, "which", return_value=None):
                result = locate.resolve_exiftool_path(cfg)
            self.assertEqual(result, str(exe))

    def test_incomplete_cache_falls_back_to_path(self):
        with tempfile.TemporaryDirectory() as cache:
            self._make_cached(cache, complete=False)  # exe but no exiftool_files
            cfg = ExiftoolConfig(cache_dir=cache)
            with patch.object(
                locate, "_platform_exiftool_relpath", return_value=("win", "exiftool.exe")
            ), patch.object(locate.shutil, "which", return_value="/usr/bin/exiftool"):
                result = locate.resolve_exiftool_path(cfg)
            self.assertEqual(result, "/usr/bin/exiftool")

    def test_no_cache_uses_system_path(self):
        with tempfile.TemporaryDirectory() as cache:
            cfg = ExiftoolConfig(cache_dir=cache)
            with patch.object(
                locate, "_platform_exiftool_relpath", return_value=("win", "exiftool.exe")
            ), patch.object(locate.shutil, "which", return_value="/usr/bin/exiftool"):
                result = locate.resolve_exiftool_path(cfg)
            self.assertEqual(result, "/usr/bin/exiftool")

    def test_missing_everywhere_raises(self):
        with tempfile.TemporaryDirectory() as cache:
            cfg = ExiftoolConfig(cache_dir=cache)
            with patch.object(
                locate, "_platform_exiftool_relpath", return_value=None
            ), patch.object(locate.shutil, "which", return_value=None):
                with self.assertRaises(FileNotFoundError):
                    locate.resolve_exiftool_path(cfg)


if __name__ == "__main__":
    unittest.main()
