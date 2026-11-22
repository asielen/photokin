"""Path spelling and identity edge cases in rename mode (issues #14 and #18)."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from photokin import cli, utils
from photokin.changeset import make_photo_id
from photokin.rename import RenameItem, _shares_stem
from photokin.tests.test_rename_apply import _case_insensitive


def test_an_ordinary_photo_id_is_unchanged() -> None:
    # Arrange
    path = os.path.abspath("scans/box3_017.tif")

    # Act / Assert
    expected = hashlib.sha1(path.encode("utf-8"), usedforsecurity=False).hexdigest()
    assert make_photo_id(path) == f"sha1:{expected}"


def test_a_photo_id_hashes_a_name_that_is_not_utf8() -> None:
    # Act / Assert
    assert make_photo_id(os.path.abspath("bad\udcff.tif")).startswith("sha1:")


def test_json_text_falls_back_to_escapes_only_when_it_must() -> None:
    # Act
    readable = utils.json_text({"name": "Grand-mère.tif"})
    escaped = utils.json_text({"name": "bad\udcff.tif"})

    # Assert
    assert "Grand-mère.tif" in readable
    escaped.encode("utf-8")
    assert json.loads(escaped) == {"name": "bad\udcff.tif"}


def test_disk_spelling_returns_the_stored_case(tmp_path: Path) -> None:
    # Arrange
    folder = tmp_path / "Photos"
    folder.mkdir()
    (folder / "Box3.tif").write_bytes(b"x")
    if not _case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive filesystem")

    # Act
    spelled = utils.disk_spelling(str(tmp_path / "PHOTOS" / "BOX3.TIF"))

    # Assert
    assert spelled.endswith(os.path.join("Photos", "Box3.tif"))


def test_the_rename_folder_takes_the_disks_spelling(tmp_path: Path) -> None:
    # Arrange
    folder = tmp_path / "Photos"
    folder.mkdir()
    if not _case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive filesystem")
    item = RenameItem(path=str(tmp_path / "PHOTOS" / "a.tif"))

    # Act
    chosen = cli._rename_folder_from_items([item], MagicMock())

    # Assert
    assert os.path.basename(chosen) == "Photos"


def test_two_new_destinations_in_one_folder_collide_by_name(tmp_path: Path) -> None:
    # Act / Assert
    assert utils.destinations_may_collide(str(tmp_path / "plan.json"), str(tmp_path / "plan.json"))
    assert not utils.destinations_may_collide(
        str(tmp_path / "plan.json"), str(tmp_path / "other.json")
    )


def test_new_destinations_under_a_folder_alias_collide(tmp_path: Path) -> None:
    # Arrange
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create a symlink here")

    # Act / Assert
    assert utils.destinations_may_collide(str(alias / "c.ndjson"), str(real / "c.ndjson"))


def test_a_non_image_manifest_entry_is_left_out(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    pdf = tmp_path / "document.pdf"
    tif = tmp_path / "scan.tif"
    for path in (pdf, tif):
        path.write_bytes(b"x")
    document = {"items": [{"path": str(pdf)}, {"path": str(tif)}]}

    # Act
    with caplog.at_level(logging.WARNING):
        items = cli._build_rename_items_from_manifest(document)

    # Assert
    assert [os.path.basename(item.path) for item in items] == ["scan.tif"]
    assert "Not an image, so not renamed" in caplog.text


def test_a_case_twin_is_a_companion_only_where_case_folds(tmp_path: Path) -> None:
    # Arrange
    image = tmp_path / "photo.tif"
    image.write_bytes(b"x")
    twin = tmp_path / "PHOTO.md"
    twin.write_bytes(b"x")

    # Act
    shares = _shares_stem(str(image), str(twin))

    # Assert
    assert shares is _case_insensitive(tmp_path)
    assert _shares_stem(str(image), str(tmp_path / "photo.md"))
