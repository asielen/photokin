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

Any group whose primary front is absent is skipped with no warning. That is every
multipage or album set and every negative-only set. The completion line then reports
only what was processed, so the run looks clean and the count looks plausible.

`core.py:913-914` · count at `core.py:929` · album pages documented at `README.md:50`

**Status after Phase A:** the silence is gone, the loss is not. Every skipped group
now logs its reason and the completion line carries a skipped count
(`core.py:979-993`, `core.py:1041-1048`), so the run no longer looks clean. The
groups still go unanalyzed until Phase B routes folder input through the manifest
pipeline, and the README now carries that caveat at the point where it used to
promise otherwise (`README.md:240`, `README.md:254`).

Verification, real grouper against a fixture folder:

```
GROUP 'album': primary.front=None  pages={1: album-page1.jpg, 2: album-page2.jpg}
GROUP 'box3_025': primary.front=box3_025.jpg
GROUP 'neg':   primary.front=None  negative=neg-negative.jpg

--- groups analyze_folder would SKIP (primary.front is None) ---
  SKIPPED: album -> ['album-page1.jpg', 'album-page2.jpg']
  SKIPPED: neg   -> ['neg-negative.jpg']
```

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
mode is unaffected in outcome only because `group_folder_images` keeps crops in their own
slots and never analyzes them at all.

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
| `core.py:915` | `--process-all-variants` is dead in folder mode; the group path is never reached. |
| `config.py:14-19` | `EXIFTOOL_WRITE_ENABLED=""` silently resolves to false, as does any unrecognized value. |
| `cli.py:310` | A `.json` output file yields a generic `changeset.ndjson` that collides across runs. |
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
won the master pick, so a `preferred` versioned back no longer files the front's `PC*`
codes against its own variant. Slot claimants are addressed by resolved path, so a
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

**Still owed by B2:** folder and single-photo routing through `process_manifest_stream`,
`--generate-manifest`, the `analyze_folder` return shape, retiring or rewrapping
`group_folder_images`, and the richer variant map. Note for B2's parity goldens: manifest
mode addresses negatives per variant, `group_folder_images` bins them at stem level with
an unconditional assignment (`utils.py:1402-1407`), so two negatives differing only by
variant letter overwrite each other there. The per-variant form is the one to encode.

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
- CHANGELOG, version bump, tag.

**Exit:** all three inputs read identically; nothing is written without an explicit
opt-in; every error case in the message set has a test asserting exit code and first
line.

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

### 2. `analyze_folder` return shape

Split across two phases, because half of it has already shipped.

**Already in, from Phase A:** the return value is `{"results": ..., "errors": ...}`
instead of `{"results": ...}`. Additive, so embedders reading `["results"]` are
untouched, which is why it was allowed to land with the error isolation that needed
it rather than wait for Phase B. The rider is behavioral rather than structural: a
per-group failure is now collected instead of propagated, and the call raises only
when no group succeeded at all.

**Still owed by Phase B:** unification changes the result from one entry per group
to one per file, and records gain merge reports, scoped keywords and a richer
variant map. That is the half no caller can absorb by reading `["results"]`, so this
entry stays on the list. The function is re-exported from both `public.py` and
`__init__.py`, so it breaks embedders independently of anything on the CLI.

### 3. Deprecations

`--folder` and `--manifest` keep working until 1.0 and emit a one-time note. No
removal in this release.

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
| C | Detection matrix: directory, `.json`, image; deprecated aliases still work; positional plus alias conflicts; `--back` with a folder errors. |
| C | Every error case asserts exit code and first line. `-w` expands correctly in all modes, explicit flags override the expansion, and the contradictory combination errors. |
| C | Regression for the default flip: with no write flags, nothing is written in any mode. |

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
