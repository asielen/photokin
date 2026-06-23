# photo_archiver.exiftool (wrapper layer)

ExifTool integration on top of the core library. It exists because the
Lightroom SDK cannot reliably read or write some fields — most importantly
`EXIF:UserComment` — so the pipeline uses ExifTool for exactly those gaps:

- **Read (hydration)**: before analysis, fill manifest items whose
  `userComment` is missing by reading `EXIF:UserComment` from the files.
- **Write (apply)**: after analysis, write selected changeset fields directly
  into the files (or sidecars), so they survive even where Lightroom can't
  write them.

The core never imports this package; the CLI composes the two
(**hydrate → analyze → apply**). Anything embedding the core can skip this
layer entirely.

## Modules

- `config.py` — `ExiftoolConfig` dataclass + `ExiftoolConfig.from_env()`.
- `locate.py` — `resolve_exiftool_path()`: binary discovery.
- `hydrate.py` — `hydrate_user_comments(items, cfg)` and
  `make_manifest_hydrator(cfg)` (returns a callable for
  `core.process_manifest_stream(metadata_hydrator=...)`).
- `apply.py` — `apply_changeset(changeset_path, cfg, ...)` and the CLI.

## Configuration

`ExiftoolConfig` fields: `path`, `cache_dir`, `enabled` (default False),
`fields` (allowed write tags), `dry_run`, `overwrite_original` (default True),
`write_sidecar_only`.

`ExiftoolConfig.from_env()` builds the pipeline config from environment
variables — `EXIFTOOL_WRITE_ENABLED` (default true), `EXIFTOOL_PATH`,
`EXIFTOOL_FIELDS` (comma-separated, default `EXIF:UserComment`) — with
explicit keyword overrides winning. The main CLI's `--exiftool-*` flags layer
on top (flag > env > default).

## Binary resolution order

1. Configured path (`ExiftoolConfig.path` / `--exiftool-path` /
   `EXIFTOOL_PATH`) — errors if set but missing.
2. Bundled resource under `photo_archiver/tools/exiftool/<platform>/`
   (not currently shipped in this repo; extraction-to-cache machinery is in
   place for future bundled distribution).
3. System `PATH` (`which exiftool`).

## Hydration semantics

`hydrate_user_comments` is best-effort and conservative:

- only queries files whose manifest metadata lacks a non-empty `userComment`;
- never overwrites a value Lightroom provided;
- silently no-ops if ExifTool or the top-level `mel_exiftool_manifest.py`
  reader is unavailable.

## Apply semantics

`apply_changeset` reads a changeset NDJSON (written by the core when the CLI
runs with `--changeset true`) and for each record:

- filters `proposed_changes.set` down to the allowed `fields`;
- normalizes date tags (`EXIF:DateTimeOriginal`, `EXIF:CreateDate`) to EXIF
  `YYYY:MM:DD HH:MM:SS` format (ISO input, `Z` suffix, and date-only values
  are handled; unparseable dates become warnings, not writes);
- invokes ExifTool with `-overwrite_original` (or `-o %d%f.xmp` when
  `write_sidecar_only`);
- returns a summary dict: `files_seen`, `files_written`, `tags_written`,
  `errors`, `warnings`, `dry_run`. The main CLI appends this summary as an
  `exiftool_apply` status record to the results NDJSON.

`dry_run` counts what would be written without invoking ExifTool.

## Standalone CLI

```bash
python -m photo_archiver.exiftool --changeset batch_changeset.ndjson \
  --enabled [--fields EXIF:UserComment,EXIF:DateTimeOriginal] \
  [--dry-run] [--exiftool-path /usr/local/bin/exiftool] \
  [--no-overwrite-original | --write-sidecar-only] [--output summary.json]
```

(`photo_archiver/exiftool_apply.py` remains as a deprecated shim for the old
module path.)

## Relationship to `mel_exiftool_manifest.py`

The top-level `mel_exiftool_manifest.py` script is the standalone
ExifTool→manifest reader (usable on its own to build a manifest outside
Lightroom). `hydrate.py` reuses its `run_exiftool_json` primitive rather than
duplicating the subprocess/JSON handling.
