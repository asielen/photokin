"""Tests for :mod:`photokin.rename_apply`, the only module that renames files.

Every test runs inside ``tmp_path``; nothing here touches a path it did not
create. The suite is deliberately weighted toward the failure cases rather
than the happy path, because the happy path is the one a person notices going
wrong: a run that half-happened, a rollback that did not roll all the way
back, or a journal that does not describe what is actually on the disk are
the outcomes this module exists to make impossible.

Crashes are injected as a ``BaseException`` rather than an ``OSError``,
because that is the difference the module's contract turns on: an ``OSError``
is a failure the executor handles and reverses, while a crash is the process
dying mid-run, which must leave the journal ``in_progress`` for a later
``--rename-resume`` to finish.
"""

from __future__ import annotations

import builtins
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from photokin import rename_apply
from photokin.changeset import make_photo_id
from photokin.rename_apply import (
    RenamePreflightError,
    apply_plan,
    finish_plan,
    latest_journal,
    preflight,
    read_journal,
    resume_run,
    undo_run,
)

RUN_ID = "2026-08-29T20:14:03Z_a1b2c3d4"


class _Crash(BaseException):
    """Stands in for the process dying mid-run: never caught by the executor."""


# --- Fixtures ----------------------------------------------------------


def _write(path: Path, content: str) -> Path:
    """Write *content* to *path* as UTF-8 with LF endings, returning the path."""
    path.write_bytes(content.encode("utf-8"))
    return path


def _sidecar(path: Path, image_name: str, body: str = "Dear Mother,\n") -> Path:
    """Write a markdown sidecar whose frontmatter points at *image_name*."""
    return _write(
        path,
        f'---\nsource_file: "{image_name}"\ngroup: "g1"\npart: "Front"\n---\n\n{body}',
    )


def _entry(
    image: Path, target: str, companions: dict[Path, str] | None = None
) -> dict[str, Any]:
    """Build one plan entry, stamping the image's real size and mtime."""
    stat = image.stat()
    return {
        "path": str(image),
        "photo_id": make_photo_id(str(image)),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "target": target,
        "target_stem": os.path.splitext(target)[0],
        "changed": os.path.basename(str(image)) != target,
        "notes": [],
        "companions": [
            {"path": str(path), "target": name} for path, name in (companions or {}).items()
        ],
    }


def _plan(folder: Path, entries: list[dict[str, Any]], run_id: str = RUN_ID) -> dict[str, Any]:
    """Build a section 6.2 plan around *entries*."""
    return {
        "schema_version": 1,
        "run_id": run_id,
        "photokin_version": "0.5.0",
        "folder": str(folder),
        "prefix_template": "newname",
        "digits": 3,
        "order": "name",
        "managed_by": None,
        "entries": entries,
        "left_behind": [],
        "warnings": [],
        "errors": [],
    }


def _names(folder: Path) -> set[str]:
    """Return the folder's names, journals excluded."""
    return {name for name in os.listdir(folder) if not name.endswith(".ndjson")}


def _records(journal_path: str) -> list[dict[str, Any]]:
    """Return every parsed record of a journal file, in order."""
    with open(journal_path, "r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _fail_at(monkeypatch: pytest.MonkeyPatch, call: int, error: BaseException) -> None:
    """Make the *call*-th ``os.rename`` of the test raise *error*, once."""
    real = os.rename
    seen = {"count": 0}

    def fake_rename(src: Any, dst: Any, **kwargs: Any) -> None:
        seen["count"] += 1
        if seen["count"] == call:
            raise error
        real(src, dst, **kwargs)

    monkeypatch.setattr(os, "rename", fake_rename)


def _case_insensitive(folder: Path) -> bool:
    """Return whether *folder*'s filesystem folds case in file names."""
    probe = folder / "PhotokinCaseProbe.tmp"
    probe.write_bytes(b"probe")
    try:
        return (folder / "photokincaseprobe.tmp").exists()
    finally:
        probe.unlink()


def _simple_folder(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """Two images, one companion sidecar, and the plan that renames them."""
    folder = tmp_path / "scans"
    folder.mkdir()
    first = _write(folder / "file102.tif", "first image")
    second = _write(folder / "file105.tif", "second image")
    sidecar = _sidecar(folder / "file105.md", "file105.tif")
    plan = _plan(
        folder,
        [
            _entry(first, "newname-001.tif"),
            _entry(second, "newname-002.tif", {sidecar: "newname-002.md"}),
        ],
    )
    return folder, plan


def _chain_folder(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """An upward chain: the first file's target is the second file's own name."""
    folder = tmp_path / "scans"
    folder.mkdir()
    first = _write(folder / "a.tif", "a bytes")
    second = _write(folder / "bw-001.tif", "b bytes")
    plan = _plan(folder, [_entry(first, "bw-001.tif"), _entry(second, "bw-002.tif")])
    return folder, plan


def _renumber_folder(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """A gap-closing renumber: every target is the current name of another file.

    The shape rename mode exists to produce, and the one that makes "is this
    name free" answerable only against the plan as a whole: ``file004.tif``
    sits in the folder before the run because it is the second op's source,
    and after it because it is the second op's target.
    """
    folder = tmp_path / "scans"
    folder.mkdir()
    entries = [
        _entry(_write(folder / source, f"bytes of {source}"), target)
        for source, target in (("file004.tif", "file003.tif"), ("file005.tif", "file004.tif"))
    ]
    return folder, _plan(folder, entries)


def _two_sidecar_folder(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """Two images, each with a transcript sidecar of its own."""
    folder = tmp_path / "scans"
    folder.mkdir()
    first = _write(folder / "file102.tif", "first image")
    second = _write(folder / "file105.tif", "second image")
    first_md = _sidecar(folder / "file102.md", "file102.tif")
    second_md = _sidecar(folder / "file105.md", "file105.tif")
    plan = _plan(
        folder,
        [
            _entry(first, "newname-001.tif", {first_md: "newname-001.md"}),
            _entry(second, "newname-002.tif", {second_md: "newname-002.md"}),
        ],
    )
    return folder, plan



# --- Apply and verify --------------------------------------------------


def test_apply_renames_images_and_companions(tmp_path: Path) -> None:
    """The happy path: every target lands, no temporary survives, journal closed."""
    folder, plan = _simple_folder(tmp_path)

    report = apply_plan(plan)

    assert report.status == rename_apply.STATUS_APPLIED
    assert report.exit_code == 0
    assert (report.renamed, report.companions) == (2, 1)
    assert _names(folder) == {"newname-001.tif", "newname-002.tif", "newname-002.md"}
    assert (folder / "newname-001.tif").read_bytes() == b"first image"


def test_apply_writes_the_journal_before_it_renames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The journal is on disk, complete, before the first rename happens."""
    folder, plan = _simple_folder(tmp_path)
    seen: list[list[dict[str, Any]]] = []
    real = os.rename

    def fake_rename(src: Any, dst: Any, **kwargs: Any) -> None:
        if not seen:
            journal = latest_journal(str(folder))
            assert journal is not None
            seen.append(_records(journal))
        real(src, dst, **kwargs)

    monkeypatch.setattr(os, "rename", fake_rename)
    apply_plan(plan)

    header, *files = seen[0]
    assert header["status"] == rename_apply.STATUS_IN_PROGRESS
    assert header["run_id"] == RUN_ID
    assert header["folder"] == str(folder)
    assert header["prefix_template"] == "newname"
    assert header["digits"] == 3
    assert header["photokin_version"] == "0.5.0"
    assert [record["kind"] for record in files] == ["image", "image", "companion"]
    assert all(record["tmp"].startswith(".photokin-rename-") for record in files)


def test_journal_footer_records_the_counts(tmp_path: Path) -> None:
    """The closing record carries ``applied`` and the run summary."""
    _folder, plan = _simple_folder(tmp_path)

    report = apply_plan(plan)

    assert report.journal_path is not None
    footer = _records(report.journal_path)[-1]
    assert footer["status"] == rename_apply.STATUS_APPLIED
    assert (footer["renamed"], footer["companions"]) == (2, 1)


def test_the_sidecar_source_file_line_follows_the_image(tmp_path: Path) -> None:
    """A renamed sidecar points at the image's new name, and nothing else moves."""
    folder, plan = _simple_folder(tmp_path)

    apply_plan(plan)

    text = (folder / "newname-002.md").read_text(encoding="utf-8")
    assert 'source_file: "newname-002.tif"' in text
    assert 'group: "g1"' in text
    assert text.endswith("Dear Mother,\n")


def test_a_plan_that_changes_nothing_writes_no_journal(tmp_path: Path) -> None:
    """An already-clean folder is left completely alone."""
    folder = tmp_path / "scans"
    folder.mkdir()
    image = _write(folder / "newname-001.tif", "image")
    plan = _plan(folder, [_entry(image, "newname-001.tif")])

    report = apply_plan(plan)

    assert report.status == rename_apply.STATUS_NOTHING_TO_DO
    assert report.exit_code == 0
    assert report.journal_path is None
    assert os.listdir(folder) == ["newname-001.tif"]


def test_dry_run_touches_nothing(tmp_path: Path) -> None:
    """``dry_run`` preflights and counts without writing."""
    folder, plan = _simple_folder(tmp_path)

    report = apply_plan(plan, dry_run=True)

    assert report.status == rename_apply.STATUS_WOULD_APPLY
    assert (report.renamed, report.companions) == (2, 1)
    assert report.journal_path is None
    assert _names(folder) == {"file102.tif", "file105.tif", "file105.md"}


# --- Chains and case-only renames --------------------------------------


def test_a_chain_applies_without_collision(tmp_path: Path) -> None:
    """A gap-closing renumber whose targets are other sources' current names."""
    folder = tmp_path / "scans"
    folder.mkdir()
    sources = ["file001.tif", "file002.tif", "file004.tif", "file005.tif"]
    targets = ["file001.tif", "file002.tif", "file003.tif", "file004.tif"]
    entries = []
    for source, target in zip(sources, targets, strict=True):
        image = _write(folder / source, f"bytes of {source}")
        entries.append(_entry(image, target))

    report = apply_plan(_plan(folder, entries))

    assert report.status == rename_apply.STATUS_APPLIED
    assert _names(folder) == set(targets)
    assert (folder / "file003.tif").read_bytes() == b"bytes of file004.tif"
    assert (folder / "file004.tif").read_bytes() == b"bytes of file005.tif"
    assert report.renamed == 2


def test_a_case_only_rename_works(tmp_path: Path) -> None:
    """Renaming a file to its own name in another case survives both phases."""
    folder = tmp_path / "scans"
    folder.mkdir()
    image = _write(folder / "file001.tif", "image")
    if not _case_insensitive(folder):
        pytest.skip("this filesystem is case-sensitive; the two-phase path is the same")

    report = apply_plan(_plan(folder, [_entry(image, "FILE001.tif")]))

    assert report.status == rename_apply.STATUS_APPLIED
    assert _names(folder) == {"FILE001.tif"}


# --- Undo --------------------------------------------------------------


def test_undo_restores_every_name_and_byte(tmp_path: Path) -> None:
    """An undo puts the folder back exactly as the apply found it."""
    folder, plan = _simple_folder(tmp_path)
    before = {name: (folder / name).read_bytes() for name in _names(folder)}

    applied = apply_plan(plan)
    assert applied.journal_path is not None
    report = undo_run(applied.journal_path)

    assert report.status == rename_apply.STATUS_UNDONE
    assert report.exit_code == 0
    assert _names(folder) == set(before)
    assert {name: (folder / name).read_bytes() for name in _names(folder)} == before


def test_undo_reverses_a_gap_closing_renumber(tmp_path: Path) -> None:
    """The undo of the thing this feature is for must not read as a collision.

    Every old name in a renumber is some other op's target, so an occupancy
    check made one entry at a time sees a stranger holding ``file004.tif`` --
    and one such skip makes the undo builder refuse the whole run, which
    leaves --rename-undo unable to reverse a renumber at all.
    """
    folder, plan = _renumber_folder(tmp_path)
    before = {name: (folder / name).read_bytes() for name in _names(folder)}
    applied = apply_plan(plan)
    assert applied.status == rename_apply.STATUS_APPLIED
    assert applied.journal_path is not None

    report = undo_run(applied.journal_path)

    assert report.status == rename_apply.STATUS_UNDONE
    assert report.skipped == ()
    assert {name: (folder / name).read_bytes() for name in _names(folder)} == before


def test_undo_still_refuses_an_old_name_held_by_a_stranger(tmp_path: Path) -> None:
    """Reading a renumber as normal may not wave a real collision through."""
    folder, plan = _renumber_folder(tmp_path)
    applied = apply_plan(plan)
    assert applied.journal_path is not None
    _write(folder / "file005.tif", "a file nobody planned for")

    with pytest.raises(RenamePreflightError) as excinfo:
        undo_run(applied.journal_path)

    assert "something is already called that: file005.tif" in str(excinfo.value)
    assert (folder / "file005.tif").read_bytes() == b"a file nobody planned for"


def test_undo_appends_to_the_journal_rather_than_rewriting_it(tmp_path: Path) -> None:
    """The journal stays an ordered account of everything that happened."""
    _folder, plan = _simple_folder(tmp_path)
    applied = apply_plan(plan)
    assert applied.journal_path is not None

    undo_run(applied.journal_path)

    records = _records(applied.journal_path)
    statuses = [record["status"] for record in records if "status" in record]
    assert statuses == [
        rename_apply.STATUS_IN_PROGRESS,
        rename_apply.STATUS_APPLIED,
        rename_apply.STATUS_IN_PROGRESS,
        rename_apply.STATUS_UNDONE,
    ]
    assert read_journal(applied.journal_path).status == rename_apply.STATUS_UNDONE


def test_undo_twice_is_refused(tmp_path: Path) -> None:
    """A journal that is already undone has nothing left to reverse."""
    _folder, plan = _simple_folder(tmp_path)
    applied = apply_plan(plan)
    assert applied.journal_path is not None
    undo_run(applied.journal_path)

    with pytest.raises(RenamePreflightError) as excinfo:
        undo_run(applied.journal_path)
    assert "already undone" in str(excinfo.value)


def test_undo_is_refused_while_the_run_is_unfinished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted run must be resumed, not undone, from where it stopped."""
    _folder, plan = _simple_folder(tmp_path)
    _fail_at(monkeypatch, 4, _Crash())
    with pytest.raises(_Crash):
        apply_plan(plan)
    monkeypatch.undo()

    journal = latest_journal(str(tmp_path / "scans"))
    assert journal is not None
    with pytest.raises(RenamePreflightError) as excinfo:
        undo_run(journal)
    assert "in_progress" in str(excinfo.value)


# --- Fault injection, resume -------------------------------------------


def test_a_crash_after_phase_a_leaves_in_progress_and_resume_completes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contract in one test: crash, in_progress, resume, undo."""
    folder, plan = _simple_folder(tmp_path)
    before = {name: (folder / name).read_bytes() for name in _names(folder)}

    # Three files change, so phase A is renames 1-3 and phase B starts at 4.
    _fail_at(monkeypatch, 4, _Crash())
    with pytest.raises(_Crash):
        apply_plan(plan)
    monkeypatch.undo()

    journal = latest_journal(str(folder))
    assert journal is not None
    assert read_journal(journal).status == rename_apply.STATUS_IN_PROGRESS
    assert all(name.startswith(".photokin-rename-") for name in _names(folder))

    resumed = resume_run(journal)
    assert resumed.status == rename_apply.STATUS_APPLIED
    assert _names(folder) == {"newname-001.tif", "newname-002.tif", "newname-002.md"}
    assert 'source_file: "newname-002.tif"' in (folder / "newname-002.md").read_text(
        encoding="utf-8"
    )

    undone = undo_run(journal)
    assert undone.status == rename_apply.STATUS_UNDONE
    assert {name: (folder / name).read_bytes() for name in _names(folder)} == before


def test_resume_completes_a_run_stopped_midway_through_phase_b(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files half at their temporaries and half at their targets still resolve."""
    folder, plan = _simple_folder(tmp_path)
    _fail_at(monkeypatch, 5, _Crash())
    with pytest.raises(_Crash):
        apply_plan(plan)
    monkeypatch.undo()

    journal = latest_journal(str(folder))
    assert journal is not None
    resumed = resume_run(journal)

    assert resumed.status == rename_apply.STATUS_APPLIED
    assert _names(folder) == {"newname-001.tif", "newname-002.tif", "newname-002.md"}


def test_resume_is_refused_on_a_finished_run(tmp_path: Path) -> None:
    """There is nothing to resume once the footer says applied."""
    _folder, plan = _simple_folder(tmp_path)
    applied = apply_plan(plan)
    assert applied.journal_path is not None

    with pytest.raises(RenamePreflightError) as excinfo:
        resume_run(applied.journal_path)
    assert "nothing to resume" in str(excinfo.value)


def test_an_unfinished_run_blocks_a_new_apply_in_the_same_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder with an open journal is not replanned around."""
    folder, plan = _simple_folder(tmp_path)
    _fail_at(monkeypatch, 4, _Crash())
    with pytest.raises(_Crash):
        apply_plan(plan)
    monkeypatch.undo()

    with pytest.raises(RenamePreflightError) as excinfo:
        apply_plan(_plan(folder, [], run_id="2026-08-29T21:00:00Z_ffffffff"))
    assert "in_progress" in str(excinfo.value)


def test_resume_finishes_an_upward_chain_killed_at_the_first_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A staged file's destination is the next op's source, not a second copy.

    Phase A killed after one rename is the ordinary shape of a chain: the
    temporary carries this run's id and belongs to that one op, so the name
    sitting at its destination is somebody else's file. Reading that as "in
    two places at once" closes every route out of the folder at once --
    resume, undo and a fresh apply all refuse it.
    """
    folder, plan = _chain_folder(tmp_path)
    # Rename 1 stages a.tif; rename 2 is where the process dies.
    _fail_at(monkeypatch, 2, _Crash())
    with pytest.raises(_Crash):
        apply_plan(plan)
    monkeypatch.undo()
    assert "a.tif" not in _names(folder)
    assert "bw-001.tif" in _names(folder)

    journal = latest_journal(str(folder))
    assert journal is not None
    resumed = resume_run(journal)

    assert resumed.status == rename_apply.STATUS_APPLIED
    assert _names(folder) == {"bw-001.tif", "bw-002.tif"}
    assert (folder / "bw-001.tif").read_bytes() == b"a bytes"
    assert (folder / "bw-002.tif").read_bytes() == b"b bytes"


def test_resume_finishes_a_renumber_killed_between_the_last_rename_and_the_footer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every file is at its target and no temporary is left: that is resumable.

    What distinguishes this from a chain killed inside phase A is that there
    is no temporary anywhere to be decisive about. Each op then looks both
    moved and unmoved at once -- its target is on disk, and so is its source,
    because that name is the *next* op's target -- and an op-by-op reading
    calls the first one pending and then refuses to rename into its own
    finished target. The folder would have no way out: not resumable, not
    undoable (the journal is open), and closed to a fresh apply.
    """
    folder, plan = _renumber_folder(tmp_path)

    def crash(path: str, footer: Any) -> None:
        raise _Crash()

    monkeypatch.setattr(rename_apply, "_append_footer", crash)
    with pytest.raises(_Crash):
        apply_plan(plan)
    monkeypatch.undo()
    assert _names(folder) == {"file003.tif", "file004.tif"}

    journal = latest_journal(str(folder))
    assert journal is not None
    assert read_journal(journal).status == rename_apply.STATUS_IN_PROGRESS
    resumed = resume_run(journal)

    assert resumed.status == rename_apply.STATUS_APPLIED
    assert _names(folder) == {"file003.tif", "file004.tif"}
    assert (folder / "file003.tif").read_bytes() == b"bytes of file004.tif"
    assert (folder / "file004.tif").read_bytes() == b"bytes of file005.tif"


def test_resume_refuses_a_destination_held_by_a_stranger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Taking chains as normal must not wave a real collision through."""
    folder, plan = _chain_folder(tmp_path)
    _fail_at(monkeypatch, 2, _Crash())
    with pytest.raises(_Crash):
        apply_plan(plan)
    monkeypatch.undo()
    _write(folder / "bw-002.tif", "a file nobody planned for")

    journal = latest_journal(str(folder))
    assert journal is not None
    with pytest.raises(RenamePreflightError) as excinfo:
        resume_run(journal)

    assert "a file is already called that: bw-002.tif" in str(excinfo.value)
    assert (folder / "bw-002.tif").read_bytes() == b"a file nobody planned for"


def test_a_rollback_during_a_resume_reverses_the_whole_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``rolled_back`` has to mean the same thing on a resume as on an apply.

    docs/rename-contract.md defines it as "every completed step was put back;
    the folder is as it was", and it is a closed status: a folder left holding
    the interrupted run's already-placed ops under that footer is neither
    resumable nor undoable, and the original names survive only in the journal.
    """
    folder, plan = _simple_folder(tmp_path)
    before = {name: (folder / name).read_bytes() for name in _names(folder)}

    # Renames 1-3 are phase A; rename 4 places the first op, rename 5 dies.
    _fail_at(monkeypatch, 5, _Crash())
    with pytest.raises(_Crash):
        apply_plan(plan)
    monkeypatch.undo()
    assert (folder / "newname-001.tif").exists()

    journal = latest_journal(str(folder))
    assert journal is not None
    _fail_at(monkeypatch, 1, OSError("the disk said no"))
    report = resume_run(journal)
    monkeypatch.undo()

    assert report.status == rename_apply.STATUS_ROLLED_BACK
    assert report.exit_code == 1
    assert report.stranded == ()
    assert _names(folder) == set(before)
    assert {name: (folder / name).read_bytes() for name in _names(folder)} == before
    assert _records(journal)[-1]["status"] == rename_apply.STATUS_ROLLED_BACK


def test_resume_rewrites_the_sidecar_of_an_entry_that_finished_before_the_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sidecar pass covers the whole segment, not the resumed subset.

    An entry whose image and whose .md both cleared phase B is not part of the
    work a resume has left to do, and verification checks names only -- so a
    pass driven off the resumed subset leaves that transcript pointing at a
    file that no longer exists, under an ``applied`` report with no warnings.
    """
    folder, plan = _two_sidecar_folder(tmp_path)
    # Phase A is renames 1-4; renames 5 and 6 place the first entry's pair.
    _fail_at(monkeypatch, 7, _Crash())
    with pytest.raises(_Crash):
        apply_plan(plan)
    monkeypatch.undo()
    assert {"newname-001.tif", "newname-001.md"} <= _names(folder)

    journal = latest_journal(str(folder))
    assert journal is not None
    resumed = resume_run(journal)

    assert resumed.status == rename_apply.STATUS_APPLIED
    assert resumed.warnings == ()
    assert 'source_file: "newname-001.tif"' in (folder / "newname-001.md").read_text(
        encoding="utf-8"
    )
    assert 'source_file: "newname-002.tif"' in (folder / "newname-002.md").read_text(
        encoding="utf-8"
    )



# --- Rollback ----------------------------------------------------------


def test_the_renamed_folder_is_synced_before_the_journal_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A closed ``applied`` footer may not outlive the renames it describes.

    A filesystem is free to make a file's own fsync durable without the
    directory updates around it, so a power cut between the last rename and
    the footer can keep the footer and lose phase B -- after which resume
    refuses the folder (its journal is closed) and undo assumes targets that
    are not there. The journal's own creation is synced too, which is why this
    pins the *order* rather than counting the calls.
    """
    _folder, plan = _simple_folder(tmp_path)
    order: list[str] = []
    real_fsync = rename_apply._fsync_directory
    real_footer = rename_apply._append_footer
    real_rename = os.rename

    def record_fsync(directory: str) -> None:
        order.append("fsync")
        real_fsync(directory)

    def record_footer(path: str, footer: Any) -> None:
        order.append(f"footer {footer['status']}")
        real_footer(path, footer)

    def record_rename(src: Any, dst: Any, **kwargs: Any) -> None:
        order.append("rename")
        real_rename(src, dst, **kwargs)

    monkeypatch.setattr(rename_apply, "_fsync_directory", record_fsync)
    monkeypatch.setattr(rename_apply, "_append_footer", record_footer)
    monkeypatch.setattr(os, "rename", record_rename)
    report = apply_plan(plan)
    monkeypatch.undo()

    assert report.status == rename_apply.STATUS_APPLIED
    assert "rename" in order
    assert order[-2:] == ["fsync", "footer applied"]


def test_a_failure_in_phase_b_puts_every_file_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError is handled, not propagated: the run reverses and says so."""
    folder, plan = _simple_folder(tmp_path)
    before = {name: (folder / name).read_bytes() for name in _names(folder)}

    _fail_at(monkeypatch, 4, OSError("the disk said no"))
    report = apply_plan(plan)

    assert report.status == rename_apply.STATUS_ROLLED_BACK
    assert report.exit_code == 1
    assert report.stranded == ()
    assert {name: (folder / name).read_bytes() for name in _names(folder)} == before
    assert report.journal_path is not None
    assert _records(report.journal_path)[-1]["status"] == rename_apply.STATUS_ROLLED_BACK


# --- Preflight ---------------------------------------------------------


def test_a_changed_source_mtime_fails_preflight(tmp_path: Path) -> None:
    """A file touched since planning stops the run before anything moves."""
    folder, plan = _simple_folder(tmp_path)
    image = folder / "file102.tif"
    os.utime(image, (image.stat().st_atime, image.stat().st_mtime + 120))

    problems = preflight(plan)
    assert problems == ["changed since the plan was made: file102.tif"]
    with pytest.raises(RenamePreflightError):
        apply_plan(plan)
    assert _names(folder) == {"file102.tif", "file105.tif", "file105.md"}


def test_a_changed_source_size_fails_preflight(tmp_path: Path) -> None:
    """So does a file whose content grew, even if its mtime somehow matches."""
    folder, plan = _simple_folder(tmp_path)
    image = folder / "file102.tif"
    stat = image.stat()
    image.write_bytes(b"first image, but longer")
    os.utime(image, (stat.st_atime, stat.st_mtime))

    assert preflight(plan) == ["changed since the plan was made: file102.tif"]


def test_a_missing_source_fails_preflight(tmp_path: Path) -> None:
    """A source that is gone is a stale plan, and a stale plan is replanned."""
    folder, plan = _simple_folder(tmp_path)
    (folder / "file105.md").unlink()

    assert preflight(plan) == ["gone since the plan was made: file105.md"]


def test_an_existing_non_source_target_fails_preflight(tmp_path: Path) -> None:
    """A bystander already holding a target name is never overwritten."""
    folder, plan = _simple_folder(tmp_path)
    _write(folder / "newname-001.tif", "a file nobody planned for")

    problems = preflight(plan)
    assert problems == ["a file is already called that: newname-001.tif"]
    with pytest.raises(RenamePreflightError):
        apply_plan(plan)
    assert (folder / "newname-001.tif").read_bytes() == b"a file nobody planned for"


def test_a_directory_holding_a_target_name_fails_preflight(tmp_path: Path) -> None:
    """Occupancy is a question about names, and a directory holds one too.

    A check made against the file snapshot cannot see a directory or a
    symlink, so the run gets its journal written and every source staged
    before phase B notices and reverses the lot.
    """
    folder, plan = _simple_folder(tmp_path)
    (folder / "newname-001.tif").mkdir()

    problems = preflight(plan)

    assert problems == ["a file is already called that: newname-001.tif"]
    with pytest.raises(RenamePreflightError):
        apply_plan(plan)
    assert (folder / "newname-001.tif").is_dir()
    assert not list(folder.glob("*.ndjson"))
    assert _names(folder) == {"file102.tif", "file105.tif", "file105.md", "newname-001.tif"}


def test_two_entries_wanting_one_name_fails_preflight(tmp_path: Path) -> None:
    """Duplicate targets are compared case-insensitively, as the planner does."""
    folder = tmp_path / "scans"
    folder.mkdir()
    first = _write(folder / "a.tif", "a")
    second = _write(folder / "b.tif", "b")
    plan = _plan(folder, [_entry(first, "new-001.tif"), _entry(second, "NEW-001.tif")])

    assert preflight(plan) == ["two files want the same name: NEW-001.tif"]


def test_duplicate_targets_are_folded_by_the_plan_not_by_the_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os.path.normcase`` folds nothing on POSIX, so the check may not use it.

    Patching it to the identity it already is on Linux and macOS puts a
    Windows run in the position CI runs in: a plan whose two targets differ
    only in case is unsafe wherever it was planned, and preflight has to say
    so on every platform.
    """
    monkeypatch.setattr(os.path, "normcase", lambda name: name)
    folder = tmp_path / "scans"
    folder.mkdir()
    first = _write(folder / "a.tif", "a")
    second = _write(folder / "b.tif", "b")
    plan = _plan(folder, [_entry(first, "new-001.tif"), _entry(second, "NEW-001.tif")])

    assert preflight(plan) == ["two files want the same name: NEW-001.tif"]


def test_a_bystander_differing_only_in_case_still_holds_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Target against bystander is the same question, and gets the same answer."""
    monkeypatch.setattr(os.path, "normcase", lambda name: name)
    folder = tmp_path / "scans"
    folder.mkdir()
    image = _write(folder / "a.tif", "a")
    _write(folder / "NEWNAME-001.TIF", "a file nobody planned for")
    plan = _plan(folder, [_entry(image, "newname-001.tif")])

    assert preflight(plan) == ["a file is already called that: newname-001.tif"]


@pytest.mark.parametrize(
    "target",
    ["../new-001.tif", "..\\new-001.tif", "sub/new-001.tif", "sub\\new-001.tif", ".."],
)
def test_a_target_naming_a_path_fails_preflight(tmp_path: Path, target: str) -> None:
    """The executor never renames across a folder boundary.

    Every spelling is read the same way on both platforms: ``os.path.basename``
    alone waves a backslash through on Linux and ``..`` through everywhere.
    """
    folder = tmp_path / "scans"
    folder.mkdir()
    image = _write(folder / "a.tif", "a")
    plan = _plan(folder, [_entry(image, target)])

    assert preflight(plan) == [f"a target names a path, not a file: {target}"]


def test_a_source_spelled_in_another_case_is_renamed_under_its_disk_name(
    tmp_path: Path,
) -> None:
    """A manifest may spell an existing file in its own case, and often does.

    On a case-insensitive volume that spelling names the file that is there,
    which the planner's identity check has already accepted, so preflight may
    not call it missing -- and every rename still uses the name on disk. On a
    case-sensitive one the two spellings are two files and the planned one is
    genuinely gone.
    """
    folder = tmp_path / "scans"
    folder.mkdir()
    image = _write(folder / "file102.tif", "first image")
    entry = _entry(image, "newname-001.tif")
    entry["path"] = str(folder / "FILE102.TIF")
    plan = _plan(folder, [entry])

    if not _case_insensitive(folder):
        assert preflight(plan) == ["gone since the plan was made: FILE102.TIF"]
        return

    assert preflight(plan) == []
    report = apply_plan(plan)

    assert report.status == rename_apply.STATUS_APPLIED
    assert _names(folder) == {"newname-001.tif"}
    assert report.journal_path is not None
    moved = [r for r in _records(report.journal_path) if r.get("record") == "file"]
    assert [record["from"] for record in moved] == ["file102.tif"]


def test_a_plan_carrying_errors_is_refused(tmp_path: Path) -> None:
    """The planner's own errors stop the executor too."""
    _folder, plan = _simple_folder(tmp_path)
    plan["errors"] = ["digit overflow in bucket 'newname'"]

    with pytest.raises(RenamePreflightError) as excinfo:
        apply_plan(plan)
    assert "digit overflow" in str(excinfo.value)


# --- rename-finish -----------------------------------------------------


def _finish_folder(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """One image a catalog has already renamed, one it has not; both with sidecars."""
    folder = tmp_path / "scans"
    folder.mkdir()
    done = _write(folder / "file102.tif", "first image")
    pending = _write(folder / "file105.tif", "second image")
    done_sidecar = _sidecar(folder / "file102.md", "file102.tif")
    pending_sidecar = _sidecar(folder / "file105.md", "file105.tif")
    plan = _plan(
        folder,
        [
            _entry(done, "newname-001.tif", {done_sidecar: "newname-001.md"}),
            _entry(pending, "newname-002.tif", {pending_sidecar: "newname-002.md"}),
        ],
    )
    # The catalog renamed the first image and nothing else, which is exactly
    # the state --rename-finish exists to clean up after.
    done.rename(folder / "newname-001.tif")
    return folder, plan


def test_finish_renames_only_the_companions_whose_image_is_in_place(tmp_path: Path) -> None:
    """The rest are reported, not guessed at."""
    folder, plan = _finish_folder(tmp_path)

    report = finish_plan(plan)

    assert report.status == rename_apply.STATUS_APPLIED
    assert report.companions == 1
    assert report.skipped == ("image not renamed yet: file105.tif",)
    assert _names(folder) == {
        "newname-001.tif",
        "newname-001.md",
        "file105.tif",
        "file105.md",
    }
    assert 'source_file: "newname-001.tif"' in (folder / "newname-001.md").read_text(
        encoding="utf-8"
    )


def test_finish_carries_the_companions_of_a_gap_closing_renumber(tmp_path: Path) -> None:
    """The catalog renumbered; every old name it freed is a new name it used.

    Requiring an entry's old name to be absent reports the first entry of the
    chain as "not renamed" and leaves its transcript behind under a name the
    next entry's transcript also wants, so the second entry is refused too and
    the run ends having done nothing at all.
    """
    folder = tmp_path / "scans"
    folder.mkdir()
    first = _write(folder / "file004.tif", "first image")
    second = _write(folder / "file005.tif", "second image")
    first_md = _sidecar(folder / "file004.md", "file004.tif")
    second_md = _sidecar(folder / "file005.md", "file005.tif")
    plan = _plan(
        folder,
        [
            _entry(first, "file003.tif", {first_md: "file003.md"}),
            _entry(second, "file004.tif", {second_md: "file004.md"}),
        ],
    )
    # The catalog renamed both images, lowest first, as any renumber must.
    first.rename(folder / "file003.tif")
    second.rename(folder / "file004.tif")

    report = finish_plan(plan)

    assert report.status == rename_apply.STATUS_APPLIED
    assert report.skipped == ()
    assert report.companions == 2
    assert _names(folder) == {"file003.tif", "file003.md", "file004.tif", "file004.md"}
    assert 'source_file: "file003.tif"' in (folder / "file003.md").read_text(encoding="utf-8")
    assert 'source_file: "file004.tif"' in (folder / "file004.md").read_text(encoding="utf-8")


def test_finish_is_safe_to_run_again(tmp_path: Path) -> None:
    """A second run finds nothing left to do and writes no second journal."""
    folder, plan = _finish_folder(tmp_path)
    first = finish_plan(plan)
    assert first.journal_path is not None

    second = finish_plan(plan)

    assert second.status == rename_apply.STATUS_NOTHING_TO_DO
    assert second.journal_path is None
    assert second.skipped == ("image not renamed yet: file105.tif",)
    assert len([name for name in os.listdir(folder) if name.endswith(".ndjson")]) == 1


def test_finish_marks_the_image_as_renamed_by_someone_else(tmp_path: Path) -> None:
    """The journal says who did what, so an undo knows what it may reverse."""
    _folder, plan = _finish_folder(tmp_path)

    report = finish_plan(plan)

    assert report.journal_path is not None
    records = _records(report.journal_path)
    header = records[0]
    image = next(record for record in records if record.get("kind") == "image")
    assert header["mode"] == rename_apply.MODE_FINISH
    assert image["renamed_by"] == "external"
    assert image["tmp"] is None


def test_finish_refuses_a_companion_target_that_leaves_the_folder(tmp_path: Path) -> None:
    """A damaged plan may not walk a companion out of the folder it names.

    The refusal comes before the journal, because a companion moved out and
    then interrupted is a file ``--rename-resume`` cannot even see: resume
    lists the folder, and the file is no longer in it.
    """
    folder, plan = _finish_folder(tmp_path)
    plan["entries"][0]["companions"][0]["target"] = "../outside.md"
    before = _names(folder)

    with pytest.raises(RenamePreflightError) as excinfo:
        finish_plan(plan)

    assert "a target names a path, not a file: ../outside.md" in str(excinfo.value)
    assert _names(folder) == before
    assert not (tmp_path / "outside.md").exists()
    assert not list(folder.glob("*.ndjson"))


def test_undo_of_a_finish_journal_reverses_companions_only(tmp_path: Path) -> None:
    """The catalog puts its own images back first; photokin follows with the rest."""
    folder, plan = _finish_folder(tmp_path)
    report = finish_plan(plan)
    assert report.journal_path is not None
    (folder / "newname-001.tif").rename(folder / "file102.tif")

    undone = undo_run(report.journal_path)

    assert undone.status == rename_apply.STATUS_UNDONE
    assert undone.companions == 1
    assert _names(folder) == {"file102.tif", "file102.md", "file105.tif", "file105.md"}
    assert 'source_file: "file102.tif"' in (folder / "file102.md").read_text(encoding="utf-8")


def test_undo_of_a_finish_journal_waits_for_the_image_to_come_back(tmp_path: Path) -> None:
    """A companion whose image is still renamed is reported and left alone."""
    folder, plan = _finish_folder(tmp_path)
    report = finish_plan(plan)
    assert report.journal_path is not None

    undone = undo_run(report.journal_path)

    assert undone.status == rename_apply.STATUS_NOTHING_TO_DO
    assert undone.skipped == (
        "image not put back yet: newname-001.tif",
        "left alone: newname-001.md",
    )
    assert (folder / "newname-001.md").exists()


# --- Journals ----------------------------------------------------------


def test_a_root_folders_journal_is_still_discovered(monkeypatch: pytest.MonkeyPatch) -> None:
    """A folder with no basename writes and looks for the same journal name.

    A filesystem or drive root is the case: the journal is created as
    ``folder_rename-...`` and discovery has to look for exactly that, or
    ``--rename-resume``, ``--rename-undo`` and the open-journal guard all miss
    it. The listing is stubbed because the folder in question is the root
    itself, which no test may write to.
    """
    root = os.path.abspath(os.sep)
    journal_name = os.path.basename(rename_apply.journal_path_for(root, RUN_ID))
    monkeypatch.setattr(rename_apply, "_list_names", lambda folder: {journal_name})

    assert rename_apply._journal_candidates(root) == [os.path.join(root, journal_name)]


def test_latest_journal_filters_by_status(tmp_path: Path) -> None:
    """Undo and resume find the journal they are each allowed to act on."""
    folder, plan = _simple_folder(tmp_path)
    applied = apply_plan(plan)

    assert latest_journal(str(folder)) == applied.journal_path
    assert latest_journal(str(folder), [rename_apply.STATUS_APPLIED]) == applied.journal_path
    assert latest_journal(str(folder), rename_apply.OPEN_STATUSES) is None


def test_a_damaged_journal_is_refused_rather_than_guessed_at(tmp_path: Path) -> None:
    """Half a line of NDJSON is not something to reason from."""
    folder, plan = _simple_folder(tmp_path)
    applied = apply_plan(plan)
    assert applied.journal_path is not None
    with open(applied.journal_path, "a", encoding="utf-8") as handle:
        handle.write('{"record": "foot\n')

    with pytest.raises(RenamePreflightError) as excinfo:
        read_journal(applied.journal_path)
    assert "damaged" in str(excinfo.value)
    assert _names(folder) == {"newname-001.tif", "newname-002.tif", "newname-002.md"}


def test_an_unterminated_last_record_is_ignored_rather_than_fatal(tmp_path: Path) -> None:
    """The one damage a crash can do to this file is the line it was writing.

    Every record before it was fsynced and still describes a folder resume and
    undo can act on, so refusing the journal for a torn tail takes both away
    at exactly the failure the journal exists to survive. The tail is dropped
    on the next append too, or the record written behind it would splice the
    two into one complete line no reader may accept.
    """
    folder, plan = _simple_folder(tmp_path)
    applied = apply_plan(plan)
    assert applied.journal_path is not None
    with open(applied.journal_path, "a", encoding="utf-8") as handle:
        handle.write('{"record": "footer", "status": "und')

    assert read_journal(applied.journal_path).status == rename_apply.STATUS_APPLIED
    undone = undo_run(applied.journal_path)

    assert undone.status == rename_apply.STATUS_UNDONE
    assert _names(folder) == {"file102.tif", "file105.tif", "file105.md"}
    assert read_journal(applied.journal_path).status == rename_apply.STATUS_UNDONE
    assert [record["status"] for record in _records(applied.journal_path) if "status" in record] == [
        rename_apply.STATUS_IN_PROGRESS,
        rename_apply.STATUS_APPLIED,
        rename_apply.STATUS_IN_PROGRESS,
        rename_apply.STATUS_UNDONE,
    ]


def test_the_journal_lands_inside_the_renamed_folder(tmp_path: Path) -> None:
    """Named for the folder and the run, beside the files it describes."""
    folder, plan = _simple_folder(tmp_path)

    report = apply_plan(plan)

    assert report.journal_path is not None
    assert os.path.dirname(report.journal_path) == str(folder)
    # The run id is an ISO timestamp, so ':' is folded out of the file name.
    assert os.path.basename(report.journal_path) == (
        "scans_rename-2026-08-29T20-14-03Z_a1b2c3d4.ndjson"
    )


# --- Sidecars ----------------------------------------------------------


def test_a_sidecar_without_frontmatter_is_left_alone_and_reported(tmp_path: Path) -> None:
    """A file photokin does not own is renamed but never rewritten."""
    folder = tmp_path / "scans"
    folder.mkdir()
    image = _write(folder / "file102.tif", "image")
    notes = _write(folder / "file102.md", "just some notes\n")
    plan = _plan(folder, [_entry(image, "newname-001.tif", {notes: "newname-001.md"})])

    report = apply_plan(plan)

    assert report.status == rename_apply.STATUS_APPLIED
    assert (folder / "newname-001.md").read_bytes() == b"just some notes\n"
    assert report.warnings == ("sidecar not updated (no frontmatter): newname-001.md",)


def test_a_sidecar_pointing_at_another_file_is_renamed_but_never_rewritten(
    tmp_path: Path,
) -> None:
    """Its ``source_file`` value is the only record of what it pointed at.

    Rewriting a link that names some other image destroys that value outright,
    and the undo then writes the op's own old image name rather than putting
    back what was there, so the original is gone for good.
    """
    folder = tmp_path / "scans"
    folder.mkdir()
    image = _write(folder / "file102.tif", "image")
    sidecar = _sidecar(folder / "file102.md", "somebody-else.tif")
    plan = _plan(folder, [_entry(image, "newname-001.tif", {sidecar: "newname-001.md"})])

    report = apply_plan(plan)

    assert report.status == rename_apply.STATUS_APPLIED
    assert report.warnings == ("sidecar not updated (it names another file): newname-001.md",)
    assert 'source_file: "somebody-else.tif"' in (folder / "newname-001.md").read_text(
        encoding="utf-8"
    )

    assert report.journal_path is not None
    undo_run(report.journal_path)
    assert 'source_file: "somebody-else.tif"' in (folder / "file102.md").read_text(
        encoding="utf-8"
    )


def test_the_staged_sidecar_carries_the_originals_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The swap must not hand a private transcript back with umask permissions.

    The staged replacement is a new inode, so its mode is whatever the umask
    gives it and every extended attribute of the original is dropped at the
    swap -- a transcript deliberately stored 0600 comes back readable by
    everyone. The mode is read off the staged file at the moment of the swap
    because that is the only reading every platform can make: Windows keeps
    one permission bit, not nine, and refuses to replace a read-only file at
    all.
    """
    original = tmp_path / "note.md"
    _write(original, "old bytes")
    os.chmod(original, 0o444)
    expected = stat.S_IMODE(original.stat().st_mode)
    staged = tmp_path / "staged.md"
    seen: dict[str, int] = {}
    real_replace = os.replace

    def spy_replace(src: Any, dst: Any, **kwargs: Any) -> None:
        seen["mode"] = stat.S_IMODE(os.stat(src).st_mode)
        real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", spy_replace)
    try:
        rename_apply._swap_in(str(original), str(staged), b"new bytes")
    finally:
        monkeypatch.undo()
        for path in (original, staged):
            if path.exists():
                os.chmod(path, 0o666)

    assert seen["mode"] == expected


def test_a_sidecar_keeps_its_line_endings(tmp_path: Path) -> None:
    """Only the one line changes; CRLF elsewhere survives the rewrite."""
    folder = tmp_path / "scans"
    folder.mkdir()
    image = _write(folder / "file102.tif", "image")
    sidecar = folder / "file102.md"
    sidecar.write_bytes(
        b'---\r\nsource_file: "file102.tif"\r\npart: "Front"\r\n---\r\n\r\nBody\r\n'
    )
    plan = _plan(folder, [_entry(image, "newname-001.tif", {sidecar: "newname-001.md"})])

    apply_plan(plan)

    assert (folder / "newname-001.md").read_bytes() == (
        b'---\r\nsource_file: "newname-001.tif"\r\npart: "Front"\r\n---\r\n\r\nBody\r\n'
    )


def test_a_crash_during_the_sidecar_rewrite_never_costs_the_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The only path in this module that can destroy bytes, killed mid-write.

    Opening the transcript ``"wb"`` in place truncates it before a byte is
    written, so a process killed at that instant leaves zero bytes and no copy
    anywhere -- and the journal still says ``in_progress``, so a later resume
    reports ``applied``, exit 0, warnings empty, over the loss. The rewrite is
    staged in a sibling temporary and swapped in instead, so the kill leaves
    either the old bytes or the new ones.
    """
    folder, plan = _simple_folder(tmp_path)
    before = (folder / "file105.md").read_bytes()
    real_open = builtins.open

    def fake_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = real_open(file, mode, *args, **kwargs)
        if "w" in mode and str(file).endswith(".md"):
            handle.close()
            raise _Crash()
        return handle

    monkeypatch.setattr(builtins, "open", fake_open)
    with pytest.raises(_Crash):
        apply_plan(plan)
    monkeypatch.undo()

    # Phase B finished before the rewrite began, so the transcript is at its
    # new name -- with every byte it started with.
    assert (folder / "newname-002.md").read_bytes() == before

    journal = latest_journal(str(folder))
    assert journal is not None
    resumed = resume_run(journal)

    assert resumed.status == rename_apply.STATUS_APPLIED
    assert resumed.warnings == ()
    assert _names(folder) == {"newname-001.tif", "newname-002.tif", "newname-002.md"}
    assert 'source_file: "newname-002.tif"' in (folder / "newname-002.md").read_text(
        encoding="utf-8"
    )



# --- The safety net itself ---------------------------------------------


def test_rename_never_overwrites_an_existing_file(tmp_path: Path) -> None:
    """The primitive every phase is built on refuses to clobber, on any platform.

    ``os.rename`` refuses on Windows and silently replaces on POSIX, so this
    pins the behavior the module guarantees rather than the one the OS
    happens to provide.
    """
    keep = _write(tmp_path / "keep.tif", "keep me")
    other = _write(tmp_path / "other.tif", "other")

    with pytest.raises(FileExistsError):
        rename_apply._rename(str(other), str(keep))

    assert keep.read_bytes() == b"keep me"
    assert other.exists()


def test_a_bystander_changing_mid_run_fails_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verification reads the folder, not this process's idea of the folder."""
    folder, plan = _simple_folder(tmp_path)
    bystander = _write(folder / "notes.txt", "untouched")
    before = {name: (folder / name).read_bytes() for name in _names(folder)}
    real = os.rename
    seen = {"count": 0}

    def fake_rename(src: Any, dst: Any, **kwargs: Any) -> None:
        seen["count"] += 1
        real(src, dst, **kwargs)
        if seen["count"] == 6:
            bystander.write_bytes(b"someone else got here first")

    monkeypatch.setattr(os, "rename", fake_rename)
    report = apply_plan(plan)

    assert report.status == rename_apply.STATUS_ROLLED_BACK
    assert report.exit_code == 1
    assert set(_names(folder)) == set(before)
    # The sidecars were rewritten before verification ran, so the rollback has
    # to bring their frontmatter back with them.
    assert (folder / "file105.md").read_bytes() == before["file105.md"]


def test_a_failed_rollback_names_the_temporaries_it_could_not_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worst case still ends with a file list a person can act on."""
    _folder, plan = _simple_folder(tmp_path)
    real = os.rename
    seen = {"count": 0}

    def fake_rename(src: Any, dst: Any, **kwargs: Any) -> None:
        seen["count"] += 1
        # Rename 4 is the first of phase B; rename 5 is the first step of the
        # reversal that failure triggers.
        if seen["count"] in (4, 5):
            raise OSError("the disk said no")
        real(src, dst, **kwargs)

    monkeypatch.setattr(os, "rename", fake_rename)
    report = apply_plan(plan)

    assert report.status == rename_apply.STATUS_NEEDS_ATTENTION
    assert report.exit_code == 1
    assert len(report.stranded) == 1
    stranded = report.stranded[0]
    assert os.path.basename(stranded).startswith(".photokin-rename-")
    assert os.path.exists(stranded)
    assert report.journal_path is not None
    footer = _records(report.journal_path)[-1]
    assert footer["status"] == rename_apply.STATUS_NEEDS_ATTENTION
    assert footer["stranded"] == [stranded]


def test_resume_finishes_a_run_a_failed_rollback_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``needs_attention`` is resumable forward, not a dead end."""
    folder, plan = _simple_folder(tmp_path)
    real = os.rename
    seen = {"count": 0}

    def fake_rename(src: Any, dst: Any, **kwargs: Any) -> None:
        seen["count"] += 1
        if seen["count"] in (4, 5):
            raise OSError("the disk said no")
        real(src, dst, **kwargs)

    monkeypatch.setattr(os, "rename", fake_rename)
    report = apply_plan(plan)
    monkeypatch.undo()
    assert report.journal_path is not None

    resumed = resume_run(report.journal_path)

    assert resumed.status == rename_apply.STATUS_APPLIED
    assert _names(folder) == {"newname-001.tif", "newname-002.tif", "newname-002.md"}


def test_the_same_plan_can_be_attempted_again_after_a_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry writes its own journal instead of appending to the failed one."""
    folder, plan = _simple_folder(tmp_path)
    _fail_at(monkeypatch, 4, OSError("the disk said no"))
    first = apply_plan(plan)
    monkeypatch.undo()
    assert first.status == rename_apply.STATUS_ROLLED_BACK

    second = apply_plan(plan)

    assert second.status == rename_apply.STATUS_APPLIED
    assert second.journal_path != first.journal_path
    assert _names(folder) == {"newname-001.tif", "newname-002.tif", "newname-002.md"}
    assert second.journal_path is not None
    header = _records(second.journal_path)[0]
    assert header["plan_run_id"] == RUN_ID
    assert header["run_id"] != RUN_ID


def test_finish_picks_up_an_image_the_catalog_renamed_later(tmp_path: Path) -> None:
    """The second run finishes what the first had to skip, on the same plan."""
    folder, plan = _finish_folder(tmp_path)
    first = finish_plan(plan)
    assert first.companions == 1
    (folder / "file105.tif").rename(folder / "newname-002.tif")

    second = finish_plan(plan)

    assert second.status == rename_apply.STATUS_APPLIED
    assert second.companions == 1
    assert second.skipped == ()
    assert second.journal_path != first.journal_path
    assert _names(folder) == {
        "newname-001.tif",
        "newname-001.md",
        "newname-002.tif",
        "newname-002.md",
    }
    assert 'source_file: "newname-002.tif"' in (folder / "newname-002.md").read_text(
        encoding="utf-8"
    )
