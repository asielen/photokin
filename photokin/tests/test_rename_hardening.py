"""Rename-mode edge cases from issues #12, #13, #14 and #18."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from photokin import rename_apply
from photokin.rename_apply import (
    RenameOp,
    RenamePreflightError,
    _is_bare_filename,
    _without_blocked,
    apply_plan,
    finish_plan,
    preflight,
    undo_run,
)
from photokin.tests.test_rename_apply import (
    _case_insensitive,
    _cycle_folder,
    _entry,
    _finish_folder,
    _names,
    _plan,
    _records,
    _simple_folder,
    _write,
)


def test_a_nul_byte_is_never_a_bare_filename() -> None:
    # Act / Assert
    assert not _is_bare_filename("bad\x00.tif")


def test_a_nul_target_is_refused_before_anything_moves(tmp_path: Path) -> None:
    # Arrange
    folder, plan = _simple_folder(tmp_path)
    plan["entries"][0]["target"] = "bad\x00.tif"
    before = _names(folder)

    # Act / Assert
    with pytest.raises(RenamePreflightError):
        apply_plan(plan)
    assert _names(folder) == before
    assert not [n for n in os.listdir(folder) if n.endswith(".ndjson")]


def test_a_catalog_undo_that_left_nothing_to_move_closes_the_journal(tmp_path: Path) -> None:
    # Arrange: the catalog puts the image back, and its companion too.
    folder, plan = _finish_folder(tmp_path)
    report = finish_plan(plan)
    assert report.journal_path is not None
    (folder / "newname-001.tif").rename(folder / "file102.tif")
    (folder / "newname-001.md").rename(folder / "file102.md")

    # Act
    undone = undo_run(report.journal_path)

    # Assert
    assert undone.status == rename_apply.STATUS_UNDONE
    assert _records(report.journal_path)[-1]["status"] == rename_apply.STATUS_UNDONE
    with pytest.raises(RenamePreflightError, match="already undone"):
        undo_run(report.journal_path)


def test_finish_refuses_a_swap_the_folder_cannot_vouch_for(tmp_path: Path) -> None:
    # Arrange
    folder, plan = _cycle_folder(tmp_path)

    # Act / Assert
    with pytest.raises(RenamePreflightError, match="cycle"):
        finish_plan(plan)
    assert not [n for n in os.listdir(folder) if n.endswith(".ndjson")]


def test_finish_skips_a_companion_changed_since_the_plan(tmp_path: Path) -> None:
    # Arrange
    folder, plan = _finish_folder(tmp_path)
    companion = plan["entries"][0]["companions"][0]
    info = Path(companion["path"]).stat()
    companion.update(size=info.st_size, mtime=info.st_mtime)
    _write(Path(companion["path"]), "rewritten after the plan was made, and longer")

    # Act
    report = finish_plan(plan)

    # Assert
    assert "companion changed since the plan was made: file102.md" in report.skipped
    assert (folder / "file102.md").exists()


def test_a_blocked_renumber_is_dropped_whole_in_one_pass() -> None:
    # Arrange: f1 <- f2 <- ... <- f5000, and a stranger holds f0.
    count = 5000
    ops = [RenameOp(f"f{n + 1}", f"f{n}", f"t{n}", "image", f"p{n}") for n in range(count)]
    names = {f"f{n}" for n in range(count + 1)}
    skipped: list[str] = []

    # Act
    kept = _without_blocked(ops, names, skipped)

    # Assert
    assert kept == []
    assert len(skipped) == count


def test_an_unblocked_renumber_keeps_every_op() -> None:
    # Arrange
    ops = [RenameOp(f"f{n + 1}", f"f{n}", f"t{n}", "image", f"p{n}") for n in range(3)]

    # Act / Assert
    assert _without_blocked(ops, {"f1", "f2", "f3"}, []) == ops


def test_a_differently_cased_folder_is_the_plan_folder(tmp_path: Path) -> None:
    # Arrange
    folder, plan = _simple_folder(tmp_path)
    if not _case_insensitive(folder):
        pytest.skip("needs a case-insensitive filesystem")
    entry = plan["entries"][0]
    entry["path"] = os.path.join(str(folder).upper(), os.path.basename(entry["path"]))

    # Act
    problems = preflight(plan)

    # Assert
    assert not [p for p in problems if p.startswith("outside the plan's folder")]


def test_finish_still_carries_a_plain_renumber(tmp_path: Path) -> None:
    # Arrange: a chain is not a cycle.
    folder = tmp_path / "scans"
    folder.mkdir()
    first = _write(folder / "file004.tif", "a")
    second = _write(folder / "file005.tif", "b")
    plan = _plan(folder, [_entry(first, "file003.tif"), _entry(second, "file004.tif")])
    first.rename(folder / "file003.tif")
    second.rename(folder / "file004.tif")

    # Act
    report = finish_plan(plan)

    # Assert
    assert report.status == rename_apply.STATUS_NOTHING_TO_DO
