"""Underscore part separators and the in-order rule for variant letters (issue #26)."""

from __future__ import annotations

import logging
import os

import pytest

from photokin import utils
from photokin.core import build_manifest_buckets


def _buckets(*names: str, **extra: dict) -> dict[str, list[str]]:
    """Bucket bare filenames and return ``{group key: [(basename, version)]}``."""
    items = [{"path": name, **extra.get(name, {})} for name in names]
    return {
        key: [(os.path.basename(entry["path"]), entry["version"]) for entry in entries]
        for key, entries in build_manifest_buckets(items, log_overrides=False).items()
    }


@pytest.mark.parametrize(
    ("name", "base_id", "part_kind", "is_crop"),
    [
        ("church_edward_002_back.tif", "church_edward_002", "back", False),
        ("church_edward_002_front.tif", "church_edward_002", "front", False),
        ("church_edward_002_negative.tif", "church_edward_002", "negative", False),
        ("church_edward_002_back_crop.tif", "church_edward_002", "back", True),
        ("church_edward_002_page3.tif", "church_edward_002", "page", False),
        ("feedback.jpg", "feedback", "none", False),
    ],
)
def test_underscore_part_words_parse_like_hyphens(
    name: str, base_id: str, part_kind: str, is_crop: bool
) -> None:
    # Act
    parsed = utils.parse_media_filename(name)

    # Assert
    assert (parsed.base_id, parsed.part_kind, parsed.is_crop) == (base_id, part_kind, is_crop)


@pytest.mark.parametrize(
    ("name", "variant_id", "suffix"),
    [
        ("box3_017-b.tif", "b", "-b"),
        ("box3_017b.tif", "b", "b"),
        ("box3_017_b.tif", "b", "_b"),
        ("box3_017_B-back.tif", "b", "_B"),
        ("photo_b.tif", None, ""),
    ],
)
def test_variant_suffix_keeps_the_names_spelling(
    name: str, variant_id: str | None, suffix: str
) -> None:
    # Act
    parsed = utils.parse_media_filename(name)

    # Assert
    assert (parsed.variant_id, parsed.variant_suffix) == (variant_id, suffix)


def test_front_and_underscore_back_form_one_group() -> None:
    # Act
    buckets = _buckets("church_edward_002.tif", "church_edward_002_back.tif")

    # Assert
    assert list(buckets) == ["church_edward_002"]


def test_letters_in_order_are_variants_and_a_is_optional() -> None:
    # Act
    buckets = _buckets("013.tif", "013_b.tif", "013c.tif", "013-d.tif")

    # Assert
    assert buckets == {
        "013": [("013.tif", None), ("013_b.tif", "b"), ("013c.tif", "c"), ("013-d.tif", "d")]
    }


def test_lone_z_is_its_own_object() -> None:
    # Act
    buckets = _buckets("church_edward_013.tif", "church_edward_013_z.tif")

    # Assert
    assert buckets == {
        "church_edward_013": [("church_edward_013.tif", None)],
        "church_edward_013_z": [("church_edward_013_z.tif", None)],
    }


def test_gap_folds_every_letter_after_it_and_keeps_its_back() -> None:
    # Act
    buckets = _buckets("025.tif", "025b.tif", "025d.tif", "025d-back.tif", "025e.tif")

    # Assert
    assert buckets == {
        "025": [("025.tif", None), ("025b.tif", "b")],
        "025d": [("025d.tif", None), ("025d-back.tif", None)],
        "025e": [("025e.tif", None)],
    }


def test_explicit_version_is_kept_and_counts_as_present() -> None:
    # Act
    buckets = _buckets("025.tif", "025x.tif", "025c.tif", **{"025x.tif": {"version": "b"}})

    # Assert
    assert buckets == {"025": [("025.tif", None), ("025x.tif", "b"), ("025c.tif", "c")]}


def test_explicit_group_keeps_an_out_of_order_letter() -> None:
    # Act
    buckets = _buckets("025.tif", "025c.tif", **{"025c.tif": {"group": "025"}})

    # Assert
    assert buckets == {"025": [("025.tif", None), ("025c.tif", "c")]}


def test_fold_is_logged_with_the_missing_letter(caplog: pytest.LogCaptureFixture) -> None:
    # Arrange
    items = [{"path": "013.tif"}, {"path": "013_z.tif"}]

    # Act
    with caplog.at_level(logging.WARNING):
        build_manifest_buckets(items)

    # Assert
    assert "013_z.tif: no 'b' scan of 013, so 'z' is part of the name: object 013_z." in caplog.text


def test_entries_leave_without_the_internal_suffix_key() -> None:
    # Act
    buckets = build_manifest_buckets([{"path": "013_b.tif"}], log_overrides=False)

    # Assert
    assert all(not key.startswith("_name") for entry in buckets["013"] for key in entry)
