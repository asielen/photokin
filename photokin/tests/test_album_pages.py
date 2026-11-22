"""Album pages are separate objects; a document's pages are one (issue #26)."""

from __future__ import annotations

import os
from dataclasses import replace

from photokin import utils
from photokin.cli_messages import RunPlan
from photokin.core import album_plan_lines, build_manifest_buckets

_ALBUM = [
    "church-edward-photo-album-page-001.tif",
    "church-edward-photo-album-page-001-back.tif",
    "church-edward-photo-album-page-002.tif",
    "church-edward-photo-album-page-002-crop.tif",
]


def _groups(items: list[dict], page_grouping: str = utils.PAGE_GROUPING_AUTO) -> dict[str, list[str]]:
    """Bucket *items* and return ``{group key: [basename, ...]}``."""
    buckets = build_manifest_buckets(items, page_grouping=page_grouping, log_overrides=False)
    return {
        key: [os.path.basename(entry["path"]) for entry in entries]
        for key, entries in buckets.items()
    }


def _items(*names: str) -> list[dict]:
    return [{"path": name} for name in names]


def test_album_in_the_name_makes_each_page_an_object_with_its_back_and_crop() -> None:
    # Act
    groups = _groups(_items(*_ALBUM))

    # Assert
    assert groups == {
        "church-edward-photo-album-page-001": [_ALBUM[0], _ALBUM[1]],
        "church-edward-photo-album-page-002": [_ALBUM[2], _ALBUM[3]],
    }


def test_album_page_is_a_front_not_a_page() -> None:
    # Act
    buckets = build_manifest_buckets(_items(_ALBUM[0]), log_overrides=False)

    # Assert
    (entry,) = buckets["church-edward-photo-album-page-001"]
    assert (entry["part_kind"], entry["page_num"]) == ("none", None)


def test_underscore_album_pages_keep_their_backs() -> None:
    # Act
    groups = _groups(_items("box_album_page1.tif", "box_album_page1_back.tif"))

    # Assert
    assert groups == {"box_album_page1": ["box_album_page1.tif", "box_album_page1_back.tif"]}


def test_a_hierarchy_leaf_keyword_marks_an_album() -> None:
    # Arrange
    items = _items("box3-page1.tif", "box3-page2.tif")
    items[1]["metadata"] = {"keywords": ["Source|Album"]}

    # Act
    groups = _groups(items)

    # Assert
    assert list(groups) == ["box3-page1", "box3-page2"]


def test_letters_and_albumen_prints_stay_documents() -> None:
    # Act
    groups = _groups(_items("letter-page1.jpg", "letter-page2.jpg", "albumen-page1.jpg"))

    # Assert
    assert groups == {
        "letter": ["letter-page1.jpg", "letter-page2.jpg"],
        "albumen": ["albumen-page1.jpg"],
    }


def test_page_grouping_document_keeps_an_album_whole() -> None:
    # Act
    groups = _groups(_items(*_ALBUM[::2]), utils.PAGE_GROUPING_DOCUMENT)

    # Assert
    assert list(groups) == ["church-edward-photo-album"]


def test_page_grouping_album_splits_any_page_set() -> None:
    # Act
    groups = _groups(_items("letter-page1.jpg", "letter-page2.jpg"), utils.PAGE_GROUPING_ALBUM)

    # Assert
    assert list(groups) == ["letter-page1", "letter-page2"]


def test_an_explicit_manifest_group_is_never_split() -> None:
    # Arrange
    items = [{"path": name, "group": "album-one"} for name in _ALBUM[::2]]

    # Act
    groups = _groups(items)

    # Assert
    assert groups == {"album-one": [_ALBUM[0], _ALBUM[2]]}


def test_plan_lines_count_pages_not_files() -> None:
    # Arrange
    buckets = build_manifest_buckets(_items(*_ALBUM), log_overrides=False)

    # Act
    lines = album_plan_lines(buckets)

    # Assert
    assert lines == [
        "church-edward-photo-album: 2 page(s), one object each (the name says album)"
    ]


def test_plan_renders_an_albums_row_only_when_there_is_one() -> None:
    # Arrange
    without = RunPlan(
        input_location="C:/Scans",
        input_kind="folder",
        file_count=4,
        group_count=2,
        group_by="object",
        read="none",
        output="stdout",
        changeset="none",
        write="none",
        provider="ChatGPT",
        model="gpt-4o",
        dry_run=False,
    )

    # Act
    with_album = replace(without, albums=("x: 2 page(s), one object each (the name says album)",))

    # Assert
    assert "  albums    : x: 2 page(s), one object each" in with_album.render()
    assert "albums" not in without.render()
