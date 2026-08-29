# Rename mode: convention-aware mass rename

Implementation plan for a grammar-aware, order-preserving mass rename in
photokin. photokin is self-sufficient here: it plans the names, previews them,
and applies them on disk with the same write-safety model as everything else
it does. Catalog applications that own their files (Lightroom today) do not
let photokin rename, so they consume the same plan through manifest mode and
apply it themselves; the Lightroom wrapper is specified separately in
`lightroom-rename.md` and depends on nothing in this document but section 6.

## 1. What the feature does

Given a folder of scans that follow the naming grammar
(`name[letter][-front|-back|-negative|-pageN][-crop]`), a prefix template and a
digit width, produce new names that keep every variant tag, close the gaps in
the numbering, and follow the folder's current order.

```
file102.tif          ->  newname-001.tif
file105.tif          ->  newname-002.tif
file105b.tif         ->  newname-002b.tif
file105b-back.tif    ->  newname-002b-back.tif
```

The sentence this implements: "clean up and rename the files in this folder
using this prefix." A `-` always separates the prefix from the number.

```
photokin ./scans --rename "newname"                       preview, touches nothing
photokin ./scans --rename "newname" -w                    record the plan and apply it
photokin ./scans --rename "{date:yymmdd}-bag" -w          520601-bag-001.tif; numbering restarts per date
photokin ./scans --rename "{today:yymmdd}-bag" -w         batch date (the run date) instead
photokin ./scans --rename "newname{date:yyyy-mm-dd}" -w   newname1952-06-01-001.tif
photokin ./scans --rename "{orig}" -w                     keep the prefix, just renumber and clean up
photokin ./scans --rename-undo                            reverse the last applied run in that folder
```

## 2. Decisions

| Question | Decision |
|---|---|
| Who renames | photokin, on disk, only with `-w`. A bare `--rename` run is the preview, exactly as a bare analysis run is the "check it's wired up" run. A manifest exported by a catalog application (section 6.1, `managed_by`) makes `-w` a usage error: the catalog applies the plan, photokin only writes it. |
| Numbering | Three digits, zero-padded, `--digits` to change. Numbering restarts at 001 for each distinct rendered prefix (each date when the prefix contains `{date}`). |
| Order | The folder's current alphabetical order, the `(name.lower(), name)` key `list_folder_images` already uses. Files with no trailing number are not special; they take the number their position gives them. A manifest may carry an explicit `order` per item, which wins. |
| Cleanup | Variant form normalized (`box3_025-b` becomes `...025b`); part separators normalized (`_back`, `.back`, ` back` become `-back`); companion files carried along (`.md`, `.json`, `.xmp`, `.txt` sharing a stem, plus the `.jpg` twin of a `.tif`, which is an image in its own right and gets the same stem). Extensions are left exactly as they are. |
| Prefix | The template renders the prefix; the renderer always puts `-` between it and the number, so every name ends in `-NNN[letter][-part][-crop]` and `parse_media_filename` reads it back unchanged. A prefix that already begins or ends in `-` is trimmed rather than doubled; an empty prefix is an error. (Amended 2026-08-29, during the build: this said trailing only. `{folder}` at a drive root renders empty, so `"{folder}-bag"` produced the prefix `-bag` and every target began with a dash -- which is the same objection this table already makes to an empty prefix, that a name starting with `-001` is not a name, and a leading-dash filename is read as a flag by every command-line tool that meets it.) The separator is fixed because it is also the grammar's part separator. |
| Safety | Two-phase rename through hidden temporary names, journal written and flushed before the first rename, nothing ever overwritten (`os.rename`, never `os.replace`), full verification after, undo and resume from the journal. |

## 3. Architecture

Three modules with one direction of dependency.

`utils.py` gains the grammar's inverse next to `parse_media_filename`:
`canonicalize_stem` and `render_media_filename` (section 4.2).

`photokin/rename.py` is the planner: a pure function from an in-memory folder
listing (plus optional per-item order, date and manifest overrides) to a plan
(section 6.2). It performs no IO beyond what is injected. Every rule in
section 4 lives here and nowhere else.

`photokin/rename_apply.py` is the executor: it takes a plan, re-checks it
against the disk, writes the journal, performs the two-phase rename, brings
companions along, verifies, and can undo or resume (section 5). It is the only
module that renames anything, and it is reached only through `-w`,
`--rename-undo`, `--rename-resume` and `--rename-finish`.

`cli.py` wires `--rename` in as a mode flag on the existing positional input,
following the `--generate-manifest` precedent (section 7).

## 4. The planner

### 4.1 Inputs

A folder or a manifest, the same two inputs the analysis path resolves. Each
item carries:

| Key | Source | Meaning |
|---|---|---|
| `path` | both | Absolute path of an image file. |
| `metadata["EXIF:DateTimeOriginal"]` | hydration or the manifest author | `%Y:%m:%d %H:%M:%S`, `%Y:%m:%d`, or ISO 8601. This is the tag `date_guess` is written to, so a folder photokin has already dated needs nothing else. |
| `order` (new, optional) | manifest author | Integer position. When present on every item it replaces the alphabetical sort. A catalog wrapper passes its grid order here. |
| `is_back`, `version`, `preferred` | existing overrides | Honored exactly as the grouper honors them: an explicit flag beats the filename, and the rename materializes the override into the name (`box3_017_back.jpg` flagged `is_back` becomes `...-back.jpg`). |

When the template uses `{date}` and an item has no date in its metadata,
rename mode hydrates `EXIF:DateTimeOriginal` for the folder itself
in one ExifTool call. Reading is not writing, so this needs no flag; `-r` is
accepted and redundant.

The planner always lists the folder on disk, in manifest mode too, because two
things live there that a manifest does not describe: companions (non-image
files sharing a stem) and bystanders (images not in the manifest, whose names
are reserved).

### 4.2 Parsing and rendering the grammar

`parse_media_filename` already decomposes a name into
`ParsedName(base_id, variant_id, part_kind, page_num, is_crop)`. Two additions
go beside it, in the same section of `utils.py`, so the grammar stays in one
place.

`canonicalize_stem(stem) -> tuple[str, list[str]]` is a lenient pre-pass
applied right to left before parsing: `-crop` first, then a part suffix. It
rewrites `[_. ]` separators in front of `crop`, `back`, `front`, `negative`
and `pageN` to `-` and returns the notes ("part separator normalized") that
end up in the preview. `_EXPLICIT_BACK_SUFFIX_RE` in `core.py` is the
precedent for accepting those separators. It does not touch `_b` or `.b`: the
grammar reads only `-b` and `5b`, and widening it is out of scope. The dashed
variant form needs no rewriting because the renderer always emits the letter
directly after the digits; the "variant form normalized" note comes from
which alternative of the parser's variant regex matched, never from a text
search (a search for `-b` fires on every `-back`).

`render_media_filename(prefix, number, digits, parsed, ext) -> str` is the
inverse: `f"{prefix}-{number:0{digits}d}{variant}{part}{crop}{ext}"`, with
`prefix` already stripped of any trailing `-`, `variant` written directly
after the digits, `part` one of `-front`, `-back`, `-negative`, `-page{N}` or
empty, and `crop` `-crop` or empty. Page numbers are carried, never
renumbered.

Invariant, tested as a property: for any tail,
`parse_media_filename(render_media_filename(...))` returns the same
`variant_id`, `part_kind`, `page_num` and `is_crop`.

### 4.3 Grouping and order

Group key: `(folder, canonical base_id.lower())`. `box3_017.tif`,
`box3_017.jpg`, `box3_017-b.tif`, `box3_017_back.tif` and
`box3_017b-back-crop.tif` are one group. Same stem, different extension is one
slot and gets one target stem with each file's own extension.

Order: sort every item by `order` when supplied, else `(name.lower(), name)`.
A group's position is the position of its earliest member, and groups are
numbered in that order. `--order natural` (digit runs compared numerically, so
`file9` precedes `file10`) is cheap to add but not the default, because the
default must match what photokin's other modes show.

### 4.4 Prefix template

`{name}` or `{name:FORMAT}` tokens, everything else literal, `{{` for a
literal brace.

| Token | Value |
|---|---|
| `{date}` / `{date:FORMAT}` | The photo's own date: `EXIF:DateTimeOriginal`, the tag photokin writes `date_guess` to. |
| `{today}` / `{today:FORMAT}` | The run's date in local time, for batch-dated prefixes. `--today YYYY-MM-DD` overrides it, so a plan is reproducible and a batch scanned last week can carry its own date. |
| `{folder}` | Name of the containing folder. |
| `{orig}` | The group's current base_id with its trailing digit run and any `-` before it removed (`file105` gives `file`, `newname-001` gives `newname`), so `--rename "{orig}"` is "keep the prefix, renumber and clean up", and running it on already-clean names changes nothing. |

FORMAT is a small case-insensitive grammar: `yyyy`, `yy`, `mmmm` (June),
`mmm` (Jun), `mm`, `dd`; every other character is literal; the default is
`yyyy-mm-dd`. `mm` always means the month. Filenames do not carry minutes,
and the Java and Qt convention where `mm` means minutes is the ambiguity this
grammar exists to avoid, so `YYYY-MM-DD` and `yyyy-mm-dd` are the same
format. A FORMAT containing `%` is handed to strftime unchanged for anyone who
wants `%j` or `%U`. Rendering walks the tokens and reads the parts off the
parsed date directly rather than translating to strftime, so `mmm` and
`mmmm` are the same on Windows as everywhere else. A partial date (`1952`,
`1952:06`) renders `00` for the parts it lacks and is flagged in the preview.

The group's date comes from its representative: the member with the lowest
`PART_RANK` (a front before a page before a back), ties broken by order. If
members disagree, the representative wins and a warning names the others. A
group with no date is an error listing every such group, unless
`--undated LITERAL` is given, in which case the literal stands in for `{date}`
and those groups form their own numbering bucket. `{today}` never needs any
of this; it renders the same for every group, so it never splits the
numbering on its own.

Token values are sanitized with the existing `_TAG_FILENAME_UNSAFE_RE` policy.
Literal template text is validated, not altered: a path separator, a character
illegal on Windows, or a Windows reserved device name is an error.

The separator between prefix and number is always `-` and is never typed: the
renderer adds it. `newname{date:yyyy-mm-dd}` gives
`newname1952-06-01-001.tif`, and a prefix that ends in a digit is no problem,
because the dash keeps the prefix's digits and the number apart. A rendered
prefix that begins or ends in one or more `-` has them trimmed first, so
`{date:yymmdd}-bag-` and `{date:yymmdd}-bag` produce the same names; the
trim is reported in the preview notes. A rendered prefix that is empty (a
literal of nothing, or `{orig}` on a file named only by digits) is an error
that names the groups, since a name that starts with `-001` is not a name.

### 4.5 Numbering

Bucket key is the rendered prefix, compared case-insensitively. Each bucket
counts from 1 in global order, so with `{date:yymmdd}-bag-woodbury`:

```
scan_001.tif       1952-06-01   ->  520601-bag-woodbury-001.tif
scan_002.tif       1952-06-01   ->  520601-bag-woodbury-002.tif
scan_002-back.tif               ->  520601-bag-woodbury-002-back.tif
scan_003.tif       1961-09-14   ->  610914-bag-woodbury-001.tif
scan_004.tif       1952-06-01   ->  520601-bag-woodbury-003.tif
```

A bucket that would need more digits than `--digits` allows is an error
naming the bucket and the count; nothing widens silently.

### 4.6 Validation

Errors stop the plan; warnings ride along into the preview.

Errors: duplicate targets within a folder, compared case-insensitively so the
plan is safe on macOS and Windows; a target matching a bystander's current
name; a group whose members sit in different folders; illegal characters or a
reserved name in the template; a name longer than 255 bytes; a missing date
without `--undated`; digit overflow.

Warnings: every normalization applied; date disagreement inside a group; a
prefix ending in a digit; a companion whose stem matches two groups; a
same-stem file with an extension outside the companion set, which is left
behind and named.

Idempotency: running the planner on its own output produces a plan in which
every entry has `changed: false`, and the CLI reports "already clean".

Chains (a target equal to another planned file's current name, which any
gap-closing renumber produces) are not an error and need no special handling:
the executor is two-phase for every run.

### 4.7 A fuller worked example

Prefix `bw`, three digits, defaults for everything else. The left column is
in alphabetical order.

```
box3_017-b.tif            ->  bw-001b.tif            variant form normalized
box3_017.jpg              ->  bw-001.jpg             pair: same slot as the .tif
box3_017.tif              ->  bw-001.tif
box3_017_back.tif         ->  bw-001-back.tif        separator normalized
box3_017b-back-crop.tif   ->  bw-001b-back-crop.tif
box3_020-page1.tif        ->  bw-002-page1.tif
box3_020-page2.tif        ->  bw-002-page2.tif
reunion.tif               ->  bw-003.tif             no trailing number; keeps its place
reunion.md                ->  bw-003.md              companion
```

## 5. The executor

### 5.1 Preflight

The plan records each source's size and mtime. Before touching anything the
executor re-lists the folder and refuses to run if any source is missing or
changed since planning, if any target already exists and is not itself a
source, or if a journal for this folder is still `in_progress` (section 5.4).
A stale plan is replanned, never patched.

### 5.2 Journal, then two phases

The journal is NDJSON inside the renamed folder,
`<foldername>_rename-<run_id>.ndjson`, whichever kind of input produced the
plan: a folder run's changeset already lands inside the folder
(`_derive_changeset_path` with `ResolvedInput.directory`), and the journal
describes the folder, not the manifest that pointed at it. It is written,
flushed and fsynced before the first rename. A header record carries
`run_id`, `folder`, `prefix_template`, `digits`, `photokin_version` and
`status: in_progress`; one record per file carries `from`, `to`, `tmp`,
`kind` (`image` or `companion`) and `photo_id` from
`changeset.make_photo_id`.

Phase A renames every changing file to a hidden temporary name in the same
folder, `.photokin-rename-<run_id>-<index><ext>`. Phase B renames each
temporary to its target. Doing this for every run, not only when chains
exist, buys three things for the price of a second `os.rename` per file:
gap-closing renumbers never collide, case-only renames work on
case-insensitive filesystems, and a failure in either phase leaves a state the
journal describes exactly. `os.rename` is used rather than `os.replace`
because it fails if the target exists, which is the behavior wanted.

After phase B, a `.md` transcript sidecar has its `source_file:` frontmatter
line rewritten to the new image name, since `write_markdown_sidecar` sets it
to the image's basename.

### 5.3 Verify and report

Every `to` exists, no temporary remains, every unchanged file is untouched.
The journal footer records `status: applied` and the run summary prints
counts: renamed, companions, unchanged, left behind. Any failure in verify or
in either phase triggers a reversal of the completed steps and records
`rolled_back`; if the reversal itself fails, `needs_attention` with the exact
temporaries still on disk, exit code 1.

### 5.4 Undo and resume

`--rename-undo [JOURNAL]` builds the reverse plan from the latest `applied`
journal in the folder (or the one named), verifies every `to` is still in
place, and runs it through the same executor, appending `status: undone`.
`--rename-resume [JOURNAL]` finishes a run left `in_progress` or
`needs_attention`, forward. While such a journal exists, a new `--rename -w`
in that folder is refused until one of the two is run.

### 5.5 Companions renamed by someone else

`--rename-finish PLAN` is the executor with the images taken as already
renamed: for each entry it requires that `to` exists and `from` does not, then
renames the companions and rewrites sidecars exactly as section 5.2 does, and
writes the journal. This is what a catalog wrapper calls after the catalog has
renamed the images, and it is the only on-disk operation such a wrapper needs
from photokin. Entries whose images are not yet renamed are reported and
skipped, so the command is safe to run again. The journal it writes marks
each image record `renamed_by: external`, and `--rename-undo` on such a
journal reverses companions only, requiring each image to be back at `from`
first (the catalog's own undo) and reporting the rest.

### 5.6 Not for catalogued folders

photokin cannot tell whether a folder is in a Lightroom catalog. The preview
ends with one line saying that a folder tracked by a catalog application must
be renamed through that application, and points at the wrapper. The
`managed_by` guard in section 6.1 covers the case where the wrapper is the
caller; the sentence covers the case where a person is.

## 6. Contracts

photokin owns both files. Any wrapper reads them, never the modules.

### 6.1 Manifest in

The existing manifest shape plus two optional additions:

```json
{
  "managed_by": {"app": "lightroom", "catalog": "/Volumes/Archive/archive.lrcat"},
  "items": [
    {"path": "/Volumes/Archive/bag-woodbury/file105b-back.tif",
     "order": 3,
     "metadata": {"EXIF:DateTimeOriginal": "1952:06:01 00:00:00"}}
  ]
}
```

`managed_by` present makes `-w` a usage error in rename mode ("this manifest
was exported by lightroom; apply the plan through it") and is copied into the
plan. `order` is described in 4.1.

### 6.2 Plan out

Printed as a table by default; `--plan-out PATH` writes it as JSON.

```json
{
  "schema_version": 1,
  "run_id": "2026-08-29T20:14:03Z_a1b2c3d4",
  "photokin_version": "0.6.0",
  "folder": "/Volumes/Archive/bag-woodbury",
  "prefix_template": "{date:yymmdd}-bag-woodbury",
  "digits": 3,
  "order": "manifest",
  "managed_by": {"app": "lightroom", "catalog": "/Volumes/Archive/archive.lrcat"},
  "entries": [
    {"path": "/Volumes/Archive/bag-woodbury/file105b-back.tif",
     "photo_id": "3f2a...",
     "size": 48211904, "mtime": 1719346204.0,
     "target": "520601-bag-woodbury-002b-back.tif",
     "target_stem": "520601-bag-woodbury-002b-back",
     "group": "file105", "prefix": "520601-bag-woodbury", "number": 2,
     "variant": "b", "part": "back", "page": null, "crop": false,
     "changed": true,
     "notes": [],
     "companions": [
       {"path": "/Volumes/Archive/bag-woodbury/file105b-back.md",
        "target": "520601-bag-woodbury-002b-back.md"}
     ]}
  ],
  "left_behind": [],
  "warnings": [],
  "errors": []
}
```

`run_id` reuses `changeset.make_run_id`; `photo_id` reuses
`changeset.make_photo_id`. Schema-version it the way `changeset.py` does, with
the same rule for bumping.

### 6.3 Changeset record

With `--changeset true` (which `-w` implies), each planned file also gets a
changeset record in the existing NDJSON stream, `kind: rename`, carrying
`from`, `to` and `companions`, so a rename shows up in the same audit trail as
a metadata write. The journal (5.2) is the operational record; the changeset
is the audit one.

## 7. CLI

`--rename PREFIX` is a mode flag on the existing positional input. Like
`--generate-manifest`, it runs before any model call and stops; unlike it, it
has a write path. In rename mode:

`-w` expands to `--changeset true --rename-apply true`, a second bundle of the
same shape as `_WRITE_BUNDLE` (the file already has `_VERBOSE_BUNDLE` as the
precedent for a second one). `--exiftool-write true` beside `--rename` is a
usage error, since rename mode writes no tags. `--dry-run` with `-w` prints
what would be applied and stops. `--output-file` is refused, as it is beside
`--generate-manifest`; `--plan-out` is the rename-mode equivalent.

Mode options: `--digits N` (default 3), `--order name|natural`, `--undated
LITERAL`, `--today YYYY-MM-DD`, `--companions EXT[,EXT]` extending the
default set, `--plan-out PATH`. Executor commands: `--rename-undo` and `--rename-resume` take the
positional folder (latest journal there) or a journal path; `--rename-finish`
takes a plan path.

Exit codes follow the rest of the CLI: 2 for usage and validation errors, 1
for an executor failure, 0 otherwise, including "already clean".

## 8. Phases

Phase 1, grammar and planner. `canonicalize_stem` and `render_media_filename`
beside the parser; `photokin/rename.py` with the planner as a pure function
over an injected listing. Exit: the examples in 1, 4.5 and 4.7 pass as tests;
the round-trip property holds; idempotency holds.

Phase 2, preview and contracts. `--rename` wired into `cli.py` with the
preview table, `--plan-out`, `--changeset true`, the `managed_by` guard, and
automatic date hydration. Exit: the table and the JSON are produced for a
folder and for a manifest; `docs/rename-contract.md` holds section 6.

Phase 3, executor. `rename_apply.py` with preflight, journal, two phases,
sidecar rewrite, verify, rollback. Exit: on a tmp folder, apply then undo
returns the listing and every sidecar to its starting bytes; a fault injected
between the phases leaves a journal that `--rename-resume` completes and
`--rename-undo` reverses.

Phase 4, `--rename-finish`, then docs: README section under naming
conventions, CHANGELOG, version bump. Exit: a wrapper can complete a rename
using only sections 6 and 5.5.

Phase 5, dogfood on a copy of one real archive folder that is not in any
catalog, then the real one.

## 9. Tests

Planner, pytest, no filesystem: the brief's example verbatim; restart per
prefix with interleaved dates; an unnumbered file taking its alphabetical
place; each normalization alone (`-b`, `_back`, `.back`, `-page3` retained,
`-negative`, `-crop` stacking); a `.tif`/`.jpg` pair sharing a target stem;
companions listed and a same-stem `.pdf` reported as left behind; explicit
`order` overriding name order; `is_back` and `version` overrides materialized;
bystander collision; duplicate targets differing only in case; digit overflow;
missing date with and without `--undated`; idempotency; the parse/render round
trip as a property over generated tails. Template tests: each FORMAT token,
upper- and lower-case spellings rendering identically, `%` passthrough, a
partial date rendering `00` with its flag, `{today}` with and without
`--today`, the separator always present (`newname{date:yyyy-mm-dd}` gives
`newname1952-06-01-001`), a trailing `-` trimmed rather than doubled, an
empty rendered prefix as an error, and `{orig}` on `newname-001` giving
`newname`.

Executor, pytest with `tmp_path`: apply and verify; undo restores bytes and
names; a chain (`file001`, `file002`, `file004`, `file005`) applies without
collision; case-only rename on a case-insensitive filesystem when the runner
has one; fault injection after phase A leaves `in_progress`, resume completes,
undo reverses; a changed source mtime fails preflight; an existing non-source
target fails preflight; `--rename-finish` renames only companions whose image
is already in place and reports the rest.

CLI, following `test_cli_input_surface.py`: `--rename` alone writes nothing;
`-w` expands to the rename bundle; `--exiftool-write true` and `--output-file`
are refused; `managed_by` makes `-w` a usage error while `--plan-out` still
works; exit codes.

## 10. Risks and open questions

The journal lives in the archive folder itself, so it is one more non-image
file there; that is the existing changeset convention, and a `.ndjson` never
matches an image stem, so it can never be mistaken for a companion. Windows
path length and reserved names are validated in the planner; long-path
support is left to the OS setting. Very large folders are fine: the planner
is O(n log n) and the executor does 2n renames.

Open for the first session: whether `-w` should be the apply switch (matches
the mode-agnostic `-w` intent and the changeset semantics) or whether rename
deserves its own `--rename-apply` spelled out every time, with `-w` refused;
and whether `{orig}` should get an `{onum}` sibling for people who want to
keep the original number.

## 11. Out of scope

Renaming across folders or a partial selection; changing extension case;
widening the variant grammar to `_b`; any knowledge of Lightroom beyond the
`managed_by` guard and the one sentence in the preview.
