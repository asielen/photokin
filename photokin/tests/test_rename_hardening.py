"""Rename-mode edge cases from issues #12, #13, #14 and #18."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from photokin import rename_apply
from photokin.changeset import make_run_id
from photokin.rename_apply import (
    RenameOp,
    RenamePreflightError,
    _is_bare_filename,
    _rewrite_sidecar,
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


def _md(path: Path, line: str) -> Path:
    return _write(path, f"---\n{line}\ngroup: \"g1\"\n---\n\nDear Mother,\n")


def test_an_escaped_link_is_decoded_before_it_is_matched(tmp_path: Path) -> None:
    # Arrange
    md = _md(tmp_path / "new.md", r'source_file: "a\tb\\c\u0007.tif"')

    # Act
    warning = _rewrite_sidecar(str(md), "a\tb\\c\x07.tif", "new.tif", str(tmp_path / "t.md"))

    # Assert
    assert warning is None
    assert 'source_file: "new.tif"\n' in md.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "line",
    [
        "  source_file:   old.tif",
        "source_file: 'old.tif'",
        'source_file: "old.tif"',
        "source_file: Grandma's 1950 scan.tif",
        "source_file: photo, 1950.tif",
    ],
)
def test_apply_then_undo_gives_back_the_same_bytes(tmp_path: Path, line: str) -> None:
    # Arrange
    md = _md(tmp_path / "x.md", line)
    original = md.read_bytes()

    # Act
    old = rename_apply._sidecar_link(line.strip())
    assert _rewrite_sidecar(str(md), old, "M\u00fcller-001.tif", str(tmp_path / "t.md")) is None
    forward = md.read_text(encoding="utf-8")
    assert _rewrite_sidecar(str(md), "M\u00fcller-001.tif", old, str(tmp_path / "t.md")) is None

    # Assert
    assert "M\u00fcller-001.tif" in forward
    assert md.read_bytes() == original


def test_a_case_twin_link_is_left_alone_only_when_it_is_a_different_file(tmp_path: Path) -> None:
    # Arrange: photo.tif was renamed away; PHOTO.TIF is whatever the folder holds.
    md = _md(tmp_path / "new.md", 'source_file: "PHOTO.TIF"')
    folder_folds_case = _case_insensitive(tmp_path)
    if not folder_folds_case:
        _write(tmp_path / "PHOTO.TIF", "a bystander")

    # Act
    warning = _rewrite_sidecar(str(md), "photo.tif", "new.tif", str(tmp_path / "t.md"))

    # Assert
    if folder_folds_case:
        assert warning is None
    else:
        assert warning is not None and "names another file" in warning


def test_a_linked_transcripts_real_folder_is_synced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    real = _write(outside / "real.md", "old")
    link = tmp_path / "scans" / "link.md"
    link.parent.mkdir()
    real_realpath = os.path.realpath
    monkeypatch.setattr(
        rename_apply.os.path,
        "realpath",
        lambda p: str(real) if p == str(link) else real_realpath(p),
    )
    synced: list[str] = []
    monkeypatch.setattr(rename_apply, "_fsync_directory", synced.append)

    # Act
    warning = rename_apply._swap_in(str(link), str(link.parent / "t.md"), b"new")

    # Assert
    assert warning is None
    assert real.read_bytes() == b"new"
    assert synced == [str(outside)]


def test_run_ids_from_one_second_sort_by_age() -> None:
    # Act
    ids = [make_run_id() for _ in range(50)]

    # Assert
    assert sorted(ids, key=lambda i: i.split("_")[0]) == ids
    assert len({i.split("_")[0] for i in ids}) > 1
