# Unified Input Pipeline

Implementation plan for collapsing photokin's three input modes into one.

**Target:** 0.1.1 to 0.2.0 · **Branch:** master · **Scope:** 3 phases + 1 parallel track · **Breaking changes:** 2

The original plan framed folder mode as an ergonomics problem: read-only, dead-ends
at stdout. That is true but incomplete. The audit below found the folder path is also
silently lossy, which reorders the work.

---

## 1. What the audit found

Three defects, each verified by running the real code rather than reading it.

### CRITICAL - Folder mode silently drops whole groups

Any group whose primary front is absent is skipped with no warning. That is three
categories: every multipage or album set, every negative-only set, and every
back-only set - a folder holding the reverse of a print whose front was never
scanned, or was filed elsewhere. The completion line then reports only what was
processed, so the run looks clean and the count looks plausible.

The third is the easiest to miss and not the least costly: a back is usually the
side carrying the handwriting, the dates and the names, which is the text content
the tool exists to read.

`core.py:913-914` · count at `core.py:929` · album pages documented at `README.md:50`

**Status after Phase A:** the silence is gone, the loss is not. Every skipped group
now logs its reason and the completion line carries a skipped count, so the run no
longer looks clean. The groups still go unanalyzed until Phase B routes folder input
through the manifest pipeline.

**Status after Phase B2: fixed.** Folder input is translated into manifest items and
run through `process_manifest_stream`, so nothing is skipped and the reason-selection
block, its `continue` and the skipped counter are gone. Same fixture, same grouping,
new outcome — three model calls instead of one, and a record for all four files:

```
before (7bcaf2f):  analyze_photo(box3_025.jpg)
                   results: ['box3_025.jpg']

after:             analyze_photo(album-page1.jpg)
                   analyze_photo(box3_025.jpg)
                   analyze_photo(neg-negative.jpg)
                   results: ['album-page1.jpg', 'album-page2.jpg',
                             'box3_025.jpg', 'neg-negative.jpg']
                   Batch completed for 3 group(s); 4 file(s) recorded,
                   0 group(s) failed, 0 file(s) recorded without being
                   sent to the model.                                 [INFO]
                   (the last clause read "0 file(s) displaced or dropped
                    from their group's payload." until C2's fourth pass)

after, --process-all-variants (dead in folder mode until now):
                   analyze_group_parts(("Page 1", [album-page1.jpg]),
                                       ("Page 2", [album-page2.jpg]))
                   analyze_group_parts(("Front",  [box3_025.jpg]))
                   analyze_group_parts(("Negative", [neg-negative.jpg]))
```

The third category is not in that fixture and was verified separately, because it is
a whole folder rather than one group inside a mixed one:

```
a folder holding only box3_030-back.jpg

before (7bcaf2f):  Skipping group 'box3_030': no primary front image;
                   1 file(s) not analyzed: box3_030-back.jpg
                   results: []                                       exit 0

after:             analyze_photo(box3_030-back.jpg)
                   results: ['box3_030-back.jpg']
```

The file is uploaded once, filling the payload's front slot because a one-file group
has nothing else to put there, and the record still files it as what it is:
`all_variant_files` comes back `{"front": [], "back": [box3_030-back.jpg], ...}`. It is
specifically not sent as `photo(P, P)` - that collapse to `photo(P, None)` for a group
with no front side is the 909-case invariant-(a) family recorded under B1 above, and it
is what makes analyzing these folders safe rather than a way to pay twice for one image
and assert it is a side it is not. Manifest mode at 7bcaf2f already analyzed this
folder, so like the variant-back change recorded under B2 below, this is B1 semantics
reaching folder mode rather than a new rule.

### HIGH - No per-group error isolation in folder mode

The loop has no try/except, so one bad photo aborts the batch. Because NDJSON output
is manifest-only, every completed result is lost with it. That is the exact failure
`--output-file` exists to prevent, unavailable in the mode most likely to hit it.

`core.py:910-928` · compare the manifest handler at `core.py:1495-1501`

### HIGH - The documented `is_back` manifest flag is ignored

Grouping is derived entirely from the filename; only `path`, `preferred`, `metadata`
and `metadata_path` are read from an item. The README promises the flag exists so
files that break the naming convention can still be grouped. That promise is
unimplemented, and the README's own sample manifest relies on it.

`core.py:1008` · promise at `README.md:65` · sample at `README.md:67-76`

Verification, the README's exact sample manifest:

```
groups formed: 2   (README intends 1: one object, front + back)
  'box3_017':      [box3_017.jpg      is_back=False]
  'box3_017_back': [box3_017_back.jpg is_back=False]
```

The sample fails twice over: the `is_back` flag is discarded, and `box3_017_back.jpg`
uses an underscore, which the parser does not read as a back - only the hyphenated
`-back` form is recognized. The back scan is analyzed as a standalone front photo.

**Status after Phase B1:** fixed for manifest mode. `is_back`, `is_crop`, `version`
and `group` (alias `base_id`) are honored, and an explicit `is_back` also strips a
trailing separator-preceded `back` token from the derived group key, which is what
the sample needs. The same sample now forms one group with a front and a back, and
makes one model call (`core.py:1170-1272`, `README.md:266-296`).

### HIGH - A crop can silently displace the real scan in manifest mode

Found during Phase A doc reconciliation, not in the original audit. `parse_media_filename`
sets `is_crop`, but the manifest bucket loop never consults it: a crop and its parent
both resolve to the same `part_key`, and `variant_parts[ver].setdefault(part_key, path)`
means the first one listed wins. Whichever the manifest happens to name first is the file
sent to the model; the other is dropped with no warning.

`core.py:1198-1207` · crop documented as "a supporting view of its parent" at `README.md:251`

Verification, same two files in both orders:

```
manifest lists real scan first : {None: {'none': 'box3_025.jpg'}}
manifest lists crop first      : {None: {'none': 'box3_025-crop.jpg'}}
```

This is order-dependent silent data loss on the integration surface, and it affects backs
identically (`box3_025-back.jpg` and `box3_025-back-crop.jpg` both map to `back`). Folder
mode is unaffected in outcome only because `group_folder_images` (retired in B2) keeps
crops in their own slots and never analyzes them at all.

**Status after Phase B1:** fixed. Slot occupancy is now decided by rank rather than by
arrival (`_slot_rank_key`, `core.py:1213-1229`), and crop-ness is the leading component,
so a crop always loses its parent's slot in either order. The same rewrite covers
`part_kind == "negative"`, which used to degrade into an untagged front and could be
promoted to the group's primary; it now has its own slot and its own `Negative` part in
the model payload. Crops are recorded under `all_variant_files.crops` and warned about
rather than analyzed, matching folder mode; a crop with no parent is still analyzed,
because manifest mode owes every listed file a record.

That last exception is decided per slot, not per group. A group can hold a cropped
front and an uncropped back, and asking "does this group contain anything uncropped"
answers yes, drops the only front-side file it has, and sends the back to the model
twice - once labelled `front`. The test is therefore whether anything uncropped claims
the same `(version, part)` address. Both crop warnings are also emitted against the set
of files actually bound for the model rather than against `primary_front`, since the
group-aware path sends more than the primary and a `preferred` crop can be the primary
yet still miss the payload.

The same reasoning generalizes past crops, and not applying it there was the second
round of defects: see the two payload invariants under Phase B1 below.

### Quieter defects found alongside

| Location | Defect |
|---|---|
| `cli.py:384-387` | `--output-file` is silently ignored in folder and single-photo mode. No file, no error, exit 0. |
| `apply.py:166-170` | ExifTool presence is checked only after the entire batch is analyzed and paid for. |
| `cli.py:357` | A `.json` output path is never pre-flighted, so an unwritable destination fails at the end. The `.ndjson` branch is covered incidentally by its truncate-on-open. |
| `core.py:924-927` | Folder mode writes each sidecar twice, once inside `analyze_photo` and again with variants attached. |
| `core.py:915` | `--process-all-variants` is dead in folder mode; the group path is never reached. Fixed in B2 — folder runs take the same branch manifest runs do. |
| `config.py:14-19` | `EXIFTOOL_WRITE_ENABLED=""` silently resolves to false, as does any unrecognized value. |
| `cli.py:310` | A `.json` output file yields a generic `changeset.ndjson` that collides across runs. |
| `cli.py:675-677` | The aggregate `.json` write unlinked the destination before `os.replace`, which already overwrites atomically on Windows and POSIX both. The unlink bought nothing and opened a window in which the caller's previous results file was gone; if the replace then failed, the `finally` cleared the temp file and the run ended with neither. Found while fixing the same sequence in B2's `_write_generated_manifest`, which had copied the pattern from here - both are fixed, so the docstring's "matching how the aggregate `.json` output is written" is true again. Not in the original audit; pre-dates the whole plan. |
| `README.md:89` | Tells the reader to apply `batch_changeset.ndjson`; the file actually produced is `results_changeset.ndjson`. Corrected in the README alongside Phase A (`README.md:287-290`, `README.md:337-338`), which now also states the derivation, and in `photokin/exiftool/README.md:102`. |

---

## 2. Decisions

| Ref | Decision | Reasoning |
|---|---|---|
| Q1 | Changeset path is `dirname(--output-file or input)` | Not a new decision. That rule already exists at `cli.py:299-313`; generalize it rather than invent a convention. Unify the no-output-file basename, which is currently a bare `changeset.ndjson`. |
| Q2 | No `--recursive`; keep it separate | Recursion changes grouping semantics across directories, since the same basename can appear in several. It also interacts with write safety. Its own PR. |
| Q3 | Ship `--generate-manifest` with the refactor | Golden-file tests against its output are the cheapest way to prove the grouping refactor preserved behavior. It makes an otherwise invisible change verifiable. |
| Q4 | Defer ad-hoc multi-positional input | `nargs="*"` alongside value-taking flags is where argparse ambiguity bites, and the real use case (`photokin ./scans/*.jpg`) hits Windows argv limits anyway. |
| Q5 | Keep `--changeset true\|false`; do not make it a bare switch | A bare switch means `nargs="?"`, which becomes ambiguous once a positional path exists: `photokin --changeset ./scans/` would swallow the path. `cli.py:218` also records that the string form exists because Lua passes literal true/false. `-w` already solves the ergonomics. |
| Msg | Configure a stderr logging handler in the CLI entry point | `AGENTS.md:25` mandates `logger` over `print`, but no handler is installed, which is why `hydrate.py`'s "skipping hydration" warning is invisible today. Settling this first means the new message surface works and follows the convention. |

---

## 3. Sequence

Three phases plus one independent track. Each phase is shippable on its own; the
ordering puts the data-loss fixes ahead of the refactor churn that would otherwise
obscure them.

### Phase A - Stop the silent failures

No structural change and no new flags. This is the pain that actually bites today,
and none of it depends on the rest of the plan.

- Install a stderr logging handler in `cli.main()` and move user-facing messages onto
  the logger. Fixes the invisible hydration warning as a side effect.
- Folder mode: log every skipped group with its reason, and include the skipped count
  in the completion line.
- Folder mode: per-group try/except mirroring the manifest handler.
- Make `--output-file` outside manifest mode an explicit error rather than a silent
  no-op. Real support lands in Phase C.
- Pre-flight before the first model call: ExifTool resolvable when writes are
  requested, and output plus changeset paths writable. Closes the `.json` gap.
- Fix the double sidecar write.

**Landed early - the `analyze_folder` return shape.** Per-group error isolation
needed somewhere to put the errors, so the function now returns
`{"results": ..., "errors": ...}` rather than `{"results": ...}`. That is booked
below as Phase B's Breaking change #2; the additive half of it shipped here rather
than being held back, because it costs existing embedders nothing - anyone reading
`["results"]` sees exactly what they saw before. What did change underneath them is
failure behavior: a group that raises is recorded under `errors` and the batch
carries on, where it used to propagate immediately, and only a run in which nothing
succeeded still raises. An embedder that read "no exception" as "no failures" now
has to check `errors`. In-repo the return value has one consumer, `cli.py:557`,
which prints it, so folder mode's stdout JSON gains an `errors` key too.

**Exit:** a folder run over a fixture containing album pages and a negative accounts
for every group; a mid-batch failure leaves prior results intact; a missing ExifTool
stops the run before any provider call.

**Risk:** low. The one contract change is additive and reaches a single in-repo
caller; the behavioral half is documented above and under Breaking change #2.

**Status: shipped.** All six objectives verified by execution, 162 tests passing
(31 new). Two things went further than the bullets above:

- The logger conversion was completed rather than left half-done: all 41 remaining
  `print(..., file=sys.stderr)` diagnostics across `core.py`, `utils.py`, `merge.py`,
  `api_gemini.py` and `cli.py` are now logger calls. This was not tidying. The
  stdout-purity contract had been resting on ~40 hand-written `file=` kwargs that no
  test exercised, so deleting any one of them would have corrupted folder-mode stdout
  with the suite fully green. An AST audit now holds the invariant: the only
  `print()` to stdout reachable from `photokin.cli` is the three result-JSON writes
  and the interactive prompts.
- A pre-existing stdout bug was fixed incidentally. At HEAD, `core.py` wrote 25
  diagnostics and every provider adapter wrote `[Run] Starting analysis...` to
  **stdout**, so the README's own `photokin --folder ./scans/ > results.json` never
  produced valid JSON. It does now.

**Found during Phase A, owed to Phase C - the run-fatal asymmetry.** Folder mode now
treats `missing_api_key` and `missing_dependency` as properties of the run and aborts
on the first group (`_RUN_FATAL_ERROR_TYPES`). Manifest mode does not: a manifest with
no API key rebuilds the broken client once per item, writes one `missing_api_key`
record per item, prints nothing to stderr, and **exits 0**. That is the same
"total failure reads as success" shape flagged as CRITICAL for folder mode above.
It predates Phase A and was left alone rather than changed mid-phase, but manifest
mode is the plugin contract, so Phase C should decide deliberately whether the
asymmetry is intended. `photokin/README.md` now states the difference plainly rather
than claiming both modes fail fast.

### Phase B - One grouper, one pipeline

The structural work, with no user-facing flag changes. This is where the multipage
loss is actually repaired rather than merely reported.

- Reconcile the two grouping implementations behind a single function that takes a
  file list rather than a folder path, and handles front, back, page, negative and
  crop. `group_folder_images` becomes a thin listdir wrapper or retires.
- Route folder and single-photo input through `process_manifest_stream` by
  synthesizing an in-memory manifest. The signature already accepts a dict, so this
  is small.
- Honor explicit item overrides for `is_back`, `version` and group key, making the
  README's documented flag real.
- Add `--generate-manifest PATH`: write the synthesized manifest and exit without
  calling the model.
- Golden-file tests comparing a fixture folder's generated manifest against
  checked-in expected JSON, covering variants, backs, pages, crops and negatives.

**Exit:** folder input produces the grouping the manifest pipeline would; album pages
are analyzed; `--process-all-variants` works for folders; error isolation and
hydration come along for free.

*Met, except the last clause, which was wrong.* Error isolation is free - it is the
stream's own, extended with folder mode's abort and re-raise behind
`strict_run_failures`. Hydration is not: it would change every existing folder run's
prompt and cost, and it needs `{"metadata": {}}` seeded on every synthesized item
before it does anything at all. Both modes pass `metadata_hydrator=None`; see the
Phase C note under B2.

**Risk:** medium. The `analyze_folder` return shape changes again - the per-file
half Phase A did not take - and it is public API.

**B1 shipped - the manifest grouping fixes, ahead of the routing.** Split out and
landed first because both defects are data loss on the plugin's own contract and
neither needs the refactor: crop and negative displacement, and the inert `is_back`
flag. What changed:

- The bucket-loop entry is built by `_resolve_manifest_entry` and carries `is_crop`
  and `group_key`; `is_back`, `is_crop`, `version` and `group`/`base_id` are honored,
  explicit always beating the filename, every effective override logged.
- Slot occupancy is `min(claimants, key=_slot_rank_key)` rather than `setdefault`, and
  primary selection reads the slot winners rather than the arrival-ordered group. Every
  grouping output is now invariant under permutation of `items`; emission order and the
  `front`/`back`/`all`/`variants` lists stay input-ordered on purpose, since
  canonicalizing them would reorder NDJSON for every existing manifest.
- Negatives get their own slot and their own `Negative` part, so they are still analyzed
  (`README.md:254` promises that) but can no longer be handed to the model as the front.
- Additive record keys `all_variant_files.crops` and `.negatives`, present only for
  groups that hold such files.

Checked against a pre-change capture over 4296 ordinary manifests - no crops, no
negatives, no override keys - covering front/back, variant, multipage, back-only,
explicit-front and preferred cases in both `--process-all-variants` settings and both
update policies. 187 of them send the model a different file, and all 187 are the
permutation fix landing: in every one, the pre-change code returns a different answer
for some reordering of the same items, and the new answer is one the pre-change code
itself produces for some ordering. Confirmed by re-running all 3384 permutations of
those cases - the new code gives one answer per case, the old code does not. The
remaining record differences are the two additive keys.

The first pass at this claimed byte-identical and was wrong. It missed five defects,
all since fixed and covered by `photokin/tests/test_manifest_grouping.py`: `-page0`
addressed to the page 1 slot by an `or 1` default, a `preferred` back discarded when
resolving `primary_back`, crop-ness tested per group rather than per slot, and both
crop warnings testing `primary_front` instead of the file set actually sent.

**Second pass - the two payload invariants.** Adversarial review found the crop
reasoning above had been applied to crops and not to anything else, so the same shape
recurred wherever a role resolved to a file that could not fill it. Both rules are now
explicit guards rather than properties that happened to hold:

- *No path is sent under two labels.* Each part is uploaded, billed and described
  separately, so a file handed over as two of them is paid for twice and asserted to be
  a side it is not. It was reachable three ways: a group holding a negative and a back
  sent the back as the front as well and dropped the negative, because the rule keeping
  a negative off the primary removed negatives from the candidate list outright instead
  of narrowing the master pick; a group with no front side at all did the same with its
  back, at 525e9a6 too; and one path listed twice under contradicting flags won two
  addresses and travelled in both.
- *No listed file leaves the payload in silence.* An untagged file rides the front side
  of its variant - as `Page 1` in a multipage group, as the front otherwise - and that
  role holds one file. Where something more specific already held it, the later
  assignment simply overwrote the earlier one: `is_back: false` beside an untagged file
  dropped one of the two, and an untagged file beside an explicit `-page1` dropped
  itself. Both are now warnings naming the file and entries in an additive
  `all_variant_files.displaced`, since the `front` list is every front-side file in the
  group and on its own reads as though each of them was sent.

Four narrower fixes came with them. `preferred` moved into `_slot_rank_key` rather than
being appended to the candidates, so it wins any slot it is allowed to win and a
`preferred` crop is no longer named as the analyzed file of a payload it is not in -
which used to fail the whole group with a `KeyError` whenever the payload held more than
one file. `primary_version` now follows the front actually sent rather than the item that
won the master pick. (That fix has since been superseded for `PC*` specifically - see
"PC codes belong to the object" below - but it still governs caption variant labels.)
Slot claimants are addressed by resolved path, so a
manifest listing one path twice is not reported as colliding with itself, and a crop
listed twice does not twice stand in for the object. Crop slot labels are read after the
multipage relabel rather than before.

Re-checked by differential sweep over 4140 ordinary manifest shapes - no crops, no
negatives, no override keys, both `--process-all-variants` settings and both update
policies - against 525e9a6 and against the tree as it stood before this pass. Every
difference falls in two families and the record set is identical throughout: 909 cases
are the invariant-(a) collapse, `photo(P, P)` becoming `photo(P, None)` for a group with
no front side, which halves the payload and stops asserting the falsehood; 159 are the
untagged-file displacement, and all 159 restore the answer 525e9a6 gives. 76 of the
divergences from 525e9a6 that the first pass introduced are repaired by this one.

Two carve-outs on `preferred` follow from the above and are now documented at
`README.md:293`: it chooses among the files a group can send and cannot create a place
for one, so it does not promote a crop over the original it was cut from, nor an
untagged file into a front side already claimed.

**B2 shipped - the routing.** Folder and single-photo input are now translated into
in-memory manifest items and handed to `process_manifest_stream`, which is the whole
of the change; no grouping logic was written for B2, because B1 had already built the
target. What landed:

- `core.build_folder_manifest` and `core.build_single_photo_manifest` synthesize the
  items. A folder item carries `path` and nothing else - the filename is folder mode's
  only source of truth, so an explicit key would hand `_resolve_manifest_entry` back
  the answer it is about to derive and then freeze it. Single-photo mode is the
  deliberate exception: `--back` becomes `is_back` plus a shared `group` (without it
  `photo.jpg --back reverse.jpg` splits into two objects and two calls), and `--meta`
  rides inline on the front. Listing is `utils.list_folder_images`, sorted by
  `(basename.lower(), basename)` so the input-ordered outputs - the
  `all_variant_files` lists, the emission order - do not vary by filesystem.
- `core.build_manifest_buckets` is the extracted bucket loop, so the group count
  `--generate-manifest` reports is taken from the same code the run groups with.
- `process_manifest_stream` gained `strict_run_failures` (folder mode's Phase A
  failure contract, default off so the plug-in path is byte-identical), an `errors`
  accumulator in its return, a per-group failure ERROR line, and one completion line.
- `--generate-manifest PATH` writes the synthesized manifest, atomically and always
  pretty, and exits without building a provider client. `photo_context_text` is
  inlined resolved and sanitized so the file round-trips; nothing else about the run
  is emitted.
- `utils.group_folder_images` and `core._unanalyzed_group_files` are **deleted**. The
  first was the second implementation of the suffix grammar and the reason routing had
  to wait for B1; it carried three data-loss bugs B1 fixed on the manifest side
  (`-page0` filed as page 1, negatives binned at stem level so two variants overwrote
  each other, and slot collisions resolved by `os.listdir` order with no warning). Once
  nothing called it, leaving it in place was strictly worse than removing it. Neither
  name is on `public.py` or `__init__.py`, so neither gets a breaking-change entry;
  `list_folder_images` takes over the listing behavior unchanged, so the file set and
  every path spelling are identical.

Verified by differential run against 7bcaf2f with `process_all_variants` off - the
default, and the only setting that was reachable in folder mode. An earlier draft of
this section reported the model calls as identical across the whole fixture set; that
was wrong, and it was wrong because the fixture set did not cover the shape where they
differ. The corrected result is that the fixtures split in two.

**Model call unchanged** - identical in callee, front, back and `write_sidecar`:
front/back, front/back/variant, variant-with-its-own-back, front-only, crop,
mixed-extension, non-image. The single difference on these is `original_meta`, `None`
before and `{}` now, which `build_prompt_bundle` tests with `if forwarded_meta:` and
therefore cannot distinguish. (`results` gains one entry per file on all of them; that
is Breaking change #2 and is deliberate.)

**Model call changed** - four shapes, three of them the point of the phase. Album
pages, negative-only sets and back-only sets went from no call at all to one call
each: that is the CRITICAL finding being fixed, documented in section 1.

The fourth was not planned and is recorded here as an accepted consequence of parity.
Where a group's primary front has **no back of its own** but a variant scan does, the
variant's back is now sent as the group's back:

```
box3_025.jpg / box3_025b.jpg / box3_025b-back.jpg   (no crops, no pages, no negatives)

  7bcaf2f : photo(front=box3_025.jpg, back=None)               <- variant's back never sent
  B2      : photo(front=box3_025.jpg, back=box3_025b-back.jpg)
```

This is not something B2 invented. 7bcaf2f's *manifest* mode already made the second
call for those same three files, so this is B1 grouping arriving in folder mode, which
is what parity means. Kept deliberately: old folder mode was ignoring an available back
scan, and the variants are scans of one object, so that back is the object's back.

The cost is bounded and worth stating plainly rather than either hiding or inflating:
one extra image on one call, and only for groups shaped this way. A group whose primary
front has its own back is untouched - the primary's own back outranks a variant's for
the slot, which is why `variant-with-its-own-back` sits in the unchanged list above.

Note the fixture name is the trap the earlier draft fell into: `variant-with-its-own-back`
covers only the sub-shape where the *primary* also has a back, so a differential over it
says nothing about the case where the primary has none.

Folder input and the manifest `--generate-manifest` writes for the same folder produce
identical model calls, identical `results`, identical `errors` and an identical
diagnostic sequence, in both `process_all_variants` settings.

**Second pass - three defects the routing introduced or exposed.** Adversarial review
found them after the differential sweep above, which compared the model calls and the
record set but not the failure paths:

- *A sidecar that cannot be written no longer fails its group.* Phase A banked the
  record before touching the filesystem and caught `OSError`, on the grounds that the
  analysis is already paid for. Forwarding `write_sidecars` into the stream moved the
  write inside `analyze_photo` / `analyze_group_parts`, where it is unguarded, so a
  read-only `.json` left by a previous run - or a lock held by a sync client, a path
  over `MAX_PATH`, a read-only share - turned a completed analysis into a group
  failure typed `PermissionError`, with one error payload per file of the group, and
  when it hit every group re-raised through `strict_run_failures` and lost the whole
  batch behind exit 2. Both writes now go through `_write_sidecar_document`, which
  warns in Phase A's own wording and keeps the record. Manifest mode gains the same
  protection: it had the failing behavior before, but the reasoning was never
  folder-specific, and threading a mode flag down to the write site would encode the
  asymmetry in a third signature.
- *`results` and `errors` can no longer name the same file.* The per-file emit loop
  banks one record at a time, so a group raising part-way through it had its
  already-banked files re-emitted as errors - one path in both maps, and both an `ok`
  and an `error` line on the stream for it. The handler now skips what the group
  already banked, which restores the disjointness Breaking change #2 claims. Manifest
  mode only: folder items carry no inline `metadata`, which is what makes the
  per-file loop reachable as a failure point.
- *`--generate-manifest` refuses input the run itself would refuse.* The folder form
  already failed loudly on a missing directory, because building the manifest has to
  list it. The single-photo form built from the argument alone, so a typo wrote a
  manifest for a nonexistent image and exited 0 where the same input without the flag
  exits 2, and the file it produced only failed later, fed back through `--manifest`.
  `ensure_paths_exist` now covers the image and `--back`.

**Owed to Phase C - hydration for folder and single-photo input.** B2 passes
`metadata_hydrator=None` for both, deliberately: hydration would change every existing
folder user's prompt, cost and output in the phase whose job is routing, and it is not
free to enable. `hydrate_user_comments` skips any item whose `metadata` is not a dict,
and B2's folder items carry no `metadata` key at all, so turning it on requires seeding
`{"metadata": {}}` on every synthesized item - which also makes `load_item_metadata`
return `{}` instead of `None` for every file and changes the `merge_original_sources`
inputs for every record. When Phase C lifts the manifest-only gate on the
`--exiftool-*` flags, that seeding has to come with it.

**B2 open risks.**

- The completion line and the per-group failure ERROR line are new on *manifest* mode's
  stderr. Both are additive diagnostics, and manifest mode already writes an INFO line
  per photo there, so a plug-in that treated any stderr output as failure would be
  failing today - which is why they were not gated on `strict_run_failures`. If that
  assumption turns out to be wrong, gating them is a one-line change, at the cost of
  leaving manifest mode's "a lossy run reads as clean" hole open until Phase C.
- The `errors` key on `process_manifest_stream`'s return changes the aggregate `.json`
  that `--output-file results.json` writes and the manifest stdout fallback. Additive,
  but it is the plug-in's own artifact and this repo holds no fixture of its reader.
  Confirm before release.
- Folder mode's `--output-sidecars` content narrows: it becomes the raw
  `{"result": {front: record}}` that `analyze_photo` writes, losing the
  `all_variant_files` enrichment Phase A's single write added. Accepted for parity -
  a folder-only sidecar shape would break it outright, the enriched content is present
  in the returned records, and the alternative (per-file enriched sidecars from the
  stream) would change manifest mode's sidecars too. Filenames are unchanged, since the
  front chosen is the same file.
- `strict_run_failures` encodes the folder/manifest failure asymmetry in a signature
  rather than resolving it. It is the right seam for B2; Phase C should flip the
  default or delete the parameter.
- The `--folder` result-key set has now changed shape in two consecutive phases, for a
  public re-exported function whose only in-repo consumer is `cli.py`. Neither change
  can be validated against a real embedder here - the same blind spot the plan already
  flags for the plug-in's manifest writer.
- Deleting `utils.group_folder_images` is safe against everything greppable in this
  repo, but `utils` is an ordinary importable module and an out-of-repo script could
  import it directly. It is not on the `public.py` / `__init__.py` surface, so it gets
  no breaking-change entry; that judgment could be wrong if such a consumer exists.
- `--generate-manifest` inlines `photo_context_text` and never `photo_context_path`, so
  a large context file is embedded in full (up to `MAX_PHOTO_CONTEXT_BYTES`) in every
  generated manifest. Judged the right trade for exact round-tripping.

### Phase C - The CLI surface

Everything user-visible, released together as 0.2.0 so there is one breaking version
rather than three.

- Positional input with type detection; `--folder` and `--manifest` become deprecated
  aliases with a one-time note. The argparse mutually exclusive group has to go to
  get custom messaging.
- Lift the manifest-only gate so `--output-file`, `--changeset` and the
  `--exiftool-*` flags work for every input type.
- `-w` / `--write` as a pre-parse expansion of `--changeset true --exiftool-write
  true`, defined in exactly one place.
- Flip the `--exiftool-write` default to false, with a loud transition warning in
  manifest mode when `--changeset true` is set and the flag is unset.
- The full error-message set, centralized in one module so wording is testable.
- A one-line plan summary before the first model call: input, output, changeset,
  write set, provider. `--dry-run` prints it and stops.
- Replace two grouping knobs with one axis - see below.
- CHANGELOG, version bump, tag.

**Exit:** all three inputs read identically; nothing is written without an explicit
opt-in; every error case in the message set has a test asserting exit code and first
line.

#### One grouping axis: `--group-by {object, pair, none}`

Maintainer decision, taken during B2. Grouping today is not a mode selector but two
orthogonal knobs - `--process-all-variants` (how many images go in the call) and
`--update-policy` (which files get written) - whose four combinations include
incoherent ones, such as analyzing every variant and then writing only to the primary.
Granularity is really one axis:

| Value | Group key | On `box3_025{,-back,b,b-back,c}.jpg` |
|---|---|---|
| `object` (default) | `base_id` | 1 call; every scan of the print shares one analysis |
| `pair` | `(base_id, variant)` | 3 calls; each rescan judged on its own merits |
| `none` | the file | 5 calls; every file alone, backs separated from fronts |

`object` is the default because scans of one print are one print: a shared date and
location is the wanted answer, not three opinions to reconcile. `pair` is cheap to
keep once granularity is a single axis. `none` is an escape hatch for when filenames
lie and the grammar mis-groups - it is deliberately the costliest and the lowest
quality, because a back analyzed alone is handwriting with no photo attached, and the
caption, date and location inference all lean on seeing the front. Document it as an
escape hatch, not as a normal mode.

Consequence to state plainly in the CLI help: under `none`, a multipage document is
split into unrelated pages. Page 2 without page 1 is meaningless, and B2 only just
made those sets analyzable. That is the accepted cost of "split every file"; it is
not a carve-out.

**The primary concept retires with it.** `--process-all-variants`, `--update-policy`
(both values), the `preferred` manifest key and `utils.pick_master_index` all exist to
serve "analyze one photo per group and fan the result out". Three points against
keeping it: the saving is images-per-call, not calls, so it only bites on
multi-variant groups, which are uncommon; it is the origin of one of the two
order-dependent choices behind the B1 crop bug (`pick_master_index`, the other being
slot `setdefault`); and it multiplies every code path by a mode almost nobody selects.

`preferred` is the sharp edge. The external Lightroom plugin already sends it - B1's
differential sweep found 144 cases where it changes emitted output - so retiring it is
a plugin-contract change and needs the same deprecation cycle as `--folder` and
`--manifest`, not a silent removal.

**Owed before this can ship: there is no `negative` keyword.** Backs get one from
`utils.ensure_keyword_back`; negatives get nothing, and the only `negative` reference
in the pipeline is payload assembly. The rule this axis promises - every file in a
group shares one analysis except for its own part marker - is therefore half
implemented. Add the negative marker alongside the grouping work.

Crops need a stated rule per value as well. They are recorded and never analyzed
today; under `none` a crop would become its own object, which contradicts the README's
"a supporting view of its parent, never as its own object".

**C1 shipped - the axis, the primary's retirement and the negative marker.** The three
were landed together because they are one change: the axis has no meaning while a flag
still decides how many images go in the call, and "every file of a group shares one
analysis except its own part marker" is not true until negatives have a marker.

- `utils.GROUP_BY_OBJECT/PAIR/NONE/GROUP_BY_VALUES` sit above the `Config` dataclass, and
  `Config.process_all_variants` is replaced by `Config.group_by`. One function derives the
  key: `build_manifest_buckets(items, *, group_by=...)`, keyword-only and defaulted, so no
  existing caller changed. `object` returns byte-identically what it returned before, at
  every input; `pair` appends `|<letter>` to the key only when the entry has one, so a
  group with no variant letters keeps the same changeset `group_id` under both; `none`
  keys on the file's normalized path. An unrecognized value raises `ValueError` before the
  loop, for the library caller argparse cannot guard.

  `pair`'s join is escaped rather than assumed safe, which the first pass got wrong. The
  separator was chosen because `|` is illegal in a Windows filename, and that holds for a
  key the grammar derives *there* and for nothing else: `_manifest_group_override` takes
  any non-empty string, and the README documents `group` for exactly the names the grammar
  cannot parse. So `group: "album|b"` and a filename-derived `("album", "b")` spelt one
  key, and two unrelated objects went to the model in one call and were written one
  caption, date and location.

  The second pass got the escape itself wrong. `_pair_bucket_key` doubled the separator in
  both halves and claimed injectivity on the grounds that exactly one run of separators is
  odd-length. That argument fails at the boundary: a half's escaped trailing run merges
  with the joining separator into a single run that can be split more than one way, so
  `("a|", "a")` and `("a", "|a")` both spelt `a|||a`. Brute force over `{'', 'a', '|',
  '\', 'a|', '|a', '\|', '||'}` found 12 colliding pairs, and the collision is reachable
  end to end - a manifest naming `{group: "a|", version: "a"}` and `{group: "a", version:
  "|a"}` produced one bucket, one `analyze_group_parts([("Front", [both files])])` call,
  and one caption, date, location and changeset `group_id` written to two unrelated
  objects. Narrow reach, since it needs a `group` ending in `|` or a `version` starting
  with one, but both are free-form strings and the claim was simply false.

  The scheme is now a real escape rather than a doubling: `_escape_pair_half` rewrites `\`
  as `\\` and then `|` as `\|`, and the two escaped halves are joined on a bare `|`.
  Neither half can then contain a bare separator, so reading left to right - an escape
  consumes the character after it, and the first separator it does not consume is the join
  - recovers both halves unambiguously, and a key with no bare separator is the `None`
  version. That is injective, and the brute force above is now a test rather than a
  paragraph (`TestPairKeyCannotCollide`). A key holding neither the separator nor the
  escape character is untouched, which is every key the grammar derives on Windows, so the
  ordinary `group_id` is unchanged. The cost is that a POSIX key containing a literal `\`
  now has it doubled under `pair`; that is unavoidable once an escape character exists,
  and it does not reach `object` or `none`.
- **The primary retires.** `utils.pick_master_index` is deleted, and with it the negative
  filter that existed only to stop it preferring an unversioned negative over a versioned
  front - `_slot_rank_key` already ranks a negative last. `primary_item = candidates[0]`
  replaces both; `candidates` was already rank-ordered, so the head *is* "best non-crop,
  preferred-if-any, front-side, unversioned". That is the second of the two
  order-dependent choices behind the B1 crop bug leaving the tree. One divergence is
  accepted rather than compensated: a group holding a versioned explicit `-front` beside
  an unversioned untagged file now names the `-front` as primary where the old scan named
  the untagged file. Both are sent under `object`, so only `main_key` and the caption's
  variant label move.
- **The callee follows the payload, not the axis.** One predicate replaces three reads of
  `cfg.process_all_variants`: `multipage_present or all_negatives or len(all_fronts) > 1
  or len(all_backs) > 1`. False takes `analyze_photo(front, back)` unchanged; true takes
  the group form. This is what keeps ordinary output stable - the two analyzers differ in
  prompt (the group one opens "You are seeing multiple scans or variants"), dump tag,
  transport shape and failure mode, so routing every run through the group form would have
  changed the record of 100% of runs. The caption branch follows the same predicate, which
  is what keeps a plain front/back pair's `[Back] ` prefix.

  A consequence the first pass missed: the predicate is true for a group of *one* whenever
  that one file is a negative or a page, which is the commonest shape a negative has in an
  archive. Those calls carry one image under a note opening "You are seeing multiple scans
  or variants", a claim the payload contradicts and the model can count. The group reaches
  the analyzer for its part label rather than for its size, so the fix is the sentence, not
  the routing: it is now chosen on `len(flat_paths) > 1`, and a one-image call says so.
- **What that costs.** Calls are unchanged at every scale: `object` forms exactly the
  groups the old code formed and makes one call per group. The cost is images, and only
  for a group holding more than one front-side scan, more than one back, or a page. On the
  checked-in 6-file fixture the whole folder goes from 4 calls / 5 images to 4 calls / 8
  images. The four shapes that changed under `object` are: a second front-side file, a
  second back, any group holding a page (previously only page 1 was sent - the largest
  single quality change of the phase), and any group holding a negative (same one image,
  now labelled `Negative` rather than passed off as the front).
- **No shape reproduces the old default.** `pair` comes closest and still makes one call
  per rescan rather than one for the group. Anyone capping image cost with
  `--process-all-variants` off has no equivalent.
- **Compatibility, deliberately minimal.** `--process-all-variants` and `--update-policy`
  remain accepted, are `argparse.SUPPRESS`ed out of `--help`, do nothing, and warn once
  each naming `--group-by`. The reason is mechanical: the plug-in launches
  `python -m photokin.cli`, and deleting an argument makes argparse exit 2. No deprecation
  framework, no version gate. The Python API is not covered - `core.analyze_manifest`,
  `core.process_manifest_stream` and `public.analyze_manifest` lose `update_policy`
  outright, and `core.UPDATE_MASTER_EXACT`/`UPDATE_MERGE_PER_VARIANT` are deleted. An
  embedder passing the keyword gets a `TypeError`; embedders set `cfg.group_by` instead.
  `merge_original_sources` now runs unconditionally in the fan-out, which was the
  `merge_per_variant` default, so the default behavior is preserved.
- **`preferred` is not the no-op the plan expected.** It stops choosing the one analyzed
  file - there is no longer one - but it remains component 2 of `_slot_rank_key` and so
  still decides which of two files contesting one `(version, part)` slot is sent. It is
  left exactly in place: removing it would change every colliding-slot group the plug-in
  already sends and break the two carve-outs documented at `README.md`. Confirmed it
  cannot raise on any JSON value, since `_resolve_manifest_entry` reads it as
  `bool(raw.get("preferred"))`. No deprecation cycle is needed for a key that still has a
  job.
- **The negative marker.** `utils.ensure_keyword_back` is replaced by
  `utils.apply_part_keyword(record, marker, leaked)` over `PART_MARKER_KEYWORDS = {"back",
  "negative"}`, which asserts one marker and removes the others case-insensitively. It
  replaces both the old ensure call and the hand-written `back` strip beside it, so the
  change is net-negative in lines. `_split_keywords_for_merge` now drops either marker
  from the group-wide pool, which matters because the model emits "Negative" once it is
  told a `Negative` part is present.

  Three things went wrong before it settled, each of them the marker escaping the per-file
  scope the whole mechanism exists to enforce. The first two are one question answered
  twice, at the wrong scope each time:

  *The strip has to know what the group applied.* An unconditional `PART_MARKER_KEYWORDS`
  removed any keyword spelt like a marker from any file, and a marker no file in the group
  carries cannot have leaked from anywhere - it is the caller's own keyword. A print
  scanned from a negative and hand-tagged `Negative` in Lightroom, alone in its group, lost
  the keyword from its record and had `keywords_remove: ["Negative"]` proposed to the
  plug-in: a deletion instruction against the catalog, on the default path, for ordinary
  input. The strip set became the markers the group actually applies (`applied_markers`,
  built from `_item_part_marker` over the group). This retires the same latent bug for
  `back`, which behaved identically before C1 and at f4153ae; keeping one marker destroying
  user data and the other not would have cost more code than fixing both. It also removes
  the reason for an `is_negative` override, which stays out of scope.

  *And per file, not per group.* A group is the wrong scope for that question, which the
  second pass missed. Whether a marker leaked onto a record is a property of the file:
  a print hand-tagged `Negative` **beside** a real negative still owned that keyword before
  anything was merged into it, and a group-wide strip set took it off anyway - `print.jpg`
  carrying `metadata.keywords: ["Negative", "Trip"]` next to `print-negative.jpg` emitted
  `["Trip", ...]` and proposed `keywords_remove: ["Negative"]`, the same catalog deletion
  in a narrower group. This was strictly better than f4153ae, which destroyed the keyword
  unconditionally, but the information needed to tell the two apart is at the call site:
  `utils.load_item_metadata(it)` is read immediately before `merge_original_sources`, which
  is the last moment a file's own keywords are separable from the group's. The emit loop
  now reads `utils.part_markers_in` off that pre-merge metadata and passes
  `applied_markers - own_markers`, so the leak the strip exists for - one file's marker
  riding `merge_original_sources` onto its siblings - is still undone and nothing else is.
  `apply_part_keyword`'s third parameter is named `leaked` rather than `applied` to say so.

  *A marker must not reach the vocabulary.* "`grep -in negative` over the prompts and the
  vocabulary returns nothing" showed only that neither marker is *already* approved, not
  that one cannot be added; the vocab-insert block runs on the raw model keywords, which is
  the mechanism of the leak rather than a guard against it. Once the model is told a
  `Negative` part is present it proposes "Negative", and a run over `box3_026.jpg` plus
  `box3_026-negative.jpg` appended it to `vocab_keywords_examples.toml` inside the
  installed package - an approved keyword in every later prompt, teaching the model to emit
  a token this same commit defines as not being one. Both insert blocks now skip
  `PART_MARKER_KEYWORDS` beside the existing `PC-` guard.
- **`--back` states the whole address, not just the group.** B2 gave the pair a shared
  `group` so `photo.jpg --back reverse.jpg` could not split into two objects; under `pair`
  the bucket key carries the variant letter too, which the group key does not suppress, so
  a back named `IMG_0042b.jpg`, `scan-b.jpg` or `DSC_0001a.jpg` - ordinary camera and
  scanner output, and exactly the unreadable name `--back` exists to handle - split off
  anyway and reached the model with no front attached. That is the "handwriting with no
  photo" loss the plan reserves for `none`, and it inverted `_resolve_manifest_entry`'s own
  rule that an explicit override beats the filename. `build_single_photo_manifest` now
  pins the back's `version` to the front's, using the documented empty string when the
  front has none. Only `none` splits the pair, which is what the escape hatch is for. The
  cost under `object` is bounded and measured: the model call is unchanged, and there are
  two record differences, not one as an earlier revision of this line claimed.

  The first is the one that was stated: such a back is listed in
  `all_variant_files.variants` with `version: null` rather than the letter its name
  happened to end in - which is what `--back` asserted in the first place.

  The second follows from it and reaches **both** files of the pair. The per-variant
  caption label is chosen by asking whether the analyzed variant has a back
  (`variant_pairs.get(ver)`), and pinning is what moves the back into the front's variant,
  so the label flips from `[Front]` to `[Back]`:

  ```
  photo.jpg --back IMG_0042b.jpg          (object; the model call is identical)

    unpinned : caption '[Front] a handwritten note'   on photo.jpg and IMG_0042b.jpg
    pinned   : caption '[Back] a handwritten note'    on both
  ```

  Same for `scan-b.jpg` and `DSC_0001a.jpg`. `[Back]` is the more honest of the two - the
  caption transcribes the back's handwriting, and unpinned the group had a back nothing
  labelled as one - so this is recorded as a consequence rather than as a cost to pay
  down. A back whose name carries no letter is unaffected in both respects, at every value.
- **Crops per value.** `object` and `pair` are unchanged - a crop yields its parent's slot,
  is recorded and warned about, and an orphan crop is analyzed in its place. `pair` needs
  no rule: `parse_media_filename` strips `-crop` first, so a crop always carries its
  parent's `base_id` and version. Under `none` a crop is its own object and is analyzed,
  because "recorded but not analyzed" in a group of one means no record at all, which
  would violate the `set(results) | set(errors) == input` contract. The README sentence is
  scoped rather than contradicted. One guard was required: the orphan-crop WARNING is
  suppressed under `none`, where its condition is true for every crop on every run.

**C2 shipped - the CLI surface.** One input token, one path through `main`, and
nothing written without an explicit opt-in.

- **Positional input with detection.** `photokin ./scans/` / `photokin batch.json` /
  `photokin scan_042.jpg`. The argparse mutually exclusive group is gone, replaced by
  hand validation, which is what buys the custom messages. The rule is evaluated in one
  place (`cli._classify_positional`): `lexists`, then a dangling-symlink case, then
  directory, then "is it a regular file at all", then `.json`, then `utils.VALID_EXTS`.
  A directory called `batch.json` is a folder, because step 3 precedes step 5 - which is
  exactly why the run logs `Treating \`batch.json\` as a folder (it is a directory).` at
  INFO before it validates anything. `--folder` and `--manifest` survive as **aliases
  that assert** rather than detect, so `--folder notes.txt` is refused instead of being
  re-detected as a photo; only a positional is ever inferred, and only a positional logs.
  Deviation from the plan bullet at :533, which called for deprecating them with a
  one-time note: the brief forbids ceremony, an alias that asserts a type still has a
  job no positional does, and nothing is scheduled for removal. Section 4.3 records the
  same. All three input spellings normalize through one function, so a token that names
  nothing - `" "`, a quoted empty string - is refused rather than resolving to `"."` and
  quietly analyzing the working directory.
- **Content validation before the first model call.** An empty folder, an unreadable
  folder, a `.json` that is not a manifest, an empty `items`, an item with no `path` and
  an item whose path does not exist are all exit 2 with a two-line message. The first two
  replace `analyze_folder`'s warn-and-exit-0; the rest replace a `FATAL` JSON blob or a
  per-item error record. Fatal on the first offending item, not a collected report.
- **The gates are lifted.** `--output-file`, `--changeset` and the `--exiftool-*` write
  flags work for every input type. Phase A's "only supported in --manifest mode" stopgap
  is deleted, and its test class now pins the writes instead of the refusal. The changeset
  path is Q1 generalized into one function: `dirname(--output-file or input)` plus
  `<stem>_changeset.ndjson`. Two spellings change - a `.json` output and no output file
  both used to give a bare `changeset.ndjson`, which collided across runs in one directory.
- **`-w` / `--write`,** defined once in `cli._WRITE_BUNDLE` and expanded by one loop, so
  the expansion and the contradiction check cannot disagree. An explicit flag overrides
  the expansion; `-w --exiftool-write false` and `-w --changeset false` are errors rather
  than guesses. `--exiftool-write true` without a changeset is refused too, since there
  would be nothing to apply. `--changeset` stays a value flag per Q5.
- **The write default is flipped,** at `exiftool/config.py:57` and nowhere else. That one
  literal was the whole behavioral change: the dataclass already declared `False` and the
  CLI never reached it. `--changeset true` alone now records without applying. Five doc
  sites and two tests updated; see Breaking change #1 below.
- **The message set is centralized** in `photokin/cli_messages.py` - pure, no logging, no
  I/O, importing nothing but `dataclasses`, so the wording is testable without dragging
  the pipeline in. `cli._exit_with_usage_error` is untouched and every call site is a
  splat of a message tuple. The five Phase A pre-flight strings moved verbatim and are
  named in `_VERBATIM_FROM_PHASE_A`, because their problem lines predate the
  backticks-first style and are pinned by existing tests.
- **The plan summary** is one INFO record naming input (with what it was detected as, its
  file and group counts), output, changeset, write set, provider and model, emitted
  immediately before `process_manifest_stream` - the only call that can reach a provider,
  now that the folder branch no longer calls `analyze_folder`. The group count needs a
  second bucketing pass, so `build_manifest_buckets` and `_resolve_manifest_entry` gained
  `log_overrides` (and the two item-level readers a `log` keyword) to stop every override
  diagnostic printing twice.
- **`--dry-run` stops after the summary.** This resolves the brief's own contradiction in
  favour of "the cheapest guard before spending money": nothing is analyzed, no
  destination is truncated, and both pre-flights still run, so a missing ExifTool or an
  unwritable output still exits 2 and the summary is never printed. `cfg.dry_run` is no
  longer set from the CLI and survives as library API; the `or ecfg.dry_run` exemption in
  `_preflight_exiftool` is removed, because a plan claiming a write set while no binary
  exists would be a lie.

**Second pass - six defects adversarial review found.** Two of them are the guards C2
added having no test at all, which is the failure mode this phase exists to remove:

- *A blank input token analyzed the working directory.* `utils.normalize_path` strips
  whitespace and one surrounding quote pair and then calls `os.path.normpath`, which
  answers `"."` for what is left, so `photokin " "` - a wrapper interpolating an unset
  variable - was classified as a folder and every image in the cwd was analyzed, and
  under `-w` written to. An empty string was already refused, so blanks looked caught.
  All three spellings now normalize through `_input_path`; `--folder " "` was identical
  and did not even log a detection line. `photokin .` is untouched: the test is on the
  token, not on the normalized result.
- *The plan summary named the token rather than the directory.* Its `output` and
  `changeset` lines were already absolute; the `input` line echoed `resolved.display`,
  leaving the one value the "wrong folder" guard is about as the only unresolved thing
  in it. `RunPlan.input_display` became `input_location` and carries
  `os.path.abspath`. Error messages still quote what the user typed - that is the token
  they have to fix.
- *`--dry-run` truncated a destination.* `main` dispatched to `_generate_manifest` and
  returned above the `--dry-run` check, so previewing the flag over a hand-edited
  manifest replaced it and printed nothing, against the flag's own help text and
  `README.md:449`. It now reports the grouping it would have written and writes nothing.
  The analysis plan block is deliberately not printed there: that branch reaches no
  provider and has no output, changeset or write set to describe.
- *A remedy led back to the error it came from.* `write_needs_changeset` ran before
  `_refuse_generate_manifest_write_flags`, so `--generate-manifest --exiftool-write
  true` was told to add `--changeset true`, whose own refusal then said to drop it. The
  refusal now runs first, which also makes the branch written for that case reachable -
  it never could execute before, since `-w` and `--changeset true` each won an earlier
  branch and the bare flag exited above.
- *An unreadable manifest was reported as missing.* Every `OSError` from the read mapped
  to `input_not_found`, whose "check the spelling" remedy is wrong for the reachable
  case - a denied ACL, a lock held by a sync client - and contradicts the detection line
  printed immediately above it. Split: `FileNotFoundError` keeps that message, since a
  file removed after detection really is gone, and everything else takes
  `manifest_cannot_be_read`, the counterpart of the folder branch's existing message.
- *The two write guards and the flipped default had no working test.* Collapsing
  `_resolve_write_bundle`'s contradiction branch to an unconditional assignment, or
  deleting the needs-a-changeset guard, both left the suite green while `-w
  --exiftool-write false` silently became a write. `TestNothingIsWrittenWithoutAnOptIn`
  could not see the default flip either: every CLI test ran with
  `EXIFTOOL_WRITE_ENABLED=""`, and `_parse_bool_env` reaches its `default` argument only
  when the variable is *absent*, so the class passed unchanged against a tree with the
  pre-C2 `True` restored. `run_cli` gained an `env` parameter that can remove a
  variable rather than blank it. All three mutations now fail.

**Third pass - two leaks and the doc alignment C1 still owed.** Independent verification
passed every C2 objective; these are what it left behind.

- *The `-w` bundle was defined once and checked twice.* `_WRITE_BUNDLE` really was the
  only definition of the expansion and of the contradiction check, but
  `_refuse_generate_manifest_write_flags` restated the same membership by hand, so a
  member added to the bundle would have been expanded by `-w` and then silently permitted
  beside `--generate-manifest` - the one flag that can never honor a write, since it stops
  before any model call. The refusal now iterates the bundle. Carrying the wording is what
  made that possible: `_WriteBundleMember` holds the value, the verb and the replay
  spelling beside each other, so a member cannot join the bundle without saying how it is
  refused, and the four existing messages are unchanged. `-w` itself keeps its own branch
  and is still reported first, being the bundle's trigger rather than a member of it.
  `_resolve_write_bundle` builds its `values` map from the bundle too, which is what stops
  a third member raising `KeyError` there instead of being expanded.
  `TestTheWriteBundleIsDefinedOnce` drives all four cases off `cli._WRITE_BUNDLE` rather
  than off its current contents; two of them inject a member the CLI does not ship and
  fail against the restated implementation (the refusal exits 0 and writes the manifest;
  the contradiction check raises `KeyError` into the FATAL handler).
- *A genuinely empty input token was reported as no input at all.* `photokin ""` answered
  "no input was given." while `photokin " "` and the quoted spellings answered with the
  blank-token message. Argparse stores `""` like any other token; `_resolve_input` picked
  its source by truthiness, so the one spelling a wrapper produces most easily - an unset
  variable interpolated bare, which is the case the blank guard exists for - never reached
  `_input_path` and was told to supply an argument it had already supplied. The filter is
  now `is not None`, which is what "was this source given" actually means, and both
  aliases were identical and are fixed with it. `photokin` with flags but no input still
  says "no input was given.", pinned as the non-vacuity counterpart.
- *Dead `--dry-run` plumbing, decided per item rather than swept.* Both survivors are
  legitimate library API and both are kept, now saying so. `Config.dry_run` is live -
  `core._emit` stamps `dry_run: true` on each NDJSON record, and five test modules set it
  - and is simply not a CLI knob any more, since `--dry-run` returns before the stream is
  entered and there is no record to stamp. `cli._resolve_exiftool_config`'s `dry_run`
  parameter was required keyword-only and passed `False` by its one caller, which is
  exactly the shape that reads as flag-wired; it now defaults and `main` does not pass it.
  It is ExifTool's own preview - count the writes, perform none - which the CLI has no
  flag for and which `python -m photokin.exiftool.apply --dry-run` still reaches. Three
  words on which of the three `dry_run`s is which, at each site.
- *The doc alignment C1 owed, narrowed by what was already there.* Two of the four claims
  in the brief did not reproduce. `photokin/README.md` does not describe the pre-C1
  grouping model - C1 updated it, and it holds no `process_all_variants`, `update_policy`,
  `pick_master_index`, `primary` or `master`; and the part-marker keywords are not
  undocumented - `README.md` names both, their per-file scope and the strip. What was
  really missing: the core README never said what the three axis values *are* or what key
  each derives, never recorded the primary's retirement, and its Configuration list
  claimed to enumerate every knob while omitting two - `group_by`, and `pretty_json`,
  which has no flag behind it at all and so is reachable from nowhere else. The list is
  now complete, checked field by field against the dataclass rather than by eye, and the
  axis and the retirement are a `## Grouping`
  section, which also carries the part-marker mechanism the core README had nothing on.
  And one part-marker sentence in `README.md` was narrower than what shipped: it scoped
  the hand-tagged-marker rescue to "a group holding no negative", where C1's second pass
  moved the test per file, so a print hand-tagged `Negative` keeps the keyword even beside
  a real negative. Corrected, and the Quick Start now says where the `back` in its own
  sample output came from. Both READMEs told the reader to `cd python`, a directory the
  repo has not had since the restructure, and the core README still called the
  folder/manifest failure asymmetry an open Phase C decision after Phase C kept it.
- *And six more the same sweep found, once both READMEs were checked claim by claim
  against the tree rather than only for the four items above.* The sharpest is a data one:
  `README.md` documented `"true"/"false"/"yes"/"no"/0/1` and "null means not specified" as
  covering every manifest boolean, but only `is_back` and `is_crop` go through
  `_coerce_manifest_bool`. `preferred` is read as `bool(raw.get("preferred"))`, so
  `"preferred": "false"` and `"preferred": "no"` both mean **true** - a plug-in author
  following the documented grammar would silently pin the wrong file into a contested
  slot. Scoped, with `preferred`'s real rule stated beside it. Then: "hydration and apply
  are skipped with a warning rather than failing the batch" survived C2's own pre-flight,
  which exits 2 on a requested write with no binary; hydration was described as reading
  "fields" for any input when it reads one tag, `EXIF:UserComment`, for manifest input
  only, and only where the item already carries a `metadata` object (the omission at
  :483-491, undocumented until now); `--debug-dump-dir`'s default was given as
  `<output-dir>/debug` when that holds for manifest input alone and folder and
  single-photo runs dump into the working directory; `--batch-id` was "stored in output
  metadata and logs" when it reaches the `.ndjson` records and the dump filenames and
  neither the aggregate `.json` nor stdout; and the fetcher downloads from the project's
  SourceForge host, exiftool.org supplying the checksum. In the core README, `api_status`
  is raised by the OpenRouter adapter too, and the sentence this pass had just written
  claiming `build_manifest_buckets` is the only reader of `group_by` was itself wrong -
  `process_manifest_stream` reads it to suppress the orphan-crop warning under `none`.

  **Reported here, fixed in the pass below.** The loser of a `(version, part)` slot
  collision was warned about but added to neither `displaced_slots` nor the batch
  counter, so it was absent from `all_variant_files.displaced` and the completion line
  reported `0 file(s) displaced or dropped` for a run that dropped one.

**Fourth pass - the completion line stops contradicting its own warning.** The gap
reported directly above, taken as a maintainer decision rather than left visible.

The sharpest reproduction is not the override case the report used but the one an
archive hits on every folder: a TIFF master beside its JPEG derivative. Same stem, same
`(version, part)` address, so one is sent and the analysis is fanned out over both -
which is right, and cheaper, and is now documented as such at `README.md`. What was
wrong is that the same loss was accounted for two different ways depending on how the
two files reached the slot:

```
box3_025.tif + box3_025.jpg          two filenames parsing into one slot
  WARNING  2 file(s) claim the same none slot; analyzing box3_025.jpg
           and recording the rest: box3_025.tif
  INFO     ... 0 file(s) displaced or dropped ...        all_variant_files.displaced absent

ov1.jpg + ov1-back.jpg {is_back: false}   an override steering one onto the other's role
  WARNING  ov1.jpg and ov1-back.jpg both claim the front side ...
  WARNING  ... 1 file(s) displaced or dropped ...        displaced = {":none": [ov1.jpg]}
```

- **One map, filled by all three rules.** `displaced_slots` moves above the slot-winner
  loop and that loop registers its losers in it, so the record discloses a collision
  loser exactly as it already disclosed a displaced front. The `slot_winners` filter that
  followed the front-side rule is narrowed to the paths *that* rule unseated
  (`unseated_fronts`): a collision loser was never a winner, and a path that loses one
  address may still hold another, so filtering on the whole map would have struck a file
  the payload does carry off the candidate list.
- **The count is derived, not accumulated.** `unplaced_paths` becomes `unsent_paths` and
  is filled once per group from `{it["path"] for it in group} - analyzed_paths` rather
  than by each rule remembering to register itself - which is the bug's actual mechanism,
  and the reason it could recur at the next rule added. Two consequences, both wanted:
  every warning names a file the set holds, and the set holds nothing no warning named;
  and a path that won two addresses is no longer counted, because it *is* sent, under the
  better of them. Its surrendered address is still listed in `displaced`, which is
  slot-addressed and describes the slot rather than the file.
- **The wording changes once**, from `%d file(s) displaced or dropped from their group's
  payload.` to `%d file(s) recorded without being sent to the model.` Nothing this counts
  is dropped: every one of them keeps a full record taken from the analysis of the file
  that won the slot, and `process_manifest_stream`'s own contract is that
  `set(results) | set(errors)` is the input set. "Dropped" named a loss that does not
  occur, and it mattered more after the fix than before it - the commonest shape the
  count now reaches is the archival TIFF/JPEG pair, so the old wording would have told
  every archivist that a clean run dropped half their files. The level rule is unchanged:
  nonzero still summarizes at WARNING, matching the per-group messages. Four test
  assertions carried the old string and are updated with it; no README quoted it.
- **Coverage.** `TestSlotCollisionAccounting` (`test_manifest_grouping.py`, 6 tests) pins
  both shapes and pins them against each other, and carries the invariant in both
  directions over six shapes: the count equals the listed files no recorded model call
  carried, and every file it counts is named in a warning. `TestATiffMasterBesideIts
  JpegDerivative` (`test_folder_mode.py`) takes the archival shape through
  `analyze_folder` on real files. `ManifestGroupingTestCase.run_manifest_records` captures
  from INFO so a case can assert the completion line's *level*, which the WARNING-only
  runner could not see; `run_manifest_source` is now a filter over it, and its
  assertLogs-can-not-be-empty sentinel is gone, since every run logs a completion line.

**Fifth pass - the master goes to the model, not the export.** Fixing the accounting
exposed the tie-break underneath it. `_slot_rank_key` ended in the path, so among files of
one stem the alphabetically first won, and every archival pairing resolved the wrong way:
`.jpg` over `.tif`, `.jpeg` over `.tiff`, `.png` over `.tif`. Only one of a pair is ever
read, so the run was handing the model the compressed copy of every scan in the archive -
and folder mode could not override it, because folder items carry nothing but `path`.

`_FORMAT_RANK` (lossless, then PNG, then lossy) now sits between the version and the path.
Its position is the whole design: it settles only the case the path would otherwise settle
alphabetically, so nothing above it moves, and the path stays last so two genuinely
indistinguishable candidates still resolve identically every run. `preferred` outranks it,
being higher in the key, so an explicit choice still wins. Two tests written in the fourth
pass asserted the JPEG was sent; they encoded the old tie-break and are inverted, not
deleted. `README.md:273` said so too and is corrected.

Maintainer decision, taken with the alternatives on the table: a flag was offered and
declined as surface area for a case with one right answer. The cost is read time on a large
master, not upload size - `--max-edge` downscales and re-encodes before anything is sent,
so the bytes on the wire are unchanged.

Left open deliberately, each one recorded where it belongs: hydration stays off for folder
and single-photo input (the `{"metadata": {}}` seeding at :483-491 is a data change, not a
CLI one); `strict_run_failures` keeps its split (:514); the debug-dump directory stays
split between manifest and folder input; and the write-default transition is announced by
the plan summary line rather than by a separate warning (:539-540).

**Risk:** high. This is the breaking release, and the affected consumer is not in
this repo.

### Parallel track - Collapse the twin analyzers

The source plan targets the three entry points as the duplication to remove. The
expensive duplication is one level down and goes unmentioned: `analyze_photo` and
`analyze_group_parts` are roughly 200 near-identical lines, with three copies of the
forward-fields loader and two copies of the vocab-insert block.

- Fold `analyze_photo` into `analyze_group_parts` called with a single front/back
  pair.
- Settle the two visible blockers first: the divergent `_transport` shapes, and the
  divergent failure modes, where one path rewraps a bad result and the other raises,
  and one path raises on vocab-insert failure while the other only prints.

**Order:** independent of A, B and C, but cheapest immediately after B, once there is
only one caller shape to satisfy.

### PC codes belong to the object, not the scan

Shipped alongside B1, from a maintainer decision rather than the audit.

A `PC*` keyword is a short identifier the model transcribes off the print itself - the
prompt instructs it to emit any code it can read as `PC-<code>`
(`prompts_photo_ai/image_rules.txt:97`), and forbids those codes from entering the
vocabulary file (`image_rules.txt:100`, `:212`). So a PC code describes the physical
object, not the particular scan the model happened to be shown.

Keywords were scoped to the analyzed file's variant letter, and only one analysis runs
per group, so only files sharing that letter ever received the codes. A `-b` rescan of
the same print silently got nothing:

```
MODEL reads 'PC-R-123' off the scan it is shown
  before:  obj7.jpg=YES  obj7-back.jpg=YES  obj7b.jpg=no    (all 4 policy/variant modes)
  after:   obj7.jpg=YES  obj7-back.jpg=YES  obj7b.jpg=YES
```

The codes are now unioned across the group. The back already received them by sharing
the front's letter; the change is that sibling variants do too.

Note this is not the same mechanism as a PC code that arrives in a file's *existing*
metadata - that path was governed by `--update-policy`, which C1 retired. With the flag
gone, `merge_original_sources` runs unconditionally, which is what `merge_per_variant` -
the default and the only setting the CLI ever shipped as such - already did. Only
model-read codes are affected by the union above.

Side effect worth recording: this retires the `preferred`-versioned-back symptom rather
than re-scoping it. A code can no longer be filed against the wrong variant because
every variant gets it. `primary_version` still governs caption variant labels, so the
B1 fix is still load-bearing there.

---

## 4. Breaking changes

### 1. ExifTool write default

Today the default resolves to true, and not where you would look for it.
`ExiftoolConfig.enabled` is declared `False` on the dataclass, but that line is
unreachable from the CLI: `from_env` sets it from the environment with a `True`
fallback, then discards the `None` sentinel that a missing flag produces. Anyone
reading the dataclass would conclude writes are already off.

The behavior is reachable only through manifest mode with `--changeset true`, so
folder and single-photo users are unaffected. The sole consumer is the external
Lightroom plugin, which was split out of this repo and has no caller, test or grep
here that could catch the change. Mitigation is a loud stderr warning for one minor
version. Five documentation sites and two tests assert the current value and must be
updated deliberately.

**Status: shipped in C2.** One literal: `_parse_bool_env("EXIFTOOL_WRITE_ENABLED",
True)` became `False` at `config.py:57`. Nothing else moved, because nothing else
had to - `from_env` has exactly one caller (`cli._resolve_exiftool_config`), and every
other construction is a direct `ExiftoolConfig(...)` that already reached the
dataclass's `False`. `config.py:36` and `config.py:57` now agree, so
`photokin/exiftool/README.md`'s "`enabled` (default False)" is true rather than
misleading. Precedence is unchanged (flag > env > default), and the only runs whose
behavior changes are those passing `--changeset true` with no `--exiftool-write` flag
and no `EXIFTOOL_WRITE_ENABLED` in the environment.

The five doc sites are updated (`config.py`'s docstring, the `--exiftool-write` help,
`photokin/exiftool/README.md`, and the flag table plus the "Writing during a run"
paragraph in `README.md`), along with the rider that the Q1 changeset generalization
falsified. The two tests asserting the old default now assert the new one, and the
regression the plan asked for - with no write flags, `apply_changeset` is never called
for folder, photo or manifest input, with a resolvable binary and `--changeset true` -
is `test_cli_preflight.py::TestNothingIsWrittenWithoutAnOptIn`.

The transition notice is **not** a separate warning. It is the plan summary's
`write : none (--exiftool-write defaults to false)` line, which is printed before the
run on exactly the affected runs. Deviation from the "loud stderr warning for one minor
version" above, taken because the brief forbids ceremony and the summary is guaranteed
to be seen; the louder form is one `logger.warning()` on the same condition.

### 2. `analyze_folder` return shape

Split across two phases, because half of it has already shipped.

**Already in, from Phase A:** the return value is `{"results": ..., "errors": ...}`
instead of `{"results": ...}`. Additive, so embedders reading `["results"]` are
untouched, which is why it was allowed to land with the error isolation that needed
it rather than wait for Phase B. The rider is behavioral rather than structural: a
per-group failure is now collected instead of propagated, and the call raises only
when no group succeeded at all.

**Shipped in Phase B2 - the per-file half.** `analyze_folder` keeps its signature and
returns:

```python
{
    "results": {file_path: merged_record, ...},   # ONE ENTRY PER FILE
    "errors":  {file_path: error_payload, ...},   # ONE ENTRY PER FILE of a failed group
}
```

`set(results) | set(errors)` is exactly the set of files `list_folder_images` returned,
and the two are disjoint: nothing in the folder is unaccounted for. Keys keep the
spelling they had, so `results[some_front]` still works; what breaks is iteration -
backs, variant scans, album pages, negatives and crops each have an entry now, so a
caller making one downstream write per entry makes several. Records are exactly what
the stream stores, with no folder-specific post-processing: they gain `_merge`, per-file
scoped `keywords` and `caption`, `_usage` summed across the group, and the full
`all_variant_files` map (`front`, `back`, `variants`, `all`, plus `pages`, `crops`,
`negatives`, `displaced` where they apply) in place of the old `{"front": [...],
"back": [...]}`. Error payloads are the stream's shape - `{"type", "message"}` plus
`status_code` and `traceback` where they apply - which is a superset of Phase A's.

Nothing is kept for compatibility: no `groups` key, no primary-front-only view. A shim
would let an iterating caller stay green while its semantics changed silently, which is
the failure mode Phase A exists to remove, and a per-group view is derivable from any
record's `all_variant_files`. There is no runtime deprecation line either - a shape
change that occurs on every run would be noise on every run.

**Rider: the CLI's single-photo stdout changes too.** `core.analyze_photo` and
`public.analyze_photo` are untouched - `analyze_photo` is the leaf the stream itself
calls - but the CLI's single-photo branch now prints the stream's aggregate, so its
stdout goes from `{"result": {front: record}}` to `{"results": ..., "errors": ...}`
and gains a record for the `--back` file. Keeping the old shape would need a second
translation layer and would hide the back's record.

**Also new, and additive:** `process_manifest_stream` returns an `errors` key, so the
manifest aggregate `.json` and the manifest stdout fallback carry one too; and both
modes now get a per-group failure ERROR line and one completion line on stderr. See
the B2 open risks under Phase B.

### 3. Deprecations

None. `--folder` and `--manifest` are **not** deprecated: C2 kept them as permanent
aliases that assert a type where a positional infers one, they emit no note, and no
removal is planned. The plan bullet at :533 called for a deprecation cycle with a
one-time note; that was dropped during C2 and section 3 above records why. Anyone
writing release notes from this section should say the aliases are unchanged.

The only flags carrying a retirement notice are C1's `--process-all-variants` and
`--update-policy`, which are accepted, do nothing, and warn once each.

---

## 5. Test coverage

The plan rewrites the least-tested code in the repository. There is no CLI test file;
across all twenty test modules the only one importing `cli` exercises a single
private helper with a hand-built namespace. `main()` itself has never been executed
by a test.

| Phase | Coverage to add |
|---|---|
| A | Characterization snapshots of folder and manifest output before any change, so later phases have a baseline. Pre-flight tests asserting that a missing ExifTool and an unwritable output both stop the run before any provider call. |
| A | A fixture folder with album pages and a negative reports every group rather than skipping silently; a mid-batch exception preserves prior results. |
| B | Golden-file grouping tests over variants, backs, pages, crops and negatives. Parity test: folder input and an equivalent hand-written manifest produce identical changesets. |
| B | Explicit `is_back` override groups a non-conforming filename correctly, using the README's own sample as the case. |
| B1 | Written, `photokin/tests/test_manifest_grouping.py`: permutation test over `itertools.permutations(items)` asserting the model call, the page/crop/negative slot maps and the warning set are invariant, across crop, page-zero, negative, `preferred` and `is_back` groups in both `--process-all-variants` settings. Crop displacement in both listing orders; the cropped-front-plus-uncropped-back and crop-only-back slots; the orphan-crop and dropped-crop warnings naming the right files; `-page0` keeping its own slot and label; a `preferred` back reaching the model whether or not it carries a variant letter. It uses at most one metadata-bearing item per group, since `combine_group_metadata` is still order-dependent. |
| B1 | Still owed: the four overrides asserted in both directions, and the `feedback.jpg` non-repair for `_EXPLICIT_BACK_SUFFIX_RE`. |
| B1 | Second pass: `TestPayloadInvariants` asserts both rules over the eight shapes that resolve one file into two roles or none, in both `--process-all-variants` settings. Plus the negative-plus-back group in both listing orders, the front-side role collision in its untagged and multipage forms, the duplicate listing, the `preferred` crop that used to fail its whole group, and the version an analysis is filed under when a `preferred` back wins the master pick. |
| B2 | Rewritten, `photokin/tests/test_folder_mode.py`: the three tests that pinned the skipping now assert the opposite - every group reaches the model, every file gets a record, and the completion line reports a clean run at INFO. Plus the album-plus-pages group placing every file it can and naming the one it cannot, `--process-all-variants` sending `Page 1`/`Page 2` in one call, a two-file failed group carrying the same payload on both paths, and sidecar writing delegated to the shared analysis call. `TestUnanalyzedGroupFiles` was removed with the helper it exercised. |
| B2 | Second pass: `TestSidecarWriteFailureKeepsTheAnalysis` (`test_folder_mode.py`) holds the record through an unwritable sidecar, in the one-group and the every-group case, stubbing only the provider boundary so the write really runs and really fails - the mocked-analyzer tests elsewhere in that module cannot see it. `TestPartialGroupFailure` (`test_manifest_grouping.py`) pins `results`/`errors` disjointness, one stream line per path, and no file lost, with the failure injected at a fixed point so it cannot go vacuous. `TestGenerateManifestInputExists` (`test_cli_preflight.py`) pins the missing image, the missing `--back`, the identical first error line with and without the flag, and that a real image still writes its manifest and calls no model. |
| B2 | Written, `photokin/tests/test_folder_routing.py` (35 tests): the routing itself, where `test_folder_mode.py` covers the folder entry point's own contract. The headline regression - album pages, negative-only sets and back-only sets all reach the model, every file gets a record, and no group is reported skipped. Folder-vs-manifest parity over a fixture folder covering all five suffix forms, asserted on the model calls, the records and the diagnostic sequence in both `--process-all-variants` settings, against a hand-written manifest that spells the same paths differently so the comparison cannot collapse into a builder compared with itself. `--generate-manifest` against checked-in goldens under `photokin/tests/fixtures/manifests/` - both the document it writes and the grouping that document describes - the round trip back through `--manifest`, the file-and-group summary line, and the atomicity of the write. `--process-all-variants` changing what folder input sends, dead in that mode until B2. Single-photo `--back` matching the two-item manifest it is translated into, including a back the filename grammar cannot read. And `TestOrdinaryFolderIsUnchangedFrom7bcaf2f`, pinning call literals captured from that commit so the phase stays additive. |
| B2 | Third pass, the two shapes the B2 differential missed: `TestBackOnlyGroupsReachTheModel` and `TestAVariantsBackIsPairedWithThePrimaryFront` (`test_folder_routing.py`). Neither is reachable from the checked-in fixture folder, which is why the sweep did not see them - its only multi-file group gives the primary front a back of its own, and it holds no back-only group at all - so both build their own folders. The second also asserts the bound on the change (a primary with its own back still prefers it) and that the same call comes out of the manifest pipeline, so the pairing is pinned as parity rather than as a folder-mode quirk. `TestGeneratedManifestAtomicWrite` covers the overwrite, the temp-file cleanup and a failed write leaving the previous manifest intact; the failure is injected at `os.replace` rather than at serialization, because a serialization failure happens before either write sequence touches the destination and so cannot tell the fixed code from the code that unlinked first. |
| B2 | Nothing further owed. The two modules an earlier revision listed as outstanding - `test_generate_manifest.py` and `test_folder_manifest_parity.py` - were both written into `test_folder_routing.py` rather than as separate files, and the goldens they wanted are checked in: golden grouping over all five suffix forms and the four-group summary count in `TestGeneratedManifestGolden`, end-to-end parity in both `--process-all-variants` settings in `TestFolderManifestParity`, and the atomic write in `TestGeneratedManifestAtomicWrite`. |
| C1 | Existing modules moved rather than a new one written, because the axis replaced the setting every grouping test was already parameterized on. `test_manifest_grouping.py`, `test_folder_routing.py` and `test_folder_mode.py` take `group_by` where they took `process_all_variants`, and the permutation sweep, the payload invariants and the crop-per-slot rule now run over all three values instead of two flag settings. Changed assertions, all of them the retirement of the primary landing: the album set travels as `Page 1`/`Page 2` by default, a lone negative takes the `Negative` label by default, a four-scan group sends four images, two orphan crops are both analyzed instead of one being recorded and dropped, and `TestPreferredBack` pins "every back is sent" where it pinned "this back is chosen". `TestOrdinaryFolderIsUnchangedFrom7bcaf2f` became `TestOrdinaryFolderAgainst7bcaf2f`, keeping the 7bcaf2f literals as the documented BEFORE and adding the assertion that matters most - the call count is unchanged. |
| C1 | Second pass, one new module and two new classes, all four fixes pinned against the implementation they replace. `photokin/tests/test_group_analyzer_payload.py` stubs only the provider boundary, so the real prompt assembly and the real vocabulary-insert block run: a one-image group is not told it is seeing several while still carrying its part label, a genuine two-image group still is, and a proposed `Back`/`Negative` is refused while an ordinary keyword beside it is written - the latter proving the fixture is not vacuous. In `test_manifest_grouping.py`, `TestPairKeyCannotCollide` takes the reachable forms of the collision (an explicit `group` on any platform, a POSIX filename) through all three axis values and through the stream, and pins that an ordinary key keeps the `group_id` `object` gives it; `TestPartMarkersOnlyStripWhatTheGroupApplies` asserts the hand-applied marker survives a group with no such part, that the leak the strip exists for is still undone, and that the file the marker describes keeps it at every granularity - each over both markers. In `test_folder_routing.py`, `--back` is pinned against four back filenames the grammar reads a variant letter off, under `object` and `pair`. The three that could be injected were re-run against the pre-fix implementations and fail (4, 2 and 3 failures); the other two are covered by before/after captures of the prompt text and the vocabulary file. |
| C1 | Third pass, the two defects the second pass's own tests could not see, both pinned by extending the class that missed them. `TestPairKeyCannotCollide` gains `test_the_key_is_injective_over_the_whole_cross_product` - a brute force over every `(group_key, version)` an eight-value alphabet spells, the `None` version included; the alphabet holds the empty half, a plain letter, the separator and the escape character alone, and each of those two leading, trailing and doubled - and `test_a_trailing_separator_does_not_merge_with_the_join`, which names the `('a\|', 'a')` against `('a', '\|a')` case so the regression is unmistakable; a third colliding pair carries the same shape through the stream. The old class asserted only the two shapes that defeat an *unescaped* join, so it passed while the injectivity claim was false. `TestPartMarkersOnlyStripWhatTheGroupApplies` gains `test_a_hand_applied_marker_survives_a_sibling_that_is_that_part`, over both markers, asserting the keyword survives, is not re-added beside the caller's spelling, and draws no `keywords_remove` - the last needing the real `build_canonical_patch`, since the stub the module uses elsewhere makes every pre-existing keyword read as a deletion, so `run_manifest` now keeps it whenever a changeset writer is supplied. Both replacements were re-run against the implementations they replace: 4 of 5 pair-key tests fail against separator doubling, and the marker test fails on both markers against the group-scoped strip while the three older tests in its class still pass. |
| C1 | `--group-by` at the CLI seam, in `photokin/tests/test_group_by.py::TestTheRetiredFlagsStillParse`, which runs `cli.main` in process with a real argv. An earlier revision of this row listed all three as verified by hand; all three are pinned. `--process-all-variants` and `--update-policy` are each accepted and warn exactly once, over a folder holding three groups so a per-group or per-file warning would show up as three or four; neither warns when it is not passed, which a warning keyed on `--update-policy`'s old default would have done on every run; and `test_group_by_reaches_the_config_from_argparse` asserts the value `analyze_folder` receives for the bare default and for all three spellings. |
| C | Detection matrix: directory, `.json`, image; deprecated aliases still work; positional plus alias conflicts; `--back` with a folder errors. |
| C | Every error case asserts exit code and first line. `-w` expands correctly in all modes, explicit flags override the expansion, and the contradictory combination errors. |
| C | Regression for the default flip: with no write flags, nothing is written in any mode. |
| C2 | Third pass, both in `photokin/tests/test_cli_preflight.py`. `TestTheWriteBundleIsDefinedOnce` reads `cli._WRITE_BUNDLE` rather than restating it, so a member added to the bundle is covered the day it is added: every member is refused beside `--generate-manifest` with its own verb and replay wording, `-w` is still answered about itself, and two cases inject a member the CLI does not ship - against the restated refusal the first exits 0 and writes the manifest, and the contradiction check raises `KeyError` into the FATAL handler instead of reporting a usage error. `TestABlankInputTokenIsRefused` folds the bare empty string into its blank-token sweep and into the alias sweep, adds `test_a_genuinely_empty_token_takes_the_same_path`, and gains `test_no_input_at_all_still_says_so` so widening the source filter cannot start describing a run with no input as a blank one; four of those fail against the truthiness filter. |
| C2 | Second pass, in `photokin/tests/test_cli_preflight.py`, every case re-run against the implementation it replaces so none of it is decorative. `TestWriteBundleGuards` covers the two guards that had none - the `-w` contradiction in both its spellings and `--exiftool-write true` without a changeset - over all three input types, asserting the stream and the apply step are never entered rather than only the exit code, since a regression that runs the batch and then fails also exits 2. `TestNothingIsWrittenWithoutAnOptIn` now removes `EXIFTOOL_WRITE_ENABLED` instead of blanking it, and gains a non-vacuity case holding everything constant but that variable, so a fixture that could never reach `apply_changeset` fails loudly instead of passing quietly. `TestABlankInputTokenIsRefused` takes five blank spellings and both aliases from inside a scratch folder, and pins that `photokin .` and the empty-string message both still answer as before. Plus `TestThePlanNamesTheResolvedInput`, `TestGenerateManifestHonorsDryRun`, `TestGenerateManifestRemediesTerminate` (including that each of the four write flags keeps its own wording after the reorder) and `TestAnUnreadableManifestIsNotReportedAsMissing` in both of its branches. |
| C2 | Fourth pass, the completion line's accounting. `TestSlotCollisionAccounting` (`photokin/tests/test_manifest_grouping.py`) takes both collision shapes -- two filenames parsing into one `(version, part)` slot, and an override steering one file onto the front side another holds -- and asserts the warning, `all_variant_files.displaced` and the completion line's count and level as one story, then asserts the two shapes are reported identically, which is the asymmetry the fix removes. The invariant is carried in both directions over six shapes including two that lose nothing: the count equals the listed files no recorded model call carried, and every file it counts is named in a warning. Plus the decision that a path winning two addresses is not counted, since it is sent. `TestATiffMasterBesideItsJpegDerivative` (`test_folder_mode.py`) runs the archival shape through `analyze_folder` on real files, because that is where it actually turns up. Both halves of the change were mutated separately in a scratch copy. Reverting the accounting and keeping the wording: 6 failures over 4 of the 7 new tests, the two survivors being exactly the ones that should survive -- the override shape was always counted, and the every-count-has-a-warning direction was never broken. Reverting the wording and keeping the accounting: 15 failures, the 4 pre-existing assertions elsewhere included, which is what pins the string to one definition.  |

---

## 6. Where this diverges from the source plan

- **Section 0** - the positional-plus-alias conflict is already handled by an argparse
  mutually exclusive group, so that part is free. Getting the custom message means
  removing the group and validating by hand.
- **Section 1** - the plan assumes one suffix grammar. There were two implementations
  sharing a filename parser: the folder grouper binned crops and negatives as
  first-class slots, the manifest bucket loop did not, and degraded both into
  untagged fronts. Routing folder input at the manifest pipeline without reconciling
  them would have traded one silent loss for another. Phase B1 closed that gap from
  the manifest side, so the routing in B2 now has a target worth routing to.
- **Section 1** - the described refactor is far smaller than it reads, because
  `process_manifest_stream` already accepts a dict. The costly duplication sits below
  the entry points and is not mentioned.
- **Section 3** - "no in-repo caller will break" is accurate, and that is precisely
  the risk. Nothing here can detect the plugin breaking. Sharpened by Phase B1: four
  manifest keys that were silently ignored (`is_back`, `is_crop`, `version`, `group`)
  are now load-bearing, so any manifest the plugin already emits carrying them for its
  own bookkeeping will regroup or re-slot with no error. This repo holds no copy of the
  plugin and no fixture of its manifests, so it cannot be checked here. Confirm against
  the plugin's manifest writer before release.
- **New, from Phase B1** - `utils.combine_group_metadata` (`utils.py:1553-1570`) takes
  first-non-empty over preferred-then-arrival order, so two items in a group carrying
  different captions still yield a permutation-dependent `sent_to_model` snapshot. B1
  left it alone deliberately: it is metadata precedence, not grouping. Worth settling in
  B2 or C, and the reason B1's permutation tests must use at most one metadata-bearing
  item per group.
- **Section 5** - `-w` defaulting the output file into the scanned folder writes two
  artifacts into the user's photo directory and modifies every image. Photo
  directories are frequently cloud-synced, network-mounted or read-only. Worth
  deciding explicitly rather than letting it fall out of a default.
- **New** - the public API break to `analyze_folder` is not covered by the source plan
  at all.
