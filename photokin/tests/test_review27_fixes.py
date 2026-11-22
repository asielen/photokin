"""Regressions found by the stand-in review of PR #27."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from photokin import core, utils
from photokin.exiftool import ExiftoolConfig, apply_changeset
from photokin.exiftool.manifest import run_exiftool_json
from photokin.tests.test_cli_input_surface import _CliTestCase, _StreamSpy
from photokin.tests.test_manifest_grouping import _recording
from photokin.tests.test_rename_planner import _entry_for, _plan


def _changeset(tmp_path: Path, count: int) -> Path:
    path = tmp_path / "c.ndjson"
    records = [
        {"path": os.path.abspath(f"/x/p{n}.jpg"), "proposed_changes": {"set": {"XMP-dc:Title": "T"}}}
        for n in range(count)
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _before(count: int) -> MagicMock:
    return MagicMock(
        return_value=[
            {"SourceFile": os.path.abspath(f"/x/p{n}.jpg"), "XMP-dc:Title": "Old"} for n in range(count)
        ]
    )


def test_an_interrupted_apply_still_saves_what_it_wrote(tmp_path: Path) -> None:
    # Arrange
    changeset = _changeset(tmp_path, 3)
    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    # Act
    with (
        patch("photokin.exiftool.apply.resolve_exiftool_path", return_value="/fake/exiftool"),
        patch("photokin.exiftool.apply.run_exiftool_json", _before(3)),
        patch("photokin.exiftool.apply.subprocess.run", side_effect=[ok, KeyboardInterrupt]),
        pytest.raises(KeyboardInterrupt),
    ):
        apply_changeset(str(changeset), ExiftoolConfig(enabled=True), record_before=True)

    # Assert
    first = json.loads(changeset.read_text(encoding="utf-8").splitlines()[0])
    assert first["original_data"]["written_tags"] == {"XMP-dc:Title": "Old"}


def test_a_changeset_that_cannot_be_rewritten_is_a_warning_not_a_failed_apply(
    tmp_path: Path,
) -> None:
    # Arrange
    changeset = _changeset(tmp_path, 1)
    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    # Act
    with (
        patch("photokin.exiftool.apply.resolve_exiftool_path", return_value="/fake/exiftool"),
        patch("photokin.exiftool.apply.run_exiftool_json", _before(1)),
        patch("photokin.exiftool.apply.subprocess.run", return_value=ok),
        patch("photokin.exiftool.apply.os.replace", side_effect=PermissionError(13, "denied")),
    ):
        summary = apply_changeset(str(changeset), ExiftoolConfig(enabled=True), record_before=True)

    # Assert
    assert summary["files_written"] == 1
    assert any("cannot be reverted exactly" in w["warning"] for w in summary["warnings"])


def test_numbers_can_be_read_as_exiftool_printed_them() -> None:
    # Arrange
    printed = b'[{"SourceFile": "a.jpg", "XMP-dc:Title": 1950.10, "XMP-dc:Rights": 1943}]'
    proc = subprocess.CompletedProcess(args=[], returncode=0, stdout=printed, stderr=b"")

    # Act
    with patch("photokin.exiftool.manifest.subprocess.run", return_value=proc):
        record = run_exiftool_json("exiftool", ["a.jpg"], ["XMP-dc:Title"], numbers_as_text=True)[0]

    # Assert
    assert (record["XMP-dc:Title"], record["XMP-dc:Rights"]) == ("1950.10", "1943")


def test_an_album_named_for_a_decade_keeps_each_page_with_its_back() -> None:
    # Arrange
    names = ["smith-album-1940s-page1.tif", "smith-album-1940s-page1-back.tif"]

    # Act
    buckets = core.build_manifest_buckets([{"path": n} for n in names], log_overrides=False)

    # Assert
    assert list(buckets) == ["smith-album-1940s-page1"]


def test_plural_albums_is_an_album() -> None:
    # Act
    buckets = core.build_manifest_buckets(
        [{"path": "smith-albums-page1.tif"}, {"path": "smith-albums-page2.tif"}], log_overrides=False
    )

    # Assert
    assert list(buckets) == ["smith-albums-page1", "smith-albums-page2"]


def test_a_rescan_of_a_page_shares_that_pages_fields_only() -> None:
    # Arrange
    items = [
        {"path": "letter-page1.tif", "metadata": {"title": "Dear Mother", "keywords": ["Ruth"]}},
        {"path": "letter-b-page1.tif", "metadata": {}},
        {"path": "letter-page2.tif", "metadata": {}},
    ]
    lines: list[str] = []

    # Act
    with _recording(["Letter"]):
        core.process_manifest_stream(
            manifest={"items": items}, cfg=utils.Config(dry_run=True), ndjson_writer=lines.append
        )

    # Assert
    results = {os.path.basename(r["path"]): r["result"] for r in map(json.loads, lines)}
    assert results["letter-b-page1.tif"].get("title") == "Dear Mother"
    assert "Ruth" in results["letter-b-page1.tif"]["keywords"]
    assert "Ruth" not in results["letter-page2.tif"]["keywords"]
    assert results["letter-page2.tif"].get("title") != "Dear Mother"


def test_rename_numbers_a_lone_z_as_its_own_photo() -> None:
    # Act
    plan = _plan(["box3_013.jpg", "box3_013_z.jpg"], "smith-")

    # Assert
    assert _entry_for(plan, "box3_013.jpg")["target"] == "smith-001.jpg"
    assert _entry_for(plan, "box3_013_z.jpg")["target"] == "smith-002.jpg"


def test_rename_keeps_letters_that_are_in_order() -> None:
    # Act
    plan = _plan(["box3_013.jpg", "box3_013_b.jpg"], "smith-")

    # Assert
    assert _entry_for(plan, "box3_013_b.jpg")["target"] == "smith-001b.jpg"


class TestTheCapWaitsForMinusR(_CliTestCase):
    """With -r a page keyword can still make a set an album, so preflight does not refuse it."""

    def test_minus_r_leaves_the_cap_to_the_stream(self) -> None:
        # Arrange
        folder = self.make_folder("smith-page1.jpg", "smith-page2.jpg", "smith-page3.jpg")
        stream = _StreamSpy()

        # Act
        with (
            patch("photokin.cli.process_manifest_stream", stream),
            patch("photokin.cli._preflight_exiftool"),
        ):
            self.run_cli([folder, "-r", "--max-group-pages", "2"])

        # Assert
        self.assertTrue(stream.called)
