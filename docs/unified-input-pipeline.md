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
| Q2 | No `--recursive`; keep it separate | Recursion changes grouping semantics across directories, since the same basename can appear in several. It also interacts with write safety. Its own PR. **`-R` is reserved for it** (C3): `-r`/`--read` mirrors `-w`/`--write`, so the recursive flag takes the capital and must not be re-spelled later. Recorded in the README flag table. |
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
won the master pick. (That fix has since been superseded twice over: for `PC*` by "PC codes
belong to the object" below, and for caption labels by C3's sixth pass, which files each
file's caption under that file's own version rather than under the analyzed variant's.
`primary_version` still addresses the back slot and names the record the analysis is filed
under.)
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

**Settled in C3, and the seeding was the wrong shape.** The hydrator is now gated on the
explicit `-r` in every input mode, so no existing folder run's prompt or cost moves unless
it asks. Nothing is seeded: the guard treats a missing `metadata` as `{}` without attaching
it and creates `raw["metadata"]` only when a value is really read, so `load_item_metadata`
still answers `None` for a file that holds nothing and an item naming a `metadata_path`
keeps its sidecar. See the C3 section under Phase C.

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
  is what keeps a plain front/back pair's `[Back] ` prefix. *(The caption half is superseded
  by C3's sixth pass: the caption no longer forks on this predicate at all, because both
  branches append exactly one entry to `analyses`, and that fork was what left the default
  `object` path unlabelled. The callee still follows the predicate, which is the part that
  keeps ordinary output stable.)*

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

  *(Superseded by C3's sixth pass. Both halves of this are now wrong: the label is no longer
  chosen per analyzed variant, and labelling a front `[Back]` because its variant had a back
  is the mislabel that pass removes. A file's caption is filed under that file's own role -
  `[Photo ...]` for a front, `[Back ...]` for a back - and every file of the group receives
  the same combined block, so the two lines above are one block reading `[Photo] ...` and
  `[Back] ...`, whether or not the back was pinned with `--back`.)*
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
CLI one - **closed by C3 below**); `strict_run_failures` keeps its split (:522); the
debug-dump directory stays split between manifest and folder input; and the write-default
transition is announced by the plan summary line rather than by a separate warning
(:549-550).

**C3 shipped - `-r` / `--read`, and the date stops lying.** Hydration becomes an explicit
flag that works in every input mode, reads the whole set rather than one tag, and stops
asserting a scan date as a capture date.

- **`-r` / `--read`, mirroring `-w` / `--write`.** Both are explicit opt-ins and neither
  implies the other. The gate is one line - `metadata_hydrator=make_manifest_hydrator(ecfg)
  if args.read else None` - so folder, single-photo and manifest input all hydrate iff `-r`,
  and none hydrates without it. `-r` is deliberately **not** a `_WRITE_BUNDLE` member and is
  **not** refused beside `--generate-manifest`: it is a read, and `-r --generate-manifest` is
  the combination the round trip depends on. Explicit rather than default for two reasons: a
  folder run must not silently shell out to ExifTool and change what the model is sent, and
  the plugin should adopt the three fields it never had deliberately rather than inherit
  them.
- **What it reads, and why that list.** The field set is
  `exiftool.manifest.DEFAULT_EXIFTOOL_FIELDS` - `EXIF:DateTimeOriginal`, `EXIF:UserComment`,
  `XMP:Description`, `XMP:Title` and `XMP:Subject` - and the tag-to-key mapping is not
  re-declared: `hydrate._HYDRATED_TAGS` derives it from that tuple and the existing
  `_TAG_TO_MANIFEST_KEY`. It is the set the ExifTool subpackage already calls "what photokin
  reads from a file", every member has a mapping, and every one is in
  `DEFAULT_METADATA_FORWARD_FIELDS`, so every one can actually reach the model. Location is
  out: nothing downstream would consume it that C3 owns. Values are stored verbatim, so
  `dateTimeOriginal` keeps ExifTool's colon form, which `merge._extract_year` and `canonical`
  both already read, and `keywords` stays a list - ExifTool returns a multi-valued tag as a
  JSON list and a single-valued one as a bare string, which `manifest.manifest_value`
  normalizes for both this path and the standalone manifest builder.

  Keywords were left out of the first pass, on the grounds that they touch four coupled
  decisions none of them C3's. That was wrong in the one way that mattered: `XMP:Subject` is
  where the `DATE:` marker lives, and it is the only interlock on the heuristic that reading
  the date exists to feed. See the second pass below for the defect and for why the four
  coupled decisions each resolve in the safe direction.
- **The guard fix.** `hydrate_user_comments` skipped any item whose `metadata` was not a
  dict, which is every folder item (`{path}` only) - the item with everything to gain. The
  loop now treats a missing `metadata` as `{}` without attaching it, and creates
  `raw["metadata"]` only when a value is really written, so a file ExifTool reads nothing
  from keeps its item byte-identical and the goldens are unaffected. An item naming a
  `metadata_path` is still skipped outright, because `load_item_metadata` prefers an inline
  dict and seeding one would shadow the sidecar - which is the hazard recorded at :483-491,
  handled rather than inherited. Renamed to `hydrate_item_metadata`, since the old name
  would lie about five fields; it is on neither `public.py` nor `photokin/__init__.py`, so
  no breaking-change entry, matching the `utils.group_folder_images` precedent at :405-407.
  Still one subprocess per batch: the union of paths needing anything, requesting all five
  tags, filtered per item on write-back.
- **The forwarding was broken anyway, and the fix is one line.** `metadata_forward.toml`
  lists `dateTimeOriginal` as a forwarded field and the prompt is written to receive it, but
  `combine_group_metadata` renamed `dateTimeOriginal` to `date` and `date` is not in the
  allowlist, so it was dropped at the last step. Verified before the fix:

  ```
  combine_group_metadata emits : ['caption', 'date', 'keywords', 'title', 'userComment']
  ACTUALLY reaches the prompt  : ['caption', 'keywords', 'title', 'userComment']
  dropped at the allowlist     : ['date']
  ```

  `combine_group_metadata` now emits the value under **both** keys, and
  `merge_original_sources._pick` reads `dateTimeOriginal` as an alias for `date` on both
  primary and fallback. The allowlist is untouched and no existing key changes meaning.
  The alias is required, not cosmetic: `own_meta` is the item's raw metadata, where a
  hydrated date is spelled `dateTimeOriginal`, and `_pick` looks for `date`, which only
  `combine_group_metadata` produces - so without it no file's own scan date ever reached its
  own record and every file inherited whichever sibling was scanned first. The two rejected
  alternatives were "put `date` in the allowlist" (fixes the symptom by renaming the
  contract, leaving two spellings of one field in the wild) and "stop renaming" (breaks
  `merge.py`'s two `original["date"]` readers, including the gap heuristic that is the whole
  justification for reading the date).

  **`metadata_forward.toml` never loads, and did not before this.** `core` opens it with
  `json.load` at :422, :695 and :1584; the file is TOML, so every run raises
  `JSONDecodeError` into a handler that logs only under `MEL_VERBOSE`/`MEL_DEBUG`, and
  `forward_fields` is always `None`. Verified by execution. The effective allowlist is
  therefore always `DEFAULT_METADATA_FORWARD_FIELDS`, whose contents happen to be identical,
  so nothing is observably wrong today - but the two can silently diverge, and an implementer
  who "fixes forwarding" by editing the TOML changes nothing at all. Repairing the loader
  activates an inert config file on every run and needs its own decision; out of scope here,
  recorded under the open risks.
- **The date is evidence, not truth.** `merge.py`'s old block did
  `merged["date_guess"] = {"iso": d, "confidence": 1.0}` unconditionally whenever an original
  date was present. On a flatbed scan `EXIF:DateTimeOriginal` is the *scan* date, so that
  asserted the day you scanned the print as the day the photograph was taken, at full
  confidence, and discarded "circa 1952, confidence 0.7" with it. It now sets
  `merged["date_original"]` and fills `merged["dateTimeOriginal"]` **only when that is
  empty**; `date_guess` is not touched at all and keeps exactly what the model returned.
  The emptiness guard is what keeps the gap rule's rewrite intact, and filling
  `dateTimeOriginal` is what stops `canonical._date_from_metadata` falling through to the
  model's guess in every case where the gap rule declined - the trap in the naive edit, which
  would have proposed 1952 against a 1955 file and inverted the heuristic's entire purpose.
  `report["overrides"]` no longer gains `"date_guess"`; `"dateTimeOriginal"` is still
  appended when the gap rule fires. The fill is deliberately not reported as an override -
  nothing was overridden, since the AI never emits `dateTimeOriginal` at all, and
  `merged["date_original"]` discloses the evidence more usefully than a report entry would.
- **The gap heuristic is untouched and finally reachable.** Not one line of `merge.py:246-322`
  changed. It compares `original["date"]` against the model's inference and rewrites
  `dateTimeOriginal` only when the AI is confident and the year gap is wide - "don't clobber
  modern photos, do fix old ones". Folder items were `{path}` only, so `original["date"]` was
  always empty and **in folder mode it never fired**. `-r` plus the rename fix is what makes
  it live. The `DATE:` keyword suppression is unchanged: a human-reviewed date still stops
  the rewrite, and now `date_original` records what the file held while `date_guess` records
  what the model thought.
- **Patch neutrality, proven by differential against HEAD's `merge.py` over six shapes.** The
  patch's `EXIF:DateTimeOriginal` and the changeset diff's `set` entry are byte-identical
  before and after in every one; only `date_guess` and the new `date_original` differ.

  ```
  shape                            patch EXIF:DateTimeOriginal   diff set      identical
  gap fires (2019 vs 1952@0.7)     1952-01-01                    1952-01-01    yes
  gap silent (1955 vs 1952@0.7)    1955:06:01 09:00:00           (absent)      yes
  gap silent, low conf (0.4)       1955:06:01 09:00:00           (absent)      yes
  DATE: keyword present            2019:04:03 11:22:33           (absent)      yes
  no original date                 1952-01-01                    1952-01-01    yes
  no model date_guess              1955:06:01 09:00:00           (absent)      yes

  the worked example, scan.tif, EXIF 2019 vs model "circa 1952" at 0.7:
    BEFORE  date_guess = {"iso": "2019:04:03 11:22:33", "confidence": 1.0}
            overrides  = ["dateTimeOriginal", "date_guess"]
    AFTER   date_guess = {"iso": "1952", "import_date": "1952-01-01",
                          "confidence": 0.7, "pattern": "Y~"}
            date_original = "2019:04:03 11:22:33"
            overrides  = ["dateTimeOriginal"]
    both    dateTimeOriginal = "1952-01-01"   (the gap rule, unchanged)
  ```

  A caller who wants the original treated as authoritative does nothing and no `Config` flag
  is added: `merged["dateTimeOriginal"]` still always carries the value that will be written,
  and a consumer reading `record["date_guess"]["iso"]` to recover the file's date reads
  `date_original` instead. Suppressing the model's proposal outright already has a mechanism
  - a `DATE:` keyword on the file.
- **The title rule narrows, for titles read out of a file.** `-r` makes `merge.py`'s
  original-title-wins rule reachable in folder mode for the first time, and scanner software
  routinely writes "Scanned Image" or the bare filename into `XMP:Title`. Unnarrowed,
  boilerplate would outrank a title the model transcribed off the print, which makes reading
  the file strictly worse than not reading it. Under `-r` the original now wins only when the
  model returned none:

  ```
  model title      original title    merged             overrides
  -                -                 -                  []
  Wedding Day 1952 -                 Wedding Day 1952   []
  -                Aunt Ruth's ...   Aunt Ruth's ...    ['title']     <- unchanged
  Wedding Day 1952 Scanned Image     Wedding Day 1952   []            <- the only changed cell
  ```

  The two sources are not symmetric evidence: the prompt constrains the model to titles
  legibly printed on the object (`image_rules.txt:102-107`), so when both exist one was read
  off the physical thing and the other was written by whatever last touched the file. The
  human value is not destroyed - it stays in the file, and the changeset's
  `original_data.file_metadata` block still reports it, so a reviewing plugin sees both. The
  rejected alternatives were keeping it as-is (imports junk titles into every folder run), a
  scanner-boilerplate denylist (locale- and vendor-specific policy code of exactly the kind
  the caption half of this phase forbids), and deleting the rule (destroys a genuine human
  title). Dropping `XMP:Title` from the read set was also rejected: an existing title is
  useful context, and the reason for reading the full set is to know what *not* to update. A
  side fix rides along - the emptiness test now strips both sides, so `" Title "` beside
  `"Title"` no longer reads as a difference.

  **The first pass narrowed it for every input mode, which was the fourth defect below.** The
  fourth rejected alternative above was provenance tracking, dismissed as "real plumbing
  through two functions that drop unknown keys, for a case one line handles" - but the case
  is not the one line it looked like, because the narrowing then also governs manifest input
  with no `-r` anywhere, where the original title is a human's. Provenance is now tracked, and
  without touching either of those two functions: `original_title_from_file` is a keyword-only
  parameter defaulting to `False`, and the hydrator's presence is what sets it.
- **No new caption code.** The evaluate-then-replace policy already exists, in the prompt
  (`instructions_front_back.txt:261-276`, `output_format.txt:61-63`,
  `image_rules.txt:188-192`), and the join is `core._join_captions`. Reading the caption so it
  reaches the model was the whole job. *(Held only for the first pass. The sixth pass below
  replaced the join with a group-wide labelled block; `_join_captions` is gone, and its
  line-by-line de-duplication survives inside the block assembly as the last net. The claim
  about the prompt still stands: the semantic policy is still the model's and no second call
  was added.)*
- **`combine_group_metadata` becomes permutation-independent**, which `-r` makes necessary.
  It takes first-non-empty over `preferred + arrival` order; folder items used to carry no
  metadata, so it returned `{}` and was trivially invariant. With `-r` every file
  contributes, so a group of two disagreeing scans would yield a permutation-dependent
  forwarded snapshot, prompt and merge input - the gap this plan already recorded at
  :1279-1284, widened from "manifests with two or more metadata-bearing items" to "every
  multi-file group in every folder". Preferred still comes first; each half is now sorted on
  `(normalize_path.lower(), normalize_path)`, the tie-break the pipeline already uses
  everywhere else. Measured:

  ```
  three metadata-bearing items disagreeing on title/caption/dateTimeOriginal/userComment
                            BEFORE                    AFTER
    no preferred            6 distinct answers        1
    one preferred           2 distinct answers        1

  720 permutations of a 6-file folder in which every item carries metadata
    -> 1 distinct outcome, once all_variant_files is excluded (B1 keeps its list
       order input-ordered on purpose); model calls, results, errors and every
       per-file title/caption/date/keyword value are invariant across all 720.
  ```

  For folder input the sort reproduces arrival order exactly - one directory, so comparing
  full paths lowercased is comparing basenames lowercased, which is `list_folder_images`'s
  own key - so folder output is unchanged by the sort itself. For manifest input it replaces
  an arbitrary answer with a stable one, the same trade B1 made for slot occupancy at
  :301-306. The `it not in preferred` membership test, an O(n^2) list scan over dicts, goes
  with it. The second half of the mitigation is the `dateTimeOriginal` alias above.
- **`--generate-manifest` carries what was read, and round-trips.** `main` returns into
  `_generate_manifest` above the `ecfg` block, so the hydration and its pre-flight live
  there; `_resolve_exiftool_config(args)` is a pure function of the namespace and is called
  directly, no hoisting and no new parameter. Hydration runs before bucketing and before the
  write, so the document written and the grouping reported describe the same items. Under
  `--dry-run` the pre-flight still runs but no subprocess starts and nothing is written.
  The document holds only the five mapped keys, only where non-empty, and **omits `metadata`
  entirely** for a file ExifTool read nothing from; no `metadata["exiftool"]` raw-tag block
  and no `metadata["path"]`, both of which `exiftool_records_to_manifest_items` adds, since
  neither is in the forward allowlist and both would bloat a document that has to round-trip.
  No `"read": true` marker either: the file describes the input, never the run settings.

  What round-trips, verified end to end: `photokin -r <folder>` and `photokin <that
  document>` send the model the same work - identical grouping, identical model calls,
  identical images per call, identical `Forwarded metadata:` lines - and come back with
  identical `errors`, while the replay makes **zero** ExifTool subprocess calls. A second
  `-r --generate-manifest` over the same folder is byte-identical, and `--meta` beats `-r`
  on the front item.

  **`results` is the exception, and stdout is byte-identical only where the title rule has
  nothing to decide.** That rule keys on provenance, and the document deliberately records
  none, so on replay every hydrated title reads as a human-typed manifest title and keeps the
  precedence over the model's that a manifest title has. Measured on `print.jpg` carrying
  `XMP:Title "Scanned Image"` against a model returning `"Wedding Day 1952"`:

  ```
                  title                _merge.overrides   stdout vs folder -r
  folder -r       Wedding Day 1952     []                 -
  replay          Scanned Image        ['title']          differs
  replay -r       Wedding Day 1952     []                 byte-identical
  ```

  So the round trip is exact for every group where the model returns no title - which is the
  common case, and the shape `TestGeneratedManifestRoundTripsWhatWasRead` uses, where
  `results` and stdout do compare byte-identical - and `photokin -r <that document>` restores
  the folder result outright wherever it is not. `-r` on a replay costs no subprocess where the
  first read was complete - the hydrator queries only items still missing a key, and a document
  written from a full read leaves none - but the pre-flight still demands a resolvable binary
  and exits 2 without one, so it is not free. Closing the gap
  properly would mean the document carrying provenance, which is a fact about the run rather
  than about the input - the same line the `"read": true` decision above draws - so it is left
  stated here rather than settled in passing.
- **A read that cannot run is fatal, like a requested write.** `-r` with no resolvable binary
  exits 2 before any provider call, for folder, manifest, single-photo, `--generate-manifest`
  and `--dry-run` alike, with `cli_messages.exiftool_not_found_for_read`. The argument is
  that the failure is silent and expensive - the run proceeds to call the model with a
  strictly worse prompt, pays in full, and produces results carrying no marker
  distinguishing "read nothing" from "there was nothing to read" - where `-w`'s failure is
  loud by construction. `-r -w` with nothing available reports the *write* message, whose
  remedy fixes the read too. The README's "hydration is skipped with a warning" sentence
  described hydration when it was an implicit step nobody asked for; `-r` inverts that. The
  best-effort behavior is kept where it still belongs: after the pre-flight passes, a mid-run
  ExifTool failure warns and continues, the same split `-w` has.
- **The plan summary gains a `read` row**, between `input` and `output` since reading
  precedes everything it affects: `read : ExifTool EXIF:DateTimeOriginal, EXIF:UserComment,
  XMP:Description, XMP:Title, XMP:Subject` under `-r` and `read : none (-r not given)`
  otherwise - the row is built from `DEFAULT_EXIFTOOL_FIELDS`, so it grew with it. That is
  how the plugin's loss is announced, the same mechanism C2 used for the flipped write
  default at :1154-1158 rather than a separate deprecation warning. `_LABEL_WIDTH = 9`
  already fitted "read".

**What the Lightroom plugin loses.** A manifest run used to hydrate unconditionally. A plugin
that does not pass `-r` loses exactly one thing: `EXIF:UserComment` is no longer read out of
files for items already carrying a `metadata` object whose `userComment` is missing or empty.
Nothing else, because nothing else was ever hydrated. The reach is bounded -
`merge_original_sources._pick` does not list `userComment`, so it never reached
`merge_record_with_original`, `build_canonical_patch`, `proposed_changes` or any file. The
observable consequence is prompt quality plus one absent key in a changeset audit block; no
proposed write appears or disappears. The remedy is one token in the plugin's argv, at which
point it gains three fields it never had.

**Second pass - five defects adversarial review found, and one it was wrong about.** Four of
the five are the same shape, and it is the shape this phase set out to avoid: *reading the
file made the run worse than not reading it*. The read either armed something whose safety it
did not also read, or let a supporting file's values stand for the object's.

- *The date's human interlock was not in the read set.* The read set's whole justification is
  the gap heuristic, and that heuristic has exactly one veto: `_has_date_keyword`, which looks
  for a `DATE:` marker among the original keywords. Keywords were deliberately left out on the
  grounds that they touch four coupled decisions, so `-r` read `EXIF:DateTimeOriginal` and left
  its safety off. A print an archivist had dated by hand - `EXIF:DateTimeOriginal
  1952:06:01`, `XMP:Subject ["family", "DATE: Y!"]` - was re-dated from the model's inference
  under `photokin <folder> -r -w`: `-EXIF:DateTimeOriginal=1920:01:01 00:00:00` written to the
  file, and a second `DATE: Y~` added beside the human's. Without `-r`, on this branch and at
  a3dc4f1, the same run leaves it alone, because with no original date the heuristic cannot
  fire. `XMP:Subject` joins `DEFAULT_EXIFTOOL_FIELDS`, mapped to `keywords`.

  The four coupled decisions turned out to resolve the safe way, which is why this is one line
  of tuple and no policy: the union in `merge_record_with_original` is what manifest input has
  always done; `diff_canonical_metadata`'s `keywords_remove` gets *smaller*, since the patch's
  keywords are a superset of a before-snapshot that now holds the file's own, where before the
  snapshot was empty and every existing keyword read as an addition; and the `own_markers` /
  `leaked` calculus reads the file's real markers instead of assuming none, which is exactly
  what C1's per-file rule asks for. `XMP:Subject` is the readable spelling;
  `CANONICAL_KEYWORDS_TAG` was `XMP:dc:Subject`, which ExifTool refuses as a *write* target
  ("doesn't exist or isn't writable"). That was a separate pre-existing defect, left where it
  was at the time and since fixed - the three constants are now `XMP-dc:` and a writability
  test holds them there (see the resolved-defect entry below).
- *One command line for the whole folder.* `run_exiftool_json` put every path on a single
  argv. Windows caps that at 32767 characters and fails past it with `[WinError 206]`, which
  `subprocess` raises as `FileNotFoundError`, which the function re-wrapped as "ExifTool not
  found at: ..." naming a binary that resolves perfectly, which the hydrator logged as a
  warning and continued from. Measured: 400 files with 68-character paths hydrated 0 of 400,
  and the run went on to pay for every model call with an un-hydrated prompt and exit 0. The
  pre-flight cannot catch it, because the binary does resolve. The list is now batched under
  `_ARGV_BUDGET`; a list that fits is still exactly one invocation, so nothing small moves.
  700 files now hydrate 700. The shape predates C3 - manifest hydration could reach it - but
  C3 made it folder mode's default and widened manifest mode's query from "items missing a
  userComment" to "items missing any of the tags", and `-r --generate-manifest` failed the
  same way and wrote a round-trip document carrying no metadata at all.
- *The caption join was not idempotent.* `_join_captions` de-duplicated whole parts, so it
  matched only while the stored caption was exactly the generated one. Once run 1 had written
  `"<original>\n[Front] <ai>"` back, run 2 compared that whole string against `[Front] <ai>`,
  found no match, and appended a second copy; run 3 a third. Unbounded, silent, and only on
  the photos carrying a human caption - a file with no original caption was stable, which is
  what made it look fine. It now de-duplicates line by line, so re-feeding the join its own
  output returns it unchanged. This is not caption-merge policy, which stays in the prompt: it
  is the join failing to do the one thing it already claimed to do. *(The line-by-line rule
  survives in the block the sixth pass below builds, where it is the last net rather than the
  whole mechanism: the block is now keyed by label, so a section is recognized as its own
  before any line comparison happens.)*
- *The title narrowing was applied globally rather than to hydrated titles.* The question this
  phase was asked was whether the original-title-wins rule survives contact with titles `-r`
  reads out of `XMP:Title`. The answer shipped as an unconditional narrowing, which also
  reached manifest input with no `-r` and no ExifTool subprocess anywhere - where the original
  title is not scanner boilerplate but a field a human typed into Lightroom. A manifest item
  carrying `title: "Mom's graduation, June 1961"` against a model title read off the film edge
  merged to `"KODAK SAFETY FILM"` and proposed it for `XMP-dc:Title`, with `_merge.overrides`
  empty so nothing recorded that the human title had been dropped.

  The two directions are not symmetric and the asymmetry decides it. Un-narrowed under `-r`,
  boilerplate suppresses a genuine transcription - a *quality* loss, and nothing is written,
  since the value proposed is the one already in the file. Narrowed on the manifest path, a
  human title is overwritten on disk - a *data* loss. So the rule is now scoped by provenance:
  `merge_record_with_original` takes `original_title_from_file`, defaulting to `False`, and
  `process_manifest_stream` passes `metadata_hydrator is not None` - the hydrator being the
  only thing that ever puts a file's own tags into an item. No `Config` field, no marker keys
  threaded through the two functions that drop unknown ones, and every existing caller keeps
  a3dc4f1's behavior by default. The four cells under `-r` are unchanged from the table above;
  without `-r` all four are a3dc4f1's, keeping only the strip-both-sides side fix.
- *The group's metadata was taken from its supporting scans.* `combine_group_metadata`'s new
  path sort is permutation-independent, which was the point, but `-` (0x2D) sorts before `.`
  (0x2E) - so `box3_025-back.jpg`, `box3_025-crop.jpg` and `box3_025-negative.jpg` all precede
  `box3_025.jpg`, and first-non-empty took the negative's. The front print's own title and
  caption reached neither the model nor its siblings' records; the model was sent
  `{"title": "negative title", "caption": "THE NEGATIVE STRIP"}` for the object, and the back
  came back proposing the negative's description into a file that had none. The section above
  observed that for folder input the sort reproduces arrival order exactly and stopped there,
  without noticing that arrival order ranks supporting scans first. Unreachable before C3,
  since folder groups carried no metadata and the function returned `{}`.

  The scan now runs from the file that most is the object to the file that least is: crops
  yield first, then `PART_RANK`, then the path. That is the order `_slot_rank_key` already
  uses to decide which file fills a slot, so the group's metadata and the group's primary scan
  are the same file rather than two independent answers - which is why `_PART_RANK` moved to
  `utils.PART_RANK` and core now refers to it instead of holding a second copy. `preferred`
  still leads outright. Entries with no `part_kind` all rank alike and fall through to the
  path, so a caller passing raw manifest items sees the previous behavior.
- **Deferred at the time, then fixed: the gap override's confidence threshold.** The report's
  remaining finding was that the gap rule fired at `date_override_confidence_threshold` (0.6)
  while `canonical` would not write a *guessed* date below `date_confidence_threshold` (0.7),
  so a 0.65 inference could overwrite a date the file already held although it was too weak to
  fill an empty one. It was rejected *for C3* on the grounds that the asymmetry was real but
  neither new nor this phase's: the two are separate knobs, `merge.py:246-322` was untouched,
  and the case was reachable at a3dc4f1 through manifest input, which has always carried an
  original date. Measured then: a Lightroom-shaped item holding `dateTimeOriginal
  2019:07:04 14:05:00` with the model at 1965 @ 0.65 merged `dateTimeOriginal 1965-01-01` and
  emitted `EXIF:DateTimeOriginal {"op": "set", "value": "1965-01-01"}` byte-identically on both
  trees, so `-r` changed reach and not rule.

  The owner has since taken the decision the note left open and swapped the pair:
  `date_confidence_threshold` is now **0.6** and `date_override_confidence_threshold`
  **0.7**. Filling a date a file lacks is the cheap direction -- an empty field loses nothing
  to a poor guess -- while replacing one it holds destroys something, so the override gate now
  sits at or above the write gate rather than below it. `date_override_precise_*` (0.8 / 5
  years) is unchanged and still above both, since a narrower year gap is weaker evidence of a
  real mismatch and has to be paid for in confidence.
  `TestOverwritingADateCostsMoreThanFillingOne` (`test_read_flag.py`) pins both orderings, so
  the next person to tune either knob has to look at the other.

**Coverage.** `photokin/tests/test_read_flag_hazards.py` as the first pass left it (22 tests,
4 subtests; the sixth pass below takes it to 61 and 75), one class per
defect, each written against the story rather than the line: the interlock class pins the
suppressed rewrite, the absent second marker and - as its non-vacuity case - that the heuristic
still fires when the marker is removed; the batching class pins that a small list is still one
invocation, that a large one is split, that no invocation exceeds the budget and that every
path is requested exactly once in order; the caption class re-feeds the join its own output
twice; the title class takes all four cells in both provenance modes and follows one of them
through `build_canonical_patch` to the tag; and the group-metadata class pins the front's
values, a sibling still supplying what the front lacks, invariance over all 24 permutations,
`preferred` still leading, and the no-`part_kind` fallback. All five fixes were mutated back
in a scratch copy and the suite re-run: 4, 2, 1, 2 and 1 failures respectively, none of them
overlapping, so no test is decorative. The six-shape date differential against a3dc4f1 and the
`-r --generate-manifest` round trip were both re-run after the changes and still hold - all six
patches and diffs identical, replay making zero ExifTool calls, the repeat generate
byte-identical, and the document now carrying `keywords`.

**Sixth pass - the caption becomes the group's, and stops lying about which side it came
from.** Three defects, all of them in the one block of `core.py` that turns a group's
captions into the text written into its files. They are taken together because the first
cannot be fixed without rebuilding the other two.

- *The block was per file, so no two files agreed.* Each file got its own caption as a
  preamble and the group's generated caption after it, which means a print, its back and a
  rescan each told a third of the story and none of them told it whole. What the owner wants
  is one block, identical in every file of the group, so that whichever file someone opens a
  year from now is not an accident that costs them the other two. Measured, on
  `box3_017.jpg` / `box3_017b.jpg` / `box3_017b-back.jpg` each holding its own caption:

  ```
  before  box3_017.jpg       : 'Caption A\n<the model's caption>'
          box3_017b.jpg      : 'Caption B\n<the model's caption>'
          box3_017b-back.jpg : 'Back of Photo B\n<the model's caption>'

  after   all three          : '[Photo A] Caption A
                                [Photo B] Caption B
                                [Back] Back of Photo B
                                [AI Analysis]: Two people outside a bakery.'
  ```

  The architecture is **group-wide intake**, not per-file evaluation, and that is the whole
  of it: a per-file pass cannot produce an identical block, because each file's preamble
  differs. One sweep over the group reads each file's own caption *while it is still known
  which file it came off* - the one moment attribution is free rather than guesswork -
  attributes it to that file's label, unions the labelled sections across the group,
  de-duplicates, appends this run's analysis, and hands the one result to every member.
  There is therefore no such thing as unattributable text at intake, which is why the
  rejected "keep unlabelled prose as a personal preamble" rule had to go: it reintroduces
  exactly the divergence the block exists to remove. What survives of it is the narrow real
  case - a *multi-line* run of prose on one file - and that takes one label on its first
  line rather than a label per line, since the run is one thought and labelling each line
  would make them sections that later runs could reorder independently.

- *The default path did none of this.* The branch followed `group_payload`, so
  `--group-by object` - the default, and so the overwhelming majority of runs - reused the
  model's own caption verbatim and added no labels at all; only `pair` and `none` ever
  reached the labelling code. Both payload branches append exactly one entry to `analyses`,
  so the fork bought nothing and is gone.

- *A front was labelled `[Back]`.* The per-variant branch asked whether the analyzed variant
  *had* a back and, if so, filed that variant's caption under the back's role - so under
  `pair` the caption written onto a FRONT file read `[Back] ...`. This is the same defect
  the C1 note at :776-788 recorded as "the more honest of the two" and let stand; it is not,
  once the caption is a labelled block, because the label is now a claim about which file
  the text came off rather than a decoration. A photo is `[Photo ...]`, a back is
  `[Back ...]`. The de-duplication that branch existed for is kept and is now what it always
  should have been: sections are compared on their text with the label stripped, so one
  caption an archivist copied onto both sides of a print is written once, under the side
  that ranks first.

  Labels are added only where they distinguish something. A group of one file with no back
  gets none at all - the common case stays untouched - and a back is labelled only when the
  group holds both sides. The variant letter is decided per role, reusing the
  `multiple_fronts` / `multiple_backs` pair the merge rules already compute, so two photos
  and one back give `[Photo A]` / `[Photo B]` / a bare `[Back]`. An unversioned scan prints
  as `[Photo A]` beside a lettered sibling, because that is what it is - the reason the
  second scan is lettered `b` and not `a` - but never with no lettered sibling to
  disambiguate from, and never when the group holds a real `a`.

**Deterministic caption update, and the knob that decides it.** Rules (a)-(d) of the brief
are implemented with no second model call, because the block is labelled and therefore keyed:
the structural merge is string work, and the semantic judgement - do two differently worded
captions mean the same thing - already happens in the primary call, which under `-r` is shown
the existing caption and told to evaluate it (`instructions_front_back.txt:261-276`). A
partial block gains its missing sections and keeps the ones it has; a materially different
caption is added beside what is there; nothing is ever deleted.

Near-identity is the dangerous part, and the measurement changed the design. Scored on
normalized text with `difflib.SequenceMatcher`:

```
must SKIP   trailing period / case / spacing ................ 1.0000
must SKIP   "Ruth and Sam, outside" vs "Ruth and Sam outside"  0.9841
must SKIP   "Grandma’s porch" vs "Grandma's porch" ........... 0.9643
must SKIP   "Ohio - summer" vs "Ohio — summer" ............... 0.9444
must SKIP   '"hello"' vs "'hello'" ........................... 0.9091
must KEEP   "...bakery, 1948" vs "...bakery, 1949" ........... 0.9730
must KEEP   "Ruth and Sam" vs "Ruth and Edith" ............... 0.8750
must KEEP   one digit of a year in a 300-char analysis ....... 0.9967
```

Skipping is `ratio >= T`, so the SKIP rows demand `T <= 0.9091` and the KEEP rows demand
`T > 0.9967`. **No such T exists** - the ranges overlap almost entirely, because `ratio` is
relative to length and a changed year in a long block moves it less than a changed quote mark
in a short one. A single threshold either loses date corrections or keeps punctuation
variants, and losing a correction someone typed is the unrecoverable one. What separates the
two ranges cleanly on every row is whether any *word* changed, so the word sequence carries
the decision and the ratio remains only as a last gate for a residue too small to be a word:
`_CAPTION_NEAR_IDENTICAL_RATIO = 0.998`, above the worst measured material difference by
design, so it is structurally unable to be the thing that discards a name, a year or a place.
`test_no_single_ratio_threshold_could_have_done_this` executes that table, so the constant's
comment cannot go stale.

**Idempotency, which is the non-negotiable one.** Under `-rw` the block written here is
exactly what the next run reads back as *every* file's existing caption - not one file's, all
of them, which is the steady state and the case most likely to double. Two properties make it
a fixed point. Intake recognizes its own labelled lines and takes them verbatim rather than
attributing them again, which is the `[Photo A] [Photo A] Caption A` failure; `[Front]` is
read for this reason and never written, so an archive an older release enriched settles
instead of doubling. And everything from an `[AI Analysis]` marker to the end of a caption is
the previous run's analysis and is regenerated rather than accumulated - without which a model
that rewords itself between runs, which is the normal case in the field, would add a paragraph
per pass and the frozen-reply tests would not notice. Three consecutive runs are byte-identical
after the first, at every `--group-by` value, for prose a human typed, a multi-paragraph
caption, a block already in the labelled form, a lone file holding nothing, and the multi-file
groups the labels exist for.

**Coverage (sixth pass).** `test_read_flag_hazards.py` grows four classes on a shared
`_CaptionBlockTestCase` harness that states each case as `{filename: the caption that file
already holds}` and asserts, on every call, that the group's files all came back with the same
block: the shape and label rules, the update rules, near-identity in both directions with the
predicate and the ratio table asserted beside the end-to-end result, and a 24-permutation
sweep over a four-file group in which every file carries a different caption - the block is
assembled from four captions now, which is four chances for arrival order to reach a value
written into a photograph, so the intake sweeps in `_slot_rank_key` order like every other
choice in the bucket loop. Thirteen mutations were applied in a scratch copy and every one of
them bites: the per-file block, the `[Front]` wording, the front-as-back mislabel, re-attributing
already-labelled lines, dropping the analysis marker, loosening the ratio to 0.90, dropping the
word comparison, sweeping in manifest order, one letter rule for both roles, implying `A`
unconditionally, keeping the previous analysis, dropping the legacy `[Front]` spelling, and
restricting de-duplication to a single label. Three of the thirteen did not bite on the first
attempt: two were bad mutations, and the third found a genuinely weak test - the legacy-spelling
case had been written on a single unlabelled file, where nothing is prepended to anything and
the doubling cannot be observed, and now runs on a labelled pair with the legacy line first.

**C3 open risks.**

- Two C3 changes reach manifest mode independently of `-r`, and neither can be checked against
  the plugin here - the standing blind spot at :1272-1278. (a) `date_guess` no longer carries
  the original date at confidence 1.0, so a plugin reading `record["date_guess"]["iso"]` to
  recover the file's date must read `date_original` or `dateTimeOriginal` instead; the patch
  and the changeset diff are provably unchanged, but the record is not. (b)
  `combine_group_metadata`'s scan order becomes rank-then-path sorted rather than
  arrival-ordered, changing the forwarded snapshot only for groups holding two or more
  disagreeing metadata-bearing items. Confirm against the plugin's manifest writer and record
  reader before release. The title rule was a third such change until the second pass scoped it
  to `-r`; with no `-r` the plugin now sees a3dc4f1's title precedence exactly.
- The plugin loses automatic hydration the day this ships unless it adds `-r`. Bounded to the
  prompt and one changeset audit field, but it is a silent quality change on the plugin's own
  contract and this repo holds no fixture of it.
- `metadata_forward.toml` is dead and has been since it was written (`json.load` on a TOML
  file). Its contents happen to match `DEFAULT_METADATA_FORWARD_FIELDS`, so nothing is
  observably wrong - but the two can silently diverge, and repairing the loader activates an
  inert config file on every run, which needs its own decision.
- `DEFAULT_EXIFTOOL_FIELDS` does not request the legacy instruction tags
  (`Photoshop:Instructions`, `XMP:Instructions`, `IPTC:SpecialInstructions`) even though
  `_TAG_TO_MANIFEST_KEY` maps all three to `userComment`, nor `IPTC:Caption-Abstract`, which
  maps to `caption`. Archives written by older Lightroom versions may carry notes only there.
  Adding them is a one-line change to the tuple, but it widens what `-r` reads on the plugin's
  path too, so it is left for a follow-up with a real corpus behind it.
- The changeset's `before_snapshot` never carries the file's existing `UserComment`:
  `merge_original_sources._pick` does not list it, so every run proposes `EXIF:UserComment`
  as a change even when the value is unchanged. Pre-existing and unaffected by C3, but more
  visible now that `-r` puts the existing value in front of the model. Worth settling when
  the apply step is next opened.
- The scan-order sort is the one mitigation that changes existing manifest-mode output. The
  judgment is that a stable answer beats an arbitrary one, matching B1's slot-occupancy trade.
  If that judgment is wrong, the fallback is to leave the function alone and keep B1's
  restriction on the permutation tests - at the cost of every multi-file folder group gaining
  a permutation-dependent forwarded snapshot under `-r`.
- ~~`XMP:dc:Subject`, `XMP:dc:Title` and `XMP:dc:Description` - the three `canonical.py`
  constants the changeset writes through - are not ExifTool-writable spellings.~~ **Fixed.**
  ExifTool answered "Sorry, XMP:dc:Description doesn't exist or isn't writable" and did
  nothing, so `-w` could not write a keyword, a title or a caption into a file at all; only
  `EXIF:UserComment` and `EXIF:DateTimeOriginal` landed. End to end,
  `photokin <folder> -rw --exiftool-fields XMP:dc:Description` reported
  `files_seen=3 files_written=0 tags_written=0 errors=3` and still exited 0. The three
  constants are now `XMP-dc:Subject` / `XMP-dc:Title` / `XMP-dc:Description`, which is both
  writable and the spelling ExifTool itself prints back under `-G1`; the caption feedback loop
  the second pass fixed now closes through photokin's own writes.

  The reason it shipped is the more useful half: **nothing asserted that a canonical tag was
  writable**. Every test either mocked the binary or exercised only the default write set
  (`EXIF:DateTimeOriginal`, `EXIF:CreateDate`, `EXIF:UserComment`), all three valid, so the
  defect was invisible to a green suite. `photokin/tests/test_canonical_tags_are_writable.py`
  is the missing check: it derives the tag list from `canonical.py` by reflection - rather
  than restating it, so a tag added later is covered the day it is added - and drives the real
  ExifTool binary against a real image for each one, asserting the value reads back rather
  than trusting the exit code. It skips only when no binary is on PATH.

  Two measurements from that work worth keeping, because both contradict the obvious rule:
  a second colon is *not* itself the error (`EXIF:IFD0:Model` is valid ExifTool syntax and
  `EXIF-IFD0:Model` is not, so a blanket colon-to-hyphen rewrite would break working input),
  and `XMP:xmp:Rating` *works* where the identical-looking `XMP:dc:Description` fails - the
  middle token `xmp` happens to collide with the family-0 group name and `dc` collides with
  nothing. `--exiftool-fields` therefore rejects the `XMP:<ns>:<Tag>` shape by name and quotes
  the working spelling, in pre-flight, rather than normalizing it or letting the run reach
  "Nothing to do" after the batch has been paid for.
- `date_original` is a new key on every record whose file carries a date. It is inert against
  `canonical.py` (which reads `dateTimeOriginal`, then `date`, then `date_guess` - never
  `date_original`) and `_merge`'s report has no production reader, but both are additive
  changes to the NDJSON and the aggregate `.json` that no fixture of an external reader
  exists for. Same class of risk as the `errors` key added in B2 (:511-514).
- The title narrowing assumes the model emits a title only for text legibly printed on the
  object, which is a prompt constraint rather than an enforced one. A model that hallucinates
  titles would now beat a genuine human title where it previously could not. The human value
  is still in the file and still in the changeset's `file_metadata` block, so it is
  recoverable, but the proposal changes.
- `-r` reads `EXIF:DateTimeOriginal`, which on a camera-original JPEG is the real capture date
  and on a flatbed scan is the scan date, with nothing in the file distinguishing them. The
  design rests on the gap heuristic making that distinction from the model's inference; a scan
  whose inference is within `date_override_year_gap` (20 years) of the scan date is
  indistinguishable from a modern photo and keeps the scan date. Pre-existing behavior of the
  heuristic, but `-r` is what makes it reachable in folder mode and it will now be exercised
  on real archives for the first time.
- ~~The C3 test row in section 5 is owed rather than written.~~ Paid: section 5 now carries
  four C3 rows, and the three items they last listed as owed are closed.

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
vocabulary file (`image_rules.txt:100`, `:221`). So a PC code describes the physical
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
every variant gets it. (`primary_version` governed caption variant labels when this was
written; C3's sixth pass moved those onto each file's own version, so the B1 fix is now
load-bearing for the back slot and the analysis record rather than for the labels.)

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
| C3 | Second pass, written: `photokin/tests/test_read_flag_hazards.py` (35 tests, 37 subtests), one class per defect the adversarial review confirmed. Three of its eight classes came later, in the close-out pass, and are described in the row below. `TestTheDateKeywordInterlockSurvivesHydration` runs a hand-dated print with a `DATE:` marker through the stream and pins the suppressed rewrite, the absent second marker, the file's keywords reaching the record, the single-valued `XMP:Subject` shape, and - as its non-vacuity case - the heuristic still firing once the marker is removed. `TestTheReadIsBatchedForLargeFolders` pins that a small list is still exactly one invocation, that 900 files are split, that no invocation exceeds `_ARGV_BUDGET`, that every path is requested once in input order, and that 700 items all hydrate. `TestCaptionJoinIsIdempotent` re-feeds the join its own output twice, in both the with- and without-original-caption shapes. `TestTitlePrecedenceDependsOnProvenance` takes all four cells in both provenance modes and follows the manifest one through `build_canonical_patch` to `XMP-dc:Title`. `TestGroupMetadataComesFromTheObjectNotItsSupportingScans` pins the front's values winning over a back, a crop and a negative, a sibling still supplying what the front lacks, invariance over all 24 permutations, `preferred` still leading, and the no-`part_kind` fallback. Each fix was mutated back in a scratch copy: 4, 2, 1, 2 and 1 failures respectively, non-overlapping. |
| C3 | **Close-out pass**, three more classes in the same module, each answering a defect the phase's own verify found in the phase's own work. `TestEachFileKeepsTheDateItWasReadFrom` pins the `dateTimeOriginal` alias in `merge_original_sources._pick`, which was load-bearing and pinned by nothing: without it a back scan inherits the front's date instead of keeping the one read off itself (3 failures on revert, with a fourth test deliberately still passing as the bound rather than the pin). `TestTheConvenienceWrapperIsNotLessExpressive` pins that `core.analyze_manifest` forwards `titles_may_be_from_files` rather than dropping it — the wiring line was itself unpinned when written, and deleting it left the suite green. `TestTheFileNeverOverwritesWhatTheInputAlreadyCarried` closes the per-field non-override gap for all five keys, on **both** branches of the guard: one sweep omits a key at a time so the write-back loop actually runs (holding all five short-circuits at `if not paths_needing` and asserts nothing the no-query case does not assert more strongly), and one sweeps `""`, whitespace and `[]` so "or holds empty" is pinned per key too. Pinning absence alone was not enough — keeping the emptiness branch for `userComment` and dropping it for the other four passed the whole suite clean, which is the same asymmetry in the other half of the condition. Mutated: 13 failures for that shape, 10 for the `if not meta.get(key)` simplification, which the whitespace rows are what catch. |
| B1/C3 | **B1's permutation restriction, lifted.** `TestPermutationInvariance.GROUPS` capped each group at one metadata-bearing item, because `combine_group_metadata` was then first-non-empty over *arrival* order and a second populated item would have failed the sweep for a reason B1 was not about. C3 replaced that scan with a sorted one (crop rank, then part rank, then path), which made the cap a gap: `-r` hydrates every item, so the widened shape is now the ordinary one. Every item in all five groups carries metadata, and the values conflict deliberately -- identical values would be permutation-invariant however the scan was written, which is exactly the shape that lets a regression pass. Lifting it also required the sweep to be able to *see* forwarded metadata: `_RecordingAnalyzers` now records each call's `original_meta` into a list of its own (`self.metas`, parallel to `calls`, kept separate because 79 call sites unpack the call tuple), the harness stashes it as `last_metas`, and `_signature` pairs each meta with its call so a value that moved between calls is a diff rather than a re-sort. Proof the lift was not cosmetic, one mutation run twice: revert C3's sorted scan to arrival order and the **old** metadata-free groups give `84 passed, 477 subtests` -- fully green, the regression invisible -- while the **new** groups give **153 subtest failures**. Group 4 fails first and hardest, since it is the one where all four items carry conflicting keywords and titles. |
| C3 | **The first pass's owed list, mostly paid.** C3 shipped the implementation and updated the three existing cases its changes falsified (`test_hydrator_injection.py` follows the rename and now expects all three items queried, since five tags are read where one was; `test_cli_input_surface.py`'s plan block gains the `read` row; `TestHydrationWarningVisibility` passes `-r`, patches the CLI's own `resolve_exiftool_path` so the pre-flight passes and the hydrator alone fails, and matches the generalized "Skipping metadata hydration" wording). The new cases then landed in `photokin/tests/test_read_flag.py` (34 tests, 30 subtests) and the hazards module above rather than in the files this row originally named, since both own harnesses a third file would have had to duplicate. Item by item: the **six date shapes** are `TestTheScanDateIsEvidenceNotTruth`'s table, run through three subtest sweeps, with `date_guess` asserted to keep the model's answer in every row; the **four title cells** are `TestTitlePrecedenceDependsOnProvenance` in both provenance modes, with the `Scanned Image` regression also pinned at the CLI seam by `test_the_flag_also_tells_the_merge_where_the_titles_came_from` and bounded by `test_the_same_read_without_the_flag_keeps_the_input_title`; **per-field fill** holds for all five keys (mutating the write loop to skip one key at a time fails 24, 8, 7, 12 and 9 tests for `dateTimeOriginal`, `userComment`, `caption`, `title`, `keywords`); the **`metadata_path` item** and the **no-metadata-key-when-nothing-read** case are `test_an_item_naming_a_sidecar_is_not_shadowed_by_the_read` and `test_a_file_holding_nothing_is_left_exactly_as_it_arrived`, joined by the non-dict-`metadata` case; **`-r` exiting 2** is one subtest sweep over all five input shapes including `--generate-manifest` and `--dry-run`; the **round trip** with its zero-ExifTool replay, its byte-identical repeat generate and **`--meta` beating `-r`** are `TestGeneratedManifestRoundTripsWhatWasRead` plus `test_the_input_still_beats_the_file`; the **direct `combine_group_metadata` invariance test** is `test_the_answer_is_invariant_under_permutation` over all 24 orderings, with `TestGroupingSurvivesEveryItemCarryingMetadata` sweeping a whole folder where every item is now metadata-bearing; and **`dateTimeOriginal` reaching the prompt** is `TestTheForwardedDateReachesThePrompt`. That last one corrects the chain this row named: `select_forwarded_metadata` feeds only the changeset's `sent_to_model` snapshot, while the prompt path is `combine_group_metadata` -> `original_meta` -> `build_prompt_bundle`, which applies the allowlist itself - and that is the chain the test walks. |
| C3 | **The last three owed items, closed; each answer measured rather than assumed.** (1) **Diff neutrality**, `TestTheChangesetDiffIsNeutralExceptWhereTheReadLands` (`photokin/tests/test_read_flag.py`, 7 tests, 10 subtests). The two modules called `diff_canonical_metadata` zero times; they now execute it **50** times, through `cli.main` with `--changeset true` so the whole chain -- hydrate, group, merge, `build_canonical_patch`, `canonical_values_from_*`, the diff -- runs untouched and the assertions are read off the NDJSON a user would. Five files, one per C3-relevant shape, each `proposed_changes` block compared whole rather than by key: the gap rule rewriting a 2019 scan date to the model's 1952 and marking it `DATE: Y~`; the same rule declining at 1955 and so naming no date **at all**, rather than proposing the file's own value back at it; `Scanned Image` losing to the transcription, which is the title narrowing visible in changeset space; and a file already holding the title, the keyword and the joined caption being proposed none of the three while the two keys it says nothing about survive. Neutrality itself is the same run with and without `-r`: every canonical key outside the four the read can move (`EXIF:DateTimeOriginal`, `XMP-dc:Title`, `XMP-dc:Description`, `XMP-dc:Subject`) must be identical on every file, the exclusion list is named rather than diffed loosely, a file holding nothing is compared with **no** exclusions, and a companion case asserts the set of keys that actually move *equals* the exclusion list, so a stale entry cannot quietly widen the excuse. `EXIF:UserComment` is measured rather than excused: `-r` reads it but `merge_original_sources` never forwards it to the before snapshot -- the pre-existing gap recorded above -- so it is one of the keys neutrality is checked on. Four mutations in a scratch copy: gap rule always overrides (3 failures), file title beats the transcription (1), `before_snapshot = {}` (6), the read widened to also supply a location (6). Kept in `test_read_flag.py` rather than beside `ManifestGroupingTestCase`'s `changeset_writer` helpers, which run the stream directly and take no hydrator and so cannot express "the same run without `-r`". (3) **`-r -w` precedence**, `TestWhichMissingExiftoolMessageAReadOrWriteRunGets` (`test_cli_preflight.py`, 3 tests, 14 subtests): all three states -- read only, write only, both -- over all three input modes, each pinned to both exact lines and exit 2 with the stream and the apply step asserted un-entered; both typing orders of the two flags; and the bound, the same unresolvable `--exiftool-path` with neither flag running clean, without which the class would only be claiming that a broken path stops any run whatsoever. Swapping the two message calls fails 5 subtests, every one of them the both-flags case -- and with the class deselected that same mutation is **fully green at 465 passed, 751 subtests**, which is what it would have shipped as. (5) **The `-r` golden earns its keep and is checked in** at `photokin/tests/fixtures/manifests/read_flag_manifest.json`, generated by this code path and never hand-written, compared byte for byte after path tokenization, beside the un-hydrated golden `test_folder_routing.py` already keeps. The value is narrow and it was measured before the file was added: reshuffling the hydrated write-back to manifest-key order -- a one-line tidy-up against the key order `hydrate.py` documents as the property that makes repeat generation byte-identical -- leaves the **entire suite green at 468 passed, 765 subtests**, because the round-trip pair compares parsed dicts (order-blind) and two outputs of the same code (which move together). With the golden it fails, alone. Indent (2 -> 4) and a dropped trailing newline fail it too, but the folder-routing golden already catches both, so the hydrated `metadata` block's key order is what the new file adds and the only thing it is claimed for. |
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
  item per group. **Settled in C3**, because `-r` makes every folder item metadata-bearing
  and would otherwise widen the gap from "manifests with two or more metadata-bearing items"
  to "every multi-file group in every folder": preferred still first, each half now sorted on
  `(normalize_path.lower(), normalize_path)`. Folder output is unchanged by the sort; manifest
  output changes only where two disagreeing items share a group, replacing an arbitrary answer
  with a stable one. The restriction on B1's permutation sweep can be lifted.
- **Section 5** - `-w` defaulting the output file into the scanned folder writes two
  artifacts into the user's photo directory and modifies every image. Photo
  directories are frequently cloud-synced, network-mounted or read-only. Worth
  deciding explicitly rather than letting it fall out of a default.
- **New** - the public API break to `analyze_folder` is not covered by the source plan
  at all.
