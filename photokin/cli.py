"""
photokin.cli
==================

Thin command-line interface for the photo archiver.

Responsibilities:
- Collect CLI/interactive parameters.
- Detect what the input is, and refuse anything the run cannot use.
- Build a Config object.
- Install the package-wide stderr log handler.
- Invoke the library entrypoints.
- Write outputs per flags (NDJSON/JSON/sidecars) and/or print to stdout.

One input token, given positionally or through the ``--folder``/``--manifest``
aliases, becomes a :class:`ResolvedInput`; every mode then runs the same path.
This is the module the Lightroom plugin launches (``python -m photokin.cli``);
its flags and the manifest/NDJSON behavior are part of the plugin contract, so
treat changes here as contract changes. Analysis results go to stdout; every
diagnostic goes to stderr through the logger. Message wording lives in
``photokin.cli_messages``; this module decides when each one fires.

Code map:
- load_json                 read a UTF-8 JSON file (matches Lightroom's writes)
- _configure_logging        attach the stderr handler to the "photokin" logger
- _exit_with_usage_error    report a problem/``Try:`` pair and exit 2
- _interactive_prompt       prompt for image paths when no args are given
- ResolvedInput             the one input, classified and addressed
- _resolve_input            pick the input source, classify or assert its type
- _validate_folder_input    refuse an unreadable or imageless folder
- _load_manifest_input      read and validate a manifest before anything is paid for
- _WriteBundleMember        one flag ``-w`` expands to, and how each message names it
- _flag_spelling            an argparse destination as the user types it
- _resolve_write_bundle     expand ``-w`` and reject flags that contradict it
- _derive_changeset_path    ``dirname(--output-file or input)`` plus the stem
- _apply_common_cfg         apply flags shared across every input type
- _resolve_exiftool_config  build ExiftoolConfig (CLI flag > env > default)
- _preflight_exiftool       stop before the first model call if reads/writes can't run
- _refuse_unwritable_exiftool_fields  reject tag spellings ExifTool cannot write
- _ProtectedPath            one file the run depends on, and what it is to the run
- _preflight_destinations_are_distinct  refuse two destinations that are one file
- _preflight_output_file    refuse a destination the run needs, or cannot write
- _analysis_protected_paths what an analysis / --generate-manifest run depends on
- _write_generated_manifest atomically write a synthesized manifest, pretty-printed
- _generate_manifest        --generate-manifest: describe the input's grouping, stop
- _suggest_the_normal_run   offer ``-rw`` to a run that has asked for nothing
- _apply_exiftool_changeset apply routed fields via ExifTool + append a status line
- _rename_protected_paths   what a rename run reads, renames, leaves or recovers from
- _run_rename_mode          --rename: plan a folder/manifest's rename, preview or apply it
- _run_rename_finish        --rename-finish: companions only, images renamed elsewhere
- _run_rename_undo_or_resume  --rename-undo / --rename-resume: read the journal back
- main                      PUBLIC: resolve the input, print the plan, run it
"""

import argparse
import contextlib
import importlib.metadata
import io
import json
import logging
import os
import re
import sys
import tempfile
import threading
import traceback
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, NamedTuple, NoReturn

from . import cli_messages, rename_apply, utils
from .utils import Config, normalize_path
from .canonical import (
    CANONICAL_DATE_TAG,
    CANONICAL_DESCRIPTION_TAG,
    CANONICAL_KEYWORDS_TAG,
    CANONICAL_LOCATION_TAGS,
    CANONICAL_TITLE_TAG,
    CANONICAL_USER_COMMENT_TAG,
)
from .changeset import SCHEMA_VERSION as _CHANGESET_SCHEMA_VERSION
from .core import (
    build_folder_manifest,
    build_manifest_buckets,
    build_single_photo_manifest,
    process_manifest_stream,
)
from .doc_sidecar import sidecar_path_for
from .errors import ProviderApiError, SELF_EXPLANATORY_ERROR_TYPES
from .exiftool import (
    ExiftoolConfig,
    apply_changeset,
    hydrate_item_metadata,
    make_manifest_hydrator,
    resolve_exiftool_path,
)
from .exiftool.config import parse_fields as exiftool_parse_fields
from .exiftool.config import suggest_writable_spelling
from .exiftool.manifest import DEFAULT_EXIFTOOL_FIELDS
from .rename import DEFAULT_COMPANION_EXTENSIONS, RenameItem, plan_rename

# Named explicitly rather than via __name__: under ``python -m photokin.cli``
# (how the plugin launches this) __name__ is "__main__", which sits outside the
# package logger and would never reach the handler installed below.
logger = logging.getLogger("photokin.cli")

# Tag on the handler this module installs, so repeated main() calls reuse it.
_LOG_HANDLER_NAME = "photokin-cli-stderr"
# Tag on the optional --log-file/-v duplicate handler, same reuse reason.
_LOG_FILE_HANDLER_NAME = "photokin-cli-logfile"

# Bumped whenever a record's shape changes in a way a consumer could care
# about -- a new top-level key, a renamed one, a changed meaning for an
# existing one. Adding an *optional* key that is simply absent when there is
# nothing to say (``provider_message``, say) does not by itself require a
# bump; changing what an existing key means always does. Mirrors
# ``changeset.SCHEMA_VERSION`` -- the one other place this package
# schema-versions an NDJSON stream -- but is independent of it: the two
# streams (results, changeset) evolve on their own schedules.
#
# History (why each bump happened, not a full changelog):
#   3 -- a multipage group's per-file ``caption`` changed meaning: it used to
#        be the group's whole transcription, written byte-identically to
#        every file; it is now that file's own part. The key's shape (a
#        string) is unchanged, but a consumer that averaged, compared, or
#        deduped ``caption`` across a group's files would have gotten a
#        different answer with no shape change to detect it by. See
#        docs/per-page-captions.md, decision E11.
_NDJSON_SCHEMA_VERSION = 3

# Guards every physical append to a results NDJSON file. One process runs one
# main() at a time, but a background writer (a heartbeat, were one added) and
# the main thread could otherwise interleave two writes into one corrupted
# line; a single process-wide lock is simpler than threading one through
# every call site that can append.
_ndjson_write_lock = threading.Lock()


def _append_ndjson_record(path: str, record: dict, *, batch_id: str | None = None) -> None:
    """Append one schema-stamped record to an NDJSON destination.

    Every write to a results NDJSON file goes through this -- the run
    envelope (``run: start/plan/fatal/complete/cancelled``) and every
    per-file result/error record from ``core.process_manifest_stream`` alike
    -- so ``schema_version`` and ``batch_id`` never have to be remembered at
    more than one call site.

    Args:
        path: The NDJSON file to append to. Must already exist; this never
            creates or truncates it.
        record: The record to write. Mutated in place (two keys are added)
            rather than copied, since every caller already treats the dict as
            disposable -- built fresh for this one call.
        batch_id: ``--batch-id``, when the run was given one.
    """
    if batch_id:
        record.setdefault("batch_id", batch_id)
    record.setdefault("schema_version", _NDJSON_SCHEMA_VERSION)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with _ndjson_write_lock, open(path, "a", encoding="utf-8") as handle:
        handle.write(line)


class _RunEnvelope:
    """The open run-level NDJSON envelope for the results file, if any.

    A module-level singleton (see :data:`_active_run_envelope`) rather than a
    value threaded through every pre-flight helper's signature: a refusal can
    come from any of the many small functions scattered across this module
    (``_resolve_input``, ``_resolve_provider``, ``_preflight_exiftool``, ...),
    and centralizing the envelope here is what lets every one of them become
    observable in the results file without a signature change to each --
    ``_exit_with_usage_error`` just checks whether one is active.
    ``_configure_logging``'s handler is the same kind of process-lifetime
    state, scoped the same way, for the same reason.

    Only ever constructed by :func:`_open_run_envelope_if_fresh` or
    :func:`_open_run_envelope_deferred`, both of which write the opening
    ``run: start`` record as part of opening it -- so by the time one of
    these exists, ``run: start`` is already on the stream.
    """

    def __init__(self, path: str, *, batch_id: str | None = None):
        self.path = path
        # Captured once at open time (args.batch_id never changes after
        # parsing) so every subsequent append -- including the ones from
        # deep inside a pre-flight helper via _exit_with_usage_error, which
        # has no args namespace to read it from -- is stamped the same way
        # without having to thread batch_id to every call site individually.
        # That was tried and missed exactly one: the usage-error fatal.
        self.batch_id = batch_id

    def append(self, record: dict) -> None:
        """Append one envelope or per-file record to this run's file."""
        _append_ndjson_record(self.path, record, batch_id=self.batch_id)


#: The run's open envelope, or None when no fresh ``.ndjson`` destination has
#: been opened for it yet (no ``--output-file``, a non-``.ndjson`` extension,
#: or a pre-existing file deferred per :func:`_open_run_envelope_deferred`'s
#: own docstring). Reset at the top of every :func:`main` call so repeated
#: in-process calls -- the test suite's own pattern -- never leak one run's
#: envelope into the next.
_active_run_envelope: "_RunEnvelope | None" = None


def _open_run_envelope_if_fresh(out_path: str | None, *, batch_id: str | None) -> None:
    """Open the run's NDJSON envelope immediately if the destination is new.

    Called as early as argparse allows -- right after ``args.output_file`` is
    known, before any other pre-flight check -- so every refusal after this
    point has somewhere to land a ``run: fatal`` record instead of leaving a
    fire-and-forget caller staring at a file that never appeared.

    A *pre-existing* destination is left untouched here: opening it would
    truncate it immediately, and a refused run's caller may still need
    whatever a previous run wrote there. See
    :func:`_open_run_envelope_deferred` for where that file gets its own
    ``run: start``, once every check has passed and overwriting it is exactly
    what the run was already going to do.

    Args:
        out_path: ``args.output_file``, or ``None``.
        batch_id: ``--batch-id``, when the run was given one.
    """
    global _active_run_envelope
    if not out_path or not out_path.lower().endswith(".ndjson") or os.path.exists(out_path):
        return
    open(out_path, "w", encoding="utf-8").close()
    _active_run_envelope = _RunEnvelope(out_path, batch_id=batch_id)
    _active_run_envelope.append({"run": "start"})


def _open_run_envelope_deferred(out_path: str | None, *, batch_id: str | None) -> None:
    """Open the envelope now, for a destination that pre-existed at parse time.

    Every pre-flight check has passed by the time this runs, so truncating
    the previous run's file here is exactly what the run was already going
    to do -- this only additionally gives that file the same ``run: start``
    record a fresh destination got immediately. A no-op if the fresh-file
    path already opened one, or if the destination still isn't a usable
    ``.ndjson`` path (a ``.json`` destination, or none at all).

    Args:
        out_path: ``args.output_file``, or ``None``.
        batch_id: ``--batch-id``, when the run was given one.
    """
    global _active_run_envelope
    if _active_run_envelope is not None:
        return
    if not out_path or not out_path.lower().endswith(".ndjson"):
        return
    open(out_path, "w", encoding="utf-8").close()
    _active_run_envelope = _RunEnvelope(out_path, batch_id=batch_id)
    _active_run_envelope.append({"run": "start"})


def _photokin_version() -> str:
    """Return the installed ``photokin`` version, or ``"unknown"`` outside a build."""
    try:
        return importlib.metadata.version("photokin")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _build_capabilities(ap: argparse.ArgumentParser) -> dict:
    """Describe this build's contract: version, schema, tags, providers, flags.

    Meant to replace an import-probe heuristic (importing some internal
    symbol and trusting a pip version pin to mean the rest still matches)
    with a real, versioned answer a caller can gate a run on. The
    ``XMP:dc:`` -> ``XMP-dc:`` tag rename showed the cost of the
    alternative: a mismatched pair silently dropped keywords, title and
    caption instead of failing detectably, because nothing on either side
    stated a contract the other could check against.

    Args:
        ap: The fully-built argument parser. ``flags`` is read live off it
            via its (underscore-private, but stable and widely relied on)
            ``_actions``, so the list can never drift from what argparse
            actually accepts the way a hand-maintained one could.

    Returns:
        A JSON-serializable dict.
    """
    version = _photokin_version()
    flags = sorted(
        {opt for action in ap._actions for opt in action.option_strings if opt.startswith("--")}
    )
    return {
        "version": version,
        "ndjson_schema_version": _NDJSON_SCHEMA_VERSION,
        "changeset_schema_version": _CHANGESET_SCHEMA_VERSION,
        "canonical_tags": {
            "ai_caption": CANONICAL_USER_COMMENT_TAG,
            "caption": CANONICAL_DESCRIPTION_TAG,
            "keywords": CANONICAL_KEYWORDS_TAG,
            "title": CANONICAL_TITLE_TAG,
            "date_guess": CANONICAL_DATE_TAG,
            "location_guess": dict(CANONICAL_LOCATION_TAGS),
        },
        "providers": list(_PROVIDER_CHOICES),
        "flags": flags,
    }


def _scan_argv_for_output_file(argv: list[str]) -> str | None:
    """Best-effort peek at ``--output-file`` before argparse has run.

    Argparse can reject the rest of ``argv`` -- an unknown flag, a bad choice
    value -- before this module ever learns the output destination the normal
    way, and when it does, it exits straight out of ``parse_args()`` with a
    usage message on stderr that a fire-and-forget launch never sees. This
    lets that one failure mode still land in the results file: a linear
    scan, not real parsing, so argv it can't make sense of just yields
    ``None`` rather than raising a second error of its own.

    Args:
        argv: The raw argument list, before ``ap.parse_args`` sees it.

    Returns:
        The value following (or joined by ``=`` to) ``--output-file``, or
        ``None`` if the token never appears.
    """
    for i, token in enumerate(argv):
        if token == "--output-file" and i + 1 < len(argv):
            return argv[i + 1]
        if token.startswith("--output-file="):
            return token.split("=", 1)[1]
    return None

#: What each input kind is called in a message. The plan summary uses the same
#: words without the article, so "what it was detected as" reads identically
#: wherever it appears.
_KIND_LABELS: dict[str, str] = {
    "folder": "a folder",
    "manifest": "a manifest",
    "photo": "a single photo",
}

#: The extensions a folder is searched for, for the error that names them.
_IMAGE_EXTENSIONS = ", ".join(sorted(utils.VALID_EXTS))


@dataclass(frozen=True)
class _WriteBundleMember:
    """One flag ``-w`` expands to, with the wording every message about it needs.

    The wording lives beside the value rather than in the functions that report
    it, so a member cannot be added to the bundle without also saying how it is
    refused. That is the whole point of the type: the refusal used to restate
    the membership list, and a restated list is a list that can disagree.

    Attributes:
        value: What ``-w`` sets the flag to. Every member expands to ``"true"``,
            so agreement and contradiction are an explicit flag's only outcomes.
        verb: What the flag would have done, for the ``--generate-manifest``
            refusal -- ``"record"`` or ``"write"``.
        replay: How to ask for the same thing once the manifest exists.
    """

    value: str
    verb: str
    replay: str


#: The one definition of what ``-w`` means. The expansion, the contradiction
#: check and the ``--generate-manifest`` refusal all read it, so a third member
#: is one dict entry rather than three edits in three functions -- and cannot
#: end up expanded by ``-w`` while staying silently permitted beside a flag that
#: makes no model call. Keys are argparse destinations; iteration order is the
#: order the refusal reports them in.
_WRITE_BUNDLE: dict[str, _WriteBundleMember] = {
    "changeset": _WriteBundleMember("true", "record", "--changeset true"),
    "exiftool_write": _WriteBundleMember("true", "write", "-w"),
}

#: Input tokens that carry no path at all, once ``str.strip`` and the surrounding
#: quote pair ``utils.normalize_path`` removes are accounted for. It ends by
#: calling ``os.path.normpath``, which answers ``"."`` for what is left of any of
#: these -- so ``photokin " "`` would otherwise be classified as the current
#: working directory and every image in it analyzed, and written to under ``-w``.
#: ``photokin .`` is a real request and is deliberately not in this set.
_BLANK_INPUT_TOKENS: frozenset[str] = frozenset({"", '""', "''"})

_EPILOG = """\
examples:
  # A folder of scans
  %(prog)s ./scans/ --provider anthropic --claude-model sonnet

  # A manifest, streaming results and applying the approved tags
  %(prog)s batch.json -w --output-file results.ndjson

  # One photo and its reverse (dev/testing)
  %(prog)s scan_042.jpg --back scan_042-back.jpg --provider openai

  # Any OpenRouter-hosted vision model (Kimi, Qwen-VL, ...) via one API key
  %(prog)s ./scans/ --provider openrouter --openrouter-model moonshotai/kimi-k3

  # Check how a folder would be grouped, without calling the model
  %(prog)s ./scans/ --generate-manifest scans-manifest.json
"""


def load_json(p: str):
    """Read JSON with UTF-8 encoding to mirror Lightroom’s manifest writes."""

    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _configure_logging() -> None:
    """Install the stderr log handler on the ``photokin`` package logger.

    Every module logs through ``logging.getLogger(__name__)``, so one handler on
    the package logger surfaces all of them — including the hydration-skip
    warning in ``exiftool.hydrate``, which had no handler to reach before. The
    level is INFO unless ``MEL_VERBOSE`` or ``MEL_DEBUG`` is set to a non-empty
    value, matching how the rest of the package reads those two variables.
    Calling this more than once reuses the existing handler rather than stacking
    a second copy, re-pointing it at the current ``sys.stderr``: a handler binds
    the stream object it was built with, so a second in-process ``main()`` would
    otherwise keep writing into the stream the first call captured.
    """
    level = logging.DEBUG if (os.getenv("MEL_VERBOSE") or os.getenv("MEL_DEBUG")) else logging.INFO
    package_logger = logging.getLogger("photokin")
    package_logger.setLevel(level)
    for existing in package_logger.handlers:
        if existing.get_name() == _LOG_HANDLER_NAME and isinstance(existing, logging.StreamHandler):
            existing.setLevel(level)
            existing.setStream(sys.stderr)
            return
    handler = logging.StreamHandler(sys.stderr)
    handler.set_name(_LOG_HANDLER_NAME)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    package_logger.addHandler(handler)


def _attach_log_file_handler(path: str) -> None:
    """Duplicate every log line into *path*, in addition to stderr.

    stderr is exactly what a fire-and-forget subprocess launch throws away --
    the plugin's own launch shape discards it entirely -- so the plan
    summary, every WARNING, and the "Batch completed" line are otherwise
    invisible to a caller that only watches the results file. A second
    handler on the same logger costs one open file descriptor and changes
    nothing about what already goes to stderr.

    Truncates on open, matching every other destination this module
    manages (``--output-file``, the changeset): the file always reflects the
    run that just wrote it, not an accumulation across runs sharing a path.
    Reused rather than duplicated on a second in-process ``main()`` call, the
    same way :func:`_configure_logging` reuses its own handler.

    Args:
        path: Where to write the duplicate log.
    """
    package_logger = logging.getLogger("photokin")
    level = package_logger.level or logging.INFO
    for existing in package_logger.handlers:
        if existing.get_name() == _LOG_FILE_HANDLER_NAME:
            package_logger.removeHandler(existing)
            existing.close()
    # FileHandler does not create its own parent directory -- unlike the
    # debug-dump writers, which mkdir lazily on first use, this is attached
    # long before anything else would have created --debug-dump-dir (-v's
    # own default even points here directly).
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    handler = logging.FileHandler(path, mode="w", encoding="utf-8")
    handler.set_name(_LOG_FILE_HANDLER_NAME)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    package_logger.addHandler(handler)


def _exit_with_usage_error(problem: str, remedy: str) -> NoReturn:
    """Report a usage error as a problem line plus a ``Try:`` line, then exit 2.

    Also closes the run's NDJSON envelope with a ``run: fatal`` record, when
    one is open (see :data:`_active_run_envelope`) -- this is the one place
    every pre-flight refusal in the module already passes through, which is
    what lets the envelope cover all of them without a change at each call
    site.

    Args:
        problem: What the CLI saw, stated in a single line.
        remedy: The corrective action, rendered on a following ``Try:`` line.

    Raises:
        SystemExit: Always, with exit code 2.
    """
    logger.error("%s\nTry: %s", problem, remedy)
    if _active_run_envelope is not None:
        _active_run_envelope.append({"run": "fatal", "error": {"type": "usage_error", "message": problem}})
    sys.exit(2)


def _interactive_prompt() -> list[str]:
    """Prompt the user for image paths and return extra argv tokens.

    A blank front-image answer, an interrupt (Ctrl+C) on either prompt, or
    closed stdin (Ctrl+D on macOS/Linux, Ctrl+Z then Enter on Windows, or an
    empty piped stdin) on the front prompt all mean "nothing to run" and exit
    0 quietly -- none should raise. Closed stdin on the *back* prompt is
    narrower: the front path is already in hand, so it is treated as "no back
    image" and the run proceeds front-only, the same as a typed blank answer
    there -- unlike Ctrl+C on that same prompt, which still means "stop
    entirely", not "narrow the request".
    """

    print("Interactive mode: Provide image paths for analysis.")
    try:
        front_raw = input("Front image path (blank to quit): ")
    except (EOFError, KeyboardInterrupt):
        print("\nNo file provided. Exiting.")
        raise SystemExit(0)
    # Checked before normalizing: normalize_path("") is "." (the current
    # directory via os.path.normpath), which is truthy -- a blank answer would
    # otherwise silently become `photokin .`, folder input over the cwd.
    if not front_raw.strip():
        print("No file provided. Exiting.")
        raise SystemExit(0)
    front = normalize_path(front_raw)
    try:
        back_raw = input("Back image path (optional, blank if none): ")
    except EOFError:
        # Closed stdin here just means "no back image" -- the front path is
        # already in hand and a run with only it is a normal, complete
        # request.
        back_raw = ""
        print("")
    except KeyboardInterrupt:
        # Unlike EOF, this is the user explicitly asking to stop -- treated
        # the same as a Ctrl+C on the front prompt: the whole request is
        # abandoned, not narrowed to "front only".
        print("\nNo file provided. Exiting.")
        raise SystemExit(0)
    back = normalize_path(back_raw) if back_raw.strip() else None
    extra = [front]
    if back:
        extra.extend(["--back", back])
    print("")
    return extra


@dataclass(frozen=True)
class ResolvedInput:
    """The run's single input, classified once and addressed from one place.

    Attributes:
        kind: ``"folder"``, ``"manifest"`` or ``"photo"``.
        path: The normalized path the builders and loaders receive.
        display: The token exactly as the user typed it, for every message.
        directory: The folder itself for folder input, the containing directory
            for a manifest or a photo. This is the "input's directory" the
            changeset derivation means -- ``os.path.dirname("./scans")`` is
            ``"."``, which is not it.
        stem: The folder's own name for folder input, the filename without its
            extension otherwise. Empty at a drive root.
    """

    kind: str
    path: str
    display: str
    directory: str
    stem: str


def _resolved_input(kind: str, path: str, display: str) -> ResolvedInput:
    """Build a :class:`ResolvedInput`, deriving its directory and stem.

    No ``os.path.realpath`` is taken anywhere: the path the user typed is the
    path stored, so a changeset derived from it lands beside the symlink rather
    than beside its target.

    Args:
        kind: ``"folder"``, ``"manifest"`` or ``"photo"``.
        path: The normalized path.
        display: The token exactly as the user typed it.

    Returns:
        The resolved input.
    """
    absolute = os.path.abspath(path)
    if kind == "folder":
        return ResolvedInput(kind, path, display, absolute, os.path.basename(absolute))
    return ResolvedInput(
        kind,
        path,
        display,
        os.path.dirname(absolute),
        os.path.splitext(os.path.basename(absolute))[0],
    )


def _input_path(value: str) -> str:
    """Normalize an input token, refusing one that addresses nothing.

    The single normalization point for all three input spellings, so a blank
    token cannot be refused by one of them and silently redirected to the
    current working directory by the other two.

    Args:
        value: The token exactly as the user typed it.

    Returns:
        The normalized path.

    Raises:
        SystemExit: With code 2 when the token names no path at all.
    """
    if value.strip() in _BLANK_INPUT_TOKENS:
        _exit_with_usage_error(*cli_messages.input_names_nothing(value))
    return normalize_path(value) or ""


def _classify_positional(display: str) -> tuple[ResolvedInput, str]:
    """Decide what a positional input is, from the path alone.

    Directories win over extensions, so a directory named ``batch.json`` is a
    folder; that is the case the detection line exists to make visible.
    ``os.path.exists`` and friends follow symlinks, so a link to a folder is a
    folder and a link to an image is a photo. Only a link that resolves nowhere
    is special-cased, because "check the spelling" is the wrong remedy for it.

    Args:
        display: The token exactly as the user typed it.

    Returns:
        The resolved input and the reason it was classified that way.

    Raises:
        SystemExit: With code 2 when the path cannot be used as an input.
    """
    path = _input_path(display)
    if not os.path.lexists(path):
        _exit_with_usage_error(*cli_messages.input_not_found(display))
    if not os.path.exists(path):
        # lexists true and exists false is exactly a dangling symlink.
        try:
            target = os.readlink(path)
        except OSError as exc:
            logger.debug("Reading the link at %s failed: %s", path, exc)
            _exit_with_usage_error(*cli_messages.input_not_found(display))
        _exit_with_usage_error(*cli_messages.input_is_a_broken_symlink(display, target))
    if os.path.isdir(path):
        return _resolved_input("folder", path, display), "it is a directory"
    # ``isfile`` rather than "not a directory": a FIFO, a device or a socket
    # would otherwise fall through to the extension test below.
    if not os.path.isfile(path):
        _exit_with_usage_error(*cli_messages.input_is_not_a_file_or_folder(display))
    extension = os.path.splitext(path)[1].lower()
    if extension == ".json":
        return _resolved_input("manifest", path, display), "it is a .json file"
    if extension in utils.VALID_EXTS:
        return _resolved_input("photo", path, display), f"it is a {extension} file"
    _exit_with_usage_error(*cli_messages.unrecognized_input_extension(display))


def _resolve_folder_alias(value: str) -> ResolvedInput:
    """Resolve ``--folder``, which asserts a directory rather than detecting one.

    Args:
        value: The value the flag carried.

    Returns:
        The resolved input.

    Raises:
        SystemExit: With code 2 when the path is not an existing directory.
    """
    path = _input_path(value)
    if not os.path.exists(path):
        _exit_with_usage_error(*cli_messages.input_not_found(value))
    if not os.path.isdir(path):
        _exit_with_usage_error(*cli_messages.alias_is_not_a_directory(value))
    return _resolved_input("folder", path, value)


def _resolve_manifest_alias(value: str) -> ResolvedInput:
    """Resolve ``--manifest``, which asserts a ``.json`` file.

    Args:
        value: The value the flag carried.

    Returns:
        The resolved input.

    Raises:
        SystemExit: With code 2 when the path is not an existing ``.json`` file.
    """
    path = _input_path(value)
    if not os.path.exists(path):
        _exit_with_usage_error(*cli_messages.input_not_found(value))
    if os.path.isdir(path) or os.path.splitext(path)[1].lower() != ".json":
        _exit_with_usage_error(*cli_messages.alias_is_not_a_json_file(value))
    return _resolved_input("manifest", path, value)


def _resolve_input(args: argparse.Namespace) -> ResolvedInput:
    """Pick the run's one input and settle what it is.

    An alias asserts the type and infers nothing, so ``--manifest ./scans/`` is
    refused rather than silently re-detected as a folder. Only a positional is
    classified, and only a positional logs what it was taken to be.

    Args:
        args: The parsed namespace.

    Returns:
        The resolved input.

    Raises:
        SystemExit: With code 2 when no input, or more than one, was given.
    """
    sources = [
        ("positional", args.input_path),
        ("--folder", args.folder),
        ("--manifest", args.manifest),
    ]
    # "Was this source given" is ``is not None``, not truthiness. All three
    # default to None, so an empty token is a source that was given and names
    # nothing -- which is ``_input_path``'s job to say. Filtering on truthiness
    # dropped it instead, and ``photokin ""`` was reported as no input at all
    # while ``photokin " "`` got the blank-token message.
    given = [(name, value) for name, value in sources if value is not None]
    if not given:
        # The interactive prompt already ran if argv was empty, so reaching here
        # means flags were passed with nothing to run them against.
        if args.generate_manifest:
            _exit_with_usage_error(
                *cli_messages.generate_manifest_without_input(args.generate_manifest)
            )
        _exit_with_usage_error(*cli_messages.no_input_given())
    if len(given) > 1:
        # With all three present the two aliases are reported and the positional
        # is named in neither message: one error per run is the contract.
        if args.folder and args.manifest:
            _exit_with_usage_error(*cli_messages.two_aliases(args.folder, args.manifest))
        alias_flag, alias_value = given[1]
        _exit_with_usage_error(
            *cli_messages.positional_and_alias(args.input_path, alias_flag, alias_value)
        )
    name, value = given[0]
    if name == "--folder":
        return _resolve_folder_alias(value)
    if name == "--manifest":
        return _resolve_manifest_alias(value)
    resolved, reason = _classify_positional(value)
    logger.info("%s", cli_messages.detected_as(value, _KIND_LABELS[resolved.kind], reason))
    return resolved


def _validate_folder_input(resolved: ResolvedInput) -> None:
    """Refuse a folder that cannot be listed or holds nothing to analyze.

    An empty folder used to warn and exit 0, which is the "total failure reads
    as success" shape the rest of this pipeline exists to remove.

    Args:
        resolved: The folder input.

    Raises:
        SystemExit: With code 2 when the folder is unreadable or imageless.
    """
    try:
        files = utils.list_folder_images(resolved.path)
    except OSError as exc:
        _exit_with_usage_error(
            *cli_messages.folder_cannot_be_read(resolved.display, exc.strerror or str(exc))
        )
    if not files:
        _exit_with_usage_error(
            *cli_messages.folder_has_no_images(resolved.display, _IMAGE_EXTENSIONS)
        )


def _load_manifest_input(resolved: ResolvedInput) -> dict:
    """Read a manifest and refuse one the run cannot process.

    Fatal on the first offending item rather than collecting a report: one
    problem line and one ``Try:`` line is the house style.

    Args:
        resolved: The manifest input.

    Returns:
        The parsed manifest document.

    Raises:
        SystemExit: With code 2 for an unreadable, misshapen or broken manifest.
    """
    try:
        document = load_json(resolved.path)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _exit_with_usage_error(*cli_messages.json_is_unreadable(resolved.display, str(exc)))
    except FileNotFoundError:
        # Detection proved the file was there, so reaching this means it was
        # removed in between -- the one OSError for which "not found" is true.
        _exit_with_usage_error(*cli_messages.input_not_found(resolved.display))
    except OSError as exc:
        # A denied ACL, a lock held by a sync client, a handle another process
        # opened exclusively. "Check the spelling" is actively wrong for these.
        _exit_with_usage_error(
            *cli_messages.manifest_cannot_be_read(resolved.display, exc.strerror or str(exc))
        )
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        _exit_with_usage_error(*cli_messages.json_is_not_a_manifest(resolved.display))
    items = document["items"]
    if not items:
        _exit_with_usage_error(*cli_messages.manifest_has_no_items(resolved.display))
    for index, item in enumerate(items):
        raw_path = item.get("path") if isinstance(item, dict) else None
        if not isinstance(raw_path, str) or not raw_path.strip():
            _exit_with_usage_error(
                *cli_messages.manifest_item_has_no_path(resolved.display, index)
            )
        if not os.path.isfile(normalize_path(raw_path) or ""):
            _exit_with_usage_error(
                *cli_messages.manifest_item_not_found(resolved.display, index, raw_path)
            )
    return document


def _validate_single_photo_flags(args: argparse.Namespace, resolved: ResolvedInput) -> None:
    """Refuse ``--back``/``--meta`` against the wrong input, or a path that is gone.

    Both flags are read only by the single-photo path, so folder and manifest
    input used to drop them silently. The existence check lives here rather than
    in ``utils.ensure_paths_exist`` so ``--generate-manifest`` and the analysis
    path answer identically for one input, in this module's own wording.

    Args:
        args: The parsed namespace.
        resolved: The run's input.

    Raises:
        SystemExit: With code 2 when a flag does not apply or names nothing.
    """
    for flag, value in (("--back", args.back), ("--meta", args.meta)):
        if not value:
            continue
        if resolved.kind != "photo":
            _exit_with_usage_error(
                *cli_messages.flag_needs_single_photo_input(
                    flag, value, resolved.display, _KIND_LABELS[resolved.kind]
                )
            )
        if not os.path.isfile(normalize_path(value) or ""):
            _exit_with_usage_error(*cli_messages.flag_path_not_found(flag, value))


def _flag_spelling(dest: str) -> str:
    """Return the long-flag spelling of an argparse destination.

    Args:
        dest: The destination name, such as ``"exiftool_write"``.

    Returns:
        The flag as the user types it, such as ``"--exiftool-write"``.
    """
    return "--" + dest.replace("_", "-")


def _resolve_write_bundle(args: argparse.Namespace) -> tuple[str, str | None]:
    """Expand ``-w`` and reject any explicit flag that contradicts it.

    Args:
        args: The parsed namespace. Every :data:`_WRITE_BUNDLE` destination
            defaults to ``None`` there so "unset" is distinguishable from an
            explicit value that happens to agree with the expansion.

    Returns:
        ``(changeset, exiftool_write)``. ``changeset`` is ``"true"`` or
        ``"false"``; ``exiftool_write`` is ``"true"``, ``"false"``, or ``None``
        meaning "defer to EXIFTOOL_WRITE_ENABLED, else the default".

    Raises:
        SystemExit: With code 2 when ``-w`` is given beside a contradicting flag.
    """
    values: dict[str, str | None] = {dest: getattr(args, dest) for dest in _WRITE_BUNDLE}
    if args.write:
        for dest, member in _WRITE_BUNDLE.items():
            given = values[dest]
            if given is not None and given != member.value:
                _exit_with_usage_error(
                    *cli_messages.write_bundle_contradiction(_flag_spelling(dest), given)
                )
            values[dest] = member.value if given is None else given
    return values["changeset"] or "false", values["exiftool_write"]


def _refuse_generate_manifest_write_flags(args: argparse.Namespace) -> None:
    """Refuse write flags beside ``--generate-manifest``, which writes nothing else.

    The flag exits before a model is called, so a write flag beside it is a
    request that cannot be partly honored.

    Which flags those are is read off :data:`_WRITE_BUNDLE` rather than restated
    here. Restating it was a leak: the bundle stayed the one definition of what
    ``-w`` expands to, but a member added to it would have been expanded by
    ``-w`` and then silently permitted beside ``--generate-manifest``, which is
    the one place a write flag can never be honored at all.

    Args:
        args: The parsed namespace, before ``-w``'s expansion is written back.

    Raises:
        SystemExit: With code 2 for the first conflicting flag found.
    """
    if args.output_file:
        _exit_with_usage_error(
            *cli_messages.generate_manifest_with_output_file(args.output_file)
        )
    # ``-w`` is the bundle's trigger rather than a member of it, so it is named
    # on its own -- and named first, since a run passing it asked for every
    # member at once and should be told about the shorthand it actually typed.
    if args.write:
        _exit_with_usage_error(
            *cli_messages.generate_manifest_with_write_flag("-w", "write", "-w")
        )
    for dest, member in _WRITE_BUNDLE.items():
        if getattr(args, dest) == member.value:
            _exit_with_usage_error(
                *cli_messages.generate_manifest_with_write_flag(
                    f"{_flag_spelling(dest)} {member.value}", member.verb, member.replay
                )
            )


#: The one definition of what ``-v`` means, same shape as :data:`_WRITE_BUNDLE`
#: and for the same reason: the expansion, the contradiction check and the
#: ``--generate-manifest`` refusal all read it, so a third dump flag is one
#: dict entry rather than three edits in three functions.
_VERBOSE_BUNDLE: dict[str, _WriteBundleMember] = {
    "debug_dump_llm_request": _WriteBundleMember("true", "dump", "--debug-dump-llm-request true"),
    "debug_dump_hydration": _WriteBundleMember("true", "dump", "--debug-dump-hydration true"),
}


def _resolve_verbose_bundle(args: argparse.Namespace) -> tuple[str, str]:
    """Expand ``-v`` and reject any explicit flag that contradicts it.

    Mirrors :func:`_resolve_write_bundle` for the debug-dump flags ``-v`` is
    shorthand for: an explicit ``--debug-dump-llm-request false`` beside
    ``-v`` is a contradiction to refuse, not a value to silently pick between.
    ``--log-file`` is deliberately not a member -- it takes a path, not a
    fixed value, so there is nothing for ``-v`` to compare an explicit one
    against; ``-v``'s own default for it is filled in separately, once the
    directory it belongs beside is known.

    Args:
        args: The parsed namespace. Both bundle destinations default to
            ``None`` there, so "unset" is distinguishable from an explicit
            value that happens to agree with the expansion.

    Returns:
        ``(debug_dump_llm_request, debug_dump_hydration)``, each ``"true"``
        or ``"false"``.

    Raises:
        SystemExit: With code 2 when ``-v`` is given beside a contradicting flag.
    """
    values: dict[str, str | None] = {dest: getattr(args, dest) for dest in _VERBOSE_BUNDLE}
    if args.verbose:
        for dest, member in _VERBOSE_BUNDLE.items():
            given = values[dest]
            if given is not None and given != member.value:
                _exit_with_usage_error(
                    *cli_messages.verbose_bundle_contradiction(_flag_spelling(dest), given)
                )
            values[dest] = member.value if given is None else given
    return values["debug_dump_llm_request"] or "false", values["debug_dump_hydration"] or "false"


def _refuse_generate_manifest_verbose_flags(args: argparse.Namespace) -> None:
    """Refuse ``-v`` and its dump flags beside ``--generate-manifest``.

    ``--generate-manifest`` makes no model call, so there is nothing for a
    debug-dump flag to dump -- the same reasoning
    :func:`_refuse_generate_manifest_write_flags` applies to ``-w``, applied
    to the sibling bundle. ``--log-file`` is not refused here: unlike the
    dump flags, a log still means something without a model call -- the
    "wrote manifest for N files" line still goes somewhere.

    Args:
        args: The parsed namespace, before ``-v``'s expansion is written back.

    Raises:
        SystemExit: With code 2 for the first conflicting flag found.
    """
    if args.verbose:
        _exit_with_usage_error(
            *cli_messages.generate_manifest_with_write_flag("-v", "dump", "-v")
        )
    for dest, member in _VERBOSE_BUNDLE.items():
        if getattr(args, dest) == member.value:
            _exit_with_usage_error(
                *cli_messages.generate_manifest_with_write_flag(
                    f"{_flag_spelling(dest)} {member.value}", member.verb, member.replay
                )
            )


def _refuse_generate_manifest_sidecar_flags(args: argparse.Namespace) -> None:
    """Refuse ``-s`` and a non-off ``--sidecar-md`` beside ``--generate-manifest``.

    A transcript sidecar is written from an analysis, and ``--generate-manifest``
    exits before any model call -- the same reasoning
    :func:`_refuse_generate_manifest_write_flags` applies to ``-w`` and the dump
    bundle. Before this guard the sidecar request was silently discarded, which
    is exactly the inconsistency the other bundles' refusals exist to prevent.
    An explicit ``--sidecar-md off`` is not refused: it asks for nothing, so
    there is nothing the manifest run fails to honor.

    Args:
        args: The parsed namespace, before ``-s``'s expansion is written back.

    Raises:
        SystemExit: With code 2 for the first conflicting flag found.
    """
    if args.sidecar_auto:
        _exit_with_usage_error(
            *cli_messages.generate_manifest_with_write_flag("-s", "write", "-s")
        )
    if args.sidecar_md is not None and args.sidecar_md != utils.SIDECAR_MD_OFF:
        spelled = f"--sidecar-md {args.sidecar_md}"
        _exit_with_usage_error(
            *cli_messages.generate_manifest_with_write_flag(spelled, "write", spelled)
        )


def _resolve_sidecar_bundle(args: argparse.Namespace) -> str:
    """Expand ``-s`` and reject an explicit ``--sidecar-md`` that contradicts it.

    Mirrors :func:`_resolve_write_bundle` for the one flag ``-s`` is shorthand
    for: ``-s`` beside an explicit ``--sidecar-md off`` or ``--sidecar-md all``
    is a contradiction to refuse, not a value to silently pick between, while
    an explicit ``--sidecar-md auto`` merely agrees with it.

    Args:
        args: The parsed namespace. ``--sidecar-md`` defaults to ``None`` there
            so "unset" is distinguishable from an explicit value that happens
            to agree with the expansion.

    Returns:
        The resolved ``--sidecar-md`` value, one of
        :data:`utils.SIDECAR_MD_VALUES`.

    Raises:
        SystemExit: With code 2 when ``-s`` is given beside a contradicting
            ``--sidecar-md`` value.
    """
    given = args.sidecar_md
    if args.sidecar_auto:
        if given is not None and given != utils.SIDECAR_MD_AUTO:
            _exit_with_usage_error(*cli_messages.sidecar_bundle_contradiction(given))
        return utils.SIDECAR_MD_AUTO
    return given if given is not None else utils.SIDECAR_MD_OFF


def _derive_changeset_path(resolved: ResolvedInput, out_path: str | None) -> str:
    """Return where the changeset goes: ``dirname(--output-file or input)``.

    The stem follows the output file when there is one -- ``results.ndjson``
    yields ``results_changeset.ndjson`` and ``batch_results.ndjson`` yields
    ``batch_changeset.ndjson`` -- and the input's own stem otherwise, so two
    runs in one directory no longer collide on a bare ``changeset.ndjson``.

    Args:
        resolved: The run's input.
        out_path: The ``--output-file`` value, or ``None``.

    Returns:
        The absolute or input-relative changeset path.
    """
    if out_path:
        base_dir = os.path.dirname(os.path.abspath(out_path))
        name = os.path.basename(out_path)
        lowered = name.lower()
        for suffix in ("_results.ndjson", "_results.json"):
            if lowered.endswith(suffix):
                stem = name[: -len(suffix)]
                break
        else:
            stem = os.path.splitext(name)[0]
    else:
        base_dir, stem = resolved.directory, resolved.stem
    return os.path.join(base_dir, f"{stem}_changeset.ndjson" if stem else "changeset.ndjson")


def _apply_common_cfg(cfg: Config, args: argparse.Namespace, *, manifest: dict | None = None) -> None:
    """Apply shared CLI arguments to *cfg* that are identical across all modes."""

    cfg.photo_context_text = utils.resolve_photo_context(
        cli_text=args.photo_context_text,
        cli_file=args.photo_context_file,
        manifest=manifest,
    )
    cfg.debug_dump_llm_request = args.debug_dump_llm_request == "true"
    cfg.debug_dump_hydration = args.debug_dump_hydration == "true"
    cfg.debug_dump_dir = args.debug_dump_dir or os.path.join(os.getcwd(), "debug")
    cfg.run_batch_id = args.batch_id
    cfg.pretty_json = args.pretty_json == "true"


def _apply_manifest_debug_settings(
    cfg: Config, args: argparse.Namespace, manifest: dict, base_path: str
) -> None:
    """Let a manifest document override the debug-dump settings, as it always could.

    Args:
        cfg: The run configuration, already carrying the shared flags.
        args: The parsed namespace.
        manifest: The manifest document.
        base_path: ``--output-file`` when given, else the manifest itself; its
            directory is where dumps land unless a flag or the document says
            otherwise.
    """
    cli_debug_dump = (
        None if args.debug_dump_llm_request is None else (args.debug_dump_llm_request == "true")
    )
    cfg.debug_dump_llm_request = (
        cli_debug_dump
        if cli_debug_dump is not None
        else bool(manifest.get("debug_dump_llm_request"))
    )
    manifest_dump_dir = manifest.get("debug_dump_dir")
    if not isinstance(manifest_dump_dir, str):
        manifest_dump_dir = None
    cfg.debug_dump_dir = args.debug_dump_dir or manifest_dump_dir or os.path.join(
        os.path.dirname(base_path), "debug"
    )


def _resolve_exiftool_config(args: argparse.Namespace, *, dry_run: bool = False) -> ExiftoolConfig:
    """Build the pipeline ExiftoolConfig with precedence: CLI flag > env var > default.

    ``dry_run`` is not wired to any flag of this CLI and takes its default on
    every run. It is ExifTool's own preview mode -- count the writes, perform
    none -- which ``--dry-run`` here is not: that flag stops before the first
    model call, so no changeset exists for the apply step to preview. The two
    were conflated before C2 separated them. The parameter stays because it is
    how a direct caller reaches the preview through the same flag/env precedence
    the CLI uses; the preview's own command line is
    ``python -m photokin.exiftool.apply --dry-run``.

    Args:
        args: The parsed namespace, read for the three ``--exiftool-*`` flags.
        dry_run: Whether ExifTool should count writes instead of performing
            them. Library-only, as above.

    Returns:
        The resolved ExifTool configuration for this run.
    """
    flag_enabled = None if args.exiftool_write is None else (args.exiftool_write == "true")
    flag_fields = exiftool_parse_fields(args.exiftool_fields)
    return ExiftoolConfig.from_env(
        enabled=flag_enabled,
        fields=flag_fields,
        path=args.exiftool_path or None,
        dry_run=dry_run,
        overwrite_original=True,
    )


def _writes_are_planned(ecfg: ExiftoolConfig, *, changeset_requested: bool) -> bool:
    """Report whether this run will really hand fields to ExifTool.

    Args:
        ecfg: The resolved ExifTool configuration.
        changeset_requested: True when ``--changeset true`` was resolved.

    Returns:
        True when the apply step has a changeset, permission and fields.
    """
    return changeset_requested and ecfg.enabled and bool(ecfg.fields)


#: Same set the ``--provider`` flag enforces via argparse ``choices``. ``LLM_PROVIDER``
#: bypasses that argparse check (it is never parsed as a flag value), so
#: ``_resolve_provider`` re-validates it against this list itself -- otherwise a
#: typo'd env var would fall through ``normalize_provider``'s permissive default
#: and run OpenAI silently, the exact guess this whole resolution order exists
#: to avoid.
_PROVIDER_CHOICES = ("openai", "anthropic", "gemini", "openrouter")


def _resolve_provider(flag_value: str | None) -> str:
    """Resolve the provider for this run: flag, then ``LLM_PROVIDER``, then the installed SDK.

    There is deliberately no hardcoded fallback. A machine with exactly one
    provider SDK installed runs with that provider -- installing ``[anthropic]``
    is already a choice, and asking for it again on every command line taught
    nothing. A machine with several SDKs gets a usage error rather than a
    guess, because any guess spends money with a provider the user may not
    have meant. OpenRouter shares OpenAI's SDK and so is never auto-selected;
    it always takes an explicit flag or env value.

    Args:
        flag_value: The ``--provider`` value, or None when the flag was not given.

    Returns:
        The provider identifier this run should use.

    Raises:
        SystemExit: With code 2 when nothing chose a provider and zero or
            several SDKs are installed, or when ``LLM_PROVIDER`` names something
            that is not one of ``_PROVIDER_CHOICES``.
    """
    if flag_value:
        return flag_value
    env_value = (os.getenv("LLM_PROVIDER") or "").strip()
    if env_value:
        normalized = env_value.lower()
        if normalized not in _PROVIDER_CHOICES:
            _exit_with_usage_error(*cli_messages.invalid_llm_provider_env(env_value))
        return normalized
    installed = utils.installed_provider_sdks()
    if len(installed) == 1:
        return installed[0]
    if not installed:
        _exit_with_usage_error(*cli_messages.no_provider_sdk_installed())
    _exit_with_usage_error(*cli_messages.multiple_provider_sdks_installed(installed))


def _preflight_exiftool(
    ecfg: ExiftoolConfig, *, changeset_requested: bool, read_requested: bool = False
) -> None:
    """Fail before the first model call when a requested ExifTool run cannot happen.

    ``apply_changeset`` only looks for the binary once the whole batch has been
    analyzed and paid for, so the same lookup is done up front here. ``--dry-run``
    is not exempt: it stops after printing the plan, and a plan reporting
    ``write : ExifTool EXIF:UserComment`` when no binary exists would be a lie.

    ``-r`` is guarded for the same reason and more sharply. A write that cannot
    run is loud by construction -- no file changes -- while a read that cannot
    run proceeds to call the model with a strictly worse prompt, pays for it in
    full, and produces a record nothing in the results distinguishes from "there
    was nothing to read". Writes are still reported first when both are broken,
    since fetching a binary fixes the read at the same time.

    Args:
        ecfg: The resolved ExifTool configuration for this run.
        changeset_requested: True when ``--changeset true`` was resolved.
        read_requested: True when ``-r`` was given.

    Raises:
        SystemExit: With code 2 when a read or write is requested but no binary
            resolves.
    """
    writes_requested = ecfg.enabled and changeset_requested
    if not (writes_requested or read_requested):
        return
    try:
        # Not gated on ``ecfg.enabled``: a -r-only run has writes off and still
        # needs the binary.
        resolve_exiftool_path(ecfg)
    except OSError as exc:
        logger.debug("ExifTool resolution failed: %s", exc)
        if writes_requested:
            _exit_with_usage_error(*cli_messages.exiftool_not_found(ecfg.path or ""))
        _exit_with_usage_error(*cli_messages.exiftool_not_found_for_read(ecfg.path or ""))


def _refuse_unwritable_exiftool_fields(value: str | None) -> None:
    """Reject ``--exiftool-fields`` tags ExifTool is known to refuse.

    The rejected spelling is ``XMP:<namespace>:<Tag>``. It is rejected rather
    than quietly rewritten: it has never worked, so there is no behaviour to
    preserve, and the same spelling fails identically in ExifTool itself and in
    any other tool the user drives with it -- so correcting it silently here
    would leave them with a habit that keeps failing everywhere else. Naming
    the working spelling once teaches it; accepting it hides it.

    Rewriting is also not generally safe. ``suggest_writable_spelling``
    documents the measurements: ``EXIF:IFD0:Model`` is valid ExifTool syntax
    whose hyphenated form is not, so a blanket colon-to-hyphen rewrite would
    break input that works today.

    Args:
        value: The raw ``--exiftool-fields`` string, or ``None``.

    Raises:
        SystemExit: With code 2 when any named tag uses the rejected spelling.
    """
    for tag in exiftool_parse_fields(value) or ():
        better = suggest_writable_spelling(tag)
        if better:
            _exit_with_usage_error(
                *cli_messages.exiftool_field_is_not_writable(tag, better)
            )


class _ProtectedPath(NamedTuple):
    """One file this run depends on, and the phrase that says what it is.

    Attributes:
        path: Any spelling of the file -- absolute, relative, or as a plan
            happens to record it. :func:`_preflight_output_file` resolves it
            before comparing *and* before reporting it, so callers pass what
            they already have and no caller has to normalize first.
        description: What the file is to this run, as a noun phrase that
            reads after a flag name: "a photo this run would rename". It is
            the whole point of the guard -- a user told only that two paths
            clashed still has to work out which of their files was at stake.
    """

    path: str
    description: str


def _preflight_destinations_are_distinct(destinations: Sequence[tuple[str, str]]) -> None:
    """Refuse when two of this run's own destinations name the same file.

    Every destination here is written by a truncating open or an
    ``os.replace``, so two that resolve to one file do not merge: whichever
    write runs second replaces the first's output, and the run reports
    success. Checked before any of them is opened, so a refusal leaves
    neither file created.

    Pairwise because the list is at most five long, and because the pair that
    matched is exactly what the message has to name.

    Args:
        destinations: ``(role, path)`` for each destination this run would
            write, ordered with the one the user can most easily move first.

    Raises:
        SystemExit: With code 2 when two of them are the same file.
    """
    for index, (role_a, path_a) in enumerate(destinations):
        for role_b, path_b in destinations[index + 1:]:
            if utils.paths_are_same_file(os.path.abspath(path_a), os.path.abspath(path_b)):
                _exit_with_usage_error(
                    *cli_messages.output_destinations_alias(role_a, path_a, role_b, path_b)
                )


def _preflight_output_file(
    out_path: str,
    *,
    role: str = "--output-file",
    protects: Sequence[_ProtectedPath] = (),
    creates_its_parent: bool = False,
) -> None:
    """Ask what is already at a destination, then whether it can be written.

    The second question was the only one this ever asked, and six review
    rounds found the same defect wearing different clothes: a write that
    lands on a file the run itself needs. Every destination here goes down
    with a truncating open or an ``os.replace``, both of which take the
    destination whatever it held, so naming one of the run's own inputs
    destroys that file's only copy -- with no ``-w``, and under ``--dry-run``
    as readily as without it. So *protects* is asked first: "you named a
    photo this run renames" is more use than "not writable", and on Windows a
    read-only source would otherwise preempt it with the wrong answer.

    Matching is :func:`utils.paths_are_same_file`, filesystem identity rather
    than spelling, so a relative path, a ``..`` detour, a symlink and a hard
    link are all caught. There is deliberately no "skip this when the
    destination does not exist" short-circuit: dropping it is what lets the
    one loop also answer for a destination the run has not created yet -- a
    rename target, a sidecar it is about to write. The cost is that for a
    destination that does not exist, ``paths_are_same_file`` can only fall
    back to a case-folded string compare, so on a case-sensitive filesystem
    ``--plan-out folder/BOX3_017.JSON`` beside ``box3_017.json`` is refused
    although POSIX would have allowed both. Refusing a contrived command is
    the side to err on when the alternative is destroying a file.

    The writability half is unchanged, and probes with a uniquely named temp
    file in the destination *directory*, so no pre-existing file is ever
    opened, truncated, or removed.

    Args:
        out_path: The resolved destination path.
        role: The flag the destination came from, quoted back in the error text.
        protects: Every file this run reads, renames, leaves behind, or writes
            somewhere else. Empty for a caller that has nothing to protect.
        creates_its_parent: True for a destination whose own writer makes its
            parent directory (``--log-file`` does, on attach), so a parent
            that is not there yet is not a refusal. The *protects* question
            above is still asked -- it is the half that has to run for every
            destination, whoever creates the directory.

    Raises:
        SystemExit: With code 2 when the destination is one of *protects*, or
            cannot be written.
    """
    destination = os.path.abspath(out_path)
    for protected in protects:
        # Resolved once, and the same resolved spelling is what the message
        # prints: a companion, a left-behind file and a target are all built by
        # joining the folder as the user spelled it, so for a relative folder
        # the raw path names no location on its own -- it would send the user
        # looking for their file against a working directory the message never
        # states, next to a destination they may have spelled a third way.
        needed = os.path.abspath(protected.path)
        if utils.paths_are_same_file(destination, needed):
            _exit_with_usage_error(
                *cli_messages.output_file_is_needed_by_the_run(
                    role, out_path, needed, protected.description
                )
            )
    out_dir = os.path.dirname(destination)
    if not os.path.isdir(out_dir):
        if creates_its_parent:
            # Nothing below can be answered about a directory that does not
            # exist yet, and the writer is about to make it.
            return
        _exit_with_usage_error(*cli_messages.output_dir_missing(role, out_dir))
    # Caught here rather than at the write: a directory passes every check below
    # (it exists, its parent is writable) and would only fail after the batch had
    # already been analyzed and paid for.
    if os.path.isdir(out_path):
        _exit_with_usage_error(*cli_messages.output_is_a_directory(role, out_path))
    # Windows refuses to unlink or rename over a read-only file, so the write
    # sequence really does need this one. POSIX gates both on the directory,
    # which the probe below already covers, and would abort a run that works.
    if os.name == "nt" and os.path.exists(out_path) and not os.access(out_path, os.W_OK):
        _exit_with_usage_error(*cli_messages.output_not_writable(role, out_path))
    # A unique probe, not ``out_path + ".tmp"``: that name is the atomic write's
    # own temp file, and opening it "w" would truncate a real file that happens
    # to be sitting there before deleting it outright.
    try:
        with tempfile.NamedTemporaryFile(dir=out_dir, prefix=".photokin-preflight-", suffix=".tmp"):
            pass
    except OSError as exc:
        _exit_with_usage_error(
            *cli_messages.output_destination_not_writable(
                role, out_path, exc.strerror or str(exc)
            )
        )


def _analysis_protected_paths(
    resolved: ResolvedInput, loaded_manifest: dict | None, args: argparse.Namespace
) -> list[_ProtectedPath]:
    """Return every file an analysis or ``--generate-manifest`` run depends on.

    Built from what the run already knows at the top of ``main`` -- the input
    it resolved, the manifest it loaded, the flags it parsed -- because that
    is the last point above ``--log-file``'s truncating attach, and a guard
    below that attach cannot save the file the log lands on.

    The sidecar destinations are a deliberate superset: ``--sidecar-md auto``
    writes only some of the ``.md`` paths it could, and which ones is not
    knowable until the model has answered. Refusing a destination the run
    might have written is the correct side of that to be wrong on.

    Args:
        resolved: The run's input, already classified.
        loaded_manifest: The manifest document, for manifest input; ``None``
            otherwise.
        args: The parsed namespace, read for the read-only paths it names and
            for the two sidecar switches.

    Returns:
        One entry per file, in the order a message should prefer to name them:
        what the run reads first, what it would write beside them after.
    """
    protected: list[_ProtectedPath] = []
    if resolved.kind == "folder":
        images = utils.list_folder_images(resolved.path)
    elif resolved.kind == "manifest":
        protected.append(_ProtectedPath(resolved.path, "the manifest this run reads"))
        items = (loaded_manifest or {}).get("items") or []
        images = [path for item in items if (path := normalize_path(item.get("path")))]
    else:
        images = [resolved.path, *([args.back] if args.back else [])]
    protected.extend(_ProtectedPath(path, "a photo this run reads") for path in images)
    for flag, value, noun in (
        ("--meta", args.meta, "the metadata"),
        ("--photo-context-file", args.photo_context_file, "the photo context"),
        ("--cancel-file", args.cancel_file, "the cancel sentinel"),
    ):
        if value:
            protected.append(_ProtectedPath(value, f"{noun} this run reads ({flag})"))
    if args.output_sidecars:
        protected.extend(
            _ProtectedPath(
                f"{os.path.splitext(path)[0]}.json",
                "a sidecar this run writes (--output-sidecars)",
            )
            for path in images
        )
    if args.sidecar_md != utils.SIDECAR_MD_OFF:
        protected.extend(
            _ProtectedPath(
                sidecar_path_for(path), "a transcript this run writes (--sidecar-md)"
            )
            for path in images
        )
    return protected


def _write_generated_manifest(manifest: dict, out_path: str) -> None:
    """Write *manifest* to *out_path*, atomically and always pretty-printed.

    Pretty regardless of ``Config.pretty_json``, which the other two JSON writes
    honor: this file exists to be read and hand edited, so it does not follow
    ``--pretty-json`` the way they do. Written through a
    sibling temp file and ``os.replace`` so an
    interrupted write never leaves a truncated manifest behind, matching how the
    aggregate ``.json`` output is written. ``os.replace`` overwrites an existing
    destination atomically on Windows as well as POSIX, so it is called against
    the live file rather than after unlinking it: unlinking first would open a
    window in which neither the old manifest nor the new one exists, which is
    the very failure the temp file is here to prevent.

    Args:
        manifest: The synthesized manifest document.
        out_path: Destination, already pre-flighted.
    """
    tmp_path = out_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_path, out_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _write_aggregate_json(data: dict, out_path: str, *, pretty: bool) -> None:
    """Write the whole result set to *out_path* through a sibling temp file.

    No unlink ahead of the replace: ``os.replace`` overwrites atomically on
    Windows as well as POSIX, so removing first only opens a window in which the
    caller's previous results file is gone and the new one does not exist yet --
    and if the replace then fails, the ``finally`` clears the temp file and the
    run ends with neither.

    Args:
        data: The aggregate the stream returned.
        out_path: Destination, already pre-flighted.
        pretty: Whether to indent the document.
    """
    tmp_path = out_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2 if pretty else None, ensure_ascii=False)
        os.replace(tmp_path, out_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _generate_manifest(resolved: ResolvedInput, args: argparse.Namespace, cfg: Config) -> None:
    """Describe how this run's input would be grouped, then stop.

    The manifest written here holds the very ``items`` list the analysis path
    would have processed -- same builder, same order, same keys -- so it is a
    description of the run rather than a separate rendering of it, and re-running
    it with the file as the input reproduces the run exactly. Only the input is
    described: provider, model and debug settings are run settings, and baking
    them in would let a generated file silently override a later run's flags.

    ``-r`` fills each item's ``metadata`` from the file itself before the write,
    so feeding the document back reproduces the same grouping and the same
    forwarded metadata without needing ``-r`` again. The flag is not recorded:
    the metadata is the evidence that a read happened, and a run setting in the
    file would be exactly the silent override the rule above forbids.

    Args:
        resolved: The run's input, already validated.
        args: Parsed CLI arguments.
        cfg: The run configuration, with photo context already resolved.

    Raises:
        SystemExit: With code 2 for every usage error.
    """
    if resolved.kind == "manifest":
        _exit_with_usage_error(*cli_messages.generate_manifest_with_manifest_input())
    out_path = args.generate_manifest
    if not out_path.lower().endswith(".json"):
        _exit_with_usage_error(*cli_messages.generate_manifest_extension(out_path))
    _preflight_output_file(
        out_path,
        role="--generate-manifest",
        protects=_analysis_protected_paths(resolved, None, args),
    )

    if resolved.kind == "folder":
        manifest = build_folder_manifest(resolved.path, photo_context_text=cfg.photo_context_text)
    else:
        # Loaded eagerly, exactly as the analysis path does, so an unreadable or
        # malformed --meta still exits 2 before anything is written.
        orig_meta = load_json(args.meta) if args.meta else None
        manifest = build_single_photo_manifest(
            resolved.path,
            args.back,
            meta=orig_meta,
            photo_context_text=cfg.photo_context_text,
        )

    if args.read:
        # Hydrated before the bucketing and before the write, so the document
        # written and the grouping reported describe the same items. Under
        # --dry-run the pre-flight still runs -- matching the analysis path,
        # where --dry-run is deliberately not exempt -- but no subprocess starts
        # and nothing is written; grouping does not read metadata, so the group
        # count is unaffected either way.
        ecfg = _resolve_exiftool_config(args)
        _preflight_exiftool(ecfg, changeset_requested=False, read_requested=True)
        if not args.dry_run:
            hydrate_item_metadata(manifest["items"], ecfg)

    # Bucketed before the write, not after: the group count is what the user is
    # checking when they reach for this flag, and resolving the entries is also
    # what reports an explicit override that disagrees with a filename -- most
    # usefully a --back the grammar reads as a front.
    buckets = build_manifest_buckets(manifest["items"], group_by=cfg.group_by)
    if args.dry_run:
        # The flag promises no destination is touched, and this branch's
        # destination is a file the user may well have hand edited. The grouping
        # is still reported, since that is what the flag pair is asking for.
        logger.info(
            "--dry-run: would write a manifest for %d file(s) in %d group(s) to %s; "
            "nothing was written.",
            len(manifest["items"]),
            len(buckets),
            out_path,
        )
        return
    _write_generated_manifest(manifest, out_path)
    logger.info(
        "Wrote manifest for %d file(s) in %d group(s) to %s; no model call was made.",
        len(manifest["items"]),
        len(buckets),
        out_path,
    )


def _suggest_the_normal_run(args: argparse.Namespace, argv: list[str]) -> str | None:
    """Return the ``-rw`` command to advise, or None when the run has declared itself.

    ``photokin <input>`` with nothing else analyzes, prints JSON and leaves the
    files untouched. That is the documented way to check the wiring before
    spending anything on it and it is not changed here; what it lacks is the next
    step, which this supplies as one row of the plan rather than as a warning,
    because nothing is wrong.

    Silence is the answer for anyone who has already said what they want. Reading,
    writing, previewing the plan, or naming a destination are all decisions, and
    ``-rw`` is not news to a run that made one. Two of the flags are treated as
    declarations at *either* value, which the write-half of the run does not:
    ``--changeset false`` and ``--exiftool-write false`` say no just as plainly as
    ``true`` says yes, and appending ``-rw`` beside either is refused outright by
    :func:`cli_messages.write_bundle_contradiction` -- so a note offered there
    would print a command that exits 2. ``--generate-manifest`` needs no entry at
    all: it writes its manifest and returns above the plan, so the row this would
    suppress is never built. The pre-flight refusals behave the same way, exiting
    before the plan exists.

    Nothing here consults ``sys.stdout.isatty()``, and that is a decision rather
    than an omission. The note is a row of the plan summary, which goes to
    *stderr*: redirecting stdout does not move it, and hiding one row of a block
    printed on the other stream would make the block's shape turn on something it
    is not printed to. A shell redirect is also a choice about where the JSON
    lands rather than a statement about the photos -- ``--output-file`` is spelled
    in photokin's own vocabulary and means the flag table was read, while
    ``> results.json`` is a shell habit, and the reader who types it and then
    wonders why Lightroom shows nothing is precisely who the note is for. Staying
    unconditional also keeps one command's output identical everywhere, including
    under the tests, whose captured stdout is never a terminal and would
    otherwise exercise only the silent branch.

    Args:
        args: The parsed namespace, read before ``-w``'s expansion is written
            back over it so every value here is one the user actually typed.
        argv: The argv that produced it, without the program name. The suggestion
            is built from these tokens rather than from the resolved values, so
            what is printed is the command that was typed with two flags added.

    Returns:
        The suggested command line, or None when nothing should be printed.
    """
    declared = (
        args.read
        or args.write
        or args.dry_run
        or args.changeset is not None
        or args.exiftool_write is not None
        or bool(args.output_file)
        or args.output_sidecars
        # Same reasoning as --output-sidecars just above: naming a non-default
        # --sidecar-md value is a stated "write this additional output" intent,
        # not something -rw would be news to.
        or args.sidecar_md != utils.SIDECAR_MD_OFF
    )
    if declared:
        return None
    return cli_messages.normal_run_command(argv)


def _render_output_clause(out_path: str | None) -> str:
    """Return the plan summary's ``output`` line for this destination.

    Args:
        out_path: The ``--output-file`` value, or ``None``.

    Returns:
        The destination and what it will hold, or ``"stdout"``.
    """
    if not out_path:
        return "stdout"
    absolute = os.path.abspath(out_path)
    if out_path.lower().endswith(".ndjson"):
        return f"{absolute} (ndjson, one record per finished photo)"
    return f"{absolute} (json, one object written at the end)"


def _render_read_clause(read_requested: bool) -> str:
    """Return the plan summary's ``read`` line, naming the tags ``-r`` will read.

    Stated on every run, including the runs that read nothing. Manifest input
    used to hydrate unasked, so a plugin that never passed ``-r`` needs to see
    that it now reads nothing -- the same mechanism C2 used to announce the
    flipped write default rather than a separate deprecation warning.

    Args:
        read_requested: True when ``-r`` was given.

    Returns:
        The read set, or a ``none (...)`` clause stating what turned it off.
    """
    if read_requested:
        return "ExifTool " + ", ".join(DEFAULT_EXIFTOOL_FIELDS)
    return "none (-r not given)"


def _render_sidecar_clause(sidecar_md: str) -> str:
    """Describe what ``--sidecar-md`` will create, for the plan summary.

    Args:
        sidecar_md: The resolved mode, one of :data:`utils.SIDECAR_MD_VALUES`.

    Returns:
        A one-line clause naming what gets written and where, or a
        ``none (...)`` clause saying which flag value said no.
    """
    if sidecar_md == utils.SIDECAR_MD_ALL:
        return "<image stem>.md beside every analyzed image except crops (--sidecar-md all)"
    if sidecar_md == utils.SIDECAR_MD_AUTO:
        return (
            "<image stem>.md beside each image of a group the model calls "
            "Document or Postcard (--sidecar-md auto)"
        )
    return "none (--sidecar-md off)"


def _render_write_clause(
    ecfg: ExiftoolConfig,
    *,
    changeset_requested: bool,
    exiftool_write: str | None,
    dry_run: bool,
) -> str:
    """Return the plan summary's ``write`` line, naming why nothing is written.

    The "defaults to false" spelling is the only place the flipped default is
    announced, which is deliberate: it is guaranteed to be printed before the
    run, so it needs no second warning.

    Args:
        ecfg: The resolved ExifTool configuration.
        changeset_requested: True when ``--changeset true`` was resolved.
        exiftool_write: The resolved ``--exiftool-write`` value, or ``None``.
        dry_run: Whether the run stops after the summary.

    Returns:
        The write set, or a ``none (...)`` clause stating what turned it off.
    """
    planned = _writes_are_planned(ecfg, changeset_requested=changeset_requested)
    if dry_run and planned:
        return "none (--dry-run)"
    if planned:
        return "ExifTool " + ", ".join(ecfg.fields)
    if exiftool_write == "false":
        return "none (--exiftool-write false)"
    if exiftool_write is None and changeset_requested:
        return "none (--exiftool-write defaults to false)"
    return "none"


def _apply_exiftool_changeset(
    *,
    ecfg: ExiftoolConfig,
    changeset_path: str | None,
    out_path: str | None,
    strict: bool = False,
    batch_id: str | None = None,
) -> bool:
    """Apply routed fields from a changeset using ExifTool and append a status record.

    Args:
        ecfg: The resolved ExifTool configuration for this run.
        changeset_path: The changeset to apply, or ``None`` when none was written.
        out_path: The run's output destination, used to route the status record.
        strict: Whether a total write failure should be reported to the caller.
            False for manifest input, which keeps the plug-in's contract.
        batch_id: ``--batch-id``, stamped onto the status record the same way
            every other record in this file gets it.

    Returns:
        True when the run should be treated as failed: writes were requested,
        files were seen, and not one of them was written. A *partial* failure
        returns False and stays in the records, because some files did get their
        metadata and per-file trouble is ordinary -- one locked file, one corrupt
        image. Zero written out of many seen is not ordinary; it means the
        configuration was wrong (an unwritable tag, a missing binary, a read-only
        tree) and every file failed for the same reason.
    """
    if not changeset_path or not os.path.isfile(changeset_path):
        return False

    if not ecfg.enabled or not ecfg.fields:
        return False

    exiftool_path = ecfg.path or "(auto-detect)"
    logger.info(
        "[ExifTool] Starting apply: binary=%s fields=%s changeset=%s",
        exiftool_path,
        list(ecfg.fields),
        changeset_path,
    )
    try:
        exif_summary = apply_changeset(
            changeset_path,
            ecfg,
            enabled=True,
            fields=ecfg.fields,
        )
    except (FileNotFoundError, OSError, ValueError, RuntimeError) as exc:
        logger.warning("ExifTool apply failed: %s", exc)
        # The whole apply step raised, so nothing was written -- the same
        # outcome the counters below describe, reached a different way.
        return strict

    files_seen = int(exif_summary.get("files_seen") or 0)
    files_written = int(exif_summary.get("files_written") or 0)
    tags_written = int(exif_summary.get("tags_written") or 0)
    raw_errors = exif_summary.get("errors")
    raw_warnings = exif_summary.get("warnings")
    errors = raw_errors if isinstance(raw_errors, list) else []
    warnings = raw_warnings if isinstance(raw_warnings, list) else []
    logger.info(
        "[ExifTool] Apply result: files_seen=%d files_written=%d "
        "tags_written=%d errors=%d warnings=%d",
        files_seen,
        files_written,
        tags_written,
        len(errors),
        len(warnings),
    )
    if errors:
        logger.error("[ExifTool] Errors: %s", json.dumps(errors, ensure_ascii=False))
    if warnings:
        logger.warning("[ExifTool] Warnings: %s", json.dumps(warnings, ensure_ascii=False))

    status_record = {"run": "exiftool_apply", "summary": exif_summary}
    if out_path and out_path.lower().endswith(".ndjson"):
        _append_ndjson_record(out_path, status_record, batch_id=batch_id)
    else:
        logger.info("[ExifTool] Apply status: %s", json.dumps(status_record, ensure_ascii=False))

    # Reported after the status record is written, not instead of it: the
    # summary is how a caller finds out which files failed and why, and it has
    # to survive the run being called a failure.
    return strict and files_seen > 0 and files_written == 0


def _refuse_rename_mode_conflicts(args: argparse.Namespace) -> None:
    """Refuse combining ``--rename``, its executor commands, or ``--generate-manifest``.

    Each of these runs on its own and stops the run before a provider client
    can be built (``docs/rename-mode.md`` section 7, mirroring the
    ``--generate-manifest`` precedent) -- so, unlike an ordinary flag, two of
    them together is not "do both", it is "which one actually ran".

    Args:
        args: The parsed namespace, before any of the four branches runs.

    Raises:
        SystemExit: With code 2 when more than one mode flag was given.
    """
    modes = [
        ("--rename", args.rename is not None),
        ("--rename-undo", args.rename_undo is not None),
        ("--rename-resume", args.rename_resume is not None),
        ("--rename-finish", args.rename_finish is not None),
    ]
    given = [name for name, present in modes if present]
    if len(given) > 1:
        _exit_with_usage_error(*cli_messages.rename_mode_conflict(given[0], given[1]))
    if given and args.generate_manifest:
        _exit_with_usage_error(
            *cli_messages.rename_mode_conflict(given[0], "--generate-manifest")
        )


def _refuse_executor_dry_run(args: argparse.Namespace) -> None:
    """Refuse ``--dry-run`` on the three executor commands; each of them writes.

    ``--rename -w --dry-run`` already rehearses faithfully, through
    ``rename_apply.apply_plan``'s own ``dry_run`` parameter (preflight and
    count, nothing written). ``finish_plan``, ``undo_run`` and ``resume_run``
    -- the executors behind ``--rename-finish``/``--rename-undo``/
    ``--rename-resume`` -- have no such parameter: each one opens a fresh
    journal segment and starts moving files the moment it runs, so there is
    no non-destructive path through them for this module to drive. Refusing
    keeps the global promise (``--dry-run`` touches no destination) instead
    of a rehearsal that would have to guess at, or reimplement, their
    two-phase mechanics from the outside.

    Args:
        args: The parsed namespace, checked before any of the three branches
            runs.

    Raises:
        SystemExit: With code 2 when ``--dry-run`` was given alongside one of
            them.
    """
    if not args.dry_run:
        return
    for flag, given in (
        ("--rename-finish", args.rename_finish is not None),
        ("--rename-undo", args.rename_undo is not None),
        ("--rename-resume", args.rename_resume is not None),
    ):
        if given:
            _exit_with_usage_error(*cli_messages.rename_executor_dry_run_refused(flag))


def _exit_with_rename_preflight_error(exc: rename_apply.RenamePreflightError) -> NoReturn:
    """Report a :class:`rename_apply.RenamePreflightError` and exit 2.

    The exception's own ``str()`` is already the house error-message shape
    (a one-line problem, a one-line fix, then the specific paths or names
    that failed each on their own line -- see the class's docstring), so it
    is logged verbatim rather than re-split into a ``(problem, remedy)`` pair.

    Args:
        exc: The refusal raised before anything on disk was touched.

    Raises:
        SystemExit: Always, with exit code 2.
    """
    logger.error("%s", str(exc))
    sys.exit(2)


def _resolve_companion_extensions(value: str | None) -> frozenset[str]:
    """Return the companion-extension set ``--companions`` extends (4.6, 7).

    Args:
        value: The raw ``--companions`` value (``"pdf,csv"`` or similar), or
            ``None`` when the flag was not given.

    Returns:
        :data:`DEFAULT_COMPANION_EXTENSIONS`, plus every extension named in
        *value*, lower-cased and dot-prefixed.
    """
    if not value:
        return DEFAULT_COMPANION_EXTENSIONS
    extra = set()
    for token in value.split(","):
        token = token.strip().lower()
        if not token:
            continue
        extra.add(token if token.startswith(".") else f".{token}")
    return DEFAULT_COMPANION_EXTENSIONS | frozenset(extra)


def _rename_folder_from_items(items: list[RenameItem], resolved: ResolvedInput) -> str:
    """Return the folder a manifest's items live in, which is the one renamed.

    A manifest says where its images are; where the manifest itself was saved
    says nothing about them. Reading the folder off ``ResolvedInput.directory``
    -- right for a changeset, which belongs beside the manifest -- pointed
    rename mode at the wrong directory for the shape the contract's own example
    uses: a manifest exported by a catalog application, written wherever that
    application put it, listing images in an archive elsewhere. Every group then
    failed validation as "members sit in different folders", and the disk
    listing that finds companions and bystanders scanned a directory holding
    neither.

    Args:
        items: The manifest's rename items, already built.
        resolved: The run's input, used only as the fallback for a manifest
            that lists nothing.

    Returns:
        The directory of the first item in name order, or the manifest's own
        directory when there are no items. A manifest whose items genuinely
        span directories still fails in the planner, which is where that rule
        lives (plan section 4.6).
    """
    directories = sorted(
        {os.path.dirname(os.path.abspath(item.path)) for item in items if item.path}
    )
    return directories[0] if directories else resolved.directory


def _list_all_files(folder: str) -> list[str]:
    """Return every file directly inside *folder*, images and non-images alike.

    ``rename.plan_rename``'s ``disk_files`` needs the whole listing, not just
    the images :func:`utils.list_folder_images` returns -- it is how a
    companion or a same-stem non-companion (4.6's "left behind") is found.

    Args:
        folder: The directory to scan.

    Returns:
        Absolute paths, in no particular order -- the planner sorts its own
        copy by name before it matters (see ``rename._name_key``).
    """
    with os.scandir(folder) as entries:
        names = [entry.name for entry in entries if entry.is_file()]
    return [os.path.join(folder, name) for name in names]


def _stat_for_rename(path: str) -> tuple[int | None, float | None]:
    """Return ``(size, mtime)`` for *path*, or ``(None, None)`` if it cannot be stat'd.

    Read fresh off disk rather than trusted from a manifest: these values
    become the plan's own preflight record (``RenameItem.size``/``.mtime``),
    which is what ``rename_apply.preflight`` compares the folder against
    before touching anything -- a manifest-supplied value would let a plan
    lie about the very drift that check exists to catch.

    Args:
        path: The file to stat.

    Returns:
        Its current size and mtime, or ``(None, None)`` on any OS error --
        non-fatal here; a missing file surfaces as a preflight refusal (or,
        for a bare preview, is simply absent from the folder listing) rather
        than as a crash while planning.
    """
    try:
        stat = os.stat(path)
    except OSError as exc:
        logger.debug("Could not stat %s for rename planning: %s", path, exc)
        return None, None
    return stat.st_size, stat.st_mtime


def _build_rename_items_from_folder(folder: str) -> list[RenameItem]:
    """Build the planner's items from a folder input: every image, no overrides.

    Args:
        folder: The folder being renamed.

    Returns:
        One :class:`RenameItem` per image, in ``utils.list_folder_images``
        order (which the planner re-derives itself when ``order_mode`` is
        ``"name"``; this order is only ever read for its side effect of
        stat'ing each file once).
    """
    items: list[RenameItem] = []
    for path in utils.list_folder_images(folder):
        size, mtime = _stat_for_rename(path)
        items.append(RenameItem(path=path, size=size, mtime=mtime))
    return items


#: Manifest-item boolean flags photokin already reads as tri-state (mirrors
#: ``core._coerce_manifest_bool``'s accepted spellings; kept local rather
#: than imported so rename mode does not reach into the analysis grouper's
#: internals for one small parse).
_MANIFEST_TRUE_TOKENS = frozenset({"true", "1", "yes", "y"})
_MANIFEST_FALSE_TOKENS = frozenset({"false", "0", "no", "n"})


def _manifest_bool(value: Any) -> bool | None:
    """Read a tri-state boolean flag off a manifest item's raw JSON value.

    Args:
        value: The raw value, whatever JSON type it arrived as.

    Returns:
        The flag's value, or ``None`` when it is absent, null, or not one of
        the recognized spellings -- all of which mean "no override".
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _MANIFEST_TRUE_TOKENS:
            return True
        if token in _MANIFEST_FALSE_TOKENS:
            return False
    return None


def _build_rename_items_from_manifest(document: dict[str, Any]) -> list[RenameItem]:
    """Build the planner's items from a manifest's ``items`` list (4.1, 6.1).

    Args:
        document: The loaded manifest, already validated by
            :func:`_load_manifest_input` (every item has a ``path`` that
            exists on disk).

    Returns:
        One :class:`RenameItem` per item, carrying its own ``metadata``,
        ``order``, ``is_back``, ``version`` and ``preferred`` overrides
        exactly as section 4.1 describes them.
    """
    items: list[RenameItem] = []
    for raw in document.get("items") or []:
        if not isinstance(raw, dict):
            continue
        path = normalize_path(raw.get("path") or "") or ""
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else None
        order = raw.get("order")
        order_value = order if isinstance(order, int) and not isinstance(order, bool) else None
        version = raw.get("version")
        version_value = version if isinstance(version, str) else None
        size, mtime = _stat_for_rename(path)
        items.append(
            RenameItem(
                path=path,
                metadata=metadata,
                order=order_value,
                is_back=_manifest_bool(raw.get("is_back")),
                version=version_value,
                preferred=bool(raw.get("preferred")),
                size=size,
                mtime=mtime,
            )
        )
    return items


#: Matches ``{date}``/``{date:FORMAT}`` in a raw prefix template, case
#: insensitively -- a lightweight echo of ``rename._tokenize_prefix_template``
#: used only to decide whether the folder needs its dates hydrated (4.1's
#: "when the template uses {date}"). Good enough for that one yes/no
#: question even though, unlike the real tokenizer, it does not understand
#: ``{{`` escaping: the cost of a false positive here is one harmless extra
#: ExifTool read, not a wrong plan -- the planner re-derives everything from
#: the template itself.
_DATE_TOKEN_RE = re.compile(r"\{\s*date\s*(?::[^}]*)?\s*\}", re.IGNORECASE)


def _template_uses_date(template: str) -> bool:
    """Whether *template* references ``{date}``, per :data:`_DATE_TOKEN_RE`."""
    return bool(_DATE_TOKEN_RE.search(template))


def _hydrate_rename_dates(items: Sequence[RenameItem], ecfg: ExiftoolConfig) -> None:
    """Best-effort fill ``EXIF:DateTimeOriginal`` for items that have none (4.1).

    One ExifTool call for the whole folder (``run_exiftool_json`` batches
    internally when the file list is large), reading only the one tag the
    planner's ``{date}`` token needs -- a read, not a write, so this needs no
    permission flag and runs whether or not ``-r`` was given.

    Non-fatal by design, matching ``exiftool.hydrate.hydrate_item_metadata``:
    a missing binary or a failed read leaves the affected groups exactly as
    undated as they already were, which the planner already turns into a
    named error (or, with ``--undated``, a literal) rather than a crash here.

    Args:
        items: The planner's items, mutated in place -- each one missing a
            date gets ``metadata["EXIF:DateTimeOriginal"]`` filled when
            ExifTool has one to offer.
        ecfg: The resolved ExifTool configuration for this run.
    """
    # Imported here rather than at module scope so a test's patch onto
    # photokin.exiftool.manifest.run_exiftool_json is the one this call
    # reaches -- the same reasoning hydrate_item_metadata's own import gives.
    from .exiftool.manifest import _find_tag_value, run_exiftool_json

    needing = [
        item
        for item in items
        if not (
            isinstance(item.metadata, dict)
            and isinstance(item.metadata.get("EXIF:DateTimeOriginal"), str)
            and item.metadata["EXIF:DateTimeOriginal"].strip()
        )
    ]
    if not needing:
        return
    try:
        exiftool_path = resolve_exiftool_path(ecfg)
    except OSError as exc:
        logger.warning("Skipping rename date hydration: %s", exc)
        return

    by_path: dict[str, list[RenameItem]] = {}
    for item in needing:
        by_path.setdefault(normalize_path(item.path) or item.path, []).append(item)
    try:
        records = run_exiftool_json(
            exiftool_path=exiftool_path,
            files=[item.path for item in needing],
            fields=["EXIF:DateTimeOriginal"],
            timeout_sec=max(60, len(needing) * 2),
        )
    except (FileNotFoundError, RuntimeError, OSError) as exc:
        logger.warning("Skipping rename date hydration: ExifTool read failed: %s", exc)
        return

    for record in records:
        source = record.get("SourceFile") or ""
        if not source:
            continue
        value = _find_tag_value(record, "EXIF:DateTimeOriginal")
        if not isinstance(value, str) or not value.strip():
            continue
        for item in by_path.get(normalize_path(source) or source, []):
            if not isinstance(item.metadata, dict):
                item.metadata = {}
            item.metadata["EXIF:DateTimeOriginal"] = value


#: ``-w``'s rename-mode expansion (docs/rename-mode.md section 7): a second
#: bundle of the same shape as :data:`_WRITE_BUNDLE`, for which
#: :data:`_VERBOSE_BUNDLE` is the precedent for a second one. Its other
#: member -- actually applying the plan -- has no CLI spelling of its own
#: (the brief for this build is explicit: no ``--rename-apply`` for a user
#: to type), so there is nothing for a contradicting flag to name; only
#: ``--changeset`` can disagree with ``-w`` here.
_RENAME_WRITE_BUNDLE: dict[str, _WriteBundleMember] = {
    "changeset": _WriteBundleMember("true", "record", "--changeset true"),
}


def _resolve_rename_write_bundle(args: argparse.Namespace) -> tuple[str, bool]:
    """Expand ``-w`` in rename mode: ``--changeset true``, and apply the plan.

    Args:
        args: The parsed namespace.

    Returns:
        ``(changeset, apply_requested)``. ``changeset`` is ``"true"`` or
        ``"false"``; ``apply_requested`` is ``args.write`` itself, since
        nothing else can set or contradict it.

    Raises:
        SystemExit: With code 2 when ``-w`` is given beside a contradicting
            ``--changeset false``.
    """
    values: dict[str, str | None] = {dest: getattr(args, dest) for dest in _RENAME_WRITE_BUNDLE}
    if args.write:
        for dest, member in _RENAME_WRITE_BUNDLE.items():
            given = values[dest]
            if given is not None and given != member.value:
                _exit_with_usage_error(
                    *cli_messages.rename_write_bundle_contradiction(_flag_spelling(dest), given)
                )
            values[dest] = member.value if given is None else given
    return values["changeset"] or "false", bool(args.write)


def _rename_protected_paths(
    resolved: ResolvedInput, folder: str, plan: dict[str, Any], disk_files: Sequence[str]
) -> list[_ProtectedPath]:
    """Return every file a rename run reads, renames, leaves behind or recovers from.

    Built from the plan, because the plan is the first and only place the run
    knows what it covers -- and it is still ahead of every write, so a
    destination that lands on any of these is refused before the plan file or
    the changeset is opened.

    Four kinds of file are easy to miss and were each lost in turn: a file the
    run only *reports* as left behind is still that file's only copy; a photo
    the manifest never listed appears nowhere in ``entries``, so only the disk
    listing sees it; a journal from an earlier run is the undo record for that
    rename; and a *target* is a path this run is about to move a file onto.
    The target case loses no data today -- the executor's own preflight
    refuses an occupied target -- but it refuses after the plan file has
    already been written, blaming the folder for what the flag did.

    Paths are returned in whatever spelling their source used: ``entries``
    carry absolute paths while companion and ``left_behind`` records are
    joined onto the caller's *folder*, which may be relative. The guard
    resolves each one, so no normalization is done here.

    Args:
        resolved: The run's input, so manifest input protects its own document.
        folder: The folder being renamed.
        plan: The plan :func:`plan_rename` just built.
        disk_files: Every file directly inside *folder*, as listed for the plan.

    Returns:
        One entry per file. Order is load-bearing: the guard reports the first
        match, so the descriptions that pin a file most precisely come first
        and a file listed twice (a planned photo whose manifest spelling the
        bystander scan did not recognize) is still named by what it really is.
    """
    protected: list[_ProtectedPath] = []
    if resolved.kind == "manifest":
        protected.append(_ProtectedPath(resolved.path, "the manifest this run reads"))
    planned: set[str] = set()
    # Names, not paths: a target is a bare filename inside *folder*, and it is
    # kept aside so the files that really exist are described first.
    target_names: list[str] = []
    for entry in plan["entries"]:
        planned.add(os.path.normpath(os.path.abspath(entry["path"])))
        protected.append(_ProtectedPath(entry["path"], "a photo this run would rename"))
        for companion in entry.get("companions") or ():
            protected.append(
                _ProtectedPath(companion["path"], "a companion file this run would rename")
            )
            target_names.append(companion["target"])
        # None for a group the planner could not render, which names nothing.
        if entry["target"]:
            target_names.append(entry["target"])
    protected.extend(
        _ProtectedPath(record["path"], "a file this run leaves behind")
        for record in plan["left_behind"]
    )
    protected.extend(
        _ProtectedPath(path, "a photo in this folder this run did not plan")
        for path in disk_files
        if os.path.splitext(path)[1].lower() in utils.VALID_EXTS
        and os.path.normpath(os.path.abspath(path)) not in planned
    )
    protected.extend(
        _ProtectedPath(path, "a rename journal --rename-undo would read")
        # Private only because nothing outside rename_apply had asked before;
        # latest_journal, the public one, answers for a single journal and
        # every one of them is an undo record this run must not overwrite.
        for path in rename_apply._journal_candidates(folder)
    )
    protected.extend(
        _ProtectedPath(
            os.path.join(folder, name), "a path this run would rename a file to"
        )
        for name in target_names
    )
    return protected


def _write_rename_changeset_records(changeset_path: str, plan: dict[str, Any]) -> None:
    """Write one ``kind: rename`` record per changed entry (section 6.3).

    The rename journal (``rename_apply``) is the operational record; this is
    the audit one, in the same NDJSON stream a metadata write's changeset
    already uses -- so a rename shows up beside every other proposed change
    to a folder, not in a file of its own.

    Called once per run, and only for a run whose renames really happened
    (or, without ``-w``, never happened at all): a record here is an assertion
    about the folder that outlives the run, so it is written after the apply
    it describes, not before. It truncates the changeset itself, the way every
    other mode opens that file fresh for its own run.

    Args:
        changeset_path: The changeset file this run writes.
        plan: The plan just built (section 6.2).
    """
    run_id = str(plan.get("run_id") or "")
    created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(changeset_path, "w", encoding="utf-8") as handle:
        for entry in plan.get("entries") or []:
            if not entry.get("changed") or not entry.get("target"):
                continue
            record = {
                "schema_version": _CHANGESET_SCHEMA_VERSION,
                "kind": "rename",
                "run_id": run_id,
                "created_at": created_at,
                "photo_id": entry.get("photo_id"),
                "from": os.path.basename(str(entry.get("path") or "")),
                "to": entry.get("target"),
                "companions": [
                    {
                        "from": os.path.basename(str(companion.get("path") or "")),
                        "to": companion.get("target"),
                    }
                    for companion in entry.get("companions") or []
                ],
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _run_rename_mode(args: argparse.Namespace) -> None:
    """``--rename``: plan the input's rename, preview it, and apply it under ``-w``.

    Follows the ``--generate-manifest`` precedent named in the module
    docstring -- runs before any provider client is built and stops -- with
    the one addition ``docs/rename-mode.md`` section 7 calls out: this one
    has a write path, guarded by ``-w`` exactly as the analysis run's own
    write bundle is.

    Args:
        args: The parsed namespace.

    Raises:
        SystemExit: 2 for a usage or plan-validation problem, 1 if applying
            the plan did not finish cleanly, 0 otherwise (including an
            already-clean folder).
    """
    if args.exiftool_write is not None:
        _exit_with_usage_error(*cli_messages.rename_with_exiftool_write(args.exiftool_write))
    if args.output_file:
        _exit_with_usage_error(*cli_messages.rename_with_output_file(args.output_file))
    if args.digits < 1:
        _exit_with_usage_error(*cli_messages.rename_digits_invalid(args.digits))

    today: date | None = None
    if args.today:
        try:
            today = datetime.strptime(args.today, "%Y-%m-%d").date()  # noqa: DTZ007 (date-only value, no zone to carry)
        except ValueError:
            _exit_with_usage_error(*cli_messages.rename_today_invalid(args.today))
    companions = _resolve_companion_extensions(args.companions)

    resolved = _resolve_input(args)
    if resolved.kind == "photo":
        _exit_with_usage_error(*cli_messages.rename_needs_folder_or_manifest(resolved.display))

    # Any JSON value the wrapper wrote, carried to the plan verbatim (6.1):
    # a shape this code does not recognize is still a shape that says "a
    # catalog tracks this archive", so it is typed as loosely as it is read.
    managed_by: Any = None
    if resolved.kind == "folder":
        _validate_folder_input(resolved)
        folder = resolved.path
        items = _build_rename_items_from_folder(folder)
    else:
        loaded_manifest = _load_manifest_input(resolved)
        managed_by = loaded_manifest.get("managed_by")
        items = _build_rename_items_from_manifest(loaded_manifest)
        folder = _rename_folder_from_items(items, resolved)

    changeset_flag, apply_requested = _resolve_rename_write_bundle(args)
    changeset_requested = changeset_flag == "true"

    if managed_by is not None and apply_requested:
        # The guard is on presence (6.1). ``app``/``catalog`` only sharpen the
        # wording, so they are read when the value happens to be an object and
        # skipped -- not coerced -- when it is a string, list, number or bool.
        named = managed_by if isinstance(managed_by, dict) else {}
        app = named.get("app")
        catalog = named.get("catalog")
        _exit_with_usage_error(
            *cli_messages.rename_managed_by_refuses_write(
                app if isinstance(app, str) and app else None,
                catalog if isinstance(catalog, str) and catalog else None,
            )
        )

    # Derived under --dry-run too. Where the changeset would go is a fact
    # about the invocation, not about whether this one writes it, and a
    # --plan-out landing on it is the same usage error either way -- leaving
    # the path unknown only made the dry run the weaker rehearsal. The two
    # write sites below are what keep --dry-run from writing one.
    changeset_path = _derive_changeset_path(resolved, None) if changeset_requested else None

    if _template_uses_date(args.rename):
        ecfg = _resolve_exiftool_config(args)
        _hydrate_rename_dates(items, ecfg)

    disk_files = _list_all_files(folder)
    plan = plan_rename(
        folder=folder,
        disk_files=disk_files,
        items=items,
        prefix_template=args.rename,
        digits=args.digits,
        order_mode=args.order,
        undated_literal=args.undated,
        today=today,
        companion_extensions=companions,
        managed_by=managed_by,
        photokin_version=_photokin_version(),
        disk_file_stats={path: _stat_for_rename(path) for path in disk_files},
    )

    # Every destination this run has, checked here: the plan is the first
    # point the run knows what it touches, and nothing has been written yet
    # (--plan-out is the first write, below). --dry-run is deliberately not
    # exempt, for either half -- a dry run exists to answer "what would this
    # command do", and "it would destroy the only copy of box3_017.pdf" is
    # the most important answer it could give.
    _preflight_destinations_are_distinct(
        [
            (role, path)
            for role, path in (
                ("--plan-out", args.plan_out),
                ("--changeset output", changeset_path),
                (
                    "the rename journal",
                    rename_apply.journal_path_for(folder, plan["run_id"])
                    if apply_requested
                    else None,
                ),
            )
            if path
        ]
    )
    protected = _rename_protected_paths(resolved, folder, plan, disk_files)
    if changeset_path:
        _preflight_output_file(changeset_path, role="--changeset output", protects=protected)
    if args.plan_out:
        _preflight_output_file(args.plan_out, role="--plan-out", protects=protected)

    logger.info("%s", cli_messages.render_rename_preview(plan))

    if args.plan_out and not args.dry_run:
        _write_generated_manifest(plan, args.plan_out)
    elif args.plan_out:
        # --dry-run's global promise is that no destination is touched, and
        # a plan file is a destination exactly as much as the changeset
        # already treated as one above -- the preview table above still
        # shows the plan, only the file itself is skipped.
        logger.info(
            "--dry-run: would write the rename plan to %s; nothing was written.",
            args.plan_out,
        )

    if plan["errors"]:
        # A plan that cannot be applied is a validation failure, whether or
        # not -w was even given: --rename alone is a preview, but not one a
        # caller scripting against the exit code should read as "fine".
        sys.exit(2)

    if not apply_requested:
        # --changeset true without -w records the plan as proposed and stops.
        # Nothing follows that could contradict it, so it is written here.
        if changeset_path and not args.dry_run:
            _write_rename_changeset_records(changeset_path, plan)
        return

    try:
        report = rename_apply.apply_plan(plan, dry_run=args.dry_run)
    except rename_apply.RenamePreflightError as exc:
        _exit_with_rename_preflight_error(exc)

    logger.info(
        "%s",
        cli_messages.render_rename_run_report(
            "apply",
            report.status,
            journal_path=report.journal_path,
            renamed=report.renamed,
            companions=report.companions,
            unchanged=report.unchanged,
            left_behind=report.left_behind,
            skipped=report.skipped,
            stranded=report.stranded,
            warnings=report.warnings,
        ),
    )
    if report.exit_code != 0:
        # rolled_back put every file back and needs_attention left some
        # mid-move: neither folder matches the plan, so neither gets an audit
        # record saying it does. The journal is the record for those runs.
        sys.exit(report.exit_code)

    if changeset_path and not args.dry_run:
        _write_rename_changeset_records(changeset_path, plan)


def _run_rename_finish(plan_path: str) -> None:
    """``--rename-finish PLAN``: rename companions whose images are already renamed.

    Args:
        plan_path: The plan file (as ``--plan-out`` wrote it) to read back.

    Raises:
        SystemExit: 2 for an unreadable plan or a preflight refusal, 1 if the
            run did not finish cleanly, 0 otherwise.
    """
    try:
        plan = load_json(plan_path)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _exit_with_usage_error(*cli_messages.rename_plan_unreadable(plan_path, str(exc)))
    except FileNotFoundError:
        _exit_with_usage_error(*cli_messages.input_not_found(plan_path))
    except OSError as exc:
        _exit_with_usage_error(
            *cli_messages.rename_plan_unreadable(plan_path, exc.strerror or str(exc))
        )
    if not isinstance(plan, dict) or not isinstance(plan.get("entries"), list):
        _exit_with_usage_error(*cli_messages.rename_plan_is_not_a_plan(plan_path))

    try:
        report = rename_apply.finish_plan(plan)
    except rename_apply.RenamePreflightError as exc:
        _exit_with_rename_preflight_error(exc)

    logger.info(
        "%s",
        cli_messages.render_rename_run_report(
            "finish",
            report.status,
            journal_path=report.journal_path,
            renamed=report.renamed,
            companions=report.companions,
            unchanged=report.unchanged,
            left_behind=report.left_behind,
            skipped=report.skipped,
            stranded=report.stranded,
            warnings=report.warnings,
        ),
    )
    if report.exit_code != 0:
        sys.exit(report.exit_code)


def _run_rename_undo_or_resume(args: argparse.Namespace, *, verb: str) -> None:
    """``--rename-undo``/``--rename-resume``: read a journal back and act on it.

    Args:
        args: The parsed namespace.
        verb: ``"undo"`` or ``"resume"``, both the flag's name and the
            executor call it drives.

    Raises:
        SystemExit: 2 for a usage problem or a preflight refusal, 1 if the
            run did not finish cleanly (including a resume that finds nothing
            left to finish forward -- see ``partial_forward_resume`` below),
            0 otherwise.
    """
    flag = f"--rename-{verb}"
    raw = args.rename_undo if verb == "undo" else args.rename_resume
    journal_path = raw or None

    if journal_path is None:
        resolved = _resolve_input(args)
        if resolved.kind != "folder":
            _exit_with_usage_error(
                *cli_messages.rename_command_needs_folder(flag, resolved.display)
            )
        # An undo left open by a partial catalog undo (rename-mode.md 5.5) is
        # closed ``in_progress``, not ``applied`` -- it is retried, not
        # resumed, so undo's own filter must widen to find it too. A run
        # that is ``in_progress`` for the ordinary reason (unfinished, not
        # partial) is not excluded here: ``undo_run`` itself refuses that
        # case with "finish it with --rename-resume first", which is the
        # right message and does not need this filter to pre-empt it.
        statuses = (
            (rename_apply.STATUS_APPLIED, rename_apply.STATUS_IN_PROGRESS)
            if verb == "undo"
            else tuple(rename_apply.OPEN_STATUSES)
        )
        try:
            journal_path = rename_apply.latest_journal(resolved.path, statuses)
        except rename_apply.RenamePreflightError as exc:
            _exit_with_rename_preflight_error(exc)
        if journal_path is None:
            _exit_with_usage_error(*cli_messages.rename_no_journal_found(resolved.path, verb))

    # A ``--rename-resume`` of a journal a partial undo left open on purpose
    # (rename_apply.resume_run refuses it: "there is nothing to finish
    # forward") is not a usage mistake -- the caller asked a reasonable
    # question and got a true, expected answer. It is reported as a normal
    # outcome (exit 1, the run's own report path) rather than a preflight
    # usage error (exit 2, below).
    partial_forward_resume = False
    if verb == "resume":
        try:
            journal = rename_apply.read_journal(journal_path)
        except rename_apply.RenamePreflightError as exc:
            _exit_with_rename_preflight_error(exc)
        partial_forward_resume = bool(journal.segments) and journal.last.partial

    try:
        report = (
            rename_apply.undo_run(journal_path)
            if verb == "undo"
            else rename_apply.resume_run(journal_path)
        )
    except rename_apply.RenamePreflightError as exc:
        if partial_forward_resume:
            logger.error("%s", str(exc))
            sys.exit(1)
        _exit_with_rename_preflight_error(exc)

    logger.info(
        "%s",
        cli_messages.render_rename_run_report(
            verb,
            report.status,
            journal_path=report.journal_path,
            renamed=report.renamed,
            companions=report.companions,
            unchanged=report.unchanged,
            left_behind=report.left_behind,
            skipped=report.skipped,
            stranded=report.stranded,
            warnings=report.warnings,
        ),
    )
    if report.exit_code != 0:
        sys.exit(report.exit_code)


def main() -> None:
    """CLI entry point: resolve one input, state the plan, then run it."""

    global _active_run_envelope
    # A repeated in-process main() call -- the test suite's own pattern --
    # must not inherit the previous call's envelope.
    _active_run_envelope = None
    _configure_logging()
    try:
        argv = sys.argv[1:]
        if not argv:
            # The interactive prompt is for a human at a keyboard. A headless
            # launcher (a plugin's subprocess call, a script, a scheduled task)
            # whose argument list came out empty -- e.g. a quoting bug that ate
            # every token -- is not that, and blocking it on a stdin read it
            # can never answer just trades one silent hang for another. Route
            # it to the usual usage error instead.
            if not sys.stdin.isatty():
                _exit_with_usage_error(*cli_messages.no_input_and_not_interactive())
            argv = _interactive_prompt()

        defaults = Config()
        ap = argparse.ArgumentParser(
            description="Photo Archiver CLI (single or batch).",
            epilog=_EPILOG,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )

        ap.add_argument(
            "input_path",
            nargs="?",
            metavar="INPUT",
            default=None,
            help="Folder of scans, .json manifest, or a single image. The type is "
                 "detected from the path: a directory is a folder, a .json file is a "
                 "manifest, an image file is a single photo.",
        )
        ap.add_argument(
            "--folder",
            default=None,
            help="Alias for a folder INPUT; asserts the path is a directory.",
        )
        ap.add_argument(
            "--manifest",
            default=None,
            help="Alias for a manifest INPUT; asserts the path is a .json manifest file.",
        )
        ap.add_argument(
            "--capabilities",
            action="store_true",
            help="Print this build's version, NDJSON/changeset schema versions, "
                 "canonical ExifTool tag mapping, providers and flags as JSON, "
                 "then exit -- before any input is required. For a caller that "
                 "wants to check compatibility rather than guess at it.",
        )

        ap.add_argument("--back", help="Path to the back image (single-photo input only)", default=None)
        ap.add_argument("--meta", help="Path to original metadata JSON (single-photo input only)", default=None)
        ap.add_argument("--photo-context-file", help="Path to UTF-8 text file containing authoritative photo context", default=None)
        ap.add_argument("--photo-context-text", help="Inline authoritative photo context text", default=None)

        ap.add_argument(
            "--group-by",
            choices=list(utils.GROUP_BY_VALUES),
            default=utils.GROUP_BY_OBJECT,
            help="Grouping granularity (default: %(default)s). "
                 "object: every scan of one print is one object and shares a single analysis. "
                 "pair: each rescan (print plus variant letter) is analyzed on its own. "
                 "none: every file is analyzed alone -- an escape hatch for when filenames "
                 "mis-group. It is the most expensive and the lowest quality: a back analyzed "
                 "alone is handwriting with no photo, caption, date and location inference all "
                 "lean on seeing the front, a multipage document is split into unrelated pages, "
                 "and every crop becomes its own object.",
        )
        ap.add_argument(
            "--sidecar-md",
            choices=list(utils.SIDECAR_MD_VALUES),
            # None rather than "off" so _resolve_sidecar_bundle can tell an
            # explicit value from an unset flag; it resolves None to "off".
            default=None,
            help="Write a Markdown transcript sidecar (<stem>.md) beside each analyzed "
                 "file (default: off). off: nothing new written. auto: written "
                 "when the group's category is Document or Postcard. all: written for "
                 "every emitted file, any category, except crops. Valid for every input "
                 "type -- single file, folder and manifest all flow through the same "
                 "emit loop. --sidecar-xmp and --sidecar-json are reserved spellings for "
                 "the same three values, for other sidecar formats to come.",
        )
        ap.add_argument(
            "-s",
            action="store_true",
            dest="sidecar_auto",
            help="Shorthand for --sidecar-md auto: write Markdown transcript sidecars "
                 "for the groups the model calls Document or Postcard. Combines with "
                 "the other short flags (-rws). An explicit --sidecar-md value that "
                 "contradicts it is an error rather than a guess, the same contract "
                 "-w and -v follow.",
        )
        ap.add_argument(
            "--max-images-per-call",
            type=int,
            default=defaults.max_images_per_call,
            help="Split an oversized group's images into multiple model calls of at "
                 "most this many images each, with a final text-only consolidation "
                 "pass, instead of one call carrying the whole group (default: "
                 "%(default)s). A front/back pair and a part's own variant scans are "
                 "never split across calls. 0 disables chunking: any group size is "
                 "sent in a single call, as before this flag existed.",
        )
        # Retired in 0.2.0, still accepted: the Lightroom plug-in launches
        # ``python -m photokin.cli`` and may pass either, and argparse exits 2 on
        # an unrecognized flag -- a hard crash rather than a behavior change.
        # ``--update-policy`` defaults to None so a supplied value is
        # distinguishable from the default and the warning can fire at all.
        ap.add_argument("--update-policy", choices=["master_exact", "merge_per_variant"],
                        default=None, help=argparse.SUPPRESS)
        ap.add_argument("--provider", choices=list(_PROVIDER_CHOICES), default=None,
                        help="LLM provider backend to use (default: LLM_PROVIDER, else the one "
                             "provider whose SDK is installed; with several installed this flag "
                             "is required)")
        ap.add_argument("--openai-model", default=defaults.model,
                        help="OpenAI model name (default: %(default)s)")
        ap.add_argument("--claude-model", choices=["sonnet", "haiku"], default=defaults.claude_model_name,
                        help="Claude model alias resolved via config mapping (default: %(default)s)")
        ap.add_argument("--gemini-model", default=defaults.gemini_model_name,
                        help="Gemini model name (default: %(default)s)")
        ap.add_argument("--openrouter-model", default=defaults.openrouter_model_name,
                        help="OpenRouter model slug, e.g. moonshotai/kimi-k3 or qwen/qwen3-vl-235b-a22b-instruct (default: %(default)s)")
        ap.add_argument("--jpeg-quality", type=int, default=defaults.jpeg_quality,
                        help="JPEG quality 1-100 (default: %(default)s)")
        ap.add_argument("--max-edge", type=int, default=defaults.max_edge,
                        help="Downscale longest edge before model (e.g., 1024). 0/None = keep size")
        ap.add_argument("--process-all-variants", action="store_true", help=argparse.SUPPRESS)
        ap.add_argument("--date-confidence-threshold", type=float, default=defaults.date_confidence_threshold,
                        help="Minimum confidence required to apply dates to the patch (default: %(default)s)")
        ap.add_argument("--location-confidence-threshold", type=float, default=defaults.location_confidence_threshold,
                        help="Minimum confidence required to apply locations to the patch (default: %(default)s)")
        ap.add_argument("--no-update-vocab", action="store_true", help="Do not append new keywords to TOML")

        ap.add_argument("--output-file",
                        help="Path to write results, for every input type. If it ends with .ndjson, writes one JSON object per line as items complete; if .json, writes a single JSON object at the end. Without it, results go to stdout.")
        ap.add_argument(
            "--pretty-json",
            choices=["true", "false"],
            default="true",
            help="Indent the stdout result document (and an aggregate .json --output-file) "
                 "for human reading (default: true). Pass false for compact single-line "
                 "output, e.g. when a script parses stdout itself rather than reading it "
                 "with a JSON library.",
        )
        ap.add_argument("--output-sidecars", action="store_true",
                        help="Also emit per-photo sidecar JSON next to each image")
        ap.add_argument(
            "--generate-manifest",
            metavar="PATH",
            default=None,
            help="Write the manifest folder or single-photo input would be grouped into, "
                 "then exit without calling the model.",
        )
        ap.add_argument("--batch-id", help="Optional batch identifier stored in the output metadata/logs")
        # String booleans (not store_true) because Lua passes literal "true"/"false" as args.
        # The default is None rather than "false" so ``-w`` can tell a value it may
        # fill in from one it must contradict.
        ap.add_argument(
            "--changeset",
            choices=["true", "false"],
            default=None,
            help="Write a changeset NDJSON of proposed field writes beside the output "
                 "file, or beside the input when there is none (default: false)",
        )
        ap.add_argument(
            "-w", "--write",
            action="store_true",
            dest="write",
            help="Shorthand for --changeset true --exiftool-write true: record the "
                 "proposed writes and apply them to the files.",
        )
        # Explicit, and not a member of _WRITE_BUNDLE: it reads rather than
        # writes, so -w does not expand to it and it is allowed beside
        # --generate-manifest, which is what makes that document round-trip.
        ap.add_argument(
            "-r", "--read",
            action="store_true",
            dest="read",
            help="Before analysis, read the metadata already in the files with ExifTool "
                 "(EXIF:DateTimeOriginal, EXIF:UserComment, XMP:Description, XMP:Title, "
                 "XMP:Subject) and send it to the model. Only fills values the input does "
                 "not already carry; nothing is written. Mirrors -w / --write.",
        )
        ap.add_argument(
            "-v", "--verbose",
            action="store_true",
            dest="verbose",
            help="Shorthand for --debug-dump-llm-request true --debug-dump-hydration "
                 "true, plus a default --log-file beside the other dumps: everything "
                 "this run could leave behind for debugging, in one folder.",
        )
        ap.add_argument(
            "--debug-dump-llm-request",
            choices=["true", "false"],
            default=None,
            help="Write full LLM request payload dumps to disk before each LLM call",
        )
        ap.add_argument(
            "--debug-dump-hydration",
            choices=["true", "false"],
            default=None,
            help="Write each group's assembled metadata to disk before it is merged "
                 "into a prompt -- what -r read plus whatever the manifest supplied, "
                 "one step upstream of the LLM-request dump",
        )
        ap.add_argument(
            "--debug-dump-dir",
            default=None,
            help="Directory for LLM request/hydration dump artifacts (default: "
                 "<manifest/output-dir>/debug)",
        )
        ap.add_argument(
            "--log-file",
            default=None,
            help="Duplicate the run's log output into this file, in addition to "
                 "stderr -- stderr is what a fire-and-forget subprocess launch "
                 "throws away (default: none, or <debug-dump-dir>/<batch-id>.log "
                 "under -v)",
        )
        ap.add_argument(
            "--cancel-file",
            default=None,
            help="Poll for this path before each group starts (and, once created, "
                 "for the rest of the run); its existence means stop, applying "
                 "ExifTool writes to whatever completed and exiting cleanly rather "
                 "than mid-batch.",
        )
        ap.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the plan summary and stop, before the first model call.",
        )
        ap.add_argument(
            "--exiftool-write",
            choices=["true", "false"],
            default=None,
            help="Apply changeset fields to files via ExifTool after analysis (default: env EXIFTOOL_WRITE_ENABLED, else false)",
        )
        ap.add_argument(
            "--exiftool-fields",
            default=None,
            help="Comma-separated ExifTool tags to write (default: env EXIFTOOL_FIELDS, "
                 "else every canonical tag photokin produces -- see --capabilities). "
                 "A launcher that writes some tags itself, the way the Lightroom "
                 "plug-in does, should narrow this explicitly.",
        )
        ap.add_argument(
            "--exiftool-path",
            default=None,
            help="Path to the ExifTool executable (default: env EXIFTOOL_PATH, else auto-detect)",
        )

        # --- rename mode (docs/rename-mode.md) ---
        ap.add_argument(
            "--rename",
            metavar="PREFIX",
            default=None,
            help="Mode flag: plan a grammar-aware mass rename of the input folder or "
                 "manifest's files under PREFIX (see docs/rename-mode.md), print the "
                 "preview, and stop before any model call. -w applies it.",
        )
        ap.add_argument(
            "--digits",
            type=int,
            default=3,
            metavar="N",
            help="Zero-padded number width for --rename (default: %(default)s).",
        )
        ap.add_argument(
            "--order",
            choices=["name", "natural"],
            default="name",
            help="--rename's fallback ordering when no item carries an explicit "
                 "manifest order (default: %(default)s). natural compares digit runs "
                 "numerically, so file9 precedes file10.",
        )
        ap.add_argument(
            "--undated",
            metavar="LITERAL",
            default=None,
            help="--rename: stand-in for {date} in a group with no date, instead of "
                 "refusing to plan it.",
        )
        ap.add_argument(
            "--today",
            metavar="YYYY-MM-DD",
            default=None,
            help="--rename: override {today} (default: the run's own date), so a "
                 "batch scanned earlier can carry its own date and a plan stays "
                 "reproducible.",
        )
        ap.add_argument(
            "--companions",
            metavar="EXT[,EXT]",
            default=None,
            help="--rename: extra non-image extensions carried along with a renamed "
                 "image, beyond the default .md, .json, .xmp, .txt.",
        )
        ap.add_argument(
            "--plan-out",
            metavar="PATH",
            default=None,
            help="--rename: write the plan as JSON to PATH (docs/rename-mode.md "
                 "section 6.2), instead of -- or in addition to -- the preview table.",
        )
        ap.add_argument(
            "--rename-undo",
            nargs="?",
            const="",
            default=None,
            metavar="JOURNAL",
            help="Reverse the latest applied rename in the positional folder, or the "
                 "named journal file.",
        )
        ap.add_argument(
            "--rename-resume",
            nargs="?",
            const="",
            default=None,
            metavar="JOURNAL",
            help="Finish an interrupted rename run in the positional folder, or the "
                 "named journal file, forward.",
        )
        ap.add_argument(
            "--rename-finish",
            metavar="PLAN",
            default=None,
            help="Rename only the companions of a --rename plan whose images a "
                 "catalog application has already renamed.",
        )

        # Best-effort candidate destination, scanned before argparse can reject
        # the rest of argv -- an unknown flag or a bad choice value exits
        # straight out of parse_args() with a usage message on stderr, which a
        # fire-and-forget launch never sees. Capturing it here is what lets
        # that one failure mode still land in the results file.
        candidate_output = _scan_argv_for_output_file(argv)
        parse_stderr = io.StringIO()
        try:
            with contextlib.redirect_stderr(parse_stderr):
                args = ap.parse_args(argv)
        except SystemExit as exc:
            sys.stderr.write(parse_stderr.getvalue())
            # exc.code is 0/None for --help, which is not a failure and has
            # nothing to report; batch_id is unknown this early, so this one
            # record goes out without it rather than guessing at argv a
            # second time.
            if (
                exc.code not in (0, None)
                and candidate_output
                and candidate_output.lower().endswith(".ndjson")
            ):
                _open_run_envelope_if_fresh(candidate_output, batch_id=None)
                if _active_run_envelope is not None:
                    _active_run_envelope.append(
                        {
                            "run": "fatal",
                            "error": {
                                "type": "usage_error",
                                "message": parse_stderr.getvalue().strip() or "invocation was rejected",
                            },
                        }
                    )
            raise

        # Before any input is required, same as --help: a caller checking
        # compatibility should not have to point this at a real photo first.
        if args.capabilities:
            print(json.dumps(_build_capabilities(ap), indent=2, ensure_ascii=False))
            return

        # RENAME MODE and its executor commands: each one runs on its own and
        # stops before any provider client can be built or any envelope is
        # opened, the same way --generate-manifest does -- see
        # docs/rename-mode.md section 7. Checked ahead of every other branch
        # below (including --generate-manifest's own guards) so a run that
        # named two of these gets one clear refusal instead of whichever
        # branch happened to run first.
        _refuse_rename_mode_conflicts(args)
        _refuse_executor_dry_run(args)
        if args.rename_finish is not None:
            _run_rename_finish(args.rename_finish)
            return
        if args.rename_undo is not None:
            _run_rename_undo_or_resume(args, verb="undo")
            return
        if args.rename_resume is not None:
            _run_rename_undo_or_resume(args, verb="resume")
            return
        if args.rename is not None:
            _run_rename_mode(args)
            return

        # The run envelope opens as early as physically possible -- right
        # after the output destination is known -- so every pre-flight
        # refusal from here on can be observed by a caller that only watches
        # this file, instead of looking like the run never started. Skipped
        # under --dry-run (that flag promises no destination is touched --
        # see the return below, and the envelope is a destination like any
        # other) and under --generate-manifest, which is refused outright
        # beside --output-file (_refuse_generate_manifest_write_flags): there
        # is no results file for that combination to ever open, envelope or
        # not, so opening one here would create a file the run's own
        # contract says can never exist.
        if not args.dry_run and not args.generate_manifest:
            _open_run_envelope_if_fresh(args.output_file, batch_id=args.batch_id)

        if args.process_all_variants:
            logger.warning(
                "--process-all-variants no longer does anything and is ignored; grouping is "
                "controlled by --group-by (default 'object', which sends every scan of a "
                "group in one call)."
            )
        if args.update_policy is not None:
            logger.warning(
                "--update-policy no longer does anything and is ignored; grouping is "
                "controlled by --group-by (default 'object')."
            )

        # --- early validation ---
        if args.jpeg_quality < 1 or args.jpeg_quality > 100:
            ap.error("--jpeg-quality must be between 1 and 100")

        resolved = _resolve_input(args)
        loaded_manifest: dict | None = None
        if resolved.kind == "folder":
            _validate_folder_input(resolved)
        elif resolved.kind == "manifest":
            loaded_manifest = _load_manifest_input(resolved)
        _validate_single_photo_flags(args, resolved)

        # Ahead of the write-bundle guards below, not after them: those guards
        # answer "how do I make this write happen", and for a flag that makes no
        # model call the answer is that it cannot. Asking them first sent
        # ``--generate-manifest --exiftool-write true`` to "add --changeset
        # true", whose own refusal then said to drop it again.
        if args.generate_manifest:
            _refuse_generate_manifest_write_flags(args)
            _refuse_generate_manifest_verbose_flags(args)
            _refuse_generate_manifest_sidecar_flags(args)

        # Before the write-bundle guards: an unwritable tag name is wrong however
        # the run is configured, and saying so is more use than "add --changeset
        # true" for a tag that would write nothing even with the flag.
        _refuse_unwritable_exiftool_fields(args.exiftool_fields)

        changeset_flag, exiftool_write = _resolve_write_bundle(args)
        changeset_requested = changeset_flag == "true"
        if exiftool_write == "true" and not changeset_requested:
            _exit_with_usage_error(*cli_messages.write_needs_changeset())

        # -v's expansion is written back the same way -w's is, above: every
        # downstream reader (here, _apply_common_cfg and, for manifest input,
        # _apply_manifest_debug_settings) sees one resolved value instead of
        # re-deriving the bundle.
        args.debug_dump_llm_request, args.debug_dump_hydration = _resolve_verbose_bundle(args)

        # -s's expansion is written back the same way, so every downstream
        # reader -- _analysis_protected_paths, the plan summary, the cfg --
        # sees one resolved --sidecar-md value instead of two flags.
        args.sidecar_md = _resolve_sidecar_bundle(args)

        out_path = args.output_file
        if out_path and not out_path.lower().endswith((".ndjson", ".json")):
            _exit_with_usage_error(*cli_messages.output_file_extension(out_path))

        # Every destination is checked here, against what is already at it and
        # against each other, because this is the last point above
        # ``--log-file``'s attach -- and that handler truncates on open, so a
        # check any lower cannot save the file the log lands on. It is also
        # above the model call, above --generate-manifest's own write, and
        # above the first truncating open of the results file, which is what
        # the writability half was always here for.
        changeset_path = (
            _derive_changeset_path(resolved, out_path) if changeset_requested else None
        )
        _preflight_destinations_are_distinct(
            [
                (role, path)
                for role, path in (
                    ("--log-file", args.log_file),
                    ("--output-file", out_path),
                    ("--generate-manifest", args.generate_manifest),
                    ("--changeset output", changeset_path),
                )
                if path
            ]
        )
        protected = _analysis_protected_paths(resolved, loaded_manifest, args)
        if args.log_file:
            _preflight_output_file(
                args.log_file,
                role="--log-file",
                protects=protected,
                creates_its_parent=True,
            )
        if out_path:
            _preflight_output_file(out_path, protects=protected)
        if changeset_path:
            _preflight_output_file(changeset_path, role="--changeset output", protects=protected)

        # After the flag guards above (a wrong flag is wrong whatever the
        # provider) and skipped for --generate-manifest, which never calls a
        # model and so has no business demanding a provider choice.
        if not args.generate_manifest:
            args.provider = _resolve_provider(args.provider)

        cfg = Config(
            model=args.openai_model,
            # The generate-manifest path leaves the provider unresolved; the
            # literal here only keeps the dataclass field a string it never reads.
            provider=args.provider or "openai",
            provider_name=utils.provider_display_name(args.provider),
            claude_model_name=args.claude_model,
            gemini_model_name=args.gemini_model,
            openrouter_model_name=args.openrouter_model,
            jpeg_quality=args.jpeg_quality,
            no_update_vocab=args.no_update_vocab,
            max_edge=(None if (args.max_edge in (None, 0)) else args.max_edge),
            group_by=args.group_by,
            sidecar_md=args.sidecar_md,
            max_images_per_call=args.max_images_per_call,
            date_confidence_threshold=args.date_confidence_threshold,
            location_confidence_threshold=args.location_confidence_threshold,
        )
        _apply_common_cfg(cfg, args, manifest=loaded_manifest)

        # An *explicit* --log-file attaches immediately: it names its own
        # path, needs nothing from cfg.debug_dump_dir, and --generate-manifest
        # does not refuse it (unlike the dump flags -- a log still means
        # something without a model call, if only the "wrote manifest" line).
        # -v's own *default* for this flag is different: it needs
        # cfg.debug_dump_dir's final value, which for manifest input is not
        # settled until _apply_manifest_debug_settings runs, below -- so that
        # half waits and, as a consequence, only ever fires on the full
        # analysis path, matching the dump flags -v also turns on.
        if args.log_file:
            _attach_log_file_handler(args.log_file)

        # GENERATE-MANIFEST: describe the input's grouping and stop, before any
        # provider client can be built.
        if args.generate_manifest:
            _generate_manifest(resolved, args, cfg)
            return

        # Decided above the write-back on the next line, not below it: the
        # suggestion turns on which write flags the *user* spelled out, and after
        # the expansion ``args.exiftool_write`` may hold a value nobody passed.
        suggested_command = _suggest_the_normal_run(args, argv)

        # ``-w``'s expansion is written back so everything downstream reads one
        # resolved value rather than re-deriving the bundle.
        args.exiftool_write = exiftool_write
        ecfg = _resolve_exiftool_config(args)
        _preflight_exiftool(
            ecfg, changeset_requested=changeset_requested, read_requested=args.read
        )

        if loaded_manifest is not None:
            _apply_manifest_debug_settings(
                cfg, args, loaded_manifest, out_path or resolved.path
            )

        # -v's own default for --log-file, deferred to here (an explicit
        # --log-file already attached above, before the generate-manifest
        # branch): cfg.debug_dump_dir only holds its final value once
        # _apply_manifest_debug_settings above has had its chance to replace
        # _apply_common_cfg's plain default with a manifest-relative one, so
        # reading it any earlier sends the default log file to the wrong
        # directory whenever --output-file points somewhere else. A
        # consequence worth naming: this means -v's log file, unlike an
        # explicit one, is only ever attached on the full analysis path --
        # this code is never reached for --generate-manifest, which returned
        # above -- matching the dump flags -v also turns on, which
        # --generate-manifest already refuses outright.
        if args.verbose and not args.log_file:
            # cfg.debug_dump_dir is always set by _apply_common_cfg above by
            # this point; the fallback here only mirrors that function's own
            # default rather than assuming the invariant across the call.
            debug_dir = cfg.debug_dump_dir or os.path.join(os.getcwd(), "debug")
            args.log_file = os.path.join(debug_dir, f"{args.batch_id or 'run'}.log")
            # -v's default path is derived, but from two things the user
            # chose (--debug-dump-dir and --batch-id), so it reaches the same
            # seam as the explicit flag rather than being trusted for being
            # derived. Checked immediately above its own attach, which
            # truncates.
            _preflight_destinations_are_distinct(
                [
                    (role, path)
                    for role, path in (
                        ("-v's log file", args.log_file),
                        ("--output-file", out_path),
                        ("--changeset output", changeset_path),
                    )
                    if path
                ]
            )
            _preflight_output_file(
                args.log_file,
                role="-v's log file",
                protects=protected,
                creates_its_parent=True,
            )
            _attach_log_file_handler(args.log_file)

        if loaded_manifest is not None:
            manifest_doc = loaded_manifest
        elif resolved.kind == "folder":
            manifest_doc = build_folder_manifest(
                resolved.path, photo_context_text=cfg.photo_context_text
            )
        else:
            # Loaded here rather than through a manifest ``metadata_path`` so an
            # unreadable or malformed --meta still raises and exits 2 before any
            # model call, instead of being swallowed by load_item_metadata.
            manifest_doc = build_single_photo_manifest(
                resolved.path,
                args.back,
                meta=load_json(args.meta) if args.meta else None,
                photo_context_text=cfg.photo_context_text,
            )

        if args.exiftool_fields and not _writes_are_planned(
            ecfg, changeset_requested=changeset_requested
        ):
            logger.warning(
                "%s", cli_messages.exiftool_fields_with_no_write(args.exiftool_fields)
            )

        # Bucketed with the override logging off: the stream buckets these same
        # items again, and reporting every explicit override twice would be
        # guaranteed on any --back run.
        buckets = build_manifest_buckets(
            manifest_doc["items"], group_by=cfg.group_by, log_overrides=False
        )
        plan = cli_messages.RunPlan(
            input_location=os.path.abspath(resolved.path),
            input_kind=_KIND_LABELS[resolved.kind].removeprefix("a "),
            file_count=len(manifest_doc["items"]),
            group_count=len(buckets),
            group_by=cfg.group_by,
            read=_render_read_clause(args.read),
            output=_render_output_clause(out_path),
            changeset=changeset_path or "none (--changeset false)",
            write=_render_write_clause(
                ecfg,
                changeset_requested=changeset_requested,
                exiftool_write=exiftool_write,
                dry_run=args.dry_run,
            ),
            provider=cfg.provider_name,
            model=utils.resolve_model_for_provider(cfg),
            dry_run=args.dry_run,
            sidecars=_render_sidecar_clause(cfg.sidecar_md),
            suggested_command=suggested_command,
        )
        logger.info("%s", plan.render())
        if args.dry_run:
            # Nothing below this line has run, so no destination has been
            # truncated and the previous run's artifacts are byte-identical --
            # including the run envelope, which was never opened for exactly
            # this reason (see the --dry-run guard above _open_run_envelope_if_fresh).
            return

        # Every pre-flight check has now passed. A pre-existing destination
        # was deliberately left untouched by _open_run_envelope_if_fresh
        # above, to protect it from exactly the refusal this run did not hit;
        # now that the run is committing to overwrite it anyway (a fresh
        # destination already got this at parse time), it gets the same
        # envelope.
        _open_run_envelope_deferred(out_path, batch_id=args.batch_id)
        if _active_run_envelope is not None:
            _active_run_envelope.append({"run": "plan", "plan": plan.as_dict()})

        ndjson_writer = None
        run_event_writer = None
        if out_path and out_path.lower().endswith(".ndjson"):
            # The envelope above already created (or, for a fresh
            # destination, already truncated at parse time) this file -- no
            # separate truncating open needed here.
            def ndjson_writer(line: str) -> None:
                """Append one finished record to the NDJSON output."""
                _append_ndjson_record(out_path, json.loads(line), batch_id=args.batch_id)

            def run_event_writer(event: dict) -> None:
                """Append one run-level progress event to the NDJSON output."""
                record = {"run": "progress"}
                record.update(event)
                _append_ndjson_record(out_path, record, batch_id=args.batch_id)

        changeset_writer = None
        if changeset_path:
            open(changeset_path, "w", encoding="utf-8").close()

            def changeset_writer(line: str) -> None:
                """Append one proposed-write record to the changeset."""
                with open(changeset_path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")

        should_cancel = None
        if args.cancel_file:
            cancel_path = args.cancel_file

            def should_cancel() -> bool:
                """True once the plugin's cancel-file sentinel appears."""
                return os.path.exists(cancel_path)

        data = process_manifest_stream(
            manifest=manifest_doc,
            cfg=cfg,
            write_sidecars=args.output_sidecars,
            ndjson_writer=ndjson_writer,
            changeset_writer=changeset_writer,
            metadata_hydrator=make_manifest_hydrator(ecfg) if args.read else None,
            titles_may_be_from_files=args.read,
            # Folder and single-photo input keep Phase A's failure contract;
            # manifest input keeps the plug-in's.
            strict_run_failures=resolved.kind != "manifest",
            should_cancel=should_cancel,
            run_event_writer=run_event_writer,
        )
        if not out_path:
            print(json.dumps(data, indent=2 if cfg.pretty_json else None, ensure_ascii=False))
        elif out_path.lower().endswith(".json"):
            _write_aggregate_json(data, out_path, pretty=cfg.pretty_json)
        # Manifest input is exempt for the same reason it keeps
        # ``strict_run_failures=False``: it is the Lightroom plug-in's contract,
        # and the plug-in reads the per-item records rather than the exit
        # status, so failing the batch would tell it less than the records
        # already do. See photokin/README.md's failure-contract section.
        nothing_written = _apply_exiftool_changeset(
            ecfg=ecfg,
            changeset_path=changeset_path,
            out_path=out_path,
            strict=resolved.kind != "manifest",
            batch_id=args.batch_id,
        )
        if nothing_written:
            _exit_with_usage_error(*cli_messages.every_write_failed())

        if _active_run_envelope is not None:
            # The terminal record: a caller that only watches this file --
            # never holding a process handle to poll, for a fire-and-forget
            # subprocess launch -- can treat its presence as "the run is
            # over" without guessing from the NDJSON line count, which used
            # to be the only signal and breaks the moment per-file emission
            # ever changes shape.
            _active_run_envelope.append(
                {
                    "run": "cancelled" if data.get("cancelled") else "complete",
                    "files_recorded": len(data.get("results") or {}),
                    "groups_failed": data.get("groups_failed", 0),
                    "files_unsent": data.get("files_unsent", 0),
                }
            )

    except SystemExit:
        raise
    except Exception as e:
        error_type = e.error_type if isinstance(e, ProviderApiError) else e.__class__.__name__
        err: dict[str, object] = {"level": "FATAL", "type": error_type, "message": str(e)}
        if error_type not in SELF_EXPLANATORY_ERROR_TYPES:
            err["traceback"] = traceback.format_exception(e.__class__, e, e.__traceback__)
        logger.error("%s", json.dumps(err, ensure_ascii=False))
        if _active_run_envelope is not None:
            _active_run_envelope.append({"run": "fatal", "error": {"type": error_type, "message": str(e)}})
        sys.exit(2)


if __name__ == "__main__":
    main()
