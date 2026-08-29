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

import json
import os
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


# --- Rollback ----------------------------------------------------------


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


def test_two_entries_wanting_one_name_fails_preflight(tmp_path: Path) -> None:
    """Duplicate targets are compared case-insensitively, as the planner does."""
    folder = tmp_path / "scans"
    folder.mkdir()
    first = _write(folder / "a.tif", "a")
    second = _write(folder / "b.tif", "b")
    plan = _plan(folder, [_entry(first, "new-001.tif"), _entry(second, "NEW-001.tif")])

    assert preflight(plan) == ["two files want the same name: NEW-001.tif"]


def test_a_target_naming_a_path_fails_preflight(tmp_path: Path) -> None:
    """The executor never renames across a folder boundary."""
    folder = tmp_path / "scans"
    folder.mkdir()
    image = _write(folder / "a.tif", "a")
    plan = _plan(folder, [_entry(image, os.path.join("..", "new-001.tif"))])

    assert "a target names a path, not a file" in preflight(plan)[0]


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
