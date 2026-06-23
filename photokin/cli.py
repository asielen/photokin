"""
photokin.cli
==================

Thin command-line interface for the photo archiver.

Responsibilities:
- Collect CLI/interactive parameters.
- Build a Config object.
- Invoke the library entrypoints.
- Write outputs per flags (NDJSON/JSON/sidecars) and/or print to stdout.

This is the module the Lightroom plugin launches (``python -m photokin.cli``);
its flags and the manifest/NDJSON behavior are part of the plugin contract, so
treat changes here as contract changes.

Code map:
- load_json                 read a UTF-8 JSON file (matches Lightroom's writes)
- _interactive_prompt       prompt for image paths when no args are given
- _apply_common_cfg         apply flags shared across single/folder/manifest modes
- _resolve_exiftool_config  build ExiftoolConfig (CLI flag > env > default)
- _apply_exiftool_changeset apply routed fields via ExifTool + append a status line
- main                      PUBLIC: route to single / folder / manifest flow
"""

import argparse
import json
import os
import sys
import traceback

from . import utils
from .utils import Config, normalize_path
from .core import analyze_photo, analyze_folder, process_manifest_stream
from .exiftool import ExiftoolConfig, apply_changeset, make_manifest_hydrator
from .exiftool.config import parse_fields as exiftool_parse_fields

_EPILOG = """\
examples:
  # Single photo (dev/testing)
  %(prog)s photo.jpg --back photo_back.jpg --provider openai

  # Folder of images
  %(prog)s --folder ./scans/ --provider anthropic --claude-model sonnet

  # Manifest mode (recommended, used by the Lightroom plugin)
  %(prog)s --manifest batch.json --output-file results.ndjson --changeset true
"""


def load_json(p: str):
    """Read JSON with UTF-8 encoding to mirror Lightroom’s manifest writes."""

    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


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
    print(
        f"[ExifTool] Starting apply: binary={exiftool_path} fields={list(ecfg.fields)} changeset={changeset_path}"
    )
    try:
        exif_summary = apply_changeset(
            changeset_path,
            ecfg,
            enabled=True,
            fields=ecfg.fields,
        )
    except (FileNotFoundError, OSError, ValueError, RuntimeError) as exc:
        print(f"[WARN] ExifTool apply failed: {exc}")
        print(f"[WARN] ExifTool apply failed: {exc}", file=sys.stderr)
        return

    files_seen = int(exif_summary.get("files_seen") or 0)
    files_written = int(exif_summary.get("files_written") or 0)
    tags_written = int(exif_summary.get("tags_written") or 0)
    errors = exif_summary.get("errors") if isinstance(exif_summary.get("errors"), list) else []
    warnings = exif_summary.get("warnings") if isinstance(exif_summary.get("warnings"), list) else []
    print(
        "[ExifTool] Apply result: "
        f"files_seen={files_seen} files_written={files_written} "
        f"tags_written={tags_written} errors={len(errors)} warnings={len(warnings)}"
    )
    if errors:
        print(f"[ExifTool] Errors: {json.dumps(errors, ensure_ascii=False)}")
        print(f"[ExifTool] Errors: {json.dumps(errors, ensure_ascii=False)}", file=sys.stderr)
    if warnings:
        print(f"[ExifTool] Warnings: {json.dumps(warnings, ensure_ascii=False)}")

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
        print(status_line, file=sys.stderr)


def main():
    """CLI entry point that routes to single-photo, folder, or manifest flows."""

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
        ap.add_argument("--provider", choices=["openai", "anthropic", "gemini"], default=defaults.provider,
                        help="LLM provider backend to use")
        ap.add_argument("--openai-model", default=defaults.model,
                        help="OpenAI model name (default: %(default)s)")
        ap.add_argument("--claude-model", choices=["sonnet", "haiku"], default=defaults.claude_model_name,
                        help="Claude model alias resolved via config mapping (default: %(default)s)")
        ap.add_argument("--gemini-model", default=defaults.gemini_model_name,
                        help="Gemini model name (default: %(default)s)")
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
            print("[ERROR] --changeset is only supported in --manifest mode.", file=sys.stderr)
            sys.exit(2)

        cfg = Config(
            model=args.openai_model,
            provider=args.provider,
            provider_name=utils.provider_display_name(args.provider),
            claude_model_name=args.claude_model,
            gemini_model_name=args.gemini_model,
            jpeg_quality=args.jpeg_quality,
            no_update_vocab=args.no_update_vocab,
            max_edge=(None if (args.max_edge in (None, 0)) else args.max_edge),
            process_all_variants=args.process_all_variants,
            date_confidence_threshold=args.date_confidence_threshold,
            location_confidence_threshold=args.location_confidence_threshold,
            dry_run=args.dry_run,
        )

        # MANIFEST MODE (recommended for Lightroom)
        if args.manifest:
            man = load_json(args.manifest)
            _apply_common_cfg(cfg, args, manifest=man)
            ecfg = _resolve_exiftool_config(args, dry_run=args.dry_run)
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
                elif out_ext.endswith(".json"):
                    # Aggregate then atomic write (temp → replace)
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
                        if os.path.exists(out_path):
                            os.remove(out_path)
                        os.replace(tmp_path, out_path)
                        _apply_exiftool_changeset(ecfg=ecfg, changeset_path=changeset_path, out_path=out_path)
                    finally:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                else:
                    print("[ERROR] --output-file must end with .ndjson or .json", file=sys.stderr)
                    sys.exit(2)
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
            orig_meta = load_json(args.meta) if args.meta else None
            _apply_common_cfg(cfg, args)
            data = analyze_photo(args.image, args.back, cfg, original_meta=orig_meta, write_sidecar=args.output_sidecars)
            print(json.dumps(data, indent=2 if cfg.pretty_json else None, ensure_ascii=False))

    except SystemExit:
        raise
    except Exception as e:
        err = {
            "level": "FATAL",
            "type": e.__class__.__name__,
            "message": str(e),
            "traceback": traceback.format_exception(e.__class__, e, e.__traceback__),
        }
        print(json.dumps(err, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
