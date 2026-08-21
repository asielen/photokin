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
- _preflight_output_file    stop before the first model call if an output is unwritable
- _write_generated_manifest atomically write a synthesized manifest, pretty-printed
- _generate_manifest        --generate-manifest: describe the input's grouping, stop
- _suggest_the_normal_run   offer ``-rw`` to a run that has asked for nothing
- _apply_exiftool_changeset apply routed fields via ExifTool + append a status line
- main                      PUBLIC: resolve the input, print the plan, run it
"""

import argparse
import json
import logging
import os
import sys
import tempfile
import traceback
from dataclasses import dataclass
from typing import NoReturn

from . import cli_messages, utils
from .utils import Config, normalize_path
from .core import (
    build_folder_manifest,
    build_manifest_buckets,
    build_single_photo_manifest,
    process_manifest_stream,
)
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

# Named explicitly rather than via __name__: under ``python -m photokin.cli``
# (how the plugin launches this) __name__ is "__main__", which sits outside the
# package logger and would never reach the handler installed below.
logger = logging.getLogger("photokin.cli")

# Tag on the handler this module installs, so repeated main() calls reuse it.
_LOG_HANDLER_NAME = "photokin-cli-stderr"

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


def _exit_with_usage_error(problem: str, remedy: str) -> NoReturn:
    """Report a usage error as a problem line plus a ``Try:`` line, then exit 2.

    Args:
        problem: What the CLI saw, stated in a single line.
        remedy: The corrective action, rendered on a following ``Try:`` line.

    Raises:
        SystemExit: Always, with exit code 2.
    """
    logger.error("%s\nTry: %s", problem, remedy)
    sys.exit(2)


def _interactive_prompt() -> list[str]:
    """Prompt the user for image paths and return extra argv tokens.

    A blank front-image answer, an interrupt (Ctrl+C), or closed stdin (Ctrl+D
    on macOS/Linux, Ctrl+Z then Enter on Windows, or an empty piped stdin) all
    mean "nothing to run" and exit 0 quietly -- none should raise.
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
    except (EOFError, KeyboardInterrupt):
        back_raw = ""
        print("")
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
    cfg.debug_dump_dir = args.debug_dump_dir or os.path.join(os.getcwd(), "debug")
    cfg.run_batch_id = args.batch_id


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


def _preflight_output_file(out_path: str, *, role: str = "--output-file") -> None:
    """Fail before the first model call when an output destination cannot be written.

    Both artifacts a run produces are checked up front: an unwritable aggregate
    ``.json`` would otherwise discard a paid batch, and the truncate-on-open the
    streaming paths rely on destroys the previous run's file before it can fail.
    Probes with a uniquely named temp file in the destination directory, so no
    pre-existing file is ever opened, truncated, or removed.

    Args:
        out_path: The resolved destination path.
        role: The flag the destination came from, quoted back in the error text.

    Raises:
        SystemExit: With code 2 when the destination cannot be written.
    """
    out_dir = os.path.dirname(os.path.abspath(out_path))
    if not os.path.isdir(out_dir):
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


def _write_generated_manifest(manifest: dict, out_path: str) -> None:
    """Write *manifest* to *out_path*, atomically and always pretty-printed.

    Pretty regardless of ``Config.pretty_json``, which the other two JSON writes
    honor: this file exists to be read and hand edited. (There is no
    ``--pretty-json`` flag; the field is library-only.) Written through a
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
    _preflight_output_file(out_path, role="--generate-manifest")

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
) -> bool:
    """Apply routed fields from a changeset using ExifTool and append a status record.

    Args:
        ecfg: The resolved ExifTool configuration for this run.
        changeset_path: The changeset to apply, or ``None`` when none was written.
        out_path: The run's output destination, used to route the status record.
        strict: Whether a total write failure should be reported to the caller.
            False for manifest input, which keeps the plug-in's contract.

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

    status_line = json.dumps(
        {
            "run": "exiftool_apply",
            "summary": exif_summary,
        },
        ensure_ascii=False,
    )
    if out_path and out_path.lower().endswith(".ndjson"):
        with open(out_path, "a", encoding="utf-8") as file_handle:
            file_handle.write(status_line + "\n")
    else:
        logger.info("[ExifTool] Apply status: %s", status_line)

    # Reported after the status record is written, not instead of it: the
    # summary is how a caller finds out which files failed and why, and it has
    # to survive the run being called a failure.
    return strict and files_seen > 0 and files_written == 0


def main() -> None:
    """CLI entry point: resolve one input, state the plan, then run it."""

    _configure_logging()
    try:
        argv = sys.argv[1:]
        if not argv:
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
            "--debug-dump-llm-request",
            choices=["true", "false"],
            default=None,
            help="Write full LLM request payload dumps to disk before each LLM call",
        )
        ap.add_argument(
            "--debug-dump-dir",
            default=None,
            help="Directory for LLM request dump artifacts (default: <manifest/output-dir>/debug)",
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
            help="Comma-separated ExifTool tags to write (default: env EXIFTOOL_FIELDS, else EXIF:UserComment)",
        )
        ap.add_argument(
            "--exiftool-path",
            default=None,
            help="Path to the ExifTool executable (default: env EXIFTOOL_PATH, else auto-detect)",
        )

        args = ap.parse_args(argv)

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

        # Before the write-bundle guards: an unwritable tag name is wrong however
        # the run is configured, and saying so is more use than "add --changeset
        # true" for a tag that would write nothing even with the flag.
        _refuse_unwritable_exiftool_fields(args.exiftool_fields)

        changeset_flag, exiftool_write = _resolve_write_bundle(args)
        changeset_requested = changeset_flag == "true"
        if exiftool_write == "true" and not changeset_requested:
            _exit_with_usage_error(*cli_messages.write_needs_changeset())

        out_path = args.output_file
        if out_path and not out_path.lower().endswith((".ndjson", ".json")):
            _exit_with_usage_error(*cli_messages.output_file_extension(out_path))

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
            date_confidence_threshold=args.date_confidence_threshold,
            location_confidence_threshold=args.location_confidence_threshold,
        )
        _apply_common_cfg(cfg, args, manifest=loaded_manifest)

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

        # Every destination is validated before the first truncating open:
        # those opens destroy the previous run's artifacts, so a later abort
        # would take the changeset with it.
        changeset_path = _derive_changeset_path(resolved, out_path) if changeset_requested else None
        if out_path:
            _preflight_output_file(out_path)
        if changeset_path:
            _preflight_output_file(changeset_path, role="--changeset output")

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
            suggested_command=suggested_command,
        )
        logger.info("%s", plan.render())
        if args.dry_run:
            # Nothing below this line has run, so no destination has been
            # truncated and the previous run's artifacts are byte-identical.
            return

        ndjson_writer = None
        if out_path and out_path.lower().endswith(".ndjson"):
            # Stream one line per finished photo.
            open(out_path, "w", encoding="utf-8").close()

            def ndjson_writer(line: str) -> None:
                """Append one finished record to the NDJSON output."""
                record = json.loads(line)
                if args.batch_id:
                    record["batch_id"] = args.batch_id
                with open(out_path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")

        changeset_writer = None
        if changeset_path:
            open(changeset_path, "w", encoding="utf-8").close()

            def changeset_writer(line: str) -> None:
                """Append one proposed-write record to the changeset."""
                with open(changeset_path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")

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
        )
        if nothing_written:
            _exit_with_usage_error(*cli_messages.every_write_failed())

    except SystemExit:
        raise
    except Exception as e:
        error_type = e.error_type if isinstance(e, ProviderApiError) else e.__class__.__name__
        err: dict[str, object] = {"level": "FATAL", "type": error_type, "message": str(e)}
        if error_type not in SELF_EXPLANATORY_ERROR_TYPES:
            err["traceback"] = traceback.format_exception(e.__class__, e, e.__traceback__)
        logger.error("%s", json.dumps(err, ensure_ascii=False))
        sys.exit(2)


if __name__ == "__main__":
    main()
