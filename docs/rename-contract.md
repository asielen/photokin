# Rename mode: the wire contract

This is the contract for rename mode's two on-disk shapes: the plan JSON
`--rename --plan-out PATH` writes, and the `kind: "rename"` record `-w`
appends to the changeset NDJSON stream. It is written for a wrapper that
never imports `photokin` -- the Lightroom plugin, or any other catalog
integration -- so every field below is specified by name and type, not by
pointing at the Python that builds it. It implements
[`docs/rename-mode.md`](rename-mode.md) section 6; section numbers in
parentheses below refer to that document.

A wrapper that renames images itself (a catalog application) needs three
things, in order: this file, to build a plan and read it back; plan section
5.5 (`--rename-finish`), which is the one on-disk operation such a wrapper
calls; and nothing else -- it never needs to read `photokin/rename.py` or
`photokin/rename_apply.py`.

**Where the code and this plan disagree.** `docs/rename-mode.md` section 2
says the CLI reports "already clean" for a plan that changed nothing. The
shipped CLI does not print that phrase: it prints the run report's status
field verbatim, and the status for that case is `"nothing_to_do"` (see
[Statuses](#statuses-you-will-see-on-a-run-report) below). Treat
`"nothing_to_do"` as the plan's own answer to "was anything already clean",
not the string "already clean" itself.

## Schema versioning

Both the plan and the changeset record are schema-versioned the way
`changeset.py` already versions the rest of the changeset stream: the
version is bumped whenever a shape changes in a way a consumer could care
about -- a new top-level key, a renamed one, a changed meaning for an
existing one -- and left alone for anything else (a new *value* an existing
string field can now hold is not a shape change). The plan's `schema_version`
is `photokin.rename.SCHEMA_VERSION`, currently `1`. The changeset record's
`schema_version` is `photokin.changeset.SCHEMA_VERSION`, the same number
every other changeset record in the stream carries (currently `2`) -- a
rename record is not versioned on its own schedule, because it lives in that
same stream and a reader already has to track that number to read anything
else in it.

**Why `entries[].target_photo_id` (below) did not bump `schema_version`.**
It is a new key, which the rule above would ordinarily treat as a shape
change. But it is purely additive: no existing field's presence, type or
meaning moved, so code written against the old shape reads exactly the same
plan it always did and is no worse off for the key it doesn't know about --
the failure mode a bump exists to flag (silently misreading a field that is
still there but now means something else) cannot happen here. Weighing the
other way: `rename_apply.py` refuses to act on a plan whose `schema_version`
does not equal its own pinned constant, by exact match, so bumping this
number is not a documentation-only move -- it stops every plan this planner
produces from being applied until that check is updated in step. That
coupling belongs to `rename_apply.py`, not to this contract, so it is noted
here rather than acted on.

The rename *journal* (`docs/rename-mode.md` section 5.2) has its own
`JOURNAL_SCHEMA_VERSION`, but a wrapper following the flow above never reads
a journal directly -- it is `rename_apply.py`'s own bookkeeping, replayed
back into a plan-shaped answer by `--rename-finish`. It is out of scope
here.

## 1. Manifest in

A wrapper feeds `--rename` its own manifest, in the shape folder mode's
`--generate-manifest` already produces, plus two optional fields (section
4.1, 6.1):

```json
{
  "managed_by": {"app": "lightroom", "catalog": "/Volumes/Archive/archive.lrcat"},
  "items": [
    {
      "path": "/Volumes/Archive/bag-woodbury/file105b-back.tif",
      "order": 3,
      "metadata": {"EXIF:DateTimeOriginal": "1952:06:01 00:00:00"}
    }
  ]
}
```

| Field | Type | Meaning |
|---|---|---|
| `managed_by` | any JSON value, top-level, optional | `{"app": str, "catalog": str}`, or any other shape the wrapper wants -- the guard reads **presence, not shape**. Any non-`null` value (an object, an empty object, a string, a list, a number, `true`) marks the archive as catalog-tracked and makes `-w` a usage error (exit 2): the plan is still produced and can still be written with `--plan-out`, but photokin refuses to rename the files itself. An explicit `null` reads as an absent key -- unmanaged. `app` and `catalog` are read only to word that refusal, and only when the value is an object carrying them as non-empty strings; every other shape gets the same refusal in wording that names no application. Copied verbatim into the plan's own `managed_by`, whatever its shape. |
| `items[].path` | string, required | Absolute path of an image already on disk. Everything else on the item is optional. |
| `items[].order` | integer, optional | The item's explicit position. When **every** item in the manifest carries one, it replaces alphabetical order and the plan's `order` field reports `"manifest"`. A manifest where only some items carry `order` falls back to the normal `name`/`natural` order for all of them -- there is no partial-override mode. |
| `items[].metadata["EXIF:DateTimeOriginal"]` | string, optional | `%Y:%m:%d %H:%M:%S`, `%Y:%m:%d`, a partial `"1952"`/`"1952:06"`, or ISO 8601. Feeds `{date}`. A folder input gets this hydrated automatically by one ExifTool call when the template uses `{date}`; a manifest wrapper that already has the date should put it here rather than rely on hydration re-deriving it. |
| `items[].is_back` | bool, optional | Overrides the filename's own front/back reading. `true`/`false`/`1`/`0`/`"yes"`/`"no"` are all accepted, the same tri-state spellings `core._coerce_manifest_bool` reads elsewhere in the manifest. |
| `items[].version` | string, optional | Overrides the parsed variant letter. An empty string clears an existing letter rather than leaving it alone. |
| `items[].preferred` | bool, optional | No rule in the planner gives it an effect on a rendered *image* name. It does decide which of two same-stem images owns a companion they share — the planner picks a shared sidecar's owner by the same rule the analysis half uses to write it, and `preferred` is one component of that rule — so marking the derivative of a TIFF/JPEG twin moves the shared `.md` onto the derivative. |

## 2. Plan out

`--plan-out PATH` writes the plan as JSON, indented, exactly the dict below
-- there is no envelope around it. Printed as a table on stderr by default
(or in addition, when `--plan-out` is also given); the JSON is the contract,
the table is a convenience for a human running the CLI directly.

**`PATH` is refused (exit 2, before the file is opened) if it names a file
this run depends on** -- any image or companion in `entries`, any
`left_behind` file, a bystander image the folder holds that the manifest did
not list, the input manifest itself, an existing rename journal in the
folder, a name this run is about to rename a file *to*, or the path the
changeset would also be written to. Matching is filesystem identity, not
string equality: a symlink, a hard link, or a `..` detour onto one of those
files is refused exactly as the direct spelling is. This holds for a bare
preview run (no `-w` needed) and under `--dry-run`; a wrapper building its
own plan file should apply the same check against its own destination rather
than assume `PATH` is safe merely because it differs textually from every
path the plan names. See `docs/rename-mode.md` section 7 for the full rule,
which is one guard shared with the changeset destination and every other
destination photokin writes, not something specific to this file.

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
    {
      "path": "/Volumes/Archive/bag-woodbury/file105b-back.tif",
      "photo_id": "3f2a...",
      "target_photo_id": "9c1e...",
      "size": 48211904,
      "mtime": 1719346204.0,
      "target": "520601-bag-woodbury-002b-back.tif",
      "target_stem": "520601-bag-woodbury-002b-back",
      "group": "file105",
      "prefix": "520601-bag-woodbury",
      "number": 2,
      "variant": "b",
      "part": "back",
      "page": null,
      "crop": false,
      "changed": true,
      "notes": [],
      "companions": [
        {
          "path": "/Volumes/Archive/bag-woodbury/file105b-back.md",
          "target": "520601-bag-woodbury-002b-back.md",
          "size": 412,
          "mtime": 1719346204.0
        }
      ]
    }
  ],
  "left_behind": [],
  "warnings": [],
  "errors": []
}
```

### Top level

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | int | This document's version 1, per [Schema versioning](#schema-versioning). |
| `run_id` | string | `changeset.make_run_id()` -- an ISO-timestamp-plus-token id. Names the journal `-w` would write and the changeset records this same run produces; a wrapper that wants to correlate its own log against a run does it by this string. |
| `photokin_version` | string | The installed `photokin` package version, or `"unknown"` outside a build that carries one (`importlib.metadata` lookup). |
| `folder` | string | Absolute, normalized path of the folder every entry's `path` sits directly inside. |
| `prefix_template` | string | The `--rename PREFIX` template verbatim, before rendering. |
| `digits` | int | The zero-padded number width in effect (`--digits`, default 3). |
| `order` | `"name"` \| `"natural"` \| `"manifest"` | Which ordering rule actually decided position -- `"manifest"` only when every item supplied an explicit `order` (section 1 above); otherwise `"name"` or whatever `--order` asked for. |
| `managed_by` | any JSON value \| `null` | Copied verbatim from the manifest input (section 1) -- the value that manifest carried, object or not, unchanged -- or `null` for a folder input or a manifest that carried none. |
| `entries` | array | One record per image (section below). |
| `left_behind` | array | Same-stem files the planner declined to carry along -- `{"path": str, "reason": str}`. `reason` is currently always `"extension outside companion set"`. |
| `warnings` | array of string | Non-fatal notices: normalizations applied, a date disagreement inside a group, a prefix ending in a digit, a companion stem matching two groups, a left-behind file. Human-readable sentences, not a coded shape -- match by substring if you need to react to one, and expect the wording to be able to change between releases the way any other log line can. |
| `errors` | array of string | Same shape as `warnings`, but their presence means the plan must not be applied (see [Errors](#errors-non-empty-means-do-not-apply) below). |

### `entries[]`

One record per image the planner considered, in the order the plan was
built (position order within its group, groups in the order their earliest
member sorts). A group the planner could not render at all (a missing date
with no `--undated`, an empty rendered prefix) still contributes one entry
per member, with every rendering field `null` and `changed: false` -- see
the last three rows below.

| Field | Type | Meaning |
|---|---|---|
| `path` | string | The image's current absolute path, as it exists on disk right now. |
| `photo_id` | string | `changeset.make_photo_id(path)` -- the pre-rename identity, a hash of the path in this same `path` field. The same photo's `photo_id` in the changeset's ordinary metadata records (taken before the rename) matches this value, so a consumer already tracking files by `photo_id` recognizes the file here too. It does **not** survive the rename by itself: `make_photo_id` hashes the path, the rename changes the path, so an ordinary metadata run *after* the rename reports a different `photo_id` for the same photo. Use `target_photo_id` (below) to bridge that gap. |
| `target_photo_id` | string \| `null` | `changeset.make_photo_id(target path)` -- the *post*-rename identity: the same hash `photo_id` would report for this photo the next time anything reads it, once `target` has actually been applied. Equal to `photo_id` for an unchanged entry (`changed: false`, `target` already matches the current name), since the two paths are then the same string. `null` under the same condition as `target` -- a group that could not be rendered has no post-rename path to hash. A wrapper that keys its own records on `photo_id` reads this field off the plan to move that key forward: replace its stored `photo_id` with this entry's `target_photo_id` once the rename is applied, rather than losing the photo or re-matching it by name. |
| `size` | int \| `null` | Bytes, read from disk when the plan was built. `null` only if the stat itself failed (the file vanished between listing and stat'ing); not a normal case. |
| `mtime` | float \| `null` | Epoch seconds, same caveat as `size`. Both feed the executor's preflight (`docs/rename-mode.md` 5.1): a file whose size or mtime has moved since planning fails preflight and nothing is renamed. |
| `target` | string \| `null` | The rendered filename (with extension), e.g. `"520601-bag-woodbury-002b-back.tif"`. `null` only for a member of a group that could not be rendered -- see `errors`. |
| `target_stem` | string \| `null` | `target` without its extension. `null` under the same condition as `target`. This is what a companion's own target is built from (`target_stem + companion_ext`). |
| `group` | string | The group's display name -- its representative member's `base_id` as `parse_media_filename` read it, before rendering. Not unique across the whole plan by itself; `(folder, group)` is. |
| `prefix` | string \| `null` | The group's fully rendered prefix (template resolved, leading and trailing `-` trimmed), e.g. `"520601-bag-woodbury"`. Every member of a group shares this. `null` under the same condition as `target`. |
| `number` | int \| `null` | The 1-based position within `prefix`'s numbering bucket (section 4.5) -- **not** zero-padded; `digits` plus this value is what `target` was rendered from. `null` under the same condition as `target`. |
| `variant` | string \| `null` | The single-letter variant (`"b"`, ...), or `null` when the name carries none. |
| `part` | `"front"` \| `"back"` \| `"negative"` \| `"page"` \| `null` | Which part of the object this file is, after any `is_back`/`version` override is applied. `null` means "no part suffix" (an ordinary front), not "unknown". |
| `page` | int \| `null` | The page number when `part == "page"`; `null` otherwise. |
| `crop` | bool | Whether the name carries `-crop`. |
| `changed` | bool | Whether `target` differs from the file's current basename. `false` for an already-clean file *and* for an unplannable entry (`target: null`) -- check `target is not None` first if you need to tell those apart. |
| `notes` | array of string | Human-readable normalization notes for this one file, e.g. `"variant form normalized"`, `"part separator normalized"`, `"partial date"`. Same free-text caveat as the top-level `warnings`. |
| `companions` | array | This entry's non-image files sharing its stem that are in the default or `--companions`-extended set: `{"path": str, "target": str, "size": int \| null, "mtime": float \| null}` (target is `target_stem + companion_ext`, filename only). `size`/`mtime` are the same preflight fields the entry's own top-level `size`/`mtime` carry, read from disk when the plan was built -- the executor's preflight (`docs/rename-mode.md` 5.1) reads them off each companion exactly as it reads the image's, so a companion edited between planning and applying fails preflight too, not only a changed image. Empty when the file has none. |

### `errors`: non-empty means do not apply

A plan with anything in `errors` must not be handed to `--rename -w` or
`--rename-finish` -- the CLI itself refuses (exit 2) before ever calling the
executor when `plan["errors"]` is non-empty, and a wrapper building its own
plan should apply the same rule rather than relying on the executor to catch
it. `entries` and `left_behind` are still populated for every group that
*did* plan successfully, so a caller can still show a useful preview beside
the error list.

## 3. Changeset record: `kind: "rename"`

With `--changeset true` (which `-w` implies), rename mode appends one record
per **changed** entry to the same NDJSON changeset stream every other
photokin write already uses -- so a rename shows up in the same audit trail
as a metadata write, distinguished by `"kind": "rename"`. Only entries with
`changed: true` and a non-null `target` get a record; an already-clean file
gets none.

```json
{
  "schema_version": 2,
  "kind": "rename",
  "run_id": "2026-08-29T20:14:03Z_a1b2c3d4",
  "created_at": "2026-08-29T20:14:03Z",
  "photo_id": "3f2a...",
  "from": "file105b-back.tif",
  "to": "520601-bag-woodbury-002b-back.tif",
  "companions": [
    {"from": "file105b-back.md", "to": "520601-bag-woodbury-002b-back.md"}
  ]
}
```

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | int | `changeset.SCHEMA_VERSION`, the same number every other record in this stream carries -- see [Schema versioning](#schema-versioning). |
| `kind` | `"rename"` | Distinguishes this record from an ordinary metadata-write changeset record, which carries no `kind` key at all. |
| `run_id` | string | The plan's own `run_id`, matching `entries[].photo_id`'s cross-reference and the journal `-w` wrote alongside it. |
| `created_at` | string | UTC, `%Y-%m-%dT%H:%M:%SZ`, when this changeset was written -- one timestamp shared by every rename record from the same run, not per file. |
| `photo_id` | string | The same value as the plan entry's `photo_id`. |
| `from` | string | The image's basename before the rename (filename only, no directory). |
| `to` | string | The image's basename after the rename -- the plan entry's `target`. |
| `companions` | array | `{"from": str, "to": str}` per companion, basenames only. This is the plan entry's *whole* `companions` list carried over verbatim -- it is not re-filtered to companions whose own name is actually changing, so a companion whose extension-derived target happens to equal its current name can still appear here with `from == to`. |

### When these records are written

Under `-w`, **after** the rename has actually been applied. A run whose
preflight refused (exit 2), whose failure was rolled back (`rolled_back`), or
whose rollback could not finish (`needs_attention`) writes no rename record at
all: none of those folders match the plan, and this file exists to be trusted
later. The journal, and the run report's `stranded` list, are the record for
those runs.

With `--changeset true` alone -- no `-w`, so nothing is applied -- the records
are written for the plan as proposed, which is what that flag has always
meant. `--dry-run` writes no changeset either way.

The journal `rename_apply.py` writes (`docs/rename-mode.md` section 5.2) is
the *operational* record an interrupted or resumed run is replayed from;
this changeset record is the *audit* one, meant to be read back later
alongside every other proposed or applied write to the folder. A wrapper
does not need to read the journal to answer "what got renamed and when" --
this record already answers that, once `-w` (or `--changeset true` alone,
which records without applying) has run.

## 4. Statuses you will see on a run report

`--rename -w`, `--rename-finish`, `--rename-undo` and `--rename-resume` all
report one of these strings (not part of the plan JSON -- these come back on
the executor's own run report, logged to stderr and readable off
`rename_apply.ApplyReport.status` by a Python caller):

| Status | Exit code | Meaning |
|---|---|---|
| `applied` | 0 | The run finished: every planned rename is in place. |
| `undone` | 0 | An undo (or a resume of one) finished: every file is back where it started. |
| `would_apply` | 0 | `--dry-run` beside `-w`: preflight passed, nothing written. |
| `nothing_to_do` | 0 | The run had nothing to change -- an already-clean plan, a `--rename-finish` whose companions were already in place, or an undo with nothing left to reverse. This is the plan's own answer to "already clean"; see [the note at the top of this file](#rename-contract-the-wire-contract). |
| `rolled_back` | 1 | A failure was caught mid-run and every completed step was put back; the folder is exactly as it started. |
| `needs_attention` | 1 | A rollback itself could not finish. The report's `stranded` list names the exact files left mid-move -- resolve those by hand, or by contacting whoever owns the folder, before touching it again. `stranded` can also name a `.md` transcript whose `source_file` line an otherwise-complete rollback could not rewrite back to the old name; the image and every other file are already restored, only that one line is stale. |

