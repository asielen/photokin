"""--revert-changeset puts a run's files back (issue #25)."""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from photokin.exiftool import ExiftoolConfig, apply_changeset, resolve_exiftool_path
from photokin.exiftool.revert import RevertOptions, _same, revert_changeset
from photokin.tests.test_cli_input_surface import _CliTestCase
from photokin.tests.test_exiftool_wrapper import _MINIMAL_JPEG_B64

_PROPOSED = {
    "set": {"XMP-dc:Description": "New caption", "XMP-dc:Title": "Fire Chief"},
    "keywords_add": ["Page 1"],
    "keywords_remove": ["Church"],
}
_SNAPSHOT = {
    "XMP-dc:Description": "Old caption",
    "XMP-dc:Title": None,
    "XMP-dc:Subject": ["Church"],
    "XMP-lr:HierarchicalSubject": None,
    "IPTC:Keywords": None,
}
#: What the file holds right after the run wrote it.
_AFTER_RUN = {
    "XMP-dc:Description": "New caption",
    "XMP-dc:Title": "Fire Chief",
    "XMP-dc:Subject": "Page 1",
    "XMP-lr:HierarchicalSubject": "Page 1",
    "IPTC:Keywords": "Page 1",
}


def _photo(tmp_path: Path, name: str = "a.jpg") -> str:
    path = tmp_path / name
    path.write_bytes(b"jpeg")
    return str(path)


def _changeset(tmp_path: Path, *records: dict) -> str:
    path = tmp_path / "run_changeset.ndjson"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return str(path)


def _record(path: str, **extra: object) -> dict:
    return {
        "path": path,
        "original_data": {"file_metadata": {}, "written_tags": _SNAPSHOT},
        "proposed_changes": _PROPOSED,
        **extra,
    }


def _revert(
    changeset: str, current: list[dict], options: RevertOptions | None = None, dry_run: bool = False
) -> tuple[list[dict], MagicMock]:
    """Revert with ExifTool's binary, read and write faked; return records and the write mock."""
    write = MagicMock(return_value=None)
    with (
        patch("photokin.exiftool.revert.resolve_exiftool_path", return_value="/fake/exiftool"),
        patch("photokin.exiftool.revert.run_exiftool_json", return_value=current),
        patch("photokin.exiftool.revert._run_file_write", write),
    ):
        records = revert_changeset(
            changeset, ExiftoolConfig(dry_run=dry_run), options or RevertOptions()
        )
    return records, write


@pytest.mark.parametrize(
    ("tag", "current", "wanted", "same"),
    [
        ("IPTC:TimeCreated", "00:00:00-08:00", "00:00:00", True),
        ("XMP-photoshop:DateCreated", "1948:06:01", "1948-06-01", True),
        ("XMP-photoshop:DateCreated", "1948:06:02", "1948-06-01", False),
        ("XMP-dc:Subject", "page 1", ["Page 1"], True),
        ("XMP-dc:Description", "a\n  b", "a b", True),
        ("XMP-dc:Title", None, "", True),
        ("XMP-dc:Title", None, "T", False),
    ],
)
def test_values_compare_as_exiftool_prints_them(
    tag: str, current: object, wanted: object, same: bool
) -> None:
    # Act / Assert
    assert _same(tag, current, wanted) is same


def test_a_written_file_is_put_back(tmp_path: Path) -> None:
    # Arrange
    photo = _photo(tmp_path)
    changeset = _changeset(tmp_path, _record(photo))

    # Act
    records, write = _revert(changeset, [{"SourceFile": photo, **_AFTER_RUN}])

    # Assert
    assert records[0]["status"] == "reverted"
    restore = write.call_args.args[2]
    assert restore.tags == {"XMP-dc:Description": "Old caption", "XMP-dc:Title": ""}
    assert restore.keyword_args == [
        "-XMP-dc:Subject-=Page 1",
        "-XMP-dc:Subject+=Church",
        "-XMP-lr:HierarchicalSubject=",
        "-IPTC:Keywords=",
    ]
    assert records[-1] == {
        "summary": {"reverted": 1, "unchanged": 0, "conflict": 0, "missing": 0, "error": 0},
        "dry_run": False,
    }


def test_an_edit_after_the_run_is_a_conflict_unless_forced(tmp_path: Path) -> None:
    # Arrange
    photo = _photo(tmp_path)
    changeset = _changeset(tmp_path, _record(photo))
    edited = [{"SourceFile": photo, **_AFTER_RUN, "XMP-dc:Description": "Hand edit"}]

    # Act
    refused, refused_write = _revert(changeset, edited)
    forced, forced_write = _revert(changeset, edited, RevertOptions(force=True))

    # Assert
    assert (refused[0]["status"], refused[0]["conflicts"]) == ("conflict", ["XMP-dc:Description"])
    refused_write.assert_not_called()
    assert forced[0]["status"] == "reverted"
    forced_write.assert_called_once()


def test_a_file_already_back_is_unchanged(tmp_path: Path) -> None:
    # Arrange
    photo = _photo(tmp_path)
    changeset = _changeset(tmp_path, _record(photo))
    restored = {"SourceFile": photo, "XMP-dc:Description": "Old caption", "XMP-dc:Subject": "Church"}

    # Act
    records, write = _revert(changeset, [restored])

    # Assert
    assert records[0]["status"] == "unchanged"
    write.assert_not_called()


def test_a_missing_file_is_reported(tmp_path: Path) -> None:
    # Arrange
    changeset = _changeset(tmp_path, _record(str(tmp_path / "gone.jpg")))

    # Act
    records, _write = _revert(changeset, [])

    # Assert
    assert records[0]["status"] == "missing"


def test_first_record_per_path_and_write_false_skipped(tmp_path: Path) -> None:
    # Arrange
    photo = _photo(tmp_path)
    crop = _photo(tmp_path, "crop.jpg")
    changeset = _changeset(
        tmp_path, _record(photo), _record(photo), _record(crop, write=False)
    )

    # Act
    records, _write = _revert(changeset, [{"SourceFile": photo, **_AFTER_RUN}])

    # Assert
    assert [(os.path.basename(r["path"]), r["entries"]) for r in records[:-1]] == [("a.jpg", 2)]


def test_filters_choose_the_files(tmp_path: Path) -> None:
    # Arrange
    first, second = _photo(tmp_path, "a.jpg"), _photo(tmp_path, "b.jpg")
    changeset = _changeset(tmp_path, _record(first), _record(second))
    current = [{"SourceFile": p, **_AFTER_RUN} for p in (first, second)]
    by_path = RevertOptions(only_paths=frozenset({os.path.normcase(os.path.abspath(second))}))

    # Act
    picked_by_path, _ = _revert(changeset, current, by_path)
    picked_by_glob, _ = _revert(changeset, current, RevertOptions(match="a.*"))

    # Assert
    assert [os.path.basename(r["path"]) for r in picked_by_path[:-1]] == ["b.jpg"]
    assert [os.path.basename(r["path"]) for r in picked_by_glob[:-1]] == ["a.jpg"]


def test_an_old_changeset_falls_back_and_names_what_it_inferred(tmp_path: Path) -> None:
    # Arrange
    photo = _photo(tmp_path)
    old = {
        "path": photo,
        "original_data": {"file_metadata": {"XMP-dc:Description": "Old caption"}},
        "proposed_changes": _PROPOSED,
    }
    changeset = _changeset(tmp_path, old)

    # Act
    records, write = _revert(changeset, [{"SourceFile": photo, **_AFTER_RUN}])

    # Assert
    assert records[0]["inferred"] == ["XMP-dc:Title"]
    restore = write.call_args.args[2]
    assert restore.tags == {"XMP-dc:Description": "Old caption", "XMP-dc:Title": ""}
    assert "-XMP-dc:Subject-=Page 1" in restore.keyword_args
    assert "-XMP-dc:Subject+=Church" in restore.keyword_args


class TestTheRevertCommand(_CliTestCase):
    """The CLI refuses what it cannot do, and exits 1 when a file conflicts."""

    def test_both_filters_are_refused(self) -> None:
        # Arrange
        changeset = os.path.join(self.scratch(), "c.ndjson")
        Path(changeset).write_text("", encoding="utf-8")

        # Act
        code, _stdout, stderr = self.run_cli(
            ["--revert-changeset", changeset, "--revert-paths", "a.jpg", "--revert-match", "*"]
        )

        # Assert
        self.assertEqual(code, 2)
        self.assertIn("--revert-paths and --revert-match", stderr)

    def test_a_conflict_exits_1_and_writes_the_records(self) -> None:
        # Arrange
        folder = self.scratch()
        out = os.path.join(folder, "revert.ndjson")
        records = [
            {"path": "a.jpg", "status": "conflict", "tags": []},
            {"summary": {"reverted": 0, "unchanged": 0, "conflict": 1, "missing": 0, "error": 0}},
        ]
        changeset = os.path.join(folder, "c.ndjson")
        Path(changeset).write_text("", encoding="utf-8")

        # Act
        with patch("photokin.cli.revert_changeset", return_value=records):
            code, _stdout, _stderr = self.run_cli(
                ["--revert-changeset", changeset, "--output-file", out]
            )

        # Assert
        self.assertEqual(code, 1)
        self.assertEqual(
            [json.loads(line) for line in Path(out).read_text(encoding="utf-8").splitlines()],
            records,
        )


def test_round_trip_with_the_real_exiftool(tmp_path: Path) -> None:
    # Arrange
    try:
        exiftool = resolve_exiftool_path(ExiftoolConfig())
    except FileNotFoundError:
        pytest.skip("no ExifTool binary")
    photo = tmp_path / "real.jpg"
    photo.write_bytes(base64.b64decode(_MINIMAL_JPEG_B64))
    subprocess.run(
        [exiftool, "-q", "-overwrite_original", "-XMP-dc:Description=Old caption",
         "-XMP-dc:Subject=Church", "-EXIF:DateTimeOriginal=1950:01:01 00:00:00", str(photo)],
        check=True,
    )

    def read() -> dict:
        out = subprocess.run(
            [exiftool, "-json", "-G1", "-XMP:all", "-EXIF:DateTimeOriginal", "-IPTC:all",
             "-Photoshop:all", str(photo)],
            capture_output=True, text=True, check=True,
        ).stdout
        return {k: v for k, v in json.loads(out)[0].items() if k != "SourceFile"}

    before = read()
    record = {
        "path": str(photo),
        "original_data": {"file_metadata": {}},
        "proposed_changes": {
            "set": {"XMP-dc:Description": "New\ncaption", "EXIF:DateTimeOriginal": "1916-08-17"},
            "keywords_add": ["Page 1"],
            "keywords_remove": ["Church"],
        },
    }
    changeset = _changeset(tmp_path, record)
    cfg = ExiftoolConfig(enabled=True)
    apply_changeset(changeset, cfg, record_before=True)

    # Act
    records = revert_changeset(changeset, cfg, RevertOptions())
    again = revert_changeset(changeset, cfg, RevertOptions())

    # Assert
    assert records[0]["status"] == "reverted"
    after = read()
    assert {k: v for k, v in after.items() if not k.startswith(("Photoshop:", "IPTC:"))} == {
        k: v for k, v in before.items() if not k.startswith(("Photoshop:", "IPTC:"))
    }
    assert "IPTC:Keywords" not in after
    assert again[0]["status"] == "unchanged"
