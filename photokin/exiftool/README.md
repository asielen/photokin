# photokin.exiftool (wrapper layer)

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
- `fetch.py` — `ensure_exiftool()`: download the official ExifTool on demand
  (Windows) into the cache dir; run via `python -m photokin.exiftool.fetch`.
- `manifest.py` — standalone ExifTool→manifest reader (details below).
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

ExifTool is **not** vendored in the wheel (that would put a ~34 MB Windows-only
Perl distribution into a `py3-none-any` package installed on every platform).
`resolve_exiftool_path` finds one at runtime:

1. Configured path (`ExiftoolConfig.path` / `--exiftool-path` /
   `EXIFTOOL_PATH`) — errors if set but missing.
2. A copy previously downloaded by `photokin.exiftool.fetch` into the cache dir
   (`ExiftoolConfig.cache_dir`, default `~/.photokin/bin/exiftool/<platform>`).
3. System `PATH` (`which exiftool`).

A cached copy is only used when its runtime dependencies sit alongside it (the
Windows `exiftool.exe` needs its sibling `exiftool_files/` directory; a macOS
Perl script would need a sibling `lib/Image/ExifTool.pm` tree); otherwise the
incomplete copy is skipped and resolution falls back to system `PATH` rather
than returning a path that would fail at runtime.

### Provisioning (`fetch.py`)

The Lightroom plugin's "Install/Update Requirements" flow runs
`python -m photokin.exiftool.fetch` after installing the package. On **Windows**
this downloads the official self-contained ExifTool from exiftool.org, verifies
its SHA256 (against an offline pin in `KNOWN_SHA256` when set, else the
release's published checksum file — `checksums-<version>.txt` first, then the
unversioned `checksums.txt`), and extracts it into the cache as
`exiftool.exe` + `exiftool_files/`. On **macOS/Linux** it is a no-op — install a
system ExifTool (`brew install exiftool`, `apt install libimage-exiftool-perl`).
Provisioning is best-effort: any failure leaves resolution to fall back to
system `PATH`.

## Hydration semantics

`hydrate_user_comments` is best-effort and conservative:

- only queries files whose manifest metadata lacks a non-empty `userComment`;
- never overwrites a value Lightroom provided;
- non-fatal if ExifTool can't be found or fails to run: logs a `WARNING` via
  the standard `logging` module and returns without raising, rather than
  breaking the manifest pipeline.

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
python -m photokin.exiftool --changeset batch_changeset.ndjson \
  --enabled [--fields EXIF:UserComment,EXIF:DateTimeOriginal] \
  [--dry-run] [--exiftool-path /usr/local/bin/exiftool] \
  [--no-overwrite-original | --write-sidecar-only] [--output summary.json]
```

(`photokin/exiftool_apply.py` remains as a deprecated shim for the old
module path.)

## The manifest reader (`manifest.py`)

`manifest.py` is the standalone ExifTool→manifest reader: it shells out to
ExifTool and emits a manifest-like JSON, mapping common tags into top-level
keys (`dateTimeOriginal`, `userComment`, `caption`, `title`) while preserving
every raw tag under `metadata["exiftool"]`. Run it with
`python -m photokin.exiftool.manifest FILES... [--field TAG] [--out PATH]`.
`hydrate.py` reuses its `run_exiftool_json` primitive (and the `_find_tag_value`
helper, tolerant of ExifTool's `-G1` group names) rather than duplicating the
subprocess/JSON handling.
