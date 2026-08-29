"""
photokin.rename_apply
=====================

The executor for rename mode: the only module in photokin that renames a file
on disk. It takes the planner's plan (``docs/rename-mode.md`` section 6.2),
re-checks it against the folder, writes a journal, performs a two-phase
rename, brings companions along, rewrites markdown sidecars, verifies the
result, and can resume or undo a run from the journal it wrote.

Everything here is shaped by the failure case. Every other write photokin
makes is recoverable -- a bad caption is overwritten, a bad tag is rewritten --
but a half-finished rename over a few thousand scans is not something a person
can reconstruct by hand. So the ordering is fixed and not negotiable: the
journal is written, flushed and fsynced *before* the first rename, every file
moves through a hidden temporary name whether or not this particular run needs
one, and the verification runs against a fresh directory listing rather than
against what this process believes it did.

The journal records file names, not paths. A rename never crosses a folder
boundary (``docs/rename-mode.md`` section 11), so the folder the journal
itself sits in is the folder it describes -- which also means a journal stays
valid when someone moves the whole folder between the apply and the undo. The
header's ``folder`` is provenance, not the address operations are resolved
against.

Code map:
- RenamePreflightError  refusal raised before anything on disk is touched
- RenameOp              one file's from/to/tmp triple
- ApplyReport           what a run did, its counts, and its exit code
- JournalSegment        one run's header, file records and final status
- Journal               a parsed journal file: its segments and current status
- preflight             PUBLIC: re-check a plan against the folder
- apply_plan            PUBLIC: journal, two phases, sidecars, verify
- finish_plan           PUBLIC: companions only, images renamed by someone else
- resume_run            PUBLIC: finish an interrupted run forward
- undo_run              PUBLIC: reverse an applied (or finished) run
- read_journal          PUBLIC: parse a journal file into its segments
- latest_journal        PUBLIC: newest journal in a folder, by status
- journal_path_for      PUBLIC: where a run's journal goes
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .changeset import make_run_id
from .doc_sidecar import _yaml_string

logger = logging.getLogger(__name__)

#: Bumped whenever a journal record's shape changes in a way a reader could
#: care about, on the same rule ``changeset.SCHEMA_VERSION`` follows. A journal
#: is read back by this module's own undo and resume, so a bump means older
#: journals must still parse or must be refused by name.
JOURNAL_SCHEMA_VERSION = 1

#: The plan shape this executor understands (``docs/rename-mode.md`` 6.2).
PLAN_SCHEMA_VERSION = 1

MODE_APPLY = "apply"
MODE_FINISH = "finish"
MODE_UNDO = "undo"

STATUS_IN_PROGRESS = "in_progress"
STATUS_APPLIED = "applied"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_NEEDS_ATTENTION = "needs_attention"
STATUS_UNDONE = "undone"
STATUS_WOULD_APPLY = "would_apply"
#: The run had nothing to do: a plan whose every name is already its target
#: ("already clean"), a ``--rename-finish`` whose companions are all in place,
#: or an undo with nothing left it may move. Nothing is written and no journal
#: is opened, which is what makes all three safe to run twice.
STATUS_NOTHING_TO_DO = "nothing_to_do"

#: A journal in one of these states describes a folder that is not in the state
#: anyone planned for, so a new apply there is refused until it is resumed or
#: undone (``docs/rename-mode.md`` 5.4).
OPEN_STATUSES = frozenset({STATUS_IN_PROGRESS, STATUS_NEEDS_ATTENTION})

_TMP_PREFIX = ".photokin-rename-"
_JOURNAL_MARKER = "_rename-"
_JOURNAL_SUFFIX = ".ndjson"
_SIDECAR_EXT = ".md"

_IMAGE_KIND = "image"
_COMPANION_KIND = "companion"

#: mtime comparison tolerance, in seconds. The value is carried through JSON as
#: a float and read back off a fresh ``stat``, so an exact compare is one
#: rounding away from calling an untouched file modified.
_MTIME_TOLERANCE_S = 1e-6

#: Characters that are illegal in a Windows filename but legal in a run id
#: (which is an ISO timestamp, so it always contains ``:``).
_RUN_ID_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")


class RenamePreflightError(RuntimeError):
    """A plan or a journal was refused before anything on disk was touched.

    Attributes:
        summary: One line saying what is wrong.
        fix: One line saying what to do about it.
        problems: The specific names or files that failed the check.
    """

    def __init__(self, summary: str, fix: str, problems: Sequence[str] = ()) -> None:
        self.summary = summary
        self.fix = fix
        self.problems = tuple(problems)
        super().__init__("\n".join([summary, fix, *self.problems]))


class _VerifyFailed(RuntimeError):
    """Verification found the folder in a state the run did not intend."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(problems))


@dataclass(frozen=True)
class RenameOp:
    """One file's move within a run, as names inside a single folder.

    Attributes:
        src: Current file name.
        dst: Name the file ends the run under.
        tmp: Hidden temporary name it passes through; empty for an op this
            executor does not perform (see ``external``).
        kind: ``"image"`` or ``"companion"``.
        photo_id: The owning entry's ``changeset.make_photo_id`` value, which
            is what ties a companion back to its image.
        external: The rename was performed by someone else (a catalog
            application, ``--rename-finish``). The op is recorded so undo and
            the sidecar rewrite know the image's names, and is never executed.
    """

    src: str
    dst: str
    tmp: str
    kind: str
    photo_id: str
    external: bool = False


@dataclass(frozen=True)
class ApplyReport:
    """The outcome of one executor run.

    Attributes:
        status: One of the ``STATUS_*`` constants.
        journal_path: The journal written or appended to, or ``None`` when the
            run had nothing to do and wrote nothing.
        renamed: Image files renamed by this run.
        companions: Companion files renamed by this run.
        unchanged: Planned image files that already had their target name.
        left_behind: Same-stem files the planner declined to carry along.
        skipped: Human-readable lines naming what this run declined to do.
        stranded: Absolute paths of files a failed rollback left out of place,
            temporaries first. Non-empty only for ``needs_attention``.
        warnings: Non-fatal problems, principally sidecar rewrites that did
            not apply.
    """

    status: str
    journal_path: str | None
    renamed: int = 0
    companions: int = 0
    unchanged: int = 0
    left_behind: int = 0
    skipped: tuple[str, ...] = ()
    stranded: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        """Return 0 when the folder is in a state someone asked for, else 1."""
        return (
            0
            if self.status
            in (STATUS_APPLIED, STATUS_UNDONE, STATUS_WOULD_APPLY, STATUS_NOTHING_TO_DO)
            else 1
        )


@dataclass(frozen=True)
class JournalSegment:
    """One run recorded in a journal: its header, its files, its final status."""

    header: dict[str, Any]
    ops: tuple[RenameOp, ...]
    status: str

    @property
    def mode(self) -> str:
        """Return the segment's mode, defaulting to ``apply`` for an old journal."""
        mode = self.header.get("mode")
        return mode if isinstance(mode, str) and mode else MODE_APPLY

    @property
    def run_id(self) -> str:
        """Return the segment's run id, or the empty string when it carries none."""
        run_id = self.header.get("run_id")
        return run_id if isinstance(run_id, str) else ""


@dataclass(frozen=True)
class Journal:
    """A parsed journal file.

    Attributes:
        path: The journal's own path.
        folder: The folder it describes, which is the folder it sits in.
        segments: Every run appended to it, oldest first. An undo or a resume
            appends rather than rewriting, so the file is append-only and a
            reader that stops early still sees a truthful prefix.
    """

    path: str
    folder: str
    segments: tuple[JournalSegment, ...] = field(default_factory=tuple)

    @property
    def last(self) -> JournalSegment:
        """Return the most recent segment.

        Raises:
            RenamePreflightError: If the journal has no segments at all.
        """
        if not self.segments:
            raise RenamePreflightError(
                "This rename journal has no records in it.",
                "Check the folder by hand; photokin will not guess what it describes.",
                [self.path],
            )
        return self.segments[-1]

    @property
    def status(self) -> str:
        """Return the status of the last status-bearing record."""
        return self.last.status


# --- Names -------------------------------------------------------------


def _safe_run_id(run_id: str) -> str:
    """Fold a run id into characters legal in a filename on every platform.

    ``changeset.make_run_id`` returns an ISO timestamp plus a token, so it
    always contains ``:`` -- legal in a POSIX name, illegal in a Windows one.
    The folded form names the journal and the temporaries; the unfolded run id
    stays in the header, since that is what the plan and the changeset are
    keyed by.

    Args:
        run_id: The run id to fold.

    Returns:
        The run id with every character outside ``[A-Za-z0-9_.-]`` replaced
        by ``-``.
    """
    return _RUN_ID_UNSAFE_RE.sub("-", run_id)


def journal_path_for(folder: str, run_id: str) -> str:
    """Return the journal path for a run: ``<foldername>_rename-<run_id>.ndjson``.

    Args:
        folder: The folder being renamed; the journal lands inside it.
        run_id: The run's id.

    Returns:
        An absolute path inside *folder*.
    """
    folder = os.path.abspath(folder)
    name = os.path.basename(os.path.normpath(folder)) or "folder"
    return os.path.join(folder, f"{name}{_JOURNAL_MARKER}{_safe_run_id(run_id)}{_JOURNAL_SUFFIX}")


def _fresh_journal(folder: str, plan_run_id: str) -> tuple[str, str]:
    """Return ``(run id, journal path)`` for a new run, never reusing a journal.

    A plan's run id names its journal, which is what ties the plan, the
    journal and the changeset together, so it is the id used by default. But a
    plan can legitimately be attempted twice -- after a rollback, or by a
    ``--rename-finish`` waiting on the catalog to catch up -- and a second run
    appended to a closed account of the first is not something a reader could
    untangle afterwards. So an attempt that finds its journal already there
    mints its own id and keeps the plan's in the header.

    Args:
        folder: The folder being renamed.
        plan_run_id: The run id the plan carries, if any.

    Returns:
        The run id this attempt will use, and its journal path.
    """
    run_id = plan_run_id or make_run_id()
    path = journal_path_for(folder, run_id)
    while os.path.exists(path):
        run_id = make_run_id()
        path = journal_path_for(folder, run_id)
    return run_id, path


def _tmp_name(safe_run_id: str, index: int, source_name: str) -> str:
    """Return the hidden temporary name for one file of a run."""
    return f"{_TMP_PREFIX}{safe_run_id}-{index}{os.path.splitext(source_name)[1]}"


# --- Disk primitives ---------------------------------------------------


def _rename(src: str, dst: str) -> None:
    """Move *src* to *dst*, never overwriting whatever already sits at *dst*.

    ``os.rename`` rather than ``os.replace`` is load-bearing: on Windows it
    fails when the target exists, which is precisely the behavior wanted --
    this module may never destroy a file it did not itself create. POSIX
    ``os.rename`` makes no such promise (it replaces the target silently), so
    the check is made here rather than left to the OS. The gap between the
    check and the rename is real but narrow: every name renamed onto is either
    a hidden temporary carrying this run's id or a target preflight has just
    proved absent, so the only writer that could win that race is another
    process writing into the same folder mid-run.

    Args:
        src: Absolute path of the file to move.
        dst: Absolute path to move it to.

    Raises:
        FileExistsError: If *dst* already exists.
        OSError: Whatever the rename itself raises.
    """
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "target exists", dst)
    os.rename(src, dst)


def _list_names(folder: str) -> set[str]:
    """Return the exact names directly inside *folder*.

    Exact, not case-folded, and read fresh rather than remembered: a case-only
    rename is invisible to ``os.path.exists`` on Windows, so membership in this
    set is the only check that can tell whether one happened.

    Args:
        folder: The directory to list.

    Returns:
        The names as the filesystem spells them.

    Raises:
        OSError: If the folder cannot be listed.
    """
    return set(os.listdir(folder))


def _snapshot(folder: str) -> dict[str, tuple[int, float]]:
    """Return ``name -> (size, mtime)`` for every file directly inside *folder*."""
    snapshot: dict[str, tuple[int, float]] = {}
    with os.scandir(folder) as entries:
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue
            stat = entry.stat(follow_symlinks=False)
            snapshot[entry.name] = (stat.st_size, stat.st_mtime)
    return snapshot


def _fsync_directory(folder: str) -> None:
    """fsync a directory entry so a file just created inside it is durable.

    A no-op on platforms with no directory file descriptor (Windows), where
    the file's own fsync already covers its creation.

    Args:
        folder: The directory to sync.
    """
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        return
    fd = os.open(folder, directory_flag)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# --- Journal IO --------------------------------------------------------


def _op_record(op: RenameOp) -> dict[str, Any]:
    """Render one op as its journal record."""
    record: dict[str, Any] = {
        "record": "file",
        "from": op.src,
        "to": op.dst,
        "tmp": op.tmp or None,
        "kind": op.kind,
        "photo_id": op.photo_id,
    }
    if op.external:
        record["renamed_by"] = "external"
    return record


def _op_from_record(record: Mapping[str, Any]) -> RenameOp:
    """Read one op back out of its journal record."""
    return RenameOp(
        src=str(record.get("from") or ""),
        dst=str(record.get("to") or ""),
        tmp=str(record.get("tmp") or ""),
        kind=str(record.get("kind") or _IMAGE_KIND),
        photo_id=str(record.get("photo_id") or ""),
        external=record.get("renamed_by") == "external",
    )


def _write_segment(
    path: str, header: Mapping[str, Any], ops: Sequence[RenameOp], *, append: bool = False
) -> None:
    """Write a run's header and file records, then flush and fsync them.

    This is the ordering the whole module exists to guarantee: a journal
    written after a crash describes nothing, so it goes to the platter before
    the first rename, not after it. The first segment of a journal is created
    with ``x`` so an existing file is never clobbered by a run id collision;
    later segments append.

    Args:
        path: The journal path.
        header: The header record, already carrying ``status: in_progress``.
        ops: The file records for this segment.
        append: ``True`` only for an undo, which adds a segment to the journal
            it is reversing. A new run opens its journal with ``x`` so that a
            run id collision fails loudly instead of quietly appending itself
            to somebody else's account of the folder.

    Raises:
        OSError: If the journal cannot be written; the caller must not proceed.
        FileExistsError: If a new run's journal is already there.
    """
    mode = "a" if append else "x"
    lines = [json.dumps(dict(header), ensure_ascii=False)]
    lines.extend(json.dumps(_op_record(op), ensure_ascii=False) for op in ops)
    with open(path, mode, encoding="utf-8", newline="\n") as handle:
        handle.write("".join(f"{line}\n" for line in lines))
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(os.path.dirname(path) or ".")


def _append_footer(path: str, footer: Mapping[str, Any]) -> None:
    """Append a run's closing record and flush it to disk."""
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(dict(footer), ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_journal(path: str) -> Journal:
    """Parse a rename journal into its segments.

    A journal is append-only NDJSON: a header record opens a run, file records
    describe it, and a footer closes it. An undo or a resume appends its own
    records rather than rewriting, so the current state of the folder is the
    status of the last status-bearing record.

    Args:
        path: The journal file to read.

    Returns:
        The parsed :class:`Journal`.

    Raises:
        RenamePreflightError: If the file cannot be read or does not parse.
    """
    path = os.path.abspath(path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw_lines = handle.read().splitlines()
    except OSError as exc:
        raise RenamePreflightError(
            "This rename journal cannot be read.",
            f"Check the file's permissions ({exc.strerror or exc}).",
            [path],
        ) from exc

    segments: list[JournalSegment] = []
    header: dict[str, Any] | None = None
    ops: list[RenameOp] = []
    status = STATUS_IN_PROGRESS

    def _close() -> None:
        if header is not None:
            segments.append(JournalSegment(header=header, ops=tuple(ops), status=status))

    for number, line in enumerate(raw_lines, start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RenamePreflightError(
                f"This rename journal is damaged at line {number}.",
                "Check the folder by hand; photokin will not guess what it describes.",
                [path],
            ) from exc
        if not isinstance(record, dict):
            raise RenamePreflightError(
                f"This rename journal is damaged at line {number}.",
                "Check the folder by hand; photokin will not guess what it describes.",
                [path],
            )
        kind = record.get("record")
        if kind == "header":
            _close()
            header = record
            ops = []
            status = str(record.get("status") or STATUS_IN_PROGRESS)
        elif kind == "file":
            ops.append(_op_from_record(record))
        elif isinstance(record.get("status"), str):
            status = str(record["status"])
    _close()

    return Journal(path=path, folder=os.path.dirname(path), segments=tuple(segments))


def _journal_candidates(folder: str) -> list[str]:
    """Return the rename journals in *folder*, newest run id first.

    A run id starts with a UTC ISO timestamp, so the file name sorts by age on
    its own; the name is the tie-break, which only matters for two runs that
    started in the same second.
    """
    folder = os.path.abspath(folder)
    try:
        names = _list_names(folder)
    except OSError:
        return []
    prefix = f"{os.path.basename(os.path.normpath(folder))}{_JOURNAL_MARKER}"
    matched = sorted(
        (name for name in names if name.startswith(prefix) and name.endswith(_JOURNAL_SUFFIX)),
        reverse=True,
    )
    return [os.path.join(folder, name) for name in matched]


def latest_journal(folder: str, statuses: Iterable[str] | None = None) -> str | None:
    """Return the newest journal in *folder*, optionally filtered by status.

    Args:
        folder: The renamed folder.
        statuses: Statuses to accept; ``None`` accepts any.

    Returns:
        The journal's absolute path, or ``None`` when the folder has none.

    Raises:
        RenamePreflightError: If a journal in the folder cannot be parsed.
    """
    wanted = frozenset(statuses) if statuses is not None else None
    for path in _journal_candidates(folder):
        journal = read_journal(path)
        if not journal.segments:
            continue
        if wanted is None or journal.status in wanted:
            return path
    return None


# --- Plan reading ------------------------------------------------------


def _plan_folder(plan: Mapping[str, Any]) -> str:
    """Return the plan's folder as an absolute path.

    Raises:
        RenamePreflightError: If the plan names no folder.
    """
    folder = plan.get("folder")
    if not isinstance(folder, str) or not folder:
        raise RenamePreflightError(
            "This rename plan names no folder.",
            "Re-run --rename to make a fresh plan.",
        )
    return os.path.abspath(os.path.expanduser(folder))


def _entry_files(entry: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """Return ``(source path, target name, kind)`` for an entry's image and companions."""
    files: list[tuple[str, str, str]] = [
        (str(entry.get("path") or ""), str(entry.get("target") or ""), _IMAGE_KIND)
    ]
    for companion in entry.get("companions") or []:
        if not isinstance(companion, Mapping):
            continue
        files.append(
            (str(companion.get("path") or ""), str(companion.get("target") or ""), _COMPANION_KIND)
        )
    return files


def _ops_from_plan(plan: Mapping[str, Any], run_id: str) -> list[RenameOp]:
    """Build the ops for an apply run: every planned file whose name changes.

    Whether a file moves is decided by comparing its current name to its
    target, not by the entry's ``changed`` flag. The flag is the planner's
    summary for the preview; the names are what the filesystem will be asked
    to do, and this is the last gate before it is asked.

    Args:
        plan: The plan (``docs/rename-mode.md`` 6.2).
        run_id: The run id, which names the temporaries.

    Returns:
        The ops, in plan order, images before their own companions.
    """
    safe = _safe_run_id(run_id)
    ops: list[RenameOp] = []
    for entry in plan.get("entries") or []:
        if not isinstance(entry, Mapping):
            continue
        photo_id = str(entry.get("photo_id") or "")
        for source, target, kind in _entry_files(entry):
            name = os.path.basename(source)
            if not name or not target or name == target:
                continue
            ops.append(
                RenameOp(
                    src=name,
                    dst=target,
                    tmp=_tmp_name(safe, len(ops), name),
                    kind=kind,
                    photo_id=photo_id,
                )
            )
    return ops


# --- Preflight ---------------------------------------------------------


def _open_journal_problems(folder: str) -> list[str]:
    """Return a refusal line for every unfinished journal in *folder*."""
    problems: list[str] = []
    for path in _journal_candidates(folder):
        journal = read_journal(path)
        if journal.segments and journal.status in OPEN_STATUSES:
            problems.append(f"{os.path.basename(path)} is still {journal.status}")
    return problems


def _common_refusals(folder: str, plan: Mapping[str, Any]) -> list[str]:
    """Return the refusals shared by every executor entry point."""
    problems: list[str] = []
    schema = plan.get("schema_version")
    if schema != PLAN_SCHEMA_VERSION:
        problems.append(f"plan schema_version is {schema!r}, not {PLAN_SCHEMA_VERSION}")
    if not os.path.isdir(folder):
        problems.append(f"the plan's folder is gone: {folder}")
        return problems
    for message in plan.get("errors") or []:
        problems.append(f"the plan reports an error: {message}")
    problems.extend(_open_journal_problems(folder))
    return problems


def _source_problems(
    folder: str,
    source: str,
    target: str,
    record: Mapping[str, Any],
    present: Mapping[str, tuple[int, float]],
) -> list[str]:
    """Check one planned file against the folder as it is now."""
    problems: list[str] = []
    name = os.path.basename(source)
    if not name:
        problems.append("a plan entry has no path")
        return problems
    directory = os.path.dirname(source)
    if directory and os.path.abspath(directory) != folder:
        problems.append(f"outside the plan's folder: {source}")
    if not target:
        problems.append(f"no target planned for: {name}")
    elif os.path.basename(target) != target:
        problems.append(f"a target names a path, not a file: {target}")

    found = present.get(name)
    if found is None:
        problems.append(f"gone since the plan was made: {name}")
        return problems
    size, mtime = found
    planned_size = record.get("size")
    planned_mtime = record.get("mtime")
    resized = isinstance(planned_size, int) and planned_size != size
    retouched = (
        isinstance(planned_mtime, (int, float))
        and abs(float(planned_mtime) - mtime) > _MTIME_TOLERANCE_S
    )
    if resized or retouched:
        problems.append(f"changed since the plan was made: {name}")
    return problems


def preflight(plan: Mapping[str, Any]) -> list[str]:
    """Re-check a plan against the folder before anything is renamed.

    The plan records each source's size and mtime; this refuses the run if any
    source is missing or has changed since planning, if a target already exists
    and is not itself one of the sources, if two entries want the same name, or
    if a journal in the folder is still open. A stale plan is replanned, never
    patched.

    Targets are compared with ``os.path.normcase``, which is exactly the
    question being asked: on Windows a target differing from a bystander only
    in case *is* that bystander, and on POSIX it is not.

    Args:
        plan: The plan to check.

    Returns:
        One line per problem, empty when the plan is safe to apply.

    Raises:
        RenamePreflightError: If the plan names no folder, or a journal in the
            folder cannot be parsed.
    """
    folder = _plan_folder(plan)
    problems = _common_refusals(folder, plan)
    if problems and not os.path.isdir(folder):
        return problems

    present = _snapshot(folder)
    sources_ci: set[str] = set()
    targets_ci: dict[str, str] = {}
    for entry in plan.get("entries") or []:
        if not isinstance(entry, Mapping):
            problems.append("a plan entry is not an object")
            continue
        companions = {
            str(c.get("path") or ""): c
            for c in entry.get("companions") or []
            if isinstance(c, Mapping)
        }
        image_source = str(entry.get("path") or "")
        for source, target, _kind in _entry_files(entry):
            record: Mapping[str, Any] = (
                entry if source == image_source else (companions.get(source) or {})
            )
            problems.extend(_source_problems(folder, source, target, record, present))
            sources_ci.add(os.path.normcase(os.path.basename(source)))
            if not target:
                continue
            key = os.path.normcase(target)
            if key in targets_ci:
                problems.append(f"two files want the same name: {target}")
            targets_ci[key] = target

    existing_ci = {os.path.normcase(name) for name in present}
    for key, target in sorted(targets_ci.items()):
        if key in existing_ci and key not in sources_ci:
            problems.append(f"a file is already called that: {target}")
    for name in sorted(present):
        if name.startswith(_TMP_PREFIX):
            problems.append(f"a temporary from an earlier run is still here: {name}")
    return problems


def _raise_preflight(problems: Sequence[str]) -> None:
    """Raise the standard refusal for a stale plan."""
    raise RenamePreflightError(
        "The folder no longer matches the plan; nothing was renamed.",
        "Re-run --rename to make a fresh plan.",
        problems,
    )


# --- Sidecars ----------------------------------------------------------


def _sidecar_rewrites(ops: Sequence[RenameOp]) -> list[tuple[str, str]]:
    """Return ``(sidecar name, image name)`` pairs to rewrite after phase B.

    ``write_markdown_sidecar`` sets a sidecar's ``source_file`` to its image's
    basename, so a renamed image leaves every sidecar pointing at a name that
    is no longer there. The link from a companion back to its image is the
    ``photo_id`` both carry, which means this is derivable from the journal
    alone -- resume and undo need no extra records to redo or reverse it.

    Args:
        ops: The run's ops, in the direction they are being executed.

    Returns:
        Pairs of names as they will be *after* the run.
    """
    images = {op.photo_id: op.dst for op in ops if op.kind == _IMAGE_KIND and op.photo_id}
    return [
        (op.dst, images[op.photo_id])
        for op in ops
        if op.kind == _COMPANION_KIND
        and op.dst.lower().endswith(_SIDECAR_EXT)
        and op.photo_id in images
    ]


def _swap_in(path: str, tmp_path: str, payload: bytes) -> str | None:
    """Replace *path*'s bytes with *payload*, atomically, via a sibling temporary.

    Opening the file ``"wb"`` in place would truncate it before a single byte
    was written: a process killed at that instant leaves zero bytes and no
    copy anywhere, and the journal still says ``in_progress``, so a later
    ``--rename-resume`` reports ``applied`` over the loss. The new bytes
    therefore land in a temporary in the same folder, are flushed and fsynced,
    and are only then swapped in.

    ``os.replace`` is correct here and in no other place in this module. The
    "``os.rename``, never ``os.replace``" rule (``docs/rename-mode.md``
    section 2) is about *moving a user's file*, where a destination that
    already exists is a collision that must fail loudly. This is an atomic
    content swap onto a file this run is deliberately superseding, and
    atomicity is the entire point: a kill at any instant must leave either the
    old bytes or the new ones, never zero. Do not "restore consistency" by
    changing it back to an in-place write.

    Args:
        path: The file whose content is being replaced.
        tmp_path: Sibling temporary to stage the new bytes in; it must sit in
            the same folder as *path*, or the swap is not atomic.
        payload: The complete new content.

    Returns:
        A warning line when the swap did not happen, or ``None`` on success.
    """
    try:
        with open(tmp_path, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except OSError as exc:
        if os.path.lexists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError as cleanup_exc:
                logger.warning("Could not remove %s: %s", tmp_path, cleanup_exc)
        return f"sidecar not updated ({exc.strerror or exc}): {os.path.basename(path)}"
    return None


def _rewrite_sidecar(md_path: str, image_name: str, tmp_path: str) -> str | None:
    """Point a markdown sidecar's ``source_file:`` frontmatter at *image_name*.

    Only the leading frontmatter block is touched, and only its first
    ``source_file`` line; every other byte of the file is written back exactly
    as it was read, line endings included, so an undo restores the file the
    apply started from byte for byte. The value is rendered by the sidecar
    writer's own string emitter rather than a second one here, so the line is
    identical to what a re-analysis would produce.

    A sidecar that is not this shape is left alone: the rename has already
    happened by the time this runs, and refusing to finish it over a file
    photokin does not own would be the worse outcome.

    Args:
        md_path: Absolute path of the sidecar, already at its new name.
        image_name: The image's new basename.
        tmp_path: Sibling temporary the rewritten bytes are staged in before
            they are swapped onto *md_path* (see :func:`_swap_in`).

    Returns:
        A warning line when nothing was rewritten, or ``None`` on success.
    """
    try:
        with open(md_path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        return f"sidecar not updated ({exc.strerror or exc}): {os.path.basename(md_path)}"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"sidecar not updated (not UTF-8): {os.path.basename(md_path)}"

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return f"sidecar not updated (no frontmatter): {os.path.basename(md_path)}"

    for index in range(1, len(lines)):
        stripped = lines[index].strip()
        if stripped == "---":
            break
        if not stripped.startswith("source_file:"):
            continue
        terminator = lines[index][len(lines[index].rstrip("\r\n")) :]
        lines[index] = f"source_file: {_yaml_string(image_name)}{terminator}"
        return _swap_in(md_path, tmp_path, "".join(lines).encode("utf-8"))
    return f"sidecar not updated (no source_file line): {os.path.basename(md_path)}"


def _apply_sidecars(folder: str, ops: Sequence[RenameOp], run_token: str) -> list[str]:
    """Rewrite every renamed sidecar's ``source_file``; return the warnings.

    Args:
        folder: The folder every name is resolved against.
        ops: The run's ops, in the direction they were executed.
        run_token: This run's folded id, which keeps each rewrite's staging
            temporary from colliding with another run's.

    Returns:
        One warning line per sidecar that was not rewritten.
    """
    warnings: list[str] = []
    for index, (sidecar_name, image_name) in enumerate(_sidecar_rewrites(ops)):
        tmp = f"{_TMP_PREFIX}{run_token}-sidecar-{index}{_SIDECAR_EXT}"
        warning = _rewrite_sidecar(
            os.path.join(folder, sidecar_name), image_name, os.path.join(folder, tmp)
        )
        if warning is not None:
            logger.warning("%s", warning)
            warnings.append(warning)
    return warnings


# --- The two phases ----------------------------------------------------


def _reversed_op(op: RenameOp, tmp: str = "") -> RenameOp:
    """Return *op* pointing the other way, optionally through a new temporary."""
    return RenameOp(
        src=op.dst, dst=op.src, tmp=tmp, kind=op.kind, photo_id=op.photo_id, external=op.external
    )


def _move_all(folder: str, ops: Sequence[RenameOp], *, to_tmp: bool, done: list[RenameOp]) -> None:
    """Move each op one step, recording each completed move in *done*.

    *done* is the caller's, and is appended to as the loop runs rather than
    returned at the end, because the caller needs it precisely in the case
    where this raises partway through.

    Args:
        folder: The folder every name is resolved against.
        ops: The ops to move.
        to_tmp: ``True`` for phase A (source to temporary), ``False`` for
            phase B (temporary to target).
        done: Accumulator for the ops actually moved.

    Raises:
        OSError: From the first rename that fails.
    """
    for op in ops:
        if to_tmp:
            _rename(os.path.join(folder, op.src), os.path.join(folder, op.tmp))
        else:
            _rename(os.path.join(folder, op.tmp), os.path.join(folder, op.dst))
        done.append(op)


def _reverse(folder: str, staged: Sequence[RenameOp], placed: Sequence[RenameOp]) -> list[str]:
    """Put every completed step back, and report what would not go back.

    Args:
        folder: The folder every name is resolved against.
        staged: Ops moved to their temporary.
        placed: Ops moved on to their target.

    Returns:
        Absolute paths of files left out of place, temporaries first. Empty
        when the reversal was complete.
    """
    placed_names = {op.dst for op in placed}
    for op in reversed(placed):
        try:
            _rename(os.path.join(folder, op.dst), os.path.join(folder, op.tmp))
        except OSError as exc:
            logger.error("Could not move %s back to its temporary: %s", op.dst, exc)
            continue
        placed_names.discard(op.dst)
    for op in reversed(staged):
        if op.dst in placed_names:
            continue
        try:
            _rename(os.path.join(folder, op.tmp), os.path.join(folder, op.src))
        except OSError as exc:
            logger.error("Could not move %s back to %s: %s", op.tmp, op.src, exc)

    names = _list_names(folder)
    stranded = [
        os.path.join(folder, name) for name in sorted(names) if name.startswith(_TMP_PREFIX)
    ]
    stranded.extend(
        os.path.join(folder, op.dst) for op in placed if op.dst in names and op.src not in names
    )
    return stranded


def _verify(
    folder: str,
    ops: Sequence[RenameOp],
    before: Mapping[str, tuple[int, float]],
    ignore: str,
) -> list[str]:
    """Check the folder against what the run intended.

    Args:
        folder: The renamed folder.
        ops: Every op of the run, executed or external.
        before: The ``name -> (size, mtime)`` snapshot taken before phase A.
        ignore: One name exempt from the untouched check -- the journal, which
            this run is itself writing.

    Returns:
        One line per problem, empty when the folder is exactly as intended.
    """
    names = _list_names(folder)
    problems: list[str] = []
    for op in ops:
        if op.dst not in names:
            problems.append(f"not renamed: {op.src} -> {op.dst}")
        if op.tmp and op.tmp in names:
            problems.append(f"temporary left behind: {op.tmp}")

    touched = {op.src for op in ops} | {op.dst for op in ops} | {op.tmp for op in ops if op.tmp}
    after = _snapshot(folder)
    for name, (size, mtime) in before.items():
        # A name carrying this module's temporary prefix is never a bystander:
        # it is this run's own staging, or a stray one an earlier kill left,
        # which a resume legitimately consumes. Only real files are held to
        # the untouched check.
        if name in touched or name == ignore or name.startswith(_TMP_PREFIX):
            continue
        current = after.get(name)
        if current is None:
            problems.append(f"vanished during the rename: {name}")
        elif current[0] != size or abs(current[1] - mtime) > _MTIME_TOLERANCE_S:
            problems.append(f"changed during the rename: {name}")
    return problems


def _execute(
    folder: str,
    ops: Sequence[RenameOp],
    *,
    journal: str,
    run_token: str,
    resume_from: Sequence[RenameOp] | None = None,
    already_placed: Sequence[RenameOp] | None = None,
) -> tuple[str, list[str], list[str]]:
    """Run both phases, rewrite sidecars, verify, and roll back on any failure.

    Every run goes through the temporaries, not only the ones with a chain in
    them: it is what makes a gap-closing renumber collision-free, what makes a
    case-only rename work on a case-insensitive filesystem, and what makes
    every failure state one the journal already describes.

    Args:
        folder: The renamed folder.
        ops: Every op of the run, external ones included (they are recorded
            and verified, never moved).
        journal: The journal path, exempted from the untouched check.
        run_token: This run's folded id, which names the sidecar rewrites'
            staging temporaries.
        resume_from: Ops already sitting at their temporary when this started,
            which skip phase A. ``None`` for a fresh run.
        already_placed: Ops an interrupted run had already put at their target
            before this attempt began. They are not moved forward, but they
            *are* reversed by a rollback, because a rollback's contract is the
            whole segment, not this attempt's share of it. ``None`` for a
            fresh run.

    Returns:
        ``(status, stranded, warnings)``.
    """
    staged_before = list(resume_from or [])
    placed_before = list(already_placed or [])
    settled = set(staged_before) | set(placed_before)
    pending_a = [op for op in ops if not op.external and op not in settled]

    before = _snapshot(folder)
    staged: list[RenameOp] = list(staged_before)
    placed: list[RenameOp] = list(placed_before)
    warnings: list[str] = []
    sidecars_rewritten = False
    try:
        _move_all(folder, pending_a, to_tmp=True, done=staged)
        _move_all(folder, staged, to_tmp=False, done=placed)
        warnings = _apply_sidecars(folder, ops, run_token)
        sidecars_rewritten = True
        problems = _verify(folder, ops, before, os.path.basename(journal))
        if problems:
            raise _VerifyFailed(problems)
    except (OSError, _VerifyFailed) as exc:
        logger.error("Rename failed, putting every file back: %s", exc)
        # An op the interrupted run already placed reverses exactly as one this
        # attempt placed does, so it belongs to both legs of the walk back:
        # ``placed`` moves it off its target, the staged sequence moves it on
        # to its source. Leave it out and ``rolled_back`` would be a lie --
        # docs/rename-contract.md defines that status as "the folder is exactly
        # as it started", and it would still be holding half of a run nobody
        # asked for, with no open journal left to resume or undo it from.
        stranded = _reverse(folder, [*staged, *placed_before], placed)
        if stranded:
            # A reversal that could not finish is not a state to keep editing
            # files in: every remaining decision belongs to a person.
            return STATUS_NEEDS_ATTENTION, stranded, warnings
        if sidecars_rewritten:
            # Verification can fail after the sidecars have been rewritten, so
            # they have to follow the files back or the rollback would leave
            # every sidecar pointing at a name that is no longer on disk.
            warnings.extend(_apply_sidecars(folder, [_reversed_op(op) for op in ops], run_token))
        return STATUS_ROLLED_BACK, stranded, warnings
    return STATUS_APPLIED, [], warnings


# --- Public entry points -----------------------------------------------


def _counts(plan: Mapping[str, Any], ops: Sequence[RenameOp]) -> tuple[int, int, int, int]:
    """Return ``(renamed, companions, unchanged, left_behind)`` for a report."""
    renamed = sum(1 for op in ops if op.kind == _IMAGE_KIND)
    companions = sum(1 for op in ops if op.kind == _COMPANION_KIND)
    entries = [e for e in plan.get("entries") or [] if isinstance(e, Mapping)]
    unchanged = len(entries) - renamed
    return renamed, companions, unchanged, len(plan.get("left_behind") or [])


def _header(
    run_id: str,
    folder: str,
    plan: Mapping[str, Any],
    mode: str,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a journal header record."""
    header: dict[str, Any] = {
        "record": "header",
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "run_id": run_id,
        "folder": folder,
        "prefix_template": plan.get("prefix_template"),
        "digits": plan.get("digits"),
        "photokin_version": plan.get("photokin_version"),
        "mode": mode,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": STATUS_IN_PROGRESS,
    }
    if extra:
        header.update(extra)
    return header


def _plan_run_id(run_id: str, plan_run_id: str) -> dict[str, str] | None:
    """Return the header addition naming the plan, when this run renamed itself."""
    return {"plan_run_id": plan_run_id} if plan_run_id and plan_run_id != run_id else None


def _footer(
    status: str, report_counts: tuple[int, int, int, int], stranded: Sequence[str]
) -> dict[str, Any]:
    """Build a journal footer record."""
    renamed, companions, unchanged, left_behind = report_counts
    footer: dict[str, Any] = {
        "record": "footer",
        "status": status,
        "renamed": renamed,
        "companions": companions,
        "unchanged": unchanged,
        "left_behind": left_behind,
        "closed_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if stranded:
        footer["stranded"] = list(stranded)
    return footer


def apply_plan(plan: Mapping[str, Any], *, dry_run: bool = False) -> ApplyReport:
    """Apply a rename plan to its folder.

    Preflight first (:func:`preflight`), then the journal, then phase A into
    hidden temporaries, then phase B onto the targets, then the sidecar
    rewrite, then verification against a fresh listing. Any failure in either
    phase or in verification reverses the completed steps.

    Args:
        plan: The plan to apply (``docs/rename-mode.md`` 6.2).
        dry_run: Preflight and count, write nothing.

    Returns:
        The run's :class:`ApplyReport`.

    Raises:
        RenamePreflightError: If the folder no longer matches the plan, or a
            journal in it is still open. Nothing has been touched.
    """
    folder = _plan_folder(plan)
    problems = preflight(plan)
    if problems:
        _raise_preflight(problems)

    plan_run_id = str(plan.get("run_id") or "")
    run_id, journal = _fresh_journal(folder, plan_run_id)
    safe = _safe_run_id(run_id)
    ops = _ops_from_plan(plan, run_id)
    counts = _counts(plan, ops)
    if dry_run:
        return ApplyReport(STATUS_WOULD_APPLY, None, *counts)
    if not ops:
        return ApplyReport(STATUS_NOTHING_TO_DO, None, *counts)

    header = _header(run_id, folder, plan, MODE_APPLY, _plan_run_id(run_id, plan_run_id))
    _write_segment(journal, header, ops)

    status, stranded, warnings = _execute(folder, ops, journal=journal, run_token=safe)
    _append_footer(journal, _footer(status, counts, stranded))
    return ApplyReport(
        status, journal, *counts, stranded=tuple(stranded), warnings=tuple(warnings)
    )


def finish_plan(plan: Mapping[str, Any]) -> ApplyReport:
    """Bring companions along for images someone else has already renamed.

    The executor with the images taken as done: each entry is required to have
    its target in place and its old name gone, and only then are its
    companions renamed and its sidecar rewritten. Entries whose image is not
    renamed yet are reported and skipped, so the command is safe to run again;
    so is an entry whose companions are already in place from an earlier run.

    Args:
        plan: The plan whose images a catalog application has applied.

    Returns:
        The run's :class:`ApplyReport`, with ``skipped`` naming every entry
        that was passed over.

    Raises:
        RenamePreflightError: If the folder is gone, the plan carries errors,
            or a journal in the folder is still open.
    """
    folder = _plan_folder(plan)
    problems = _common_refusals(folder, plan)
    if problems:
        _raise_preflight(problems)

    names = _list_names(folder)
    plan_run_id = str(plan.get("run_id") or "")
    run_id, journal = _fresh_journal(folder, plan_run_id)
    safe = _safe_run_id(run_id)
    ops: list[RenameOp] = []
    skipped: list[str] = []
    renamed_images = 0

    for entry in plan.get("entries") or []:
        if not isinstance(entry, Mapping):
            continue
        photo_id = str(entry.get("photo_id") or "")
        image_files = _entry_files(entry)
        source, target, _kind = image_files[0]
        name = os.path.basename(source)
        if not name or not target:
            skipped.append("an entry has no path or no target")
            continue
        if name != target and not (target in names and name not in names):
            skipped.append(f"image not renamed yet: {name}")
            continue

        entry_ops: list[RenameOp] = []
        for companion_source, companion_target, _companion_kind in image_files[1:]:
            companion_name = os.path.basename(companion_source)
            if not companion_name or not companion_target:
                continue
            if companion_name == companion_target:
                continue
            if companion_target in names:
                # Already carried across by an earlier run -- unless the old
                # name is still there too, in which case two files claim one
                # target and this is not a state to rename into.
                if companion_name in names:
                    skipped.append(f"a file is already called that: {companion_target}")
                continue
            if companion_name not in names:
                skipped.append(f"companion gone: {companion_name}")
                continue
            entry_ops.append(
                RenameOp(
                    src=companion_name,
                    dst=companion_target,
                    tmp=_tmp_name(safe, len(ops) + len(entry_ops), companion_name),
                    kind=_COMPANION_KIND,
                    photo_id=photo_id,
                )
            )
        if not entry_ops:
            continue
        # The image record carries no temporary: it is here so undo and the
        # sidecar rewrite know both of its names, never to be executed.
        ops.append(
            RenameOp(
                src=name,
                dst=target,
                tmp="",
                kind=_IMAGE_KIND,
                photo_id=photo_id,
                external=True,
            )
        )
        ops.extend(entry_ops)
        renamed_images += 1

    counts = (0, sum(1 for op in ops if op.kind == _COMPANION_KIND), renamed_images, 0)
    if not ops:
        return ApplyReport(STATUS_NOTHING_TO_DO, None, *counts, skipped=tuple(skipped))

    header = _header(run_id, folder, plan, MODE_FINISH, _plan_run_id(run_id, plan_run_id))
    _write_segment(journal, header, ops)
    status, stranded, warnings = _execute(folder, ops, journal=journal, run_token=safe)
    _append_footer(journal, _footer(status, counts, stranded))
    return ApplyReport(
        status,
        journal,
        *counts,
        skipped=tuple(skipped),
        stranded=tuple(stranded),
        warnings=tuple(warnings),
    )


def resume_run(journal_path: str) -> ApplyReport:
    """Finish a run left ``in_progress`` or ``needs_attention``, forward.

    Each op is classified by where its file actually is -- at its temporary,
    still at its source, or already at its target -- and the run continues
    from there through the same two phases. A file that is genuinely in two
    places at once, or a destination held by something the journal cannot
    account for, stops the resume rather than being guessed at.

    Args:
        journal_path: The journal to finish.

    Returns:
        The run's :class:`ApplyReport`.

    Raises:
        RenamePreflightError: If the journal is not open, or the folder is in
            a state the journal cannot explain.
    """
    journal = read_journal(journal_path)
    segment = journal.last
    if segment.status not in OPEN_STATUSES:
        raise RenamePreflightError(
            f"This rename run is already {segment.status}; there is nothing to resume.",
            "Use --rename-undo to reverse it.",
            [journal.path],
        )

    folder = journal.folder
    names = _list_names(folder)
    ops = list(segment.ops)
    staged: list[RenameOp] = []
    placed: list[RenameOp] = []
    pending: list[RenameOp] = []
    problems: list[str] = []
    for op in ops:
        if op.external:
            if op.dst not in names:
                problems.append(f"the image was not renamed after all: {op.dst}")
            continue
        at_src, at_tmp, at_dst = op.src in names, op.tmp in names, op.dst in names
        # The temporary is decisive. It carries this run's id and belongs to
        # this one op, so a file sitting at it is unambiguously staged and
        # ``at_dst`` says nothing about it -- it says some *other* file holds
        # the destination name, which during phase A is routinely a later op's
        # source that has not been staged yet. That is the ordinary shape of an
        # upward chain, not an anomaly. Only the source being back as well is a
        # contradiction the journal cannot explain.
        if at_tmp:
            if at_src:
                problems.append(f"in two places at once: {op.src}")
            else:
                staged.append(op)
        elif at_src:
            pending.append(op)
        elif at_dst:
            placed.append(op)
        else:
            problems.append(f"gone: {op.src}")

    # A destination held by another op that is itself still waiting at its
    # source is a chain phase A will unwind; anything else holding it is a
    # collision this run must not rename into.
    waiting = {op.src for op in pending}
    for op in pending:
        if op.dst != op.src and op.dst in names and op.dst not in waiting:
            problems.append(f"a file is already called that: {op.dst}")
    if problems:
        raise RenamePreflightError(
            "This folder is not in a state the rename journal describes.",
            "Check it by hand; photokin will not guess.",
            problems,
        )

    # The whole segment goes to the executor, not just the ops this attempt has
    # left to move. Two things follow from that, and both are the point of it:
    # a rollback reverses the ops the interrupted run had already placed as
    # well, so ``rolled_back`` keeps meaning "the folder is as it was" and the
    # run never closes on a state nobody asked for; and the sidecar pass runs
    # over every entry, so one whose image and .md both finished before the
    # crash still gets its ``source_file`` line pointed at the new name.
    # ``_rewrite_sidecar`` is idempotent, so redoing a correct one costs
    # nothing.
    status, stranded, warnings = _execute(
        folder,
        ops,
        journal=journal.path,
        run_token=_safe_run_id(segment.run_id) or "resume",
        resume_from=staged,
        already_placed=placed,
    )
    if status == STATUS_APPLIED and segment.mode == MODE_UNDO:
        status = STATUS_UNDONE
    counts = (
        sum(1 for op in ops if op.kind == _IMAGE_KIND and not op.external),
        sum(1 for op in ops if op.kind == _COMPANION_KIND),
        0,
        0,
    )
    _append_footer(journal.path, _footer(status, counts, stranded))
    return ApplyReport(
        status, journal.path, *counts, stranded=tuple(stranded), warnings=tuple(warnings)
    )


def _reverse_ops(
    segment: JournalSegment, names: set[str], run_id: str
) -> tuple[list[RenameOp], list[str]]:
    """Build the reverse of a completed segment, and report what cannot go back.

    An ordinary run reverses whole: every file must still be at its target.
    A ``--rename-finish`` run reverses companions only -- the catalog that
    renamed the images owns undoing them -- so each image must already be back
    at its old name, and the companions of one that is not are reported and
    left alone.

    Args:
        segment: The completed segment to reverse.
        names: The folder's current names.
        run_id: The undo run's id, which names the new temporaries.

    Returns:
        ``(ops, skipped)``.
    """
    safe = _safe_run_id(run_id)
    external = segment.mode == MODE_FINISH
    blocked: set[str] = set()
    skipped: list[str] = []

    for op in segment.ops:
        if not op.external:
            continue
        if op.src in names and op.dst not in names:
            continue
        blocked.add(op.photo_id)
        skipped.append(f"image not put back yet: {op.dst}")

    ops: list[RenameOp] = []
    for op in reversed(segment.ops):
        if op.photo_id in blocked:
            if not op.external:
                skipped.append(f"left alone: {op.dst}")
            continue
        if op.external:
            ops.append(_reversed_op(op))
            continue
        if op.dst not in names:
            skipped.append(f"not where the journal left it: {op.dst}")
            continue
        if op.src in names:
            skipped.append(f"something is already called that: {op.src}")
            continue
        ops.append(_reversed_op(op, _tmp_name(safe, len(ops), op.dst)))
    if not external and skipped:
        raise RenamePreflightError(
            "The folder has moved on since this rename; it cannot be undone.",
            "Check it by hand; photokin will not guess.",
            skipped,
        )
    return ops, skipped


def undo_run(journal_path: str) -> ApplyReport:
    """Reverse an applied run, appending ``status: undone`` to its journal.

    The reverse plan is built from the journal and run through the same
    executor, so an undo is two-phase, journalled and verified exactly as the
    apply was. The journal is appended to rather than rewritten: the file
    stays a truthful, ordered account of everything that happened to the
    folder.

    Args:
        journal_path: The journal to reverse.

    Returns:
        The run's :class:`ApplyReport`.

    Raises:
        RenamePreflightError: If the run is unfinished, already undone, or the
            files are no longer where it left them.
    """
    journal = read_journal(journal_path)
    segment = journal.last
    if segment.status in OPEN_STATUSES:
        raise RenamePreflightError(
            f"This rename run is still {segment.status}; it cannot be undone yet.",
            "Finish it with --rename-resume first.",
            [journal.path],
        )
    if segment.status != STATUS_APPLIED:
        raise RenamePreflightError(
            f"This rename run is already {segment.status}.",
            "There is nothing to undo.",
            [journal.path],
        )

    folder = journal.folder
    run_id = make_run_id()
    safe = _safe_run_id(run_id)
    ops, skipped = _reverse_ops(segment, _list_names(folder), run_id)
    counts = (
        sum(1 for op in ops if op.kind == _IMAGE_KIND and not op.external),
        sum(1 for op in ops if op.kind == _COMPANION_KIND),
        0,
        0,
    )
    if not any(not op.external for op in ops):
        return ApplyReport(STATUS_NOTHING_TO_DO, journal.path, *counts, skipped=tuple(skipped))

    header = _header(
        run_id,
        folder,
        segment.header,
        MODE_UNDO,
        {"undoes": segment.run_id},
    )
    _write_segment(journal.path, header, ops, append=True)
    status, stranded, warnings = _execute(folder, ops, journal=journal.path, run_token=safe)
    if status == STATUS_APPLIED:
        status = STATUS_UNDONE
    _append_footer(journal.path, _footer(status, counts, stranded))
    return ApplyReport(
        status,
        journal.path,
        *counts,
        skipped=tuple(skipped),
        stranded=tuple(stranded),
        warnings=tuple(warnings),
    )
