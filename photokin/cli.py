"""
photokin.cli
==================

Thin command-line interface for the photo archiver.

Responsibilities:
- Collect CLI/interactive parameters.
- Build a Config object.
- Install the package-wide stderr log handler.
- Invoke the library entrypoints.
- Write outputs per flags (NDJSON/JSON/sidecars) and/or print to stdout.

This is the module the Lightroom plugin launches (``python -m photokin.cli``);
its flags and the manifest/NDJSON behavior are part of the plugin contract, so
treat changes here as contract changes. Analysis results go to stdout; every
diagnostic goes to stderr through the logger.

Code map:
- load_json                 read a UTF-8 JSON file (matches Lightroom's writes)
- _configure_logging        attach the stderr handler to the "photokin" logger
- _exit_with_usage_error    report a problem/``Try:`` pair and exit 2
- _interactive_prompt       prompt for image paths when no args are given
- _apply_common_cfg         apply flags shared across single/folder/manifest modes
- _resolve_exiftool_config  build ExiftoolConfig (CLI flag > env > default)
- _preflight_exiftool       stop before the first model call if writes can't run
- _preflight_output_file    stop before the first model call if an output is unwritable
- _write_generated_manifest atomically write a synthesized manifest, pretty-printed
- _generate_manifest        --generate-manifest: describe the input's grouping, stop
- _apply_exiftool_changeset apply routed fields via ExifTool + append a status line
- main                      PUBLIC: route to single / folder / manifest flow
"""

import argparse
import json
import logging
import os
import sys
import tempfile
import traceback
from typing import NoReturn

from . import utils
from .utils import Config, normalize_path
from .core import (
    UPDATE_MERGE_PER_VARIANT,
    analyze_folder,
    build_folder_manifest,
    build_manifest_buckets,
    build_single_photo_manifest,
    process_manifest_stream,
)
from .errors import ProviderApiError, SELF_EXPLANATORY_ERROR_TYPES
from .exiftool import (
    ExiftoolConfig,
    apply_changeset,
    make_manifest_hydrator,
    resolve_exiftool_path,
)
from .exiftool.config import parse_fields as exiftool_parse_fields

# Named explicitly rather than via __name__: under ``python -m photokin.cli``
# (how the plugin launches this) __name__ is "__main__", which sits outside the
# package logger and would never reach the handler installed below.
logger = logging.getLogger("photokin.cli")

# Tag on the handler this module installs, so repeated main() calls reuse it.
_LOG_HANDLER_NAME = "photokin-cli-stderr"

_EPILOG = """\
examples:
  # Single photo (dev/testing)
  %(prog)s photo.jpg --back photo_back.jpg --provider openai

  # Folder of images
  %(prog)s --folder ./scans/ --provider anthropic --claude-model sonnet

  # Any OpenRouter-hosted vision model (Kimi, Qwen-VL, ...) via one API key
  %(prog)s --folder ./scans/ --provider openrouter --openrouter-model moonshotai/kimi-k3

  # Manifest mode (recommended, used by the Lightroom plugin)
  %(prog)s --manifest batch.json --output-file results.ndjson --changeset true

  # Check how a folder would be grouped, without calling the model
  %(prog)s --folder ./scans/ --generate-manifest scans-manifest.json
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
    """Prompt the user for image paths and return extra argv tokens."""

    print("Interactive mode: Provide image paths for analysis.")
    front = normalize_path(input("Front image path (blank to quit): "))
    if not front:
        print("No file provided. Exiting.")
        raise SystemExit(0)
    back_raw = input("Back image path (optional, blank if none): ")
    back = normalize_path(back_raw) if back_raw else None
    extra = [front]
    if back:
        extra.extend(["--back", back])
    print("")
    return extra


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


def _resolve_exiftool_config(args: argparse.Namespace, *, dry_run: bool) -> ExiftoolConfig:
    """Build the pipeline ExiftoolConfig with precedence: CLI flag > env var > default."""
    flag_enabled = None if args.exiftool_write is None else (args.exiftool_write == "true")
    flag_fields = exiftool_parse_fields(args.exiftool_fields)
    return ExiftoolConfig.from_env(
        enabled=flag_enabled,
        fields=flag_fields,
        path=args.exiftool_path or None,
        dry_run=dry_run,
        overwrite_original=True,
    )


def _preflight_exiftool(ecfg: ExiftoolConfig, *, changeset_requested: bool) -> None:
    """Fail before the first model call when a requested ExifTool write cannot run.

    ``apply_changeset`` only looks for the binary once the whole batch has been
    analyzed and paid for, so the same lookup is done up front here. A dry run
    is exempt: it reports what it would write without ever invoking the binary,
    so requiring one would block a preview that needs no ExifTool at all.

    Args:
        ecfg: The resolved ExifTool configuration for this run.
        changeset_requested: True when ``--changeset true`` was passed.

    Raises:
        SystemExit: With code 2 when writes are requested but no binary resolves.
    """
    if not (ecfg.enabled and changeset_requested) or ecfg.dry_run:
        return
    try:
        resolve_exiftool_path(ecfg)
    except OSError as exc:
        logger.debug("ExifTool resolution failed: %s", exc)
        configured = f" (configured path: {ecfg.path})" if ecfg.path else ""
        _exit_with_usage_error(
            "--changeset true needs ExifTool to write the results, "
            f"but no ExifTool binary was found{configured}.",
            "run `python -m photokin.exiftool.fetch` to download one, install ExifTool "
            "system-wide, or re-run with --exiftool-write false",
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
        _exit_with_usage_error(
            f"{role} directory does not exist: {out_dir}",
            "create the directory first, or point --output-file at an existing one",
        )
    # Caught here rather than at the write: a directory passes every check below
    # (it exists, its parent is writable) and would only fail after the batch had
    # already been analyzed and paid for.
    if os.path.isdir(out_path):
        _exit_with_usage_error(
            f"{role} is a directory, not a file: {out_path}",
            "name the file itself, such as results.ndjson inside that directory",
        )
    # Windows refuses to unlink or rename over a read-only file, so the write
    # sequence really does need this one. POSIX gates both on the directory,
    # which the probe below already covers, and would abort a run that works.
    if os.name == "nt" and os.path.exists(out_path) and not os.access(out_path, os.W_OK):
        _exit_with_usage_error(
            f"{role} already exists and is not writable: {out_path}",
            "clear the read-only flag on that file, or choose a different --output-file",
        )
    # A unique probe, not ``out_path + ".tmp"``: that name is the atomic write's
    # own temp file, and opening it "w" would truncate a real file that happens
    # to be sitting there before deleting it outright.
    try:
        with tempfile.NamedTemporaryFile(dir=out_dir, prefix=".photokin-preflight-", suffix=".tmp"):
            pass
    except OSError as exc:
        _exit_with_usage_error(
            f"{role} destination is not writable: {out_path} ({exc.strerror or exc})",
            "point --output-file at a writable directory",
        )


def _write_generated_manifest(manifest: dict, out_path: str) -> None:
    """Write *manifest* to *out_path*, atomically and always pretty-printed.

    Pretty regardless of ``--pretty-json``: this file exists to be read and hand
    edited. Written through a sibling temp file and ``os.replace`` so an
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


def _generate_manifest(args: argparse.Namespace, cfg: Config) -> None:
    """Describe how this run's input would be grouped, then stop.

    The manifest written here holds the very ``items`` list the analysis path
    would have processed -- same builder, same order, same keys -- so it is a
    description of the run rather than a separate rendering of it, and re-running
    it with ``--manifest`` reproduces the run exactly. Only the input is
    described: provider, model, policy and debug settings are run settings, and
    baking them in would let a generated file silently override a later run's
    flags.

    Args:
        args: Parsed CLI arguments.
        cfg: The run configuration, with photo context already resolved.

    Raises:
        SystemExit: With code 2 for every usage error.
        FileNotFoundError: If the image or ``--back`` path does not exist.
            Reported by ``main`` as exit 2, as the folder branch's
            ``NotADirectoryError`` already was.
    """
    if args.manifest:
        _exit_with_usage_error(
            "--generate-manifest describes how folder or single-photo input would be "
            "grouped, but --manifest input is already a manifest.",
            "drop --generate-manifest, or point it at a folder: "
            "photokin --folder ./scans/ --generate-manifest out.json",
        )
    if not args.folder and not args.image:
        # The interactive prompt only runs for a completely empty argv, so this
        # flag on its own reaches here with nothing to describe. Writing a
        # one-item manifest for the empty path and exiting 0 would be exactly the
        # silent nonsense every other guard in this function exists to refuse.
        _exit_with_usage_error(
            "--generate-manifest describes how an input would be grouped, but no "
            "folder or image was given.",
            f"name the input: photokin --folder ./scans/ --generate-manifest "
            f"{args.generate_manifest}",
        )
    out_path = args.generate_manifest
    if not out_path.lower().endswith(".json"):
        _exit_with_usage_error(
            f"--generate-manifest must end with .json; got {out_path}.",
            "name the file itself, such as scans-manifest.json",
        )
    _preflight_output_file(out_path, role="--generate-manifest")

    if args.folder:
        manifest = build_folder_manifest(args.folder, photo_context_text=cfg.photo_context_text)
    else:
        # Loaded eagerly, exactly as the analysis path does, so an unreadable or
        # malformed --meta still exits 2 before anything is written.
        orig_meta = load_json(args.meta) if args.meta else None
        # Also exactly as the analysis path does, and for the same reason the
        # folder branch above refuses a directory that is not there: a manifest
        # describing how a run that cannot happen would be grouped is the silent
        # nonsense this function's other guards exist to refuse, and it only
        # fails later, when the file is fed back through --manifest.
        front = normalize_path(args.image)
        back = normalize_path(args.back) if args.back else None
        utils.ensure_paths_exist([p for p in (front, back) if p])
        manifest = build_single_photo_manifest(
            args.image,
            args.back,
            meta=orig_meta,
            photo_context_text=cfg.photo_context_text,
        )

    # Bucketed before the write, not after: the group count is what the user is
    # checking when they reach for this flag, and resolving the entries is also
    # what reports an explicit override that disagrees with a filename -- most
    # usefully a --back the grammar reads as a front.
    buckets = build_manifest_buckets(manifest["items"])
    _write_generated_manifest(manifest, out_path)
    logger.info(
        "Wrote manifest for %d file(s) in %d group(s) to %s; no model call was made.",
        len(manifest["items"]),
        len(buckets),
        out_path,
    )


def _apply_exiftool_changeset(
    *,
    ecfg: ExiftoolConfig,
    changeset_path: str | None,
    out_path: str | None,
) -> None:
    """Apply routed fields from a changeset using ExifTool and append a status record."""
    if not changeset_path or not os.path.isfile(changeset_path):
        return

    if not ecfg.enabled or not ecfg.fields:
        return

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
        return

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


def main() -> None:
    """CLI entry point that routes to single-photo, folder, or manifest flows."""

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

        group = ap.add_mutually_exclusive_group(required=False)
        group.add_argument("image", nargs="?", help="Path to the main/front image")
        group.add_argument("--folder", help="Process an entire folder (batch mode)")
        group.add_argument("--manifest", help="Process a manifest JSON (flat list of files + optional metadata)")

        ap.add_argument("--back", help="Path to the back image (optional for single mode)", default=None)
        ap.add_argument("--meta", help="Path to original metadata JSON (single-photo mode)", default=None)
        ap.add_argument("--photo-context-file", help="Path to UTF-8 text file containing authoritative photo context", default=None)
        ap.add_argument("--photo-context-text", help="Inline authoritative photo context text", default=None)

        ap.add_argument("--update-policy", choices=["master_exact", "merge_per_variant"], default="merge_per_variant",
                        help="How to apply results to each file in a group")
        ap.add_argument("--provider", choices=["openai", "anthropic", "gemini", "openrouter"], default=defaults.provider,
                        help="LLM provider backend to use")
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
        ap.add_argument("--process-all-variants", action="store_true",
                        help="Also analyze -b/-c variants and fold outputs")
        ap.add_argument("--date-confidence-threshold", type=float, default=defaults.date_confidence_threshold,
                        help="Minimum confidence required to apply dates to the patch (default: %(default)s)")
        ap.add_argument("--location-confidence-threshold", type=float, default=defaults.location_confidence_threshold,
                        help="Minimum confidence required to apply locations to the patch (default: %(default)s)")
        ap.add_argument("--no-update-vocab", action="store_true", help="Do not append new keywords to TOML")

        ap.add_argument("--output-file",
                        help="Path to write results. If ends with .ndjson, writes one JSON object per line as items complete; if .json, writes a single JSON object at the end.")
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
        ap.add_argument(
            "--changeset",
            choices=["true", "false"],
            default="false",
            help="Write changeset NDJSON next to the manifest/output artifacts",
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
            help="Run analysis without applying metadata downstream. NDJSON records are marked with dry_run=true.",
        )
        ap.add_argument(
            "--exiftool-write",
            choices=["true", "false"],
            default=None,
            help="Apply changeset fields to files via ExifTool after analysis (default: env EXIFTOOL_WRITE_ENABLED, else true)",
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

        # --- early validation ---
        if args.jpeg_quality < 1 or args.jpeg_quality > 100:
            ap.error("--jpeg-quality must be between 1 and 100")

        if args.changeset == "true" and not args.manifest:
            _exit_with_usage_error(
                "--changeset is only supported in --manifest mode.",
                "photokin --manifest <manifest.json> --changeset true",
            )

        # Folder and single-photo mode print results to stdout and never read
        # --output-file; erroring beats writing nothing and exiting 0.
        if args.output_file and not args.manifest:
            seen = f"--folder {args.folder}" if args.folder else args.image
            seen = seen or "single-photo input"
            _exit_with_usage_error(
                f"--output-file is only supported in --manifest mode; saw {seen} with "
                f"--output-file {args.output_file}, which would be ignored.",
                f"redirect stdout instead: photokin {seen} > {args.output_file}",
            )

        cfg = Config(
            model=args.openai_model,
            provider=args.provider,
            provider_name=utils.provider_display_name(args.provider),
            claude_model_name=args.claude_model,
            gemini_model_name=args.gemini_model,
            openrouter_model_name=args.openrouter_model,
            jpeg_quality=args.jpeg_quality,
            no_update_vocab=args.no_update_vocab,
            max_edge=(None if (args.max_edge in (None, 0)) else args.max_edge),
            process_all_variants=args.process_all_variants,
            date_confidence_threshold=args.date_confidence_threshold,
            location_confidence_threshold=args.location_confidence_threshold,
            dry_run=args.dry_run,
        )

        # GENERATE-MANIFEST: describe the input's grouping and stop, before any
        # provider client can be built.
        if args.generate_manifest:
            _apply_common_cfg(cfg, args)
            _generate_manifest(args, cfg)

        # MANIFEST MODE (recommended for Lightroom)
        elif args.manifest:
            man = load_json(args.manifest)
            _apply_common_cfg(cfg, args, manifest=man)
            ecfg = _resolve_exiftool_config(args, dry_run=args.dry_run)
            _preflight_exiftool(ecfg, changeset_requested=args.changeset == "true")
            metadata_hydrator = make_manifest_hydrator(ecfg)
            # Manifest can override debug dump setting
            manifest_debug_dump = bool(man.get("debug_dump_llm_request"))
            cli_debug_dump = None if args.debug_dump_llm_request is None else (args.debug_dump_llm_request == "true")
            cfg.debug_dump_llm_request = cli_debug_dump if cli_debug_dump is not None else manifest_debug_dump
            out_path = args.output_file
            default_base_dir = os.path.dirname(out_path or args.manifest)
            default_dump_dir = os.path.join(default_base_dir, "debug")
            manifest_dump_dir = man.get("debug_dump_dir") if isinstance(man.get("debug_dump_dir"), str) else None
            cfg.debug_dump_dir = args.debug_dump_dir or manifest_dump_dir or default_dump_dir
            changeset_path = None
            if args.changeset == "true":
                base_dir = os.path.dirname(out_path or args.manifest)
                if out_path:
                    out_name = os.path.basename(out_path)
                    out_lower = out_name.lower()
                    if out_lower.endswith("_results.ndjson"):
                        changeset_name = out_name[:-len("_results.ndjson")] + "_changeset.ndjson"
                    elif out_lower.endswith(".ndjson"):
                        changeset_name = out_name[:-len(".ndjson")] + "_changeset.ndjson"
                    else:
                        changeset_name = "changeset.ndjson"
                else:
                    changeset_name = "changeset.ndjson"
                changeset_path = os.path.join(base_dir, changeset_name)

            # Every destination is validated before the first truncating open:
            # those opens destroy the previous run's artifacts, so a later abort
            # would take the changeset with it.
            if out_path and not out_path.lower().endswith((".ndjson", ".json")):
                _exit_with_usage_error(
                    f"--output-file must end with .ndjson or .json; got {out_path}.",
                    "use .ndjson to stream one record per finished photo, "
                    "or .json for a single object written at the end",
                )
            if out_path:
                _preflight_output_file(out_path)
            if changeset_path:
                _preflight_output_file(changeset_path, role="--changeset output")

            changeset_writer = None
            if changeset_path:
                open(changeset_path, "w", encoding="utf-8").close()

                def changeset_writer(line: str):
                    with open(changeset_path, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
            if out_path:
                out_ext = out_path.lower()
                tmp_path = out_path + ".tmp"
                if out_ext.endswith(".ndjson"):
                    # Stream one line per finished photo
                    open(out_path, "w", encoding="utf-8").close()

                    def writer(line: str):
                        rec = json.loads(line)
                        if args.batch_id:
                            rec["batch_id"] = args.batch_id
                        with open(out_path, "a", encoding="utf-8") as f:
                            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

                    process_manifest_stream(
                        manifest=man,
                        cfg=cfg,
                        update_policy=args.update_policy,
                        write_sidecars=args.output_sidecars,
                        ndjson_writer=writer,
                        changeset_writer=changeset_writer,
                        metadata_hydrator=metadata_hydrator,
                    )
                    _apply_exiftool_changeset(ecfg=ecfg, changeset_path=changeset_path, out_path=out_path)
                else:
                    # Aggregate then atomic write (temp → replace). No unlink
                    # ahead of the replace: os.replace overwrites atomically on
                    # Windows as well as POSIX, so removing first only opens a
                    # window in which the caller's previous results file is gone
                    # and the new one does not exist yet -- and if the replace
                    # then fails, the ``finally`` clears the temp file and the
                    # run ends with neither.
                    try:
                        data = process_manifest_stream(
                            manifest=man,
                            cfg=cfg,
                            update_policy=args.update_policy,
                            write_sidecars=args.output_sidecars,
                            ndjson_writer=None,
                            changeset_writer=changeset_writer,
                            metadata_hydrator=metadata_hydrator,
                        )
                        with open(tmp_path, "w", encoding="utf-8") as f:
                            json.dump(data, f, indent=2 if cfg.pretty_json else None, ensure_ascii=False)
                        os.replace(tmp_path, out_path)
                        _apply_exiftool_changeset(ecfg=ecfg, changeset_path=changeset_path, out_path=out_path)
                    finally:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
            else:
                # stdout fallback (Lightroom typically won’t use this)
                data = process_manifest_stream(
                    manifest=man,
                    cfg=cfg,
                    update_policy=args.update_policy,
                    write_sidecars=args.output_sidecars,
                    ndjson_writer=None,
                    changeset_writer=changeset_writer,
                    metadata_hydrator=metadata_hydrator,
                )
                _apply_exiftool_changeset(ecfg=ecfg, changeset_path=changeset_path, out_path=out_path)
                print(json.dumps(data, indent=2 if cfg.pretty_json else None, ensure_ascii=False))

        # FOLDER MODE
        elif args.folder:
            _apply_common_cfg(cfg, args)
            data = analyze_folder(args.folder, cfg, write_sidecars=args.output_sidecars)
            print(json.dumps(data, indent=2 if cfg.pretty_json else None, ensure_ascii=False))

        # SINGLE PHOTO MODE
        else:
            # Loaded here rather than through a manifest ``metadata_path`` so an
            # unreadable or malformed --meta still raises and exits 2 before any
            # model call, instead of being swallowed by load_item_metadata.
            orig_meta = load_json(args.meta) if args.meta else None
            _apply_common_cfg(cfg, args)
            data = process_manifest_stream(
                manifest=build_single_photo_manifest(
                    args.image,
                    args.back,
                    meta=orig_meta,
                    photo_context_text=cfg.photo_context_text,
                ),
                cfg=cfg,
                update_policy=UPDATE_MERGE_PER_VARIANT,
                write_sidecars=args.output_sidecars,
                strict_run_failures=True,
            )
            print(json.dumps(data, indent=2 if cfg.pretty_json else None, ensure_ascii=False))

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
