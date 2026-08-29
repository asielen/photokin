# Per-page captions, and long values on the command line

Implementation plan for two independent changes: giving each page of a document
its own caption instead of the whole book's, and removing the command-line
length limit that currently makes a long document unwritable.

**Target:** 0.4.0 to 0.5.0 · **Branch:** master · **Scope:** 2 parts, shippable
separately · **Breaking changes:** 1 (the caption a multipage group writes), with
a schema bump; archives already processed keep the caption they already hold

The two in one sentence each. **Part B** (ship first): ExifTool receives tag
values inline on the command line, so a transcription over roughly 20 pages
cannot be written at all on Windows — long values move into a data file that
ExifTool reads directly. **Part A**: a multipage group currently writes the whole
document's transcription into every page's `XMP-dc:Description`, which is both
wrong for the reader and 63× redundant — each page gets its own text instead,
while variant scans of one object keep sharing a block as they do today.

---

## 1. What exists today

Verified against the tree at 0.4.0 by reading and by running probes, not from
memory of it. Line numbers are that tree's.

- **One caption block per group, written byte-identically to every file.** Built
  once at `core.py:3677-3776`, assigned per file at `core.py:3925-3927`
  ("Rule 2"). This is the right rule for a print, its back and a rescan, and the
  README sells it as such: which file you open a year later is an accident of
  how you were browsing.
- **`multipage_present` already exists** (`core.py:3051`) and already means
  exactly "this group is an ordered sequence of pages rather than views of one
  object". No new plumbing is needed to tell the two cases apart.
- **The per-part material is already on every record.** `transcriptions` rides
  the canonical record and survives the fan-out (`record_for_item =
  deepcopy(canonical)`, `core.py:3915`); `resolve_part_label` already maps a
  file to its part label. The 0.4.0 feature built both. Nothing new has to be
  produced — only selected.
- **Variants of one page already share one part.** The payload is built per
  part with a list of paths, so `page2.jpg` and `page2b.jpg` both resolve to
  `Page 2` and both find that one transcription. The "variants still combine"
  half of this change needs no code at all.
- **The `.md` sidecar already prefers the per-part transcription**
  (`doc_sidecar.py:205-218`) and discloses `transcription_scope: group` when it
  has to fall back. So today, for a compliant model, **the sidecar and
  Description already disagree**: the sidecar shows page 37, Description shows
  the whole book. Part A makes them agree.
- **ExifTool values go on the command line.** `cmd.append(f"-{tag}={value}")`
  (`apply.py:117-126`), run via `subprocess.run` (`apply.py:253-255`). Measured
  on Windows against the real binary: a 32,000-character value writes fine, a
  40,000-character one raises `FileNotFoundError [WinError 206]`, caught as an
  `OSError` by the per-file handler. At ~1,500 characters per handwritten page
  that ceiling arrives at roughly **20 pages**.
- **Only `XMP-dc:Description` is realistically unbounded.** `ai_caption` reaches
  `EXIF:UserComment`, is bounded by the prompt to a few sentences, and is never
  group-merged (`core.py:3707-3712`).
- **No other subprocess site has the same exposure.** `locate.py:88` is a fixed
  three-argument command; `exiftool/manifest.py` batches short paths and already
  has its own character budget.

## 2. Decisions

| Ref | Decision | Reasoning |
|---|---|---|
| E1 | **Part B ships first, on its own.** | It fixes a shipped inability to write the exact documents 0.4.0 was built for, it is independent of Part A, and it is much smaller. Part A reduces a 63-page group's per-file value ~63-fold but does **not** subsume it: one dense page, or a heavily-inscribed non-multipage group with many variants, still builds one large inline value. |
| E2 | **`-TAG<=DATFILE`, never `-@ ARGFILE`.** | This reverses the approach first proposed, and the reversal matters. An argfile holds **one argument per line**, so a value containing newlines — which every transcription does — is silently truncated at its first line while the remaining lines are read as bogus file arguments. Measured: the write partially succeeds, persisting a truncated caption, *and* returns non-zero. That is silent data corruption. `-TAG<=DATFILE` puts only a short path on the command line and is ExifTool's documented mechanism for exactly this. |
| E3 | **Route a value to a DATFILE when it exceeds `_INLINE_VALUE_MAX` (4,000 chars), then re-check the whole command against a 30,000-char budget and route the next-longest values until it fits.** | The OS limit is on the whole command line, not on any one value, and `tags_to_write` can hold several tags. A per-value threshold alone is not sufficient; a whole-command check alone would rewrite the common short case for nothing. Threshold-gating also keeps ordinary commands human-inspectable and keeps the existing wrapper tests green unchanged. |
| E4 | **One `TemporaryDirectory` for the whole `apply_changeset` run, not a temp file per write.** | A per-write file has to be cleaned in a `finally` that a crash can skip, and collides if two runs share a temp dir. One directory, opened as a context manager around the batch loop, handles cleanup and collision in one move and leaves nothing behind on a crash. Files inside it are named per `(file index, tag)`, so a batch of 500 is deterministic. |
| E5 | **Verify the write, do not trust the exit code.** | E2's hazard is that a corrupted write can succeed *and* report an error. The apply loop already parses ExifTool's summary; the DATFILE path must also confirm the tag count written, so a truncation can never pass as success. |
| E6 | **Part A's trigger is document-ness, not size.** In a multipage group each file's caption is **its own part's transcription**; every other group keeps today's shared block. | A byte threshold makes behavior unpredictable — "why does page 20 have the whole book and page 21 doesn't?" `multipage_present` already draws the line the reasoning actually depends on: a group of views of one object versus an ordered sequence of distinct pages. |
| E7 | **The rule is per file, uniformly, within a multipage group** — a `Back` in a multipage group gets its own `Back` transcription, not the group block. | Attribution follows part-ness or it does not. A rule that says "pages get their own text but the back gets everything" would be two rules, and the back of page 3 is no more the whole book than page 3 is. |
| E8 | **A per-file multipage caption carries NO label.** | Measured against the three candidates. `[Page N]` requires teaching `_CAPTION_LABEL_RE` to match it, which re-introduces a shipped bug (see R1) — rejected outright. `[Photo N]` converges but stamps the word "photo" on a page. No label is not merely the cheapest, it is the *principled* answer: the file holds exactly one part's text, so there is nothing to tell it apart from, which is the same rule a lone scan already follows (`core.py:3627-3635`). It also converges in one run instead of two. |
| E9 | **When this file's part is not in the map — no `transcriptions` at all, a displaced or unseated file, or a partial map — the file keeps the group block, and the record says so via `caption_scope: "group"`.** | Inventing an attribution nothing supports is the one thing this codebase consistently refuses to do. Falling back to the group block preserves today's behavior exactly, and the disclosure key mirrors the sidecar's own `transcription_scope` (`doc_sidecar.py:205-218`) so an embedder can tell the two regimes apart rather than guessing from length. Logged once per group at INFO, naming the group. |
| E10 | **`transcriptions` keeps riding every file whole. Do not trim it.** | `doc_sidecar.py:205` reads this file's entry out of the whole map, and `docs/document-mode-contract.md:40-45` freezes it. Trimming it to one entry per file is the obvious "while we're here" optimization and it would break the sidecar. Stated here so nobody does it. |
| E11 | **Bump `_NDJSON_SCHEMA_VERSION`, and bump `changeset.SCHEMA_VERSION` while writing the bump criterion it never had.** | `cli.py:101-108` states the rule in its own words: changing what an existing key *means* requires a bump even when its shape is unchanged. `caption` goes from "the group's whole transcription, on every file" to "this file's part". `changeset.py` carries no such criterion anywhere — the plan writes one mirroring `cli.py`'s rather than making a silent judgment call. |
| E12 | **No migration. An archive already processed under 0.4.0 keeps the group block it already holds, on every file, permanently.** The change applies to what is written from here on. One sentence in the README and the CHANGELOG says so; nothing in the code goes looking for old captions. | Maintainer decision 2026-08-28. The alternative was a lever that could only ever fire when the model re-transcribed a page identically — and 0.4.0's own prose-flow change makes rewording likely on exactly the first re-run after upgrading, so it would have been machinery that mostly did not fire, in the most dangerous function in the codebase. The consequence is accepted and worth stating: a folder re-run after upgrading holds thin captions on newly analyzed documents and fat ones on old, and photokin will not reconcile them. Clearing `XMP-dc:Description` is the user's own act if they want it. |

## 3. Sequence

### Part B — long values off the command line (0.4.1)

Small, self-contained, no behavior change anyone would notice except that a run
which used to fail now works.

**The change.** `_build_exiftool_command` gains a way to render a tag as
`-TAG<=<path>` instead of `-TAG=<value>`, chosen per E3. `apply_changeset` opens
one `TemporaryDirectory` around its batch loop (E4) and writes each routed value
into it as UTF-8 without a BOM. Only the short path reaches the command line;
because the command is an argv list with no shell, the `<` in `-TAG<=` needs no
quoting — ExifTool's own docs warn about shell redirection, which does not apply
here, and that is worth a comment so nobody adds quotes later and breaks it.

`-charset` is **not** needed: ExifTool's default value charset is already UTF-8.
`-charset filename=utf8` becomes necessary only if a DATFILE path itself is
non-ASCII, which the temp directory makes controllable; note it and keep paths
ASCII.

**Exit:** a 40,000-character Description writes successfully and reads back
byte-identical, including newlines, non-ASCII and the `~~`/`_`/`>` marks the
0.4.0 conventions produce; a short value still takes the inline path and the two
existing wrapper assertions pass unmodified; a batch of files leaves no temp
files behind, including when one file's write fails; the truncation hazard is
covered by a case that asserts round-trip equality rather than exit status (E5).

**Risks.** Low, and bounded by the threshold: values under 4,000 characters take
exactly today's path. The one real hazard is E2's, and it is avoided by not
using an argfile at all.

### Part A — each page carries its own caption (0.5.0)

**The change.** The block build at `core.py:3677-3776` becomes a function of
(intake set, fresh transcription). For a non-multipage group it is called once
for the group, exactly as today. For a multipage group it is called **once per
file**, with two differences:

- the intake sweep reads **only that file's own** existing caption, not the
  group's (`core.py:3698-3705` currently sweeps all of them — leaving that sweep
  group-wide would hand every page every other page's stored text on the first
  `-rw`, and from then on that text is the file's own stored caption, which
  loses the entire point of the change);
- the fresh transcription is that file's part, resolved via `resolve_part_label`
  and looked up in `transcriptions`, with no label (E8).

**One hoisting job.** `resolve_part_label`'s inputs are currently computed only
inside the `cfg.sidecar_md != off` branch (`core.py:3876-3891`), so
`sidecar_relabelled_versions` defaults to an empty frozenset when sidecars are
off. Reusing that call for captions without hoisting the computation would
mis-resolve the untagged file that became Page 1 on every run with sidecars off
— a silent wrong answer on the default path. Hoist first, in its own commit.

**Docs.** The byte-identical rule is stated in three places, all of which must
be scoped rather than deleted: `README.md`'s "Captions" section (its "The shape"
and "How it is built" subsections), `photokin/README.md`'s embedder contract
(the most explicit promise, and the one an integrator would have coded against),
and `docs/document-mode-contract.md` §3, whose decision not to re-section
multipage captions this plan partially supersedes and must say so by name.
`CHANGELOG.md`'s 0.4.0 "byte-identical" claim is about formatting marks, not
document scope, and stays true — arguably becomes truer.

**Exit:** a stubbed multipage group gives each file only its own page's text and
every file a different caption; a front/back/variant group is byte-identical to
0.4.0 including the existing permutation-invariance and one-block tests; three
consecutive `-rw` runs over a multipage group are byte-identical from run 1
(E8's convergence, measured); a group whose reply carries no `transcriptions`
keeps the whole block and carries `caption_scope: "group"`; a displaced file
does the same; the `.md` sidecar and `XMP-dc:Description` cover the same page
for a document, where before the sidecar showed page 37 and Description showed
the whole book. Agreement is on scope, not forever on content: Description is
merged and a sidecar is overwritten, so a re-run that rewords a page leaves
both readings in Description and only the newest in the sidecar — the ordinary
caption-merge rule, not an exception to it.

## 4. Risks

**R1 — the label change that looks obvious is unsafe, and was measured to be.**
E8 chose no label for a per-page caption, and the natural objection is "why not
just `[Page N]`?" Because that requires teaching `_CAPTION_LABEL_RE`
(`core.py:269`) to match it, and doing so turns a letterhead repeated across two
pages into a cross-section duplicate, after which the section-scoped line dedup
deletes the second one. This is not hypothetical: the existing regression test fails with
`1 != 2 : the letterhead ... was deduplicated down to one occurrence, losing
real text`. That test exists because this was a shipped bug three weeks ago.
**Do not touch the regex.** `docs/document-mode-contract.md:113-116` states it
as a contract clause; this plan reaffirms it rather than superseding it.

**R2 — an already-processed archive keeps its fat caption, permanently.** Not a
risk to be mitigated; a consequence accepted under E12, recorded here so the
behavior is not mistaken for a bug later. Measured over five successive runs: a
multipage archive processed under 0.4.0 holds the group block in every file's
Description, and under the per-file rule that block is a **stable fixed point**.
It re-reads as one section (because `[Page N]` is not a label), and this file's
own fresh page text is then dropped by the section-scoped dedup as a repeat of
text already in section 0. Re-running does not converge it and is not meant to.

What this costs, plainly, because the README has to say it in a sentence: a
folder re-run after upgrading ends up mixed — documents analyzed from 0.5.0 on
carry per-page captions, ones analyzed earlier carry the whole book, and nothing
reconciles them. Clearing `XMP-dc:Description` for those files is the user's own
act. A `--remigrate-captions` command that could do it safely is §5's, not this
plan's.

**R3 — mixed regimes inside one folder.** A partial map (pages 1-40 answered,
41-63 not) leaves some files with a thin per-page caption and others with the
fat group one. E9's `caption_scope` key makes it legible per file rather than a
mystery, and the INFO line names the group, but the folder is genuinely
inconsistent and the README should say so.

**R4 — whether a document gets per-page captions depends on the model filling an
optional field.** `transcriptions` is optional by design (0.4.0's tolerant
fallback). A provider that ignores it gets group captions and no error. That is
the correct failure direction, but it is a user-visible inconsistency with
nothing in the *file* explaining it — which is what `caption_scope` is for.

**R5 — Part A is a deliberate reversal of a protected invariant.** "Byte-identical
blocks group-wide" is named in `docs/document-mode.md`'s build plan as one of
three caption invariants the implementer was specifically assigned to preserve,
and it is asserted by a shared test helper (`_CaptionBlockTestCase.one_block`)
used across many cases. Reversing it for one group shape is defensible; doing so
without reading every one of those tests first is not. The list is in §6.

## 5. Deferred, deliberately

| Item | Why deferred | Where it plugs in |
|---|---|---|
| `--remigrate-captions` | E12 takes no migration at all, so there is nothing half-built to finish; if the mixed archive R2 describes ever becomes annoying enough, this is where the fix goes | A pass over a group's stored captions that drops only sections this tool can prove it wrote, never a section a human may have touched |
| Master transcript per document (D4, still) | Per-page captions make it more wanted, not less: the whole-document view now lives only in the sidecars | A pure function over one group's sidecar data; no model call |
| Trimming `transcriptions` per file | E10 — the sidecar reads the whole map | Nothing; this is a note not to do it |
| A flag to keep the old group-wide caption for documents | `--group-by none` already gives per-file captions by a blunter route; nobody has asked for the inverse | One value on a future `--caption-scope` flag if ever wanted |

## 6. Build plan

Part B and Part A are independent and touch disjoint files; Part B ships and is
tagged before Part A starts, so a caption regression can never be confused with
a write-path regression.

| ID | Workstream | Files owned | Depends on |
|---|---|---|---|
| B1 | DATFILE routing + temp-dir lifecycle + round-trip tests | `photokin/exiftool/apply.py`, `photokin/tests/test_exiftool_wrapper.py` | — |
| A0 | Hoist `resolve_part_label`'s inputs out of the sidecar gate | `core.py` (emit loop only) | B1 tagged |
| A1 | Per-file caption build and `caption_scope` | `core.py` (block build + emit loop) | A0 |
| A2 | Schema bumps + the missing changeset bump criterion | `cli.py`, `changeset.py` | A1 |
| A3 | Tests: rewrite the pinned ones, add per-page convergence cases, and pin R2's accepted behavior so it reads as a decision rather than a bug | the test files in the list below | A1 |
| A4 | Docs: README "Captions" carve-out, `photokin/README.md` embedder contract, contract-doc supersession note, CHANGELOG, version | `README.md`, `photokin/README.md`, `docs/document-mode-contract.md`, `CHANGELOG.md`, `pyproject.toml` | A1 |

**Tests that pin the current behavior and must be read before A1 is written.**
These break or need scoping; the list is exhaustive as of 0.4.0:

- `test_read_flag_hazards.py::_CaptionBlockTestCase.one_block` (:332) — the
  shared helper asserting one block per group; needs a multipage-aware variant
  rather than deletion
- `::TestTheBlockIsTheWholeGroupsStory::test_pages_are_told_apart_by_number_not_a_letter_none_of_them_have` (:515) — the one that breaks outright
- `::TestTheCaptionBlockIsPermutationInvariant::test_all_twenty_four_orderings_give_one_answer` (:833) — must keep holding for non-multipage
- `::TestCaptionJoinIsIdempotent` (:927, :949, :961) — the README's "running it
  twice does not grow your captions" promise, across every grouping
- `test_transcriptions_contract.py::TestStylingMarksRoundTrip::test_the_synthesized_caption_reaches_every_file_unchanged` (:340) — multipage; breaks
- `test_review_regressions.py::TestCaptionLineDedupIsScopedPerSection` (:122) —
  breaks at its `len(set(first_captions.values())) == 1` assertion
- `test_group_by.py::_F4153AE_RECORDS` (:452) — a frozen record snapshot;
  non-multipage, should survive untouched, and confirming that is the point
- `test_doc_sidecar.py` (:231, :261) — pin the sidecar's group fallback; unchanged
  by this plan but they are the model `caption_scope` follows

**Why the order.** A0 is a one-line hoist that fixes a latent wrong answer on
the default path and is worth landing alone. A1 is the only place cross-file
caption behavior changes, and this repo's history says that is where defects
ship green — it wants the adversarial pass, not the mechanical one.
