import argparse
import base64
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from photokin import cli
from photokin.exiftool import ExiftoolConfig, apply_changeset, locate
from photokin.exiftool.apply import (
    _COMMAND_LENGTH_BUDGET,
    _INLINE_VALUE_MAX,
    _build_exiftool_command,
    _datfile_name,
    _normalize_exif_datetime,
    _select_datfile_routing,
)
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


class TestDatfileRoutingMeasuresTheRealWindowsCommandLine(_ChangesetFileMixin, unittest.TestCase):
    """F3: the routing budget check has to measure what Windows will actually see.

    ``_select_datfile_routing`` decides a command fits by rebuilding it and
    measuring its length -- and before this fix that measurement was
    ``" ".join(cmd)``, which is not what ``CreateProcess`` receives.
    ``subprocess`` quotes and escapes each argument first
    (``subprocess.list2cmdline``), and a value dense in quotes and
    backslashes has every one of them escaped, so the naive join can
    under-count the real command by nearly 2x. A set of values individually
    under the per-value threshold -- so none is force-routed on its own --
    can therefore measure comfortably under the whole-command budget while
    the command Windows actually receives sails past the 32,767-character
    OS cap, which is the exact failure this budget check exists to prevent.
    """

    def test_a_quote_dense_command_is_measured_and_routed_by_the_real_quoting(self) -> None:
        """Seven values, each under ``_INLINE_VALUE_MAX`` so none is force-routed
        on its own, built from repeated ``\\"`` pairs so quoting expands them
        sharply. The property under test is the one the budget exists for:
        the command ``_select_datfile_routing`` settles on must fit the OS
        cap as ``subprocess.list2cmdline`` -- and so Windows -- actually
        measures it, not as a naive space-join would. The exact number of
        tags routed is deliberately not asserted; only the resulting command
        actually fitting, and at least one tag having moved, are.
        """
        tags = {f"XMP-dc:Field{i}": ('\\"' * 1_999) for i in range(7)}
        for value in tags.values():
            self.assertLess(len(value), _INLINE_VALUE_MAX, "value must not force-route alone")

        exiftool = "exiftool"
        cfg = ExiftoolConfig()
        path = "/photos/adversarial.jpg"
        # A callable, not a mapping: the routing asks for a path only for a tag
        # it actually routes, which is what lets the real caller hold off
        # creating a temporary directory until one is needed.
        asked_for: list[str] = []

        def _path_for(tag: str) -> str:
            asked_for.append(tag)
            return f"/tmp/{_datfile_name(1, tag)}"

        routed = _select_datfile_routing(exiftool, cfg, tags, path, _path_for)
        self.assertGreater(len(routed), 0, "expected quoting overhead to force some routing")
        self.assertEqual(
            set(asked_for), set(routed),
            "a path was requested for a tag that was never routed",
        )

        cmd = _build_exiftool_command(exiftool, cfg, tags, path, routed)
        real_length = len(subprocess.list2cmdline(cmd).encode("utf-16-le")) // 2 + 1
        self.assertLessEqual(
            real_length,
            _COMMAND_LENGTH_BUDGET,
            "the command Windows actually sends still exceeds the budget",
        )


class TestDatfileRoutingCountsNonBMPCharactersAsWindowsDoes(_ChangesetFileMixin, unittest.TestCase):
    """C1: the routing budget must measure UTF-16 code units, not code points.

    Windows' ``CreateProcess`` counts a command line in UTF-16 code units;
    Python's ``len`` counts Unicode code points. The two agree for ordinary
    text and diverge by exactly 2x for any character outside the Basic
    Multilingual Plane -- a historic script, a musical symbol, an emoji a
    modern annotation might carry -- because each one is a single code point
    but a UTF-16 *surrogate pair*. Seven values built from such a character,
    each individually under ``_INLINE_VALUE_MAX`` so none force-routes alone,
    measure comfortably under ``_COMMAND_LENGTH_BUDGET`` by code point and
    past it by nearly 2x as Windows actually sees them -- code-point counting
    lets a command like this sail through unrouted and fail at
    ``CreateProcess`` with WinError 206.
    """

    def test_non_bmp_values_are_measured_in_utf16_units_and_routed(self) -> None:
        # MUSICAL SYMBOL G CLEF (U+1D11E): one Python code point, a UTF-16
        # surrogate pair -- two code units where ``len`` sees one.
        clef = "\U0001D11E"
        value = clef * 3_998
        tags = {f"XMP-dc:Field{i}": value for i in range(7)}
        for v in tags.values():
            self.assertLess(len(v), _INLINE_VALUE_MAX, "value must not force-route alone")

        exiftool = "exiftool"
        cfg = ExiftoolConfig()
        path = "/photos/non-bmp.jpg"

        def _path_for(tag: str) -> str:
            return f"/tmp/{_datfile_name(1, tag)}"

        routed = _select_datfile_routing(exiftool, cfg, tags, path, _path_for)
        self.assertGreater(
            len(routed),
            0,
            "expected non-BMP text to be measured at its real UTF-16 cost and routed",
        )

        cmd = _build_exiftool_command(exiftool, cfg, tags, path, routed)
        real_length = len(subprocess.list2cmdline(cmd).encode("utf-16-le")) // 2 + 1
        self.assertLessEqual(
            real_length,
            _COMMAND_LENGTH_BUDGET,
            "the command Windows actually sends still exceeds the budget",
        )


class TestBuildCommandDeclaresUtf8FilenameCharsetOnlyWhenRouting(
    _ChangesetFileMixin, unittest.TestCase
):
    """D3 (Codex review 3): a routed command must tell ExifTool its DATFILE
    path is UTF-8, and an ordinary inline-only command must stay exactly the
    command this wrapper has always built.

    ``_datfile_name`` keeps the DATFILE's own BASENAME ASCII, but the
    directory it sits in is the system temporary root, which is not this
    wrapper's to choose -- a non-ASCII Windows profile name, or a customized
    ``%TEMP%``, puts non-ASCII in the path regardless. Without
    ``-charset filename=utf8``, ExifTool decodes that path in the system
    codepage, does not find the file, and rejects every routed value on such
    a machine. The switch must appear only on a command that actually routes
    something -- an inline-only write has no DATFILE path for it to protect,
    and the read wrapper's own byte-identical command is the thing an
    unconditional switch would have changed for no reason.
    """

    def test_a_routed_command_declares_the_charset_switch(self) -> None:
        cmd = _build_exiftool_command(
            "/fake/exiftool",
            ExiftoolConfig(),
            {"XMP-dc:Description": "irrelevant once routed"},
            "/photos/a.jpg",
            {"XMP-dc:Description": "/tmp/000001_XMP-dc_Description_deadbeef.txt"},
        )
        self.assertIn("-charset", cmd)
        self.assertEqual(cmd[cmd.index("-charset") + 1], "filename=utf8")

    def test_an_inline_only_command_is_byte_identical_to_before_the_fix(self) -> None:
        cmd = _build_exiftool_command(
            "/fake/exiftool",
            ExiftoolConfig(),
            {"EXIF:UserComment": "Hello"},
            "/photos/a.jpg",
            None,
        )
        self.assertNotIn("-charset", cmd)
        self.assertEqual(
            cmd,
            ["/fake/exiftool", "-overwrite_original", "-EXIF:UserComment=Hello", "/photos/a.jpg"],
        )


class TestDatfileTempDirectoryIsCreatedLazily(_ChangesetFileMixin, unittest.TestCase):
    """C3: the batch's ``TemporaryDirectory`` must not be created until a
    value actually routes.

    Creating it unconditionally on entry meant even a dry run -- whose whole
    promise is that it touches nothing -- raised in a locked-down environment
    with no writable temporary root. Spied the same way
    ``test_no_temp_files_survive_the_batch_including_a_failed_file`` already
    does, so a regression back to eager creation shows up as a directory
    having been created in a run that should never have made one.
    """

    def _spy_temporary_directory(self) -> tuple[list[str], Any]:
        real_temporary_directory = tempfile.TemporaryDirectory
        created: list[str] = []

        def _spy(*args, **kwargs):
            instance = real_temporary_directory(*args, **kwargs)
            created.append(instance.name)
            return instance

        return created, _spy

    def test_a_dry_run_with_a_long_value_creates_no_temp_directory(self) -> None:
        long_value = "d" * 50_000
        changeset = self._write_changeset(
            [
                {
                    "path": "/photos/a.jpg",
                    "proposed_changes": {"set": {"EXIF:UserComment": long_value}},
                }
            ]
        )
        cfg = ExiftoolConfig(enabled=True, dry_run=True, fields=("EXIF:UserComment",))
        created, spy = self._spy_temporary_directory()
        with patch(
            "photokin.exiftool.apply.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch("photokin.exiftool.apply.subprocess.run") as run_mock, patch(
            "photokin.exiftool.apply.tempfile.TemporaryDirectory", side_effect=spy
        ):
            summary = apply_changeset(changeset, cfg)
        run_mock.assert_not_called()
        self.assertEqual(summary["files_written"], 1)
        self.assertEqual(created, [], "a dry run must never create a DATFILE directory")

    def test_an_ordinary_run_of_short_values_creates_no_temp_directory(self) -> None:
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
        created, spy = self._spy_temporary_directory()
        with patch(
            "photokin.exiftool.apply.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch(
            "photokin.exiftool.apply.subprocess.run", return_value=ok_result
        ) as run_mock, patch(
            "photokin.exiftool.apply.tempfile.TemporaryDirectory", side_effect=spy
        ):
            summary = apply_changeset(changeset, cfg)
        self.assertEqual(summary["files_written"], 1)
        self.assertEqual(created, [], "an all-short-values run must never create a DATFILE directory")
        cmd = run_mock.call_args[0][0]
        self.assertTrue(any(arg.startswith("-EXIF:UserComment=") for arg in cmd))

    def test_a_run_that_routes_creates_exactly_one_temp_directory_and_cleans_it_up(self) -> None:
        long_value = "e" * 5_000
        changeset = self._write_changeset(
            [
                {
                    "path": "/photos/a.jpg",
                    "proposed_changes": {"set": {"EXIF:UserComment": long_value}},
                }
            ]
        )
        cfg = ExiftoolConfig(enabled=True, fields=("EXIF:UserComment",))
        ok_result = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        created, spy = self._spy_temporary_directory()
        with patch(
            "photokin.exiftool.apply.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch(
            "photokin.exiftool.apply.subprocess.run", return_value=ok_result
        ), patch(
            "photokin.exiftool.apply.tempfile.TemporaryDirectory", side_effect=spy
        ):
            summary = apply_changeset(changeset, cfg)
        self.assertEqual(summary["files_written"], 1)
        self.assertEqual(len(created), 1, "expected exactly one DATFILE directory for the batch")
        self.assertFalse(os.path.exists(created[0]), "the DATFILE directory must be cleaned up")


class TestADatfileWriteFailureIsPerFileNotBatchFatal(_ChangesetFileMixin, unittest.TestCase):
    """C4: a ``UnicodeEncodeError`` from a routed value must not escape
    ``apply_changeset``.

    A lone surrogate reaching a routed value -- reachable from a perfectly
    valid JSON ``\\ud800`` escape, since Python's ``json`` module happily
    decodes one into a lone-surrogate ``str`` -- raises ``UnicodeEncodeError``
    when the UTF-8 DATFILE write tries to encode it, and that is a
    ``ValueError``, not an ``OSError``. Catching only ``OSError`` there let it
    escape the per-file try and ``apply_changeset`` entirely, aborting every
    remaining file in the batch rather than being recorded against the one
    file that caused it.
    """

    def test_a_lone_surrogate_is_a_per_file_error_and_the_next_file_still_writes(self) -> None:
        surrogate_value = "\ud800" * 4_001  # over _INLINE_VALUE_MAX, forces routing
        ordinary_long_value = "y" * 5_000
        changeset = self._write_changeset(
            [
                {
                    "path": "/photos/a.jpg",
                    "proposed_changes": {"set": {"EXIF:UserComment": surrogate_value}},
                },
                {
                    "path": "/photos/b.jpg",
                    "proposed_changes": {"set": {"EXIF:UserComment": ordinary_long_value}},
                },
            ]
        )
        cfg = ExiftoolConfig(enabled=True, fields=("EXIF:UserComment",))
        ok_result = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch(
            "photokin.exiftool.apply.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch("photokin.exiftool.apply.subprocess.run", return_value=ok_result):
            # Must not raise: a UnicodeEncodeError on the first record used to
            # escape apply_changeset and abort the whole batch.
            summary = apply_changeset(changeset, cfg)

        self.assertEqual(len(summary["errors"]), 1)
        self.assertEqual(summary["errors"][0]["path"], "/photos/a.jpg")
        self.assertEqual(summary["files_written"], 1, "the second file must still be written")


class TestAShortLoneSurrogateDoesNotAbortTheWholeBatch(_ChangesetFileMixin, unittest.TestCase):
    """D2 (Codex review 3): the UTF-16 command-length measurement itself must
    tolerate a lone surrogate, not just the DATFILE write ``C4`` guards.

    A lone surrogate reaches this wrapper from a perfectly valid JSON
    ``\\ud800`` escape. ``TestADatfileWriteFailureIsPerFileNotBatchFatal``
    (C4) pins the DATFILE-write encode for a value long enough to force
    routing on its own; this pins a SEPARATE encode the UTF-16 length fix
    itself introduced, reachable even for a value well under
    ``_INLINE_VALUE_MAX`` that never routes at all.
    ``_select_datfile_routing`` measures the whole assembled command on
    every call -- including the common case where nothing is long enough to
    route -- so a strict UTF-16 encode there raised ``UnicodeEncodeError``
    before either per-file guard (the DATFILE write, the ``subprocess.run``
    call) had a chance to catch it, escaping ``apply_changeset`` entirely and
    aborting every record after the first.
    """

    def test_a_short_lone_surrogate_value_still_lets_the_next_record_write(self) -> None:
        surrogate_value = "\ud800" * 10  # far under _INLINE_VALUE_MAX: must not route
        self.assertLess(len(surrogate_value), _INLINE_VALUE_MAX)
        changeset = self._write_changeset(
            [
                {
                    "path": "/photos/a.jpg",
                    "proposed_changes": {"set": {"EXIF:UserComment": surrogate_value}},
                },
                {
                    "path": "/photos/b.jpg",
                    "proposed_changes": {"set": {"EXIF:UserComment": "An ordinary caption"}},
                },
            ]
        )
        cfg = ExiftoolConfig(enabled=True, fields=("EXIF:UserComment",))
        ok_result = type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch(
            "photokin.exiftool.apply.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch("photokin.exiftool.apply.subprocess.run", return_value=ok_result):
            # Must not raise: a UnicodeEncodeError measuring the first
            # record's (short, inline) command used to escape
            # apply_changeset and abort the whole batch before the second
            # record was ever reached.
            summary = apply_changeset(changeset, cfg)

        self.assertEqual(summary["files_seen"], 2)
        self.assertEqual(summary["errors"], [])
        self.assertEqual(summary["files_written"], 2, "the second record must still be written")


class TestDatfileNamesDoNotCollideAcrossTagSpellings(_ChangesetFileMixin, unittest.TestCase):
    """F4: two tags that fold to the same safe filename must not share a DATFILE.

    ``_datfile_name`` replaces every filename-unsafe character with ``_``, so
    ``XMP-dc:Description`` and ``XMP-dc/Description`` -- both reachable
    through ``--fields``, which validates neither uniqueness nor
    filename-safety -- folded to the identical basename before this fix. Two
    tags routed to one path meant the second write overwrote the first, and
    ExifTool read the wrong value back for whichever tag lost the race, with
    nothing anywhere reporting it: the run reported success and a field
    silently carried another field's content. The fix appends a short digest
    of the tag as actually written, so two tags that collide after
    sanitization still land in different files.
    """

    def test_two_colliding_spellings_get_different_datfile_basenames(self) -> None:
        first = _datfile_name(7, "XMP-dc:Description")
        second = _datfile_name(7, "XMP-dc/Description")
        self.assertNotEqual(first, second)

    def test_an_apply_changeset_run_writes_each_tags_own_value_to_its_own_file(self) -> None:
        """Drive a real batch and inspect what each DATFILE held at the moment
        ExifTool was invoked -- matching this file's own mocked-``subprocess.run``
        convention -- rather than trusting that routing alone is proof of
        correctness.
        """
        value_for_colon_form = "A" * 5_000
        value_for_slash_form = "B" * 5_000
        changeset = self._write_changeset(
            [
                {
                    "path": "/photos/a.jpg",
                    "proposed_changes": {
                        "set": {
                            "XMP-dc:Description": value_for_colon_form,
                            "XMP-dc/Description": value_for_slash_form,
                        }
                    },
                }
            ]
        )
        cfg = ExiftoolConfig(
            enabled=True, fields=("XMP-dc:Description", "XMP-dc/Description")
        )
        captured_at_invocation: dict[str, str] = {}

        def _run(cmd, **_kw):
            for tag in ("XMP-dc:Description", "XMP-dc/Description"):
                prefix = f"-{tag}<="
                arg = next((a for a in cmd if a.startswith(prefix)), None)
                if arg is not None:
                    datfile_path = arg[len(prefix):]
                    with open(datfile_path, "r", encoding="utf-8") as handle:
                        captured_at_invocation[tag] = handle.read()
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch(
            "photokin.exiftool.apply.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch("photokin.exiftool.apply.subprocess.run", side_effect=_run):
            summary = apply_changeset(changeset, cfg)

        self.assertEqual(summary["errors"], [])
        self.assertEqual(summary["files_written"], 1)
        self.assertEqual(captured_at_invocation["XMP-dc:Description"], value_for_colon_form)
        self.assertEqual(captured_at_invocation["XMP-dc/Description"], value_for_slash_form)


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
