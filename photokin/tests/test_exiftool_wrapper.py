import argparse
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from photokin import cli
from photokin.exiftool import ExiftoolConfig, apply_changeset
from photokin.exiftool.apply import _normalize_exif_datetime
from photokin.exiftool.config import parse_fields


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
            "EXIFTOOL_FIELDS": "EXIF:UserComment, XMP:dc:Description",
            "EXIFTOOL_PATH": "/opt/exiftool",
        }
        with patch.dict(os.environ, env, clear=False):
            cfg = ExiftoolConfig.from_env()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.fields, ("EXIF:UserComment", "XMP:dc:Description"))
        self.assertEqual(cfg.path, "/opt/exiftool")

    def test_from_env_defaults_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            for name in ("EXIFTOOL_WRITE_ENABLED", "EXIFTOOL_FIELDS", "EXIFTOOL_PATH"):
                os.environ.pop(name, None)
            cfg = ExiftoolConfig.from_env()
        self.assertTrue(cfg.enabled)
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


class TestApplyChangeset(unittest.TestCase):
    def _write_changeset(self, records) -> str:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".ndjson", delete=False, encoding="utf-8"
        )
        with handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        self.addCleanup(os.unlink, handle.name)
        return handle.name

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
        with patch.dict(os.environ, {}, clear=False):
            for name in ("EXIFTOOL_WRITE_ENABLED", "EXIFTOOL_FIELDS", "EXIFTOOL_PATH"):
                os.environ.pop(name, None)
            cfg = cli._resolve_exiftool_config(self._args(), dry_run=True)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.fields, ("EXIF:UserComment",))
        self.assertTrue(cfg.dry_run)
        self.assertTrue(cfg.overwrite_original)


if __name__ == "__main__":
    unittest.main()
