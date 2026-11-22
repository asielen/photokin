# photokin.exiftool (wrapper layer)

ExifTool integration on top of the core library. It exists because the
Lightroom SDK cannot reliably read or write some fields — most importantly
`EXIF:UserComment` — so the pipeline uses ExifTool for exactly those gaps:

- **Read (hydration)**: before analysis, and only when the CLI was given `-r`,
  fill each item's metadata from the file itself — `EXIF:DateTimeOriginal`
  (else `XMP-photoshop:DateCreated`), `EXIF:UserComment`, `XMP:Description`,
  `XMP:Title`, the keywords (`XMP:Subject`, `XMP-lr:HierarchicalSubject` and
  `IPTC:Keywords`, unioned) and the four location fields (XMP, else IPTC),
  the set `DEFAULT_EXIFTOOL_FIELDS` declares — for every input type, filling
  only the keys the item does not already carry. Instruction tags map to
  their own `instructions` key, never to `userComment`, because UserComment is
  also the base the new analysis is appended to. `XMP:Subject` is read because
  `merge.py`'s date-correction heuristic treats a `DATE:` keyword as a human
  "hands off the date" signal, and reading the date without its interlock would
  re-date a print an archivist has already dated by hand. A file list too long
  for one command line is split across several ExifTool invocations rather than
  failing as a unit.
- **Write (apply)**: after analysis, write selected changeset fields directly
  into the files (or sidecars), so they survive even where Lightroom can't
  write them. Each IPTC location tag is also written to its XMP twin, and
  `EXIF:DateTimeOriginal` to `XMP-photoshop:DateCreated` (plus the IIM
  `IPTC:DateCreated`/`TimeCreated` pair for a complete date), so Lightroom
  sees every write when it re-reads the file. Every keyword addition and
  removal is made to `XMP-lr:HierarchicalSubject` and `IPTC:Keywords` as well
  as `XMP-dc:Subject` (to IIM only when it can store the keyword exactly).

The core never imports this package; the CLI composes the two
(**hydrate → analyze → apply**). Anything embedding the core can skip this
layer entirely.

## Modules

- `config.py` — `ExiftoolConfig` dataclass + `ExiftoolConfig.from_env()`.
- `locate.py` — `resolve_exiftool_path()`: binary discovery.
- `fetch.py` — `ensure_exiftool()`: download the official ExifTool on demand
  into the cache dir, on any OS; run via `python -m photokin.exiftool.fetch`.
- `manifest.py` — standalone ExifTool→manifest reader (details below).
- `hydrate.py` — `hydrate_item_metadata(items, cfg)` and
  `make_manifest_hydrator(cfg)` (returns a callable for
  `core.process_manifest_stream(metadata_hydrator=...)`). The tag-to-key pairs
  it fills are derived from `manifest.DEFAULT_EXIFTOOL_FIELDS` and
  `manifest._TAG_TO_MANIFEST_KEY` rather than restated.
- `apply.py` — `apply_changeset(changeset_path, cfg, ...)` and the CLI.
- `revert.py` — `revert_changeset(changeset_path, cfg, options)`: put a run's files back (below).

## Configuration

`ExiftoolConfig` fields: `path`, `cache_dir`, `enabled` (default False),
`fields` (allowed write tags), `dry_run`, `overwrite_original` (default True),
`write_sidecar_only`.

`ExiftoolConfig.from_env()` builds the pipeline config from environment
variables — `EXIFTOOL_WRITE_ENABLED` (default false), `EXIFTOOL_PATH`,
`EXIFTOOL_FIELDS` (comma-separated, default: every canonical tag photokin both
produces and reads back, `DEFAULT_PIPELINE_FIELDS` in `config.py`; a caller
that writes some tags itself narrows it explicitly) — with explicit keyword
overrides winning. The main CLI's `--exiftool-write`,
`--exiftool-fields` and `--exiftool-path` flags layer on top
(flag > env > default), and `-w` is the shorthand that turns writing on.

Writing to the user's files takes an explicit opt-in: `from_env` agrees with the
dataclass rather than overriding it, so an unset variable and an unset flag both
mean "record what would be written, apply nothing".

## Binary resolution order

ExifTool is **not** vendored in the wheel (that would put a ~34 MB Windows-only
Perl distribution into a `py3-none-any` package installed on every platform).
`resolve_exiftool_path` finds one at runtime:

1. Configured path (`ExiftoolConfig.path` / `--exiftool-path` /
   `EXIFTOOL_PATH`) — errors if set but missing.
2. A copy previously downloaded by `photokin.exiftool.fetch` under the cache
   root (`ExiftoolConfig.cache_dir`, default `~/.photokin/bin`), looked for at
   `<cache_dir>/exiftool/<platform>/`. `cache_dir` names the root, not that
   subdirectory — pointing it at the platform path makes resolution miss the
   cached copy and fall through to system `PATH`.
3. System `PATH` (`which exiftool`).

A cached copy is only used when its runtime dependencies sit alongside it (the
Windows `exiftool.exe` needs its sibling `exiftool_files/` directory; the
macOS/Linux Perl script needs a sibling `lib/Image/ExifTool.pm` tree); otherwise
the incomplete copy is skipped and resolution falls back to system `PATH` rather
than returning a path that would fail at runtime.

### Provisioning (`fetch.py`)

`python -m photokin.exiftool.fetch` provisions an ExifTool on any OS (the
Lightroom plugin's "Install/Update Requirements" flow runs it after installing
the package). Every download is verified by SHA256 — against an offline pin in
`KNOWN_SHA256` when set, else the checksum file exiftool.org publishes
(`checksums-<version>.txt` first, then the unversioned `checksums.txt`).

On **Windows** it downloads the official self-contained ExifTool release
archive from the project's SourceForge host and extracts it into the cache as
`exiftool.exe` + `exiftool_files/`. On **macOS/Linux** it first prefers an
already-cached copy, then a system ExifTool on `PATH`; with neither, it
downloads the official pure-Perl distribution (`Image-ExifTool-<version>.tar.gz`)
into the cache as `exiftool` + `lib/`, rewrites the script's shebang to
`env perl` and marks it executable — it runs on the system `perl`, which macOS
and nearly every Linux ship with. With no `perl` at all it points at the system
package (`brew install exiftool`, `apt install libimage-exiftool-perl`) instead.
Provisioning is best-effort: any failure leaves resolution to fall back to
system `PATH`.

## Hydration semantics

`hydrate_item_metadata` is best-effort and conservative:

- only queries files whose item metadata lacks at least one of the five keys,
  and fills only the keys that are actually missing or empty;
- never overwrites a value Lightroom or `--meta` provided;
- stores values verbatim — `dateTimeOriginal` keeps ExifTool's colon form,
  which is what the merge and canonical layers both read;
- creates an item's `metadata` object only when something was really read, so a
  file with no metadata leaves its item byte-identical;
- skips an item naming a `metadata_path`, since an inline dict would shadow the
  sidecar the caller pointed at;
- non-fatal if ExifTool fails to run: logs a `WARNING` via the standard
  `logging` module and returns without raising, rather than breaking the
  pipeline. The *request* is not best-effort — the CLI resolves the binary
  before the first model call and exits 2 when `-r` cannot be honored, so this
  path is for a mid-run failure on a file rather than for a missing install.

## Apply semantics

`apply_changeset` reads a changeset NDJSON (written by the core when the CLI
runs with `--changeset true`) and for each record:

- filters `proposed_changes.set` down to the allowed `fields`;
- normalizes date tags (`EXIF:DateTimeOriginal`, `EXIF:CreateDate`) to EXIF
  `YYYY:MM:DD HH:MM:SS` format (ISO input, `Z` suffix, and date-only values
  are handled; unparseable dates become warnings, not writes);
- invokes ExifTool with `-overwrite_original` (or `-o %d%f.xmp` when
  `write_sidecar_only`);
- returns a summary dict: `files_seen`, `files_written`, `files_left_to_caller`, `tags_written`,
  `errors`, `warnings`, `dry_run`. The main CLI appends this summary as a
  `{"run": "exiftool_apply", ...}` record to the results NDJSON when
  `--output-file` names one, and logs it to stderr otherwise — which is now the
  common case, since C2 made `--output-file` optional for every input type
  rather than the manifest-mode fixture it used to be.

`dry_run` counts what would be written without invoking ExifTool.

A record marked `"write": false` (a manifest item the caller applies itself) is skipped and counted in `files_left_to_caller`. Any write that touches an IPTC tag also recomputes the Photoshop `IPTCDigest`, since ExifTool leaves it stale and Lightroom then distrusts the IPTC block. With `record_before=True` (the main CLI's `-w` and the standalone CLI pass it), the files' values are read in one batch before writing and saved into the changeset as `original_data.written_tags`.

## Reverting a run

This is a maintenance tool for an agent or a developer cleaning up after a bad run. It reads a run's changeset back and restores each file it wrote:

```bash
photokin --revert-changeset results_changeset.ndjson --dry-run --output-file revert.ndjson
photokin --revert-changeset results_changeset.ndjson [--revert-paths PATH ... | --revert-match GLOB] [--force]
```

- The first record per path is used; virtual copies of one file share one original, and `entries` says how many records there were. Records marked `write: false` are skipped.
- Every tag the run wrote goes back to its value from `original_data.written_tags`, and a tag the file did not have is deleted. Keywords are restored as additions and removals against the file's current list. A tag the run never touched is never written.
- Before restoring, the file must still hold what the run wrote. A tag that changed since means someone edited the file after the run: the file is reported as `conflict` and left alone, unless `--force`.
- `--revert-paths` takes one file per flag (repeat it), so a name with a comma works; a filter that matches nothing says so.
- A changeset from before schema 5 has no `written_tags`, so it cannot say what the files held. Its files are reported `no_snapshot` and left alone; put them back by hand.
- Every restore is read back. A file whose tags did not come back (ExifTool skips a value it refuses, such as a `0000:00:00` date, and still exits 0) is an `error` naming them, not `reverted`.
- `--output-file` may not name the changeset itself.
- Output is NDJSON, one record per file with `status` (`reverted`, `unchanged`, `conflict`, `missing`, `no_snapshot` or `error`) and the `tags` touched, then `{"summary": {...}, "dry_run": ...}`. It goes to `--output-file` or stdout, and the exit code is 1 when any file conflicted, failed or had no snapshot.
- `--dry-run` reads and judges every file and writes nothing. `--exiftool-path` works as it does for `-w`.

## Standalone CLI

```bash
python -m photokin.exiftool --changeset results_changeset.ndjson \
  --enabled [--fields EXIF:UserComment,EXIF:DateTimeOriginal] \
  [--dry-run] [--exiftool-path /usr/local/bin/exiftool] \
  [--overwrite-original | --no-overwrite-original | --write-sidecar-only] \
  [--output summary.json]
```

`--overwrite-original` is the default and is spelled out here only because its
negation is the interesting one: ExifTool normally leaves a `_original` copy
beside every file it edits, and `--no-overwrite-original` is what keeps those
backups.

(`photokin/exiftool_apply.py` remains as a deprecated shim for the old
module path.)

## The manifest reader (`manifest.py`)

`manifest.py` is the standalone ExifTool→manifest reader: it shells out to
ExifTool and emits a manifest-like JSON, mapping common tags into top-level
keys (`dateTimeOriginal`, `userComment`, `caption`, `title`) while preserving
every raw tag under `metadata["exiftool"]`. Run it with
`python -m photokin.exiftool.manifest FILES... [--field TAG] [--out PATH]
[--include-all-if-no-fields]`. Repeat `--field` per tag; with none given it
reads a small default set, and `--include-all-if-no-fields` widens that to every
tag ExifTool can see, which is a lot of output.
`hydrate.py` reuses its `run_exiftool_json` primitive (and the `_find_tag_value`
helper, tolerant of ExifTool's `-G1` group names) rather than duplicating the
subprocess/JSON handling.
