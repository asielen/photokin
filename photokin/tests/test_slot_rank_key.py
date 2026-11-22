"""One ranking rule for analysis and the rename planner (issue #16)."""

from __future__ import annotations

from photokin import core, rename, utils


def _facts(path: str, **overrides: object) -> utils.SlotFacts:
    fields: dict = {"crop": False, "preferred": False, "part_kind": "none", "page_num": None, "version": None}
    fields.update(overrides)
    return utils.SlotFacts(path=path, **fields)


def test_a_tiff_master_outranks_its_jpeg_and_a_crop_ranks_last() -> None:
    # Arrange
    candidates = [
        _facts("box3_017-crop.tif", crop=True, preferred=True),
        _facts("box3_017.jpg"),
        _facts("box3_017.tif"),
    ]

    # Act
    ranked = sorted(candidates, key=utils.slot_rank_key)

    # Assert
    assert [f.path for f in ranked] == ["box3_017.tif", "box3_017.jpg", "box3_017-crop.tif"]


def test_analysis_and_rename_rank_the_same_file_first() -> None:
    # Arrange
    grouping_entry = {
        "is_crop": False,
        "preferred": False,
        "part_kind": "none",
        "page_num": None,
        "version": None,
        "path": "box3_017.tif",
    }
    plan_entry = {"crop": False, "part": "none", "page": None, "variant": None, "path": "box3_017.tif"}

    # Act / Assert
    assert core._slot_rank_key(grouping_entry) == rename._companion_owner_key(
        plan_entry, frozenset()
    )
