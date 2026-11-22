"""The applier records each file's pre-write values in the changeset (issue #25)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from photokin.exiftool import ExiftoolConfig, apply_changeset

_PHOTO = os.path.abspath("/photos/a.jpg")

_RECORD = {
    "schema_version": 5,
    "path": _PHOTO,
    "original_data": {"file_metadata": {}},
    "proposed_changes": {
        "set": {"XMP-dc:Description": "New caption", "EXIF:DateTimeOriginal": "1948-06-01"},
        "keywords_add": ["Ruth"],
        "keywords_remove": [],
    },
}

#: What ``exiftool -json -G1`` prints for the file before the write.
#: ``XMP-exif:DateTimeOriginal`` and ``IPTC:DateCreated`` share a bare name with
#: another written tag and must be read only as themselves.
_BEFORE = {
    "SourceFile": _PHOTO,
    "XMP-dc:Description": "Old caption",
    "XMP-dc:Subject": "Church",
    "ExifIFD:DateTimeOriginal": "1950:01:01 00:00:00",
    "XMP-exif:DateTimeOriginal": "decoy",
    "IPTC:DateCreated": "1950:01:01",
}


def _changeset(tmp_path: Path, *records: dict) -> Path:
    path = tmp_path / "run_changeset.ndjson"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _apply(
    changeset: Path, cfg: ExiftoolConfig, read: MagicMock, *, record_before: bool = False
) -> dict:
    """Apply with ExifTool's binary, its reads and its writes all faked."""
    written = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with (
        patch("photokin.exiftool.apply.resolve_exiftool_path", return_value="/fake/exiftool"),
        patch("photokin.exiftool.apply.run_exiftool_json", read),
        patch("photokin.exiftool.apply.subprocess.run", return_value=written),
    ):
        return apply_changeset(str(changeset), cfg, record_before=record_before)


def _lines(changeset: Path) -> list[dict]:
    return [json.loads(line) for line in changeset.read_text(encoding="utf-8").splitlines()]


def test_snapshot_holds_every_touched_tag_with_null_for_absent(tmp_path: Path) -> None:
    # Arrange
    changeset = _changeset(tmp_path, _RECORD)
    read = MagicMock(return_value=[_BEFORE])

    # Act
    summary = _apply(changeset, ExiftoolConfig(enabled=True), read, record_before=True)

    # Assert
    assert summary["files_written"] == 1
    assert _lines(changeset)[0]["original_data"]["written_tags"] == {
        "XMP-dc:Description": "Old caption",
        "EXIF:DateTimeOriginal": "1950:01:01 00:00:00",
        "XMP-photoshop:DateCreated": None,
        "IPTC:DateCreated": "1950:01:01",
        "IPTC:TimeCreated": None,
        "XMP-dc:Subject": ["Church"],
        "XMP-lr:HierarchicalSubject": None,
        "IPTC:Keywords": None,
    }


def test_snapshot_reads_every_file_in_one_call(tmp_path: Path) -> None:
    # Arrange
    other = {**_RECORD, "path": os.path.abspath("/photos/b.jpg")}
    changeset = _changeset(tmp_path, _RECORD, other, _RECORD)
    read = MagicMock(return_value=[_BEFORE])

    # Act
    _apply(changeset, ExiftoolConfig(enabled=True), read, record_before=True)

    # Assert
    read.assert_called_once()
    assert read.call_args.args[1] == [_PHOTO, os.path.abspath("/photos/b.jpg")]
    assert [("written_tags" in r["original_data"]) for r in _lines(changeset)] == [
        True,
        False,
        True,
    ]


@pytest.mark.parametrize(
    "cfg",
    [ExiftoolConfig(enabled=True, dry_run=True), ExiftoolConfig(enabled=True, write_sidecar_only=True)],
)
def test_no_snapshot_when_the_originals_are_not_written(tmp_path: Path, cfg: ExiftoolConfig) -> None:
    # Arrange
    changeset = _changeset(tmp_path, _RECORD)
    before = changeset.read_bytes()
    read = MagicMock(return_value=[_BEFORE])

    # Act
    _apply(changeset, cfg, read, record_before=True)

    # Assert
    read.assert_not_called()
    assert changeset.read_bytes() == before


def test_off_by_default_for_library_callers(tmp_path: Path) -> None:
    # Arrange
    changeset = _changeset(tmp_path, _RECORD)
    read = MagicMock(return_value=[_BEFORE])

    # Act
    _apply(changeset, ExiftoolConfig(enabled=True), read)

    # Assert
    read.assert_not_called()


def test_a_failed_read_still_writes_and_says_the_run_cannot_be_reverted(tmp_path: Path) -> None:
    # Arrange
    changeset = _changeset(tmp_path, _RECORD)
    before = changeset.read_bytes()
    read = MagicMock(side_effect=RuntimeError("ExifTool failed."))

    # Act
    summary = _apply(changeset, ExiftoolConfig(enabled=True), read, record_before=True)

    # Assert
    assert summary["files_written"] == 1
    assert any("cannot be reverted exactly" in w["warning"] for w in summary["warnings"])
    assert changeset.read_bytes() == before
