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
valid when someone moves or renames the whole folder between the apply and the
undo, and why a journal is looked for by its marker rather than by the folder
name baked into it. The header's ``folder`` is provenance, not the address
operations are resolved against. "Sits in" means where the file really is:
:func:`read_journal` resolves the path it is handed, so a journal reached
through a symlink is read, appended to, and *operated on* at the one end --
never read through the link and closed there while the files move somewhere
else.

A segment says everything about itself that recovery needs, because recovery
happens after the process that wrote it is gone: the format it is in, how many
file records belong to it, and whether phase A finished. The last of those is
what makes a swap recoverable at all -- two names trading places look the same
before the run and after it, and nothing in the folder can say which.

Code map:
- RenamePreflightError  refusal raised before anything on disk is touched
- RenameOp              one file's from/to/tmp triple
- ApplyReport           what a run did, its counts, and its exit code
- JournalSegment        one run's header, file records and final status
- Journal               a parsed journal file: its segments and current status
- _mark_staged          record that phase A finished, before phase B starts
- _locate_moves         where every file of a run is now, read as one mapping
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
import ntpath
import os
import re
import stat
from collections import deque
from collections.abc import Container, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import islice
from typing import Any

from .changeset import make_run_id
from .doc_sidecar import _yaml_string
from .utils import casefold_filename, paths_are_same_file

logger = logging.getLogger(__name__)

#: Bumped whenever a journal record's shape changes in a way a reader could
#: care about, on the same rule ``changeset.SCHEMA_VERSION`` follows. A journal
#: is read back by this module's own undo and resume, so a bump means older
#: journals must still parse or must be refused by name.
#:
#: Version 2 made a segment self-describing: its header declares how many file
#: records follow it, and a ``phase`` record marks the point where phase A
#: finished. Both are what recovery reads to tell a segment that was fully
#: written from one a power cut cut short, and a run that has staged every file
#: from one that has not started -- neither of which version 1 could say.
JOURNAL_SCHEMA_VERSION = 2

#: The journal formats this executor can read. Refusing anything else is the
#: point: a journal written to a shape this code does not know is read with the
#: wrong rules, and every one of those rules decides where a file is.
_READABLE_JOURNAL_SCHEMAS = frozenset({JOURNAL_SCHEMA_VERSION})

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

#: How much of the folder's own name a journal name may spend, in bytes. A
#: journal name is four things -- the folder's name, the marker, the run id and
#: the suffix -- inside one 255-byte NAME_MAX, and only the first of them can
#: give: the marker and the folder-derived part are what discovery matches on,
#: and the run id is what ties the journal to its plan and to the changeset. So
#: the folder's name is bounded here and the rest is kept whole. Unbounded, a
#: 220-byte folder name spends the entire budget on its own and every otherwise
#: valid apply in that folder fails with ENAMETOOLONG before it starts.
_MAX_JOURNAL_STEM_BYTES = 96

#: The longest a single filename component may be on the filesystems photokin
#: runs on, in bytes. It bounds the temporaries as well as the journal: a
#: temporary that runs past it fails phase A on a folder whose every planned
#: rename was otherwise legal.
_MAX_NAME_BYTES = 255

#: The record a segment appends when phase A is complete, and the value its
#: ``phase`` field carries. Everything the run renames is then sitting at its
#: temporary, which is what tells a later recovery which side of a swap the
#: folder is on -- see :func:`_locate_moves`.
_PHASE_RECORD = "phase"
_PHASE_STAGED = "staged"

_IMAGE_KIND = "image"
_COMPANION_KIND = "companion"

#: mtime comparison tolerance, in seconds. The value is carried through JSON as
#: a float and read back off a fresh ``stat``, so an exact compare is one
#: rounding away from calling an untouched file modified.
_MTIME_TOLERANCE_S = 1e-6

#: One backslash escape inside a double-quoted YAML scalar, the shape
#: ``doc_sidecar._yaml_string`` writes.
_YAML_ESCAPE_RE = re.compile(r"\\(.)")

#: How much of a journal's tail is read at a time when the last record has to
#: be checked for termination. The journal is one record per renamed file, so
#: on the mass renames this feature exists for it is the largest thing in the
#: folder that is not an image, and every footer and every appended segment
#: asks that one question of it. Walking backward a chunk at a time answers it
#: in bounded memory, out of the bytes the answer is actually in.
_TAIL_CHUNK_BYTES = 65536

#: The one byte that ends a record. NDJSON has exactly one delimiter, and the
#: reader has to split on exactly that byte and no other: ``str.splitlines``
#: also breaks on the vertical tab, the form feed, the file, group and
#: record separators, and the three Unicode breaks json.dumps leaves whole
#: inside a string -- so a filename or a template carrying one would be split
#: through the middle of its own record and the journal read as damaged.
_RECORD_SEPARATOR = b"\n"
_RECORD_SEPARATOR_TEXT = _RECORD_SEPARATOR.decode()

#: Characters that are illegal in a Windows filename but legal in a run id
#: (which is an ISO timestamp, so it always contains ``:``).
_RUN_ID_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")

#: One planned source found on disk: the folder's own spelling of its name,
#: and the size and mtime that file has right now.
_Located = tuple[str, int, float]


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
            temporaries first -- and, when the rollback itself completed but
            could not put a transcript's ``source_file`` line back, that
            transcript. Non-empty only for ``needs_attention``.
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
    """One run recorded in a journal: its header, its files, its final status.

    Attributes:
        header: The header record that opened the run.
        ops: Its file records, in the order they were journalled.
        status: The status of its last status-bearing record.
        staged: Phase A finished: every op this executor performs was sitting
            at its temporary before phase B began. Recovery reads this to know
            which side of a swap the folder is on (:func:`_locate_moves`).
        partial: The run closed having deliberately left some of its work
            undone -- a ``--rename-undo`` of a ``--rename-finish`` journal
            whose catalog has not put every image back yet. The segment stays
            open so the rest can be retried.
    """

    header: dict[str, Any]
    ops: tuple[RenameOp, ...]
    status: str
    staged: bool = False
    partial: bool = False

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
        path: The journal's own path, with every link in it resolved.
        folder: The folder it describes, which is the folder that resolved
            path sits in.
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


def _journal_prefix(folder: str) -> str:
    """Return the name a journal written in *folder* today begins with.

    Provenance, not an address: it says which folder the run was started in,
    and discovery deliberately does not match on it (:func:`_journal_candidates`),
    since the folder can be renamed afterwards. A folder with no basename of
    its own -- a filesystem root, or a Windows drive root -- stands in
    ``folder``.

    The folder's name is spent against ``_MAX_JOURNAL_STEM_BYTES`` and cut
    when it runs over, so that a folder named at nearly the length a name may
    be can still be renamed: unbounded, the name would spend the whole
    255-byte budget on its own and every otherwise valid apply in that folder
    would fail with ENAMETOOLONG before it started.

    Args:
        folder: The folder being renamed.

    Returns:
        ``<foldername>_rename-``, the folder's name cut to the byte budget.
    """
    name = os.path.basename(os.path.normpath(os.path.abspath(folder))) or "folder"
    encoded = name.encode("utf-8", "surrogateescape")
    if len(encoded) > _MAX_JOURNAL_STEM_BYTES:
        # Cut on bytes, then let the decoder drop whatever partial character
        # the cut landed inside; a name of nothing but undecodable bytes comes
        # back empty, which is the case a folder with no name of its own is.
        name = encoded[:_MAX_JOURNAL_STEM_BYTES].decode("utf-8", "ignore") or "folder"
    return f"{name}{_JOURNAL_MARKER}"


def journal_path_for(folder: str, run_id: str) -> str:
    """Return the journal path for a run: ``<foldername>_rename-<run_id>.ndjson``.

    Args:
        folder: The folder being renamed; the journal lands inside it.
        run_id: The run's id.

    Returns:
        An absolute path inside *folder*.
    """
    folder = os.path.abspath(folder)
    return os.path.join(
        folder, f"{_journal_prefix(folder)}{_safe_run_id(run_id)}{_JOURNAL_SUFFIX}"
    )


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
    """Return the hidden temporary name for one file of a run.

    The extension is carried so the staged file is still the kind of file it
    was, and it is also the only part of the name that may be cut: the prefix
    says whose temporary this is, the run id says which run, and the index is
    what makes it unique. A source name is allowed to be 255 bytes on its own,
    so an unbounded extension puts the temporary past ``NAME_MAX`` and fails
    phase A of a run whose every planned name was legal. Cutting the extension
    cannot make two temporaries collide, because what distinguishes them
    already sits in front of it.

    Args:
        safe_run_id: The run's folded id.
        index: The op's position in the run.
        source_name: The name of the file being staged.

    Returns:
        A hidden name of at most :data:`_MAX_NAME_BYTES` bytes.
    """
    stem = f"{_TMP_PREFIX}{safe_run_id}-{index}"
    extension = os.path.splitext(source_name)[1]
    budget = _MAX_NAME_BYTES - len(stem.encode("utf-8", "surrogateescape"))
    encoded = extension.encode("utf-8", "surrogateescape")
    if len(encoded) > budget:
        # Cut on bytes, then let the decoder drop whatever partial character
        # the cut landed inside, exactly as the journal name is cut.
        extension = encoded[: max(budget, 0)].decode("utf-8", "ignore")
    return f"{stem}{extension}"


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


def _scan(folder: str) -> tuple[set[str], dict[str, tuple[int, float]]]:
    """Return every name directly inside *folder*, and the stats of its files.

    Two answers from one directory walk, because preflight needs both and they
    answer different questions: whether a planned source is the file the plan
    measured is a question about files, while whether a planned target's name
    is free is a question about *names* -- a directory or a dangling symlink
    called like a target holds that name just as firmly as a file does.

    Args:
        folder: The directory to walk.

    Returns:
        ``(every name, name -> (size, mtime) for the files, a symlink to a
        file included)``.

    Raises:
        OSError: If the folder cannot be walked.
    """
    names: set[str] = set()
    files: dict[str, tuple[int, float]] = {}
    with os.scandir(folder) as entries:
        for entry in entries:
            names.add(entry.name)
            # Symlinks are followed here because they are followed where the
            # plan was made: ``utils.list_folder_images`` and cli's
            # ``_list_all_files`` both ask ``is_file()``, and the size and
            # mtime the plan carries are an ``os.stat`` of what the link
            # points at. Reading them the other way round here would report a
            # symlinked image the preview had just offered to rename as "gone
            # since the plan was made" -- and since one such refusal refuses
            # the whole run, a single link would take the folder's rename
            # away. A link to a directory, and a link to nothing, are not
            # files to any of the three.
            if not entry.is_file():
                continue
            info = entry.stat()
            files[entry.name] = (info.st_size, info.st_mtime)
    return names, files


def _snapshot(folder: str) -> dict[str, tuple[int, float]]:
    """Return ``name -> (size, mtime)`` for every file directly inside *folder*."""
    return _scan(folder)[1]


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


def _checked_op(path: str, number: int, record: Mapping[str, Any]) -> RenameOp:
    """Read one file record, refusing any name that is not a bare filename.

    A journal's names are resolved against the folder the journal sits in, so a
    path-valued one walks a rename out of that folder -- the same class of
    damage the ``--rename-finish`` target check refuses, and refused the same
    way (:func:`_is_bare_filename`), since a journal can be damaged or
    hand-edited exactly as a plan can.

    Args:
        path: The journal being read, for the message.
        number: The record's line number, for the message.
        record: The file record.

    Returns:
        The op the record describes.

    Raises:
        RenamePreflightError: If any of its three names is a path.
    """
    op = _op_from_record(record)
    for name in (op.src, op.dst, op.tmp):
        if name and not _is_bare_filename(name):
            raise RenamePreflightError(
                f"This rename journal names a path, not a file, at line {number}.",
                "Check the folder by hand; photokin will not guess what it describes.",
                [name, path],
            )
    return op


def _check_journal_schema(path: str, header: Mapping[str, Any]) -> None:
    """Refuse a journal segment written in a format this executor cannot read.

    Args:
        path: The journal being read.
        header: Its header record.

    Raises:
        RenamePreflightError: If the header declares an unreadable version.
    """
    version = header.get("schema_version")
    if version in _READABLE_JOURNAL_SCHEMAS:
        return
    raise RenamePreflightError(
        f"This rename journal is version {version!r}; "
        f"photokin reads version {JOURNAL_SCHEMA_VERSION}.",
        "Use the photokin that wrote it, or check the folder by hand.",
        [path],
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

    The header declares how many file records follow it, and that number is
    what makes a segment self-describing. A power cut can persist the header
    and only a prefix of the records -- one write, one fsync, but a filesystem
    is free to make part of it durable -- and without the count that prefix
    reads as a complete run. ``--rename-resume`` would then apply only the
    files that happened to land, verify only that subset, and close the run
    ``applied``: a two-file rename reported as a success with one file
    renamed. The count is written here, in the same call that writes the
    records it counts, so the two can never disagree.

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
    if append:
        _drop_torn_tail(path)
    mode = "a" if append else "x"
    lines = [json.dumps({**dict(header), "ops": len(ops)}, ensure_ascii=False)]
    lines.extend(json.dumps(_op_record(op), ensure_ascii=False) for op in ops)
    with open(path, mode, encoding="utf-8", newline="\n") as handle:
        handle.write("".join(f"{line}\n" for line in lines))
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(os.path.dirname(path) or ".")


def _drop_torn_tail(path: str) -> None:
    """Cut a never-finished last record off *path* before appending to it.

    :func:`read_journal` ignores a last line with no newline on the end of it,
    because a machine that died mid-append is the only thing that writes one.
    A record appended behind those bytes would splice itself onto them and
    make one complete, unparseable line out of two -- damage a reader has to
    refuse, which would cost the folder the resume or undo this same tolerance
    just bought it. What is dropped here is exactly the bytes no reader ever
    accepted.

    Only the tail is read. Whether the last record was terminated is a
    question about the last byte, and where that record began is a question
    about the bytes just before it, so both are answered by seeking to the end
    and walking back in ``_TAIL_CHUNK_BYTES`` steps -- in bounded memory, and
    at a cost that does not grow with the journal, which is the file here that
    grows fastest. A journal with no line break in it at all is one
    unterminated record and goes entirely, as it always did.

    Args:
        path: The journal about to be appended to.

    Raises:
        OSError: If the file cannot be read or trimmed; the caller must not
            append to a journal it could not seal.
    """
    with open(path, "rb") as handle:
        end = handle.seek(0, os.SEEK_END)
        if not end:
            return
        handle.seek(end - 1)
        if handle.read(1) == _RECORD_SEPARATOR:
            return
        cut = 0
        offset = end - 1
        while offset > 0:
            start = max(0, offset - _TAIL_CHUNK_BYTES)
            handle.seek(start)
            chunk = handle.read(offset - start)
            index = chunk.rfind(_RECORD_SEPARATOR)
            if index >= 0:
                cut = start + index + 1
                break
            offset = start
    logger.warning("Dropping the unterminated last record of %s; it was never finished.", path)
    with open(path, "r+b") as handle:
        handle.truncate(cut)
        handle.flush()
        os.fsync(handle.fileno())


def _append_record(path: str, record: Mapping[str, Any]) -> None:
    """Append one record to a journal and flush it to disk.

    Args:
        path: The journal to append to.
        record: The record to write.

    Raises:
        OSError: If the record cannot be written or synced.
    """
    _drop_torn_tail(path)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(dict(record), ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _append_footer(path: str, footer: Mapping[str, Any]) -> None:
    """Append a run's closing record and flush it to disk."""
    _append_record(path, footer)


def _mark_staged(path: str) -> None:
    """Record that phase A finished, before phase B moves anything.

    This is the record that makes a swap recoverable. Two names that trade
    places -- ``x-001 -> x-002`` beside ``x-002 -> x-001``, which an explicit
    manifest ``order`` can ask for -- are both on disk before the run and both
    on disk after it, so nothing in the folder distinguishes the two states and
    :func:`_locate_moves` has nothing to eliminate against. Without this record
    a resume after a crash between phase B and the footer performs the swap a
    second time, putting the original contents back under the new names, and
    then reports ``applied``; an undo, reading the same ambiguity the other
    way, refuses a run that in fact succeeded.

    The journal is append-only and already fsynced at the points that matter,
    so the durable progress marker costs one more record and one more sync per
    run, once, between the two phases.

    Args:
        path: The journal of the run that has just finished phase A.

    Raises:
        OSError: If the record cannot be written; the caller must not begin
            phase B, since nothing would be able to tell afterwards that it
            had.
    """
    _append_record(path, {"record": _PHASE_RECORD, "phase": _PHASE_STAGED})


def _terminated_prefix(raw: bytes) -> bytes:
    """Return *raw* up to and including its last record separator.

    The cut is made on bytes, before anything is decoded, and that ordering is
    the point. A crash tears the file mid-record, and with a Unicode filename
    or template that tear lands inside a multi-byte character as often as not
    -- so decoding first raises ``UnicodeDecodeError`` over the whole journal,
    including every fsynced record in front of the tear, and takes away the
    resume and the undo the journal exists to make possible.

    Args:
        raw: The journal's bytes.

    Returns:
        The bytes of every record that was finished being written.
    """
    if not raw or raw.endswith(_RECORD_SEPARATOR):
        return raw
    index = raw.rfind(_RECORD_SEPARATOR)
    return raw[: index + 1] if index >= 0 else b""


def read_journal(path: str) -> Journal:
    """Parse a rename journal into its segments.

    A journal is append-only NDJSON: a header record opens a run, file records
    describe it, and a footer closes it. An undo or a resume appends its own
    records rather than rewriting, so the current state of the folder is the
    status of the last status-bearing record.

    A last record with no newline on the end of it is dropped rather than
    refused, and it is dropped as *bytes* (:func:`_terminated_prefix`) before
    anything is decoded. That is the one damage a crash can do to this file:
    the machine died while a record was being appended, so the bytes that made
    it down are a record nobody finished writing, while every record before it
    was fsynced and still describes a recoverable folder. Refusing the file for
    it would take resume and undo away at exactly the moment the journal exists
    for. A *terminated* record that does not parse is different -- that is
    damage from somewhere else, and it stays an error.

    Three things a segment must say about itself are checked here, because
    every one of them decides where a file is: the format it was written in
    (``schema_version``), how many file records it has (``ops``, so a header
    followed by a truncated run of records is not read as a complete run), and
    that each of those records names a file rather than a path (so a damaged or
    hand-edited journal cannot walk a rename out of the folder).

    The path is resolved before anything is read off it, because where the
    journal sits is how the folder it describes is derived (:class:`Journal`)
    and the same path is what a footer is later appended to. Reached through a
    symlink, those two ends come apart: the records are read and the footer
    appended through the link, into the real journal in the folder its records
    are about, while the run would move files in the link's own folder --
    closing one folder's account of itself over another folder's work, and
    permanently disabling the recovery of the folder that needed it. Resolving
    keeps a deliberately linked journal working and keeps both ends on the
    folder the records belong to.

    Args:
        path: The journal file to read; a link to one is resolved first.

    Returns:
        The parsed :class:`Journal`, keyed to the resolved path.

    Raises:
        RenamePreflightError: If the file cannot be read, if a complete record
            in it does not parse, if it is in a format this photokin does not
            read, or if a segment is short of the records it declares.
    """
    path = os.path.realpath(path)
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise RenamePreflightError(
            "This rename journal cannot be read.",
            f"Check the file's permissions ({exc.strerror or exc}).",
            [path],
        ) from exc

    terminated = _terminated_prefix(raw)
    if terminated != raw:
        logger.warning(
            "Ignoring the unterminated last record of %s; it was never finished.", path
        )
    try:
        text = terminated.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RenamePreflightError(
            "This rename journal is damaged: it is not UTF-8.",
            "Check the folder by hand; photokin will not guess what it describes.",
            [path],
        ) from exc

    segments: list[JournalSegment] = []
    header: dict[str, Any] | None = None
    ops: list[RenameOp] = []
    status = STATUS_IN_PROGRESS
    staged = False
    partial = False

    def _close() -> None:
        if header is None:
            return
        declared = header.get("ops")
        advice = "Check the folder by hand; photokin will not guess what it describes."
        if not isinstance(declared, int) or isinstance(declared, bool):
            raise RenamePreflightError(
                "This rename journal is damaged: a run does not say how many files it covers.",
                advice,
                [path],
            )
        if declared != len(ops):
            raise RenamePreflightError(
                f"This rename journal is incomplete: a run recorded {len(ops)} "
                f"of the {declared} files it declares.",
                advice,
                [path],
            )
        segments.append(
            JournalSegment(
                header=header, ops=tuple(ops), status=status, staged=staged, partial=partial
            )
        )

    def _damaged(number: int) -> RenamePreflightError:
        return RenamePreflightError(
            f"This rename journal is damaged at line {number}.",
            "Check the folder by hand; photokin will not guess what it describes.",
            [path],
        )

    for number, line in enumerate(text.split(_RECORD_SEPARATOR_TEXT), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise _damaged(number) from exc
        if not isinstance(record, dict):
            raise _damaged(number)
        kind = record.get("record")
        if kind == "header":
            _close()
            _check_journal_schema(path, record)
            header = record
            ops = []
            status = str(record.get("status") or STATUS_IN_PROGRESS)
            staged = False
            partial = False
        elif kind == "file":
            ops.append(_checked_op(path, number, record))
        elif kind == _PHASE_RECORD:
            staged = staged or record.get("phase") == _PHASE_STAGED
        elif isinstance(record.get("status"), str):
            status = str(record["status"])
            partial = record.get("partial") is True
    _close()

    return Journal(path=path, folder=os.path.dirname(path), segments=tuple(segments))


def _journal_run_token(name: str) -> str:
    """Return the run token *name* carries as a journal, or ``""`` if it is none.

    Args:
        name: A file name from the folder's listing.

    Returns:
        Everything between the last marker and the suffix.
    """
    if not name.endswith(_JOURNAL_SUFFIX):
        return ""
    stem = name[: -len(_JOURNAL_SUFFIX)]
    marker = stem.rfind(_JOURNAL_MARKER)
    return stem[marker + len(_JOURNAL_MARKER) :] if marker >= 0 else ""


def _journal_candidates(folder: str) -> list[str]:
    """Return the rename journals in *folder*, newest run id first.

    A journal is recognized by its marker and its suffix, not by the folder
    name baked into the front of it. That name is provenance: it was the
    folder's name when the run started, and it stops being it the moment
    somebody renames the folder -- which is a thing people do to an archive
    folder, and which this module otherwise supports outright, since a journal
    records file names and never paths (see the module docstring). Matching on
    it would mean a renamed folder loses its own resume, its own undo, and the
    guard that stops a second run trampling a half-finished one, with the
    journal sitting right there.

    A run id starts with a UTC ISO timestamp, so the token after the marker
    sorts by age on its own; the whole name is the tie-break, which only
    matters for two runs that started in the same second.

    Args:
        folder: The folder to look in.

    Returns:
        Absolute paths, newest run first.
    """
    folder = os.path.abspath(folder)
    try:
        names = _list_names(folder)
    except OSError:
        return []
    matched = sorted(
        ((token, name) for name in names if (token := _journal_run_token(name))), reverse=True
    )
    return [os.path.join(folder, name) for _token, name in matched]


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


def _ops_from_plan(
    plan: Mapping[str, Any], run_id: str, located: Mapping[str, _Located]
) -> list[RenameOp]:
    """Build the ops for an apply run: every planned file whose name changes.

    Whether a file moves is decided by comparing its current name to its
    target, not by the entry's ``changed`` flag. The flag is the planner's
    summary for the preview; the names are what the filesystem will be asked
    to do, and this is the last gate before it is asked. "Its current name" is
    the folder's spelling of it (:func:`_locate_sources`), not the plan's,
    since that is the name the rename has to be given.

    Args:
        plan: The plan (``docs/rename-mode.md`` 6.2).
        run_id: The run id, which names the temporaries.
        located: ``planned name -> (disk name, size, mtime)`` for the folder.

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
            found = located.get(os.path.basename(source))
            name = found[0] if found else os.path.basename(source)
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


# --- Where every file of a run is now ----------------------------------

#: One file's move, as the three names it can be found under: where it
#: started, where it ends up, and the hidden temporary in between (empty for a
#: move this executor does not make itself).
_Move = tuple[str, str, str]

_AT_TMP = "tmp"
_AT_SOURCE = "source"
_AT_TARGET = "target"
_GONE = "gone"
_UNDECIDED = ""


def _moves_of(ops: Iterable[RenameOp]) -> list[_Move]:
    """Return the ``(source, target, temporary)`` triples of *ops*."""
    return [(op.src, op.dst, op.tmp) for op in ops]


def _locate_moves(
    moves: Sequence[_Move], names: Container[str], *, staged: bool = False
) -> tuple[list[str], set[str]]:
    """Say where each file of a run is now, reading the run as a whole.

    Asked about one move on its own the question has no honest answer for the
    very thing rename mode exists to do. In a gap-closing renumber --
    ``file004 -> file003`` beside ``file005 -> file004`` -- both of a move's
    names are routinely occupied at once, because the other move puts a file
    there, and a check that reads a name in isolation calls that a collision.
    It is the plan working. So the folder is read against the run's complete
    source/target mapping instead: a name is only unaccounted for when no
    other move explains the file sitting under it.

    The temporary decides on its own -- it carries the run's id and belongs to
    one move, so a file under it is that move's whatever else is on disk.
    Everything after that is elimination: a move with only one of its two
    names free is where that name says it is, and each such decision releases
    the other moves that could have claimed the name it just took, which walks
    a chain in from whichever end is unambiguous.

    A move still undecided when that runs out is a cycle -- ``x-001 -> x-002``
    beside ``x-002 -> x-001``, which an explicit manifest ``order`` can ask for
    -- and a cycle is the one shape the folder cannot answer for itself: both
    names are present before the run and both are present after it, so there is
    nothing to eliminate against and elimination is not the wrong tool, it is
    an empty one. What answers it is the journal, which recorded whether phase
    A finished (:func:`_mark_staged`): *staged* means every move this executor
    performs had reached its temporary, so one whose temporary is gone has been
    placed, and *not staged* means no move had left its source yet. Deduced
    from a durable record either way, never guessed.

    A move with no temporary is one this executor does not perform -- an image
    a catalog renamed -- and the phase marker says nothing about those, so an
    undecided one reads as still at its source. That is the only reading under
    which a reversed chain can be undone at all; a cycle among such images is
    the one shape where it is a guess, and the guess costs a companion put back
    beside an image that is where it started under either reading.

    Args:
        moves: The run's moves, in the order they were journalled.
        names: The folder's current names, as the filesystem spells them.
        staged: The journal records that phase A of this segment finished.
            ``False`` for a mapping that is not a journalled run's, where the
            question is asked of the plan and nothing has been staged.

    Returns:
        One location per move, in the same order (``_AT_TMP``, ``_AT_SOURCE``,
        ``_AT_TARGET`` or ``_GONE``), and the names on disk those locations
        account for.
    """
    locations = [_UNDECIDED] * len(moves)
    claimed: set[str] = set()
    holders: dict[str, list[int]] = {}
    queue: deque[int] = deque()
    for index, (source, target, tmp) in enumerate(moves):
        holders.setdefault(source, []).append(index)
        holders.setdefault(target, []).append(index)
        if tmp and tmp in names:
            locations[index] = _AT_TMP
            claimed.add(tmp)
        else:
            queue.append(index)

    while queue:
        index = queue.popleft()
        if locations[index] != _UNDECIDED:
            continue
        source, target, _tmp = moves[index]
        at_source = source in names and source not in claimed
        at_target = target in names and target not in claimed
        if at_source and at_target:
            continue
        if not at_source and not at_target:
            locations[index] = _GONE
            continue
        held = source if at_source else target
        locations[index] = _AT_SOURCE if at_source else _AT_TARGET
        claimed.add(held)
        queue.extend(other for other in holders[held] if locations[other] == _UNDECIDED)

    for index, location in enumerate(locations):
        if location != _UNDECIDED:
            continue
        source, target, tmp = moves[index]
        placed = staged and bool(tmp)
        locations[index] = _AT_TARGET if placed else _AT_SOURCE
        claimed.add(target if placed else source)
    return locations, claimed


# --- Preflight ---------------------------------------------------------


def _is_bare_filename(name: str) -> bool:
    """Return whether *name* names a file and nothing about where it sits.

    ``os.path.basename`` answers this differently depending on where photokin
    is running: on POSIX it reads ``..\\outside.md``, ``C:\\outside.md`` and
    ``C:outside.md`` as ordinary filenames, because none of those separators
    mean anything there. A target is a name this executor will rename onto
    inside one folder, and a rename never crosses a folder boundary
    (``docs/rename-mode.md`` section 11), so the answer may not depend on the
    running OS. ``ntpath`` knows every separator POSIX knows and the ones it
    does not, and ``.``/``..`` are spelled out because ``basename`` returns
    them unchanged.

    Args:
        name: The candidate filename.

    Returns:
        ``True`` when *name* is a bare filename.
    """
    return bool(name) and name not in (".", "..") and ntpath.basename(name) == name


def _target_refusals(plan: Mapping[str, Any]) -> list[str]:
    """Refuse every target that is not a bare filename.

    This runs before the journal is opened and before anything moves, for
    every entry point that takes a plan, because a companion moved out of the
    folder and then interrupted is a file a resume cannot even see: resume
    lists the folder, and the file is no longer in it. A hand-edited or
    damaged plan is the case; the answer is to refuse the plan, not to repair
    it.

    Args:
        plan: The plan to check.

    Returns:
        One line per target that names a path rather than a file.
    """
    problems: list[str] = []
    for entry in plan.get("entries") or []:
        if not isinstance(entry, Mapping):
            continue
        for _source, target, _kind in _entry_files(entry):
            if target and not _is_bare_filename(target):
                problems.append(f"a target names a path, not a file: {target}")
    return problems


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
    problems.extend(_target_refusals(plan))
    problems.extend(_open_journal_problems(folder))
    return problems


def _disk_spelling(
    folder: str,
    name: str,
    present: Mapping[str, tuple[int, float]],
    folded: Mapping[str, str],
) -> str | None:
    """Return the folder's own spelling of *name*, or ``None`` if it is not there.

    Args:
        folder: The folder both spellings are resolved against.
        name: The plan's spelling of the file.
        present: The folder's current listing.
        folded: Collision key -> the folder's spelling, built from *present*.

    Returns:
        The name as the filesystem spells it.
    """
    if name in present:
        return name
    candidate = folded.get(casefold_filename(name))
    if candidate is None or not os.path.exists(os.path.join(folder, name)):
        # Nothing on disk answers to the plan's spelling, which on a
        # case-sensitive filesystem is the whole answer: two names differing
        # only in case are two files there, and the planned one is gone. The
        # fold only nominates a candidate; identity below is what decides.
        return None
    same = paths_are_same_file(os.path.join(folder, name), os.path.join(folder, candidate))
    return candidate if same else None


def _locate_sources(
    folder: str, present: Mapping[str, tuple[int, float]], plan: Mapping[str, Any]
) -> dict[str, _Located]:
    """Find every planned source in the folder, under the folder's own spelling.

    A plan -- or the manifest a wrapper built it from -- may spell an existing
    file in a case the disk does not, and on a case-insensitive volume that
    spelling names exactly that file; the planner's own identity check has
    already accepted it. An exact lookup in the listing would call it gone and
    refuse a run whose files are all right there. So a name the listing does
    not carry verbatim is looked up by filesystem identity, and what comes
    back is the disk's spelling: the name photokin renames is the name on
    disk, never the plan's idea of it.

    Args:
        folder: The plan's folder.
        present: ``name -> (size, mtime)`` for the folder as it is now.
        plan: The plan whose sources are being located.

    Returns:
        ``planned name -> (disk name, size, mtime)``, carrying only the
        sources that are actually there.
    """
    folded: dict[str, str] = {}
    for name in present:
        folded.setdefault(casefold_filename(name), name)
    located: dict[str, _Located] = {}
    for entry in plan.get("entries") or []:
        if not isinstance(entry, Mapping):
            continue
        for source, _target, _kind in _entry_files(entry):
            name = os.path.basename(source)
            if not name or name in located:
                continue
            disk_name = _disk_spelling(folder, name, present, folded)
            if disk_name is not None:
                located[name] = (disk_name, *present[disk_name])
    return located


def _source_problems(
    folder: str,
    source: str,
    target: str,
    record: Mapping[str, Any],
    located: Mapping[str, _Located],
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

    found = located.get(name)
    if found is None:
        problems.append(f"gone since the plan was made: {name}")
        return problems
    _disk_name, size, mtime = found
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

    Targets are compared with :func:`~photokin.utils.casefold_filename` on
    every platform, never with ``os.path.normcase``: whether two planned names
    collide is a property of the plan -- of where the files might later live --
    and not of the OS that happens to be running photokin, and ``normcase`` is
    a no-op on POSIX, so a check built on it catches nothing on Linux. Whether
    a planned source is a file already on disk is the other question entirely,
    and :func:`_locate_sources` answers it by filesystem identity.

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

    listing, present = _scan(folder)
    located = _locate_sources(folder, present, plan)
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
            problems.extend(_source_problems(folder, source, target, record, located))
            sources_ci.add(casefold_filename(os.path.basename(source)))
            if not target:
                continue
            key = casefold_filename(target)
            if key in targets_ci:
                problems.append(f"two files want the same name: {target}")
            targets_ci[key] = target

    # Occupancy is asked of the whole listing, not of the file snapshot: a
    # directory or a symlink named like a target is not a file, so a check
    # made against the snapshot cannot see it, and the run would get as far as
    # phase B before ``_rename`` noticed and rolled everything back.
    existing_ci = {casefold_filename(name) for name in listing}
    for key, target in sorted(targets_ci.items()):
        if key in existing_ci and key not in sources_ci:
            problems.append(f"a file is already called that: {target}")
    for name in sorted(listing):
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


def _sidecar_rewrites(ops: Sequence[RenameOp]) -> list[tuple[str, str, str]]:
    """Return ``(sidecar name, image name before, image name after)`` triples.

    ``write_markdown_sidecar`` sets a sidecar's ``source_file`` to its image's
    basename, so a renamed image leaves every sidecar pointing at a name that
    is no longer there. The link from a companion back to its image is the
    ``photo_id`` both carry, which means this is derivable from the journal
    alone -- resume and undo need no extra records to redo or reverse it.

    Both of the image's names are carried, because the rewrite is only allowed
    to touch a link that names the image being renamed
    (:func:`_rewrite_sidecar`).

    Args:
        ops: The run's ops, in the direction they are being executed.

    Returns:
        Triples whose sidecar name is the name it carries *after* the run.
    """
    images = {
        op.photo_id: (op.src, op.dst) for op in ops if op.kind == _IMAGE_KIND and op.photo_id
    }
    return [
        (op.dst, *images[op.photo_id])
        for op in ops
        if op.kind == _COMPANION_KIND
        and op.dst.lower().endswith(_SIDECAR_EXT)
        and op.photo_id in images
    ]


def _carry_protection(original: str, staged: str) -> None:
    """Give *staged* the protection *original* carries, before it replaces it.

    The staged file is a new inode created under the process umask, so a
    transcript deliberately stored ``0600`` comes back ``0644`` and every
    extended attribute the original carried -- a POSIX ACL among them -- is
    dropped at the swap. Content somebody restricted on purpose would quietly
    become readable by everyone, which is a worse outcome than not rewriting
    the ``source_file`` line at all. The mode is the part every platform has
    and is not optional here: a file whose protection could not be set is not
    swapped in, and the caller reports it. Ownership and extended attributes
    exist only where the OS offers them and are carried as far as this process
    is permitted to, since failing at those loses metadata rather than
    exposing anything.

    Args:
        original: The file being replaced.
        staged: The temporary that will replace it.

    Raises:
        OSError: If *original* cannot be stat'd or *staged*'s mode cannot be
            set; the caller must not swap in a file it could not protect.
    """
    info = os.stat(original)
    os.chmod(staged, stat.S_IMODE(info.st_mode))
    chown = getattr(os, "chown", None)
    if chown is not None:
        try:
            chown(staged, info.st_uid, info.st_gid)
        except OSError as exc:
            logger.debug("Could not carry the ownership of %s: %s", original, exc)
    listxattr = getattr(os, "listxattr", None)
    getxattr = getattr(os, "getxattr", None)
    setxattr = getattr(os, "setxattr", None)
    if listxattr is None or getxattr is None or setxattr is None:
        return
    try:
        attributes = listxattr(original)
    except OSError as exc:
        logger.debug("Could not read the extended attributes of %s: %s", original, exc)
        return
    for name in attributes:
        try:
            setxattr(staged, name, getxattr(original, name))
        except OSError as exc:
            logger.debug("Could not carry %s of %s: %s", name, original, exc)


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

    A symlinked transcript is rewritten *through* the link, not over it: the
    swap is made onto the file the link resolves to, and the staging temporary
    moves to that file's own folder so the replace stays atomic and stays on
    one filesystem. Replacing the link itself would destroy it -- the name
    would come back a plain file holding the new bytes, and whatever the link
    pointed at would keep the old ones, still naming an image that no longer
    exists. Somebody who linked a transcript did it on purpose.

    Args:
        path: The file whose content is being replaced.
        tmp_path: Temporary to stage the new bytes in. Its name is used as
            given; its folder is *path*'s, or the resolved file's when *path*
            is a link, since the swap is only atomic within one folder.
        payload: The complete new content.

    Returns:
        A warning line when the swap did not happen, or ``None`` on success.
    """
    resolved = os.path.realpath(path)
    if resolved != path:
        tmp_path = os.path.join(os.path.dirname(resolved), os.path.basename(tmp_path))
    try:
        with open(tmp_path, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _carry_protection(resolved, tmp_path)
        os.replace(tmp_path, resolved)
    except OSError as exc:
        if os.path.lexists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError as cleanup_exc:
                logger.warning("Could not remove %s: %s", tmp_path, cleanup_exc)
        return f"sidecar not updated ({exc.strerror or exc}): {os.path.basename(path)}"
    return None


def _sidecar_link(line: str) -> str:
    """Return the file name a ``source_file:`` frontmatter line points at.

    ``_yaml_string`` always double-quotes and backslash-escapes what it emits,
    so that spelling is unwrapped exactly; a hand-written single-quoted or
    bare value is read the way YAML reads it. Anything this cannot make sense
    of comes back as the raw text, which simply will not match the image being
    renamed -- and not matching is the safe answer (:func:`_rewrite_sidecar`).

    Args:
        line: The frontmatter line, stripped of surrounding whitespace.

    Returns:
        The file name the line carries.
    """
    value = line.partition(":")[2].strip()
    if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        return _YAML_ESCAPE_RE.sub(r"\1", inner) if value[0] == '"' else inner.replace("''", "'")
    return value


def _rewrite_sidecar(md_path: str, was: str, now: str, tmp_path: str) -> str | None:
    """Point a markdown sidecar's ``source_file:`` frontmatter at *now*.

    Only the leading frontmatter block is touched, and only its first
    ``source_file`` line; every other byte of the file is written back exactly
    as it was read, line endings included, so an undo restores the file the
    apply started from byte for byte. The value is rendered by the sidecar
    writer's own string emitter rather than a second one here, so the line is
    identical to what a re-analysis would produce.

    The line is rewritten only when it names the image this op renamed. A
    same-stem ``.md`` whose ``source_file`` points somewhere else is either
    stale or deliberate, and either way that value is the only record of what
    it pointed at: overwriting it destroys that, and an undo would then write
    the op's old image name rather than what was there. Journalling the old
    value instead was the alternative, and it is the weaker one -- it helps
    only a reader holding the journal, while leaving the value alone keeps the
    file itself true. A link already naming the new image is left alone too,
    which is what lets a resume redo the whole segment's sidecars.

    A sidecar that is not this shape is left alone for the same reason: the
    rename has already happened by the time this runs, and refusing to finish
    it over a file photokin does not own would be the worse outcome.

    Args:
        md_path: Absolute path of the sidecar, already at its new name.
        was: The image's basename before this run renamed it.
        now: The image's basename after it.
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
        current = casefold_filename(_sidecar_link(stripped))
        if current == casefold_filename(now):
            return None
        if current != casefold_filename(was):
            return f"sidecar not updated (it names another file): {os.path.basename(md_path)}"
        terminator = lines[index][len(lines[index].rstrip("\r\n")) :]
        lines[index] = f"source_file: {_yaml_string(now)}{terminator}"
        return _swap_in(md_path, tmp_path, "".join(lines).encode("utf-8"))
    return f"sidecar not updated (no source_file line): {os.path.basename(md_path)}"


def _apply_sidecars(
    folder: str, ops: Sequence[RenameOp], run_token: str
) -> list[tuple[str, str | None]]:
    """Rewrite every renamed sidecar's ``source_file``; report each outcome.

    One result per rewrite attempted, in a fixed order, because the caller
    compares the forward pass against the reversing one position by position:
    the same ops filtered by the same rule give the same sequence, and what
    tells a sidecar that could not be *restored* from one that was never
    rewritten in the first place is which of the two passes it failed in.

    Args:
        folder: The folder every name is resolved against.
        ops: The run's ops, in the direction they were executed.
        run_token: This run's folded id, which keeps each rewrite's staging
            temporary from colliding with another run's.

    Returns:
        ``(sidecar name, warning or None)`` per sidecar this pass touched.
    """
    results: list[tuple[str, str | None]] = []
    for index, (sidecar_name, was, now) in enumerate(_sidecar_rewrites(ops)):
        tmp = f"{_TMP_PREFIX}{run_token}-sidecar-{index}{_SIDECAR_EXT}"
        warning = _rewrite_sidecar(
            os.path.join(folder, sidecar_name), was, now, os.path.join(folder, tmp)
        )
        if warning is not None:
            logger.warning("%s", warning)
        results.append((sidecar_name, warning))
    return results


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
    forward: list[tuple[str, str | None]] = []
    outcome: tuple[str, list[str], list[str]]
    try:
        _move_all(folder, pending_a, to_tmp=True, done=staged)
        # Phase A's directory entries go to the platter BEFORE the journal says
        # phase A finished, and the order is the whole point. A filesystem is
        # free to make the marker's own fsync durable while the renames it
        # describes are still in cache, so writing the marker first lets a
        # power cut leave a journal claiming ``staged`` over files sitting at
        # their sources. For a cycle that is silent and destructive: both names
        # are on disk either way, so a resume reading the marker deduces every
        # move is already at its target, moves nothing, and closes the run
        # ``applied`` over contents that were never swapped.
        _fsync_directory(folder)
        # Phase A is complete and durably said to be complete before phase B
        # moves anything, so that a crash inside phase B leaves a folder whose
        # state can be deduced rather than guessed at (:func:`_mark_staged`).
        _mark_staged(journal)
        _move_all(folder, staged, to_tmp=False, done=placed)
        forward = _apply_sidecars(folder, ops, run_token)
        warnings = [warning for _name, warning in forward if warning]
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
        if not stranded and forward:
            # Verification can fail after the sidecars have been rewritten, so
            # they have to follow the files back or the rollback would leave
            # every sidecar pointing at a name that is not on disk.
            back = _apply_sidecars(folder, [_reversed_op(op) for op in ops], run_token)
            warnings.extend(warning for _name, warning in back if warning)
            # A sidecar this run rewrote and could not put back is a transcript
            # naming an image that is not there any more, which ``rolled_back``
            # -- "the folder is exactly as it started" -- would report as a
            # success. One the forward pass never rewrote is a different thing:
            # nothing happened to it in either direction, so its warning is the
            # same one the apply already reported and it is not a failed
            # restoration. Position tells the two apart; the passes filter the
            # same ops by the same rule.
            failed_forward = {index for index, (_n, w) in enumerate(forward) if w}
            stranded = [
                os.path.join(folder, name)
                for index, (name, warning) in enumerate(back)
                if warning and index not in failed_forward
            ]
        if stranded:
            # A reversal that could not finish is not a state to keep editing
            # files in: every remaining decision belongs to a person.
            outcome = (STATUS_NEEDS_ATTENTION, stranded, warnings)
        else:
            outcome = (STATUS_ROLLED_BACK, stranded, warnings)
    else:
        outcome = (STATUS_APPLIED, [], warnings)

    # The renamed directory entries go to the platter here, before the caller
    # appends any closed-status footer to the journal. A filesystem is free to
    # make a file's own fsync durable without the unrelated directory updates
    # around it, so without this a power cut can leave a journal closed
    # ``applied`` over a folder that lost some or all of phase B -- a state
    # resume refuses (the journal is closed) and undo misreads (it assumes the
    # targets are there). The rollback paths are synced for the same reason:
    # ``rolled_back`` is a closed status too.
    _fsync_directory(folder)
    return outcome


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
    ops = _ops_from_plan(plan, run_id, _locate_sources(folder, _snapshot(folder), plan))
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
    its target in place and nothing unaccounted-for sitting under its old
    name, and only then are its companions renamed and its sidecar rewritten.
    Entries whose image is not renamed yet are reported and skipped, so the
    command is safe to run again; so is an entry whose companions are already
    in place from an earlier run.

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

    entries = [entry for entry in plan.get("entries") or [] if isinstance(entry, Mapping)]
    files_by_entry = [_entry_files(entry) for entry in entries]
    # Every question below -- has the catalog renamed this image yet, is this
    # companion already across, is that name held by a stranger -- is asked of
    # the plan's complete source/target mapping (:func:`_locate_moves`), never
    # of one file on its own. In a gap-closing renumber a file's old name is
    # routinely still on disk because it is the name the *next* entry was
    # renamed to; asked file by file, the first entry of every chain is
    # reported as not renamed and its companions are left behind.
    locations, accounted = _locate_moves(
        [
            (os.path.basename(source), target, "")
            for files in files_by_entry
            for source, target, _kind in files
        ],
        names,
    )
    placements = iter(locations)

    for entry, image_files in zip(entries, files_by_entry, strict=True):
        placed_at = list(islice(placements, len(image_files)))
        photo_id = str(entry.get("photo_id") or "")
        source, target, _kind = image_files[0]
        name = os.path.basename(source)
        if not name or not target:
            skipped.append("an entry has no path or no target")
            continue
        if name != target and placed_at[0] != _AT_TARGET:
            skipped.append(f"image not renamed yet: {name}")
            continue

        entry_ops: list[RenameOp] = []
        for (companion_source, companion_target, _companion_kind), location in zip(
            image_files[1:], placed_at[1:], strict=True
        ):
            companion_name = os.path.basename(companion_source)
            if not companion_name or not companion_target:
                continue
            if companion_name == companion_target or location == _AT_TARGET:
                # Already where it belongs, from this plan's own earlier run.
                continue
            if location == _GONE:
                skipped.append(f"companion gone: {companion_name}")
                continue
            if companion_target in names and companion_target not in accounted:
                # Two files claim one name and only one of them is in this
                # plan: not a state to rename into.
                skipped.append(f"a file is already called that: {companion_target}")
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
    if segment.partial:
        # An undo that stopped part way is open on purpose, not interrupted.
        # Every file it recorded is already back; what is left is the work it
        # declined to record, which only a fresh undo can build.
        raise RenamePreflightError(
            "This undo stopped part way; there is nothing to finish forward.",
            "Run --rename-undo again once the images are back at their old names.",
            [journal.path],
        )

    folder = journal.folder
    names = _list_names(folder)
    ops = list(segment.ops)
    # Where each file is is decided from the segment as a whole
    # (:func:`_locate_moves`), never op by op. Op by op, a run killed between
    # its last rename and its footer reads as unresumable: every op of a
    # gap-closing chain is sitting at its target, and every one of those
    # targets is also the *source* name of the op before it, so each op looks
    # both moved and unmoved at once and the collision check refuses the only
    # way out of the folder.
    locations, accounted = _locate_moves(_moves_of(ops), names, staged=segment.staged)
    staged: list[RenameOp] = []
    placed: list[RenameOp] = []
    pending: list[RenameOp] = []
    problems: list[str] = []
    for op, location in zip(ops, locations, strict=True):
        if op.external:
            # Whether the catalog's own rename is still in place is the same
            # chain-wide question the undo asks (:func:`_reverse_ops`): the
            # image's target name being on disk does not by itself mean this
            # image is the file under it, since in a renumber that name is
            # also the *next* image's source.
            if location != _AT_TARGET:
                problems.append(f"the image was not renamed after all: {op.dst}")
            continue
        if location == _AT_TMP:
            # The temporary is decisive: it carries this run's id and belongs
            # to this one op. A file still under the op's source name is then
            # a second copy only if nothing else in the segment accounts for
            # it -- in a chain that name is a neighbour's target or its
            # not-yet-staged source.
            if op.src in names and op.src not in accounted:
                problems.append(f"in two places at once: {op.src}")
            staged.append(op)
        elif location == _AT_SOURCE:
            pending.append(op)
        elif location == _AT_TARGET:
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


def _without_blocked(
    ops: list[RenameOp], names: Container[str], skipped: list[str]
) -> list[RenameOp]:
    """Drop every reversal whose destination is held by something else.

    A name a reversal moves onto is free when nothing is under it, or when
    what is under it is another reversal of this same run, which is about to
    move away -- every old name in a gap-closing renumber is some other file's
    new name, so anything stricter refuses the shape this feature exists to
    produce. What is left over is a file nobody in this run accounts for, and
    renaming onto it is the one thing this module may never do.

    Asking it once is not enough: dropping a reversal means the name it was
    going to vacate stays held, which can block another. The question is
    re-asked until the answer stops changing.

    Args:
        ops: The reversals built so far, in the direction they will run.
        names: The folder's current names.
        skipped: Accumulator for what this refuses, in the caller's words.

    Returns:
        The reversals that may go ahead.
    """
    while True:
        freed = {op.src for op in ops if not op.external}
        blocked = {
            op for op in ops if not op.external and op.dst in names and op.dst not in freed
        }
        if not blocked:
            return ops
        skipped.extend(f"something is already called that: {op.dst}" for op in ops if op in blocked)
        ops = [op for op in ops if op not in blocked]


def _reverse_ops(
    segment: JournalSegment, names: set[str], run_id: str
) -> tuple[list[RenameOp], list[str], bool]:
    """Build the reverse of a completed segment, and report what cannot go back.

    An ordinary run reverses whole: every file must still be at its target.
    A ``--rename-finish`` run reverses companions only -- the catalog that
    renamed the images owns undoing them -- so each image must already be back
    at its old name, and the companions of one that is not are reported and
    left alone.

    Both questions -- has the catalog put this image back, and is this
    companion still at its target -- are decided from the segment as a whole
    (:func:`_locate_moves`). Read op by op either is wrong for every
    gap-closing renumber, which is what this feature is for: after ``file004
    -> file003`` and ``file005 -> file004`` have been reversed by the catalog,
    a file called ``file004`` is on disk because it is the *first* image's
    restored source, and reading it as something else holding the second
    image's old name blocks that image, strands its companion under a name the
    first companion is about to be moved onto, and closes the journal
    ``rolled_back`` -- which is a status undo will not run on, so the undo can
    never be retried at all.

    Args:
        segment: The completed segment to reverse.
        names: The folder's current names.
        run_id: The undo run's id, which names the new temporaries.

    Returns:
        ``(ops, skipped, deferred)``. *deferred* says a later undo would still
        have something to do -- an image the catalog has not put back, or a
        name a stranger is holding -- as against a companion that is simply
        home already, which nothing is waiting on.
    """
    safe = _safe_run_id(run_id)
    external = segment.mode == MODE_FINISH
    locations, _accounted = _locate_moves(
        _moves_of(segment.ops), names, staged=segment.staged
    )
    blocked: set[str] = set()
    skipped: list[str] = []

    for op, location in zip(segment.ops, locations, strict=True):
        if not op.external:
            continue
        if location == _AT_SOURCE:
            continue
        blocked.add(op.photo_id)
        skipped.append(f"image not put back yet: {op.dst}")

    ops: list[RenameOp] = []
    for op, location in reversed(list(zip(segment.ops, locations, strict=True))):
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
        if location != _AT_TARGET:
            skipped.append(f"something is already called that: {op.src}")
            continue
        ops.append(_reversed_op(op, _tmp_name(safe, len(ops), op.dst)))
    kept = _without_blocked(ops, names, skipped)
    if not external and skipped:
        raise RenamePreflightError(
            "The folder has moved on since this rename; it cannot be undone.",
            "Check it by hand; photokin will not guess.",
            skipped,
        )
    return kept, skipped, bool(blocked) or len(kept) != len(ops)


def _segment_to_undo(journal: Journal) -> JournalSegment:
    """Return the segment an undo should reverse.

    Ordinarily the last one. But an undo of a ``--rename-finish`` journal
    reverses only the entries whose images the catalog has already put back,
    and leaves the rest for later (:func:`_reverse_ops`) -- so that segment is
    closed ``in_progress``, marked partial, precisely so the rest stays
    retryable. Retrying it means reversing the original segment again against
    the folder as it is now: the companions already put back are read off the
    disk as done, and the ones whose image has since come home are reversed.
    Undoing the partial segment itself instead would put back what was just
    put back.

    Args:
        journal: The journal being undone.

    Returns:
        The segment to reverse.
    """
    segment = journal.last
    if segment.mode != MODE_UNDO or not segment.partial:
        return segment
    undoes = segment.header.get("undoes")
    for earlier in reversed(journal.segments[:-1]):
        if earlier.run_id == undoes:
            return earlier
    return segment


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
    segment = _segment_to_undo(journal)
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
    ops, skipped, deferred = _reverse_ops(segment, _list_names(folder), run_id)
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
    # An undo that reversed what it could and left the rest for the catalog is
    # not a finished undo, and a footer saying it is closes the journal on a
    # state nobody asked for: closed, it can never be run again, so the
    # companions still sitting under their new names have no way home. It is
    # recorded open and marked partial instead, and a later --rename-undo picks
    # up where this one stopped (:func:`_segment_to_undo`).
    partial = deferred and status == STATUS_UNDONE
    footer = _footer(STATUS_IN_PROGRESS if partial else status, counts, stranded)
    if partial:
        footer["partial"] = True
    _append_footer(journal.path, footer)
    return ApplyReport(
        status,
        journal.path,
        *counts,
        skipped=tuple(skipped),
        stranded=tuple(stranded),
        warnings=tuple(warnings),
    )
