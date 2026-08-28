# Document Mode

Implementation plan for markdown transcript sidecars, formatting-aware transcription,
and large-document chunking.

**Target:** 0.3.x to 0.4.0 · **Branch:** master · **Scope:** 3 phases + 1 deferred track ·
**Breaking changes:** 0 (one model-contract extension with a tolerant fallback)

The feature in one sentence: when photokin analyzes a scanned document, it can also
write one `.md` sidecar per file — YAML frontmatter carrying the group's shared
metadata, body carrying that page's own formatting-aware markdown transcription —
triggered manually or by the model's own `category` verdict, and large documents are
chunked into multiple model calls with a final consolidation pass instead of one
oversized payload.

---

## 1. What exists today (what the plan builds on)

Verified against the tree at 0.3.2, not from memory of it.

- **One model call per group.** `object` grouping sends every page, back, variant and
  negative of one physical object in a single call — `analyze_photo` for a plain
  front/back pair, `analyze_group_parts` (`core.py:955`) for anything larger. Parts are
  labeled `Front` / `Back` / `Page N` / `Negative` and the model is told the part
  order (`core.py:1068-1095`). There is no upper bound on payload size: a 63-page
  document would be one call with 63 images.
- **One record per group, fanned out per file.** The response is a single record keyed
  by the primary front. `process_manifest_stream` deep-copies it to every file of the
  group (`core.py:2857-2922`); the only per-file differences are the part-marker
  keyword (`back`, `negative`) and merged per-file metadata. **This is already the
  metadata model document mode wants**: keywords, date, location, `ai_caption`,
  `title` and `category` are group-level; nothing per-page needs inventing there.
- **The transcription is one string.** `caption` is the whole group's verbatim
  transcription in one JSON string with bracket-labeled sections (`[Page 1]`,
  `[Back]`, …). The caption-block machinery — `_split_caption_sections`
  (`core.py:422`), `_absorb_caption` (`core.py:2694`), near-identical dedup
  (`core.py:320-344`) — already splits, files and de-duplicates *by section label*.
  What it cannot do is attribute a section to a *file* after the fact when the model
  merged variants, which is why the plan moves per-part transcription into the
  response contract rather than parsing it back out.
- **The trigger signal already exists.** `category` is a required response field and
  its vocabulary already contains `Document`, `Postcard` and `Photo Page`
  (`prompts_photo_ai/categories.txt`). Auto mode needs no extra call and no new
  model question — it gates on the answer the run already paid for.
- **Sidecar plumbing exists for JSON.** `_write_sidecar_document` (`core.py:652`)
  writes `<stem>.json` beside the image, warns instead of raising, and never takes a
  paid-for analysis down with it. The `.md` writer copies that contract exactly.
- **All providers, one dispatch.** `api.call_model` is a synchronous request/response
  dispatch over OpenAI, Anthropic, Gemini and OpenRouter. There is no provider-side
  agent or fan-out primitive shared across them — see §4 for what that means for
  chunking.

## 2. Decisions

Settled with the maintainer 2026-08-27 (D1–D4, D12) or taken here from the codebase's
own conventions (D5–D11).

| Ref | Decision | Reasoning |
|---|---|---|
| D1 | **Full markdown conventions everywhere.** One transcription logic for photos and documents; the styling marks (`~~struck~~`, `_underlined_`, blockquoted margin notes) reach the EXIF caption byte-identically with the sidecar. | Maintainer choice. `~~x~~` and `_x_` read acceptably as plain text; two renderings of one transcription would be a second thing to keep honest. Accepted consequence: re-runs over an already-processed archive produce transcriptions that differ from the stored ones — see the Phase 1 risk on the 0.85 near-identical threshold. |
| D2 | **Auto mode triggers on `category ∈ {Document, Postcard}`.** | Maintainer choice. The two text-first categories. `Photo Page` (album pages of mounted photos) stays photo-like; a Portrait with a handwritten back gets no sidecar in auto mode. Held in one frozenset so widening it later is a one-line change. |
| D3 | **Chunk size 8; groups of ≤8 images keep today's single-call behavior unchanged.** | Maintainer choice, matching what worked on the 63-page memoir (8 blocks of ~8 contiguous pages). Configurable; `0` disables chunking. This must keep fronts and backs together however if photos are included in chunking. Never split a front and a back into separate chunks. |
| D4 | **No master transcript in v1.** Per-file sidecars only. | Maintainer choice. Every sidecar carries its page number, page count and group identity in frontmatter, so a master is trivially derivable later; deferred to §5. |
| D5 | One flag, three values: `--sidecar-md {off,auto,all}`, default `off`, valid for every input type. Spelling per D12. | The unified pipeline (0.2.0) means single-file, folder and manifest input all flow through `process_manifest_stream`, so "a CLI flag for a single file" and "modes for a manifest" are the same flag. Default `off` preserves every existing run byte-for-byte. A value-taking flag rather than a bare switch, for the same Lua-argv reason as `--changeset` (unified-input-pipeline.md Q5). |
| D6 | Per-part transcription enters the **response schema** (`transcriptions` keyed by part label); the caption block is then *built* from it deterministically, not asked for twice. | The alternative — parsing the merged `caption` back into per-file pieces — fails exactly where document mode matters: the model is currently told to merge duplicate text across variants, so attribution is lost at generation time. Synthesizing the caption block from labeled parts reuses `_absorb_caption`'s own section grammar and reproduces today's labeling rules (a lone scan stays unlabeled). Tolerant fallback: a response with `caption` and no `transcriptions` is accepted and handled exactly as today, so a model that ignores the new field degrades to current behavior instead of failing. |
| D7 | The `.md` sidecar is written in the **emit loop**, after `merge_metadata`, from the merged record. | That is the one point that is per-file, post-merge, and after `category` is known — which makes manual and auto mode the same writer with one gate, and makes file creation deterministic and independent of the model call structure (the maintainer's stated requirement). |
| D8 | Sidecar path is `<image stem>.md` beside the image, mirroring `<stem>.json`. | Same derivation as `_write_sidecar_document`; nothing new to document. Same failure contract: warn, keep the record, never fail the group. |
| D9 | Crops get no sidecar; every other file with a record gets one (backs and variant rescans included — a rescan's sidecar carries its part's transcription under its own filename). | A crop is "a supporting view of its parent, never its own object" (README) and is never analyzed; its sidecar would duplicate the parent's byte-for-byte. Easy to flip later if wanted. |
| D10 | Chunked calls run **sequentially** in v1; the consolidation pass is **text-only** (no images re-sent). | Sequential first because it is deterministic, debuggable, and rate-limit-safe across four providers with four different limit models; a 63-page document is 9 calls, which is tolerable serially. The memoir workflow's ordering pass worked from transcripts alone, so the consolidation input is the per-page transcriptions + filenames + per-chunk provisional guesses, not another pass over the pixels. Parallelism is §5. |
| D11 | Page-order findings are **recorded, never acted on**. The consolidation pass may conclude the filename order is wrong; photokin writes the corrected number into the sidecar and the record and logs a warning. It does not rename, reorder, or renumber files. | Renaming is destructive, cross-tool (Lightroom catalogs reference paths), and a separate feature. Deep-doc's rename-script delivery is noted in §5. |
| D12 | **Sidecar flags are named by format and share one mode vocabulary.** `--sidecar-md {off,auto,all}` now; `--sidecar-xmp` and `--sidecar-json` are **reserved spellings** — XMP for standard metadata sidecars when they arrive, JSON for the day `--output-sidecars` is folded in as an alias of `--sidecar-json all`. Config fields mirror the grammar (`sidecar_md`, later `sidecar_xmp`). | Maintainer requirement 2026-08-27: the CLI must not need re-parameterizing when standard XMP sidecars land, and the names must stay obvious. Naming by feature ("doc") ties the flag to one use — XMP would either squat on it or sit beside it inconsistently; naming by format keeps the family self-evident, sorts `--sidecar-*` together in `--help`, lets each format default its own mode, and makes `auto`'s category gate one shared frozenset every format reads. Record the reserved spellings in the README flag table the way `-R` was reserved (unified-input-pipeline.md Q2). The same lens renames the Phase 3 knob: `--max-images-per-call`, not `--doc-chunk-size`, because the trigger is payload size, not document-ness — it applies to any oversized group. |

## 3. Sequence

Three phases, each shippable alone, ordered so the model-contract change lands and
settles before anything depends on it. Phase 2 does not require Phase 3: auto mode
works on unchunked groups (they are simply one big call, as today).

### Phase 1 — One transcription contract: formatting-aware, per part

Prompt and schema work; no new flags, no new files written. This phase deliberately
changes output for **every** run, because that is the requirement: photo and document
transcription are the same logic.

**1a. Transcription conventions (prompt files).** Fold the manuscript-workflow
conventions (docs/instructions for deep doc.md) into the shared prompt resources —
`image_rules.txt`, `instructions_front_back.txt`, `system_header.txt`,
`forbidden_inferences.txt` — as one conventions block stated once and referenced
everywhere, not restated per file:

- Fidelity rules already present stay: verbatim spelling/punctuation/grammar, no
  modernizing, no expanding abbreviations, bleed-through ignored, `[ ]` for guesses.
- New: crossed-out text as `~~struck~~` followed by its replacement; underlines as
  `_underlined_`; inserted/caret text placed where intended, `[inserted]` when
  ambiguous; margin notes as `> [margin note] …` blockquotes at the point they refer
  to; footnotes after a `---` rule keeping the author's key symbol; `[illegible]` /
  `[illegible ~N words]` / `{word?}` uncertainty markers; `[blank page]` for blank
  parts.
- New, and the sharpest behavior change: **prose flows within a paragraph.** Physical
  line-wrap breaks are not reproduced; deliberate breaks (lists, poems, addresses,
  sign-offs, letterheads) are. Today's rules say "preserving line breaks"
  unconditionally, which is right for postcard backs and wrong for a letter's prose.

**1b. Per-part transcriptions (schema).** `output_format.txt` gains an optional
`transcriptions` object keyed by the exact part labels the prompt assigned
(`"Front"`, `"Back"`, `"Page 1"`, …); the instruction for `caption` becomes "omit it
when `transcriptions` is present". Parsing (per D6):

- `transcriptions` present → the fresh caption string is **synthesized**: one part →
  bare text, no label (preserving the lone-scan-carries-no-label rule,
  `core.py:2617-2680`); multiple parts → `[Label]\ntext` sections in part order. The
  synthesized string enters `_absorb_caption` exactly where the model's `caption`
  does today (`core.py:2734-2740`), so the group caption block, its dedup and its
  merge behavior are all unchanged in shape.
- `transcriptions` absent → today's path verbatim. No retry is spent demanding the
  field.
- The per-part map rides the canonical record (as `transcriptions`, kept through the
  fan-out beside `all_variant_files`) so Phase 2's writer can address it. Each file
  resolves to its part label from what the bucket loop already knows
  (`_manifest_part_key`, `core.py:1783`, after the page-1 relabel); variant rescans
  of one part share that part's transcription, which is correct — they are scans of
  the same physical page.

**Exit:** the checked-in fixture folder produces byte-identical caption blocks
through the synthesized path and the fallback path with a stubbed model; a stub
returning styling marks round-trips them untouched through merge and changeset; all
existing caption/merge/read-flag tests pass unmodified (they stub `caption` and land
on the fallback path, which is itself the proof the fallback works).

**Risks.** (1) Model compliance with the new field varies by provider; the fallback
bounds the damage at "current behavior". Verify once per provider with a live
smoke-run before release. (2) *Re-run drift, the accepted consequence of D1:* an
archive processed under the old conventions and re-run with `-rw` produces
transcriptions that differ (flowed line breaks, styling marks). Where the difference
stays above the 0.85 near-identical ratio (`core.py:320`) the old text is silently
superseded in the block; below it, both are kept side by side — the documented
reworded-transcription behavior (`test_read_flag_hazards.py:975`). State this in the
CHANGELOG plainly rather than discovering it in an issue. (3) Blockquote `>` lines
and `---` rules now legitimately appear inside caption strings; audit
`_split_caption_sections` and the empty-section stripper (`core.py:220`) for any
assumption that a caption line starting with punctuation is noise.

### Phase 2 — The sidecar writer and its trigger

**The writer.** `write_markdown_sidecar(merged, item, group_info, config)` in a new
module `photokin/doc_sidecar.py` — its own module rather than another 100 lines of
`core.py`, which also lets it be built in parallel with the Phase 1 work (§6) — with
`_write_sidecar_document`'s exact contract (warn on `OSError`, return path or
`None`), called from the emit loop after `merged["all_variant_files"]` is attached
(`core.py:2901`). Layout:

```markdown
---
source_file: box3_017-page2.jpg
group: box3_017
part: Page 2
page: 2                # corrected number if Phase 3 disagreed with the filename
page_count: 6
group_files: [box3_017-page1.jpg, box3_017-page2.jpg, ...]
title: ...
category: Document
keywords: [...]        # merged, part markers included — this file's real keyword set
date: 1944-11-27       # date_guess: iso, pattern, confidence
date_pattern: "Y!M!D!"
date_confidence: 0.95
location: {country: France, city: Le Mans, confidence: 0.9}
analyzed_by: Claude claude-sonnet-5 (2026-08-27)
---

# <title, or the group id when title is null>

[AI Analysis]: ...     # group-level, verbatim from ai_caption

## Transcription — Page 2

<this part's markdown transcription, verbatim from transcriptions>
```

Frontmatter values come from the **merged** record, so they reflect exactly what the
run concluded for that file (same values the changeset would write) — group-shared
by construction, since the record is the fanned-out canonical. The transcription is
this file's part only. When the record has no `transcriptions` (fallback path), the
body falls back to the whole caption block under a `## Transcription` heading with a
frontmatter note `transcription_scope: group` — honest about what it is rather than
pretending attribution that was never made.

**The trigger.** `Config.sidecar_md: str = "off"` + `--sidecar-md {off,auto,all}`,
named by format rather than by feature (D12) so `--sidecar-xmp` slots in beside it
later without touching this flag:

- `off` — today, nothing new written.
- `all` — every emitted file except crops (D9), any category. This is "manual mode"
  and the single-file trigger: `photokin letter.jpg --sidecar-md all`.
- `auto` — same writer, gated on the group's merged `category` being in
  `SIDECAR_AUTO_CATEGORIES = frozenset({"Document", "Postcard"})` (D2). Group-level
  gate, per-file writes. The frozenset is format-neutral on purpose: a future
  `--sidecar-xmp auto` reads the same gate rather than growing its own.

Orthogonal to `--output-sidecars` (JSON) and to `-w`; no changeset interaction —
sidecars are additive files, and a re-run overwrites them (they are derived output,
like the JSON sidecar, not user data). `--output-sidecars` is left untouched now and
folds into the family later as `--sidecar-json` (D12).

**Docs.** README gets a short section in the basics ("get a readable transcript file
per page") with the flag's three values, and the advanced detail (auto's category
set, crop exclusion, frontmatter shape) lower down — README structure and its
enforcement by `photokin/tests/test_docs_alignment.py` both apply. The flag table
records `--sidecar-xmp` and `--sidecar-json` as reserved spellings (D12), the way
`-R` was reserved for recursion.

**Exit:** fixture run under each of the three values produces the expected file set
(golden-file the `.md` for one front/back pair and one multipage group); `auto`
writes nothing for a Portrait group and a full set for a Document group with a
stubbed category; an unwritable `.md` destination warns and the record survives;
`--sidecar-md` rejects unknown values at argparse level.

**Risks.** Low. The writer is deterministic and post-model. One real decision is
YAML escaping — transcribed titles and keywords contain colons, quotes and brackets;
emit frontmatter through a tiny always-quote serializer rather than hand-joined
strings, and test with hostile strings. (No YAML library dependency: the subset
emitted — scalars, flat lists, one flat map — does not need one.)

### Phase 3 — Large documents: chunked calls + consolidation

The scaling half. Trigger: after slot resolution, a group whose model-bound payload
exceeds `max_images_per_call` (Config default 8, `--max-images-per-call N`, `0`
disables) takes the chunked path; everything at or under it takes today's single
call untouched (D3). The flag is named for what it constrains, not for document
mode (D12): the trigger is payload size, not category, so an oversized group of
photo variants chunks the same way.

**Chunking.** Parts are already an ordered list of `(label, [paths])`. Split into
contiguous blocks of ≤`max_images_per_call` *images* on part boundaries, with two
cohesion rules the partitioner enforces rather than hopes for: a part's variants
never straddle chunks, and **a front and its back are never split into separate
chunks** (D3) — the pair travels together even when that makes a chunk run one
image over the target. Non-page parts (Front/Back/Negative) ride in the first chunk.
Each chunk is one `analyze_group_parts`-shaped call carrying the standard prompt
bundle plus a chunk note: which pages of how many it is seeing, that the object
continues beyond the payload, that transcription conventions are the Phase 1 ones,
and that its metadata guesses are provisional. Contiguity matters for the same reason
it did in the memoir workflow: consecutive pages let the model notice continuity
within its block for free. Chunk calls run sequentially (D10); a failed chunk call
(after the existing per-call retries) fails the group exactly like a failed single
call — per-group isolation already handles it.

**Consolidation.** One final **text-only** call per chunked group. Input: every
part's transcription with its filename and filename-derived page number, plus each
chunk's provisional `keywords` / `title` / `category` / `ai_caption` / `date_guess` /
`location_guess`. Output, same strict-JSON discipline as the main schema:

- the final group-level metadata (one value per field — this replaces the
  per-analysis best-confidence pick for chunked groups; unchunked groups keep the
  existing `_best_guess` logic untouched),
- a page-order verdict: for each file, the corrected page number, plus flags for
  suspected missing pages and out-of-order scans, judged by narrative flow across
  chunk boundaries the way the memoir workflow's ordering pass did — text ends
  mid-sentence, next page completes it,
- nothing else; transcriptions are **not** re-emitted or revised by this pass. The
  per-chunk transcriptions are the evidence; letting a text-only pass rewrite them
  would launder the fidelity the conventions exist to protect.

The verdict lands as data, not action (D11): `page` in each sidecar's frontmatter,
a `page_order` map plus any anomaly flags on the record (additive key beside
`all_variant_files`), and a WARNING naming the group when the corrected order
disagrees with the filenames. Usage sums across chunk calls + consolidation into the
group's `_usage`, which the existing summation shape already supports
(`core.py:2765-2783`).

**Cost, stated plainly.** For a group of P pages: today 1 call with P images;
chunked, `ceil(P/8)` calls with ≤8 images each plus one text-only call. Total images
sent is identical; the added cost is the repeated prompt bundle per chunk plus the
consolidation tokens. What it buys: per-page attention that does not degrade with
document length, payloads that stay inside every provider's request-size limits, and
partial progress that is at least diagnosable when call 7 of 9 fails.

**Exit:** a stubbed 20-page group produces 3 chunk calls with the right contiguous
part assignments and one consolidation call; the sidecars carry consolidated
metadata and corrected page numbers; a fixture with deliberately misnamed page files
gets the right `page` frontmatter and the order warning; a fixture whose front/back
pair sits at a chunk boundary keeps the pair in one chunk; a ≤8-image group's call
sequence is byte-identical to Phase 2's; `--max-images-per-call 0` restores
single-call behavior at any size.

**Risks.** Medium — this is the phase that changes call structure. (1) The
consolidation prompt is new surface with its own schema; keep its parser as tolerant
as the main one (missing page-order verdict → fall back to filename order, log it).
(2) Chunk-boundary duplicate text: a sentence spanning pages 8→9 appears in two
chunks' transcriptions; the caption-block line-level dedup (`core.py:2748-2762`)
absorbs exact repeats, and the conventions tell the model to transcribe the page, not
complete it — verify on a real document before release. (3) Provisional per-chunk
metadata can disagree wildly (chunk 1 sees the letterhead, chunk 5 sees only prose);
the consolidation prompt must be told to weigh evidence-bearing pages, not vote.

## 4. The multi-provider orchestration question, answered

Can the memoir workflow's "spin up an agent per batch of pages" be done through API
calls? **Not as a provider service, no — and photokin should not want it.** What the
Claude-Code workflow calls an agent is a client-side loop; none of the four providers
exposes a portable server-side fan-out primitive. The honest inventory:

- **Plain concurrent calls** — universal. Chunked calls through the existing
  `call_model` can run in a `ThreadPoolExecutor`; the SDKs are thread-safe for
  request/response use. This is the only portable "agents" there is, photokin manages
  it itself, and it is deliberately deferred (D10): four providers means four
  rate-limit regimes, and sequential chunking already fixes the payload problem —
  parallelism only fixes wall-clock.
- **Provider batch APIs** (OpenAI Batch, Anthropic Message Batches, Gemini batch
  mode) — real and ~50% cheaper, but asynchronous with minutes-to-hours turnaround,
  a different transport per provider, and no OpenRouter equivalent. Wrong latency
  profile for an interactive CLI run; right shape for a future `photokin --queue`
  overnight-archive mode. Noted in §5, out of scope here.
- **Provider agent frameworks** (Assistants/Responses-with-tools, Claude agent SDKs)
  — non-portable and would invert the architecture: photokin's pipeline is the
  orchestrator; the model is a function it calls.

So the answer to "do we have to manage that on our own?" is yes — and Phase 3's
sequential chunk loop *is* that management, with the executor as a later drop-in
inside one function once it proves out.

## 5. Deferred, deliberately

| Item | Why deferred | Where it plugs in |
|---|---|---|
| Master transcript per document (D4) | Derivable from sidecars; keeps v1 scope tight | A pure function over one group's sidecar data; no model call |
| Standard XMP sidecars | Not wanted yet; D12 keeps the seam open so adding them changes no existing parameter | `--sidecar-xmp {off,auto,all}` + an XMP serializer over the same merged record the md writer reads; `--output-sidecars` folds in as `--sidecar-json` |
| Parallel chunk calls | Sequential proves the seam first; rate limits differ per provider | `ThreadPoolExecutor` around the Phase 3 chunk loop, `--max-parallel-calls N` |
| Provider batch APIs | Hours-scale latency; per-provider transports | A separate submit/poll/collect mode, not a change to this pipeline |
| Rename script for misordered pages | Renaming is destructive and catalog-hostile (D11) | Generator over the Phase 3 `page_order` verdict, memoir-workflow style |
| Widening `SIDECAR_AUTO_CATEGORIES` / per-run override | Ship the agreed set first | One frozenset shared by every format's `auto`; a `--sidecar-auto-categories` flag if ever needed |
| Sidecars for crops (D9) | Byte-duplicate of the parent's | Flip the one exclusion in the emit-loop gate |

## 6. Build plan — parallel workstreams and model assignments

§3 is the *shipping* order; the *build* dependencies are looser. Two facts shape the
parallelization, and everything else follows from them:

1. **The one real coupling is the `transcriptions` interface** — the record key, the
   part-label vocabulary it is keyed by, and the file→part-label resolution. Every
   workstream either produces it (W2) or consumes it (W3, W5, W6). Freeze it on
   paper first and the consumers can be built against stubs.
2. **`core.py` is the hot file.** Anything that edits it concurrently will merge
   painfully (the emit loop and the group-call path are 400 lines apart but one
   module). So each wave gives `core.py` exactly one owner; parallel workstreams in
   the same wave live in their own files.

**Wave 0 — interface freeze** (main session, an hour, no parallelism): a short
contract note pinned at the top of the feature branch settling (a) `transcriptions`
rides the canonical record under that exact key, part labels are the payload labels
post-relabel; (b) `resolve_part_label(item) -> str` is the one file→label function,
signature fixed even before its body; (c) the sidecar filename rule and frontmatter
key list from Phase 2; (d) the chunk partitioner's signature:
`partition_parts(parts, chunk_size) -> list[list[part]]`. Wave 1 builds against this
note, not against each other's branches.

| ID | Workstream | Scope (from §3) | Depends on | Files owned | Model |
|---|---|---|---|---|---|
| W1 | Transcription conventions | Phase 1a: the conventions block across the prompt resources | Wave 0 | `image_rules.txt`, `instructions_front_back.txt`, `system_header.txt`, `forbidden_inferences.txt` | **Opus 5** |
| W2 | Response contract + caption synthesis | Phase 1b: `transcriptions` in the schema, tolerant parse, caption-string synthesis into `_absorb_caption`, the map riding the fan-out, `resolve_part_label` | Wave 0 | `core.py` (sole owner, wave 1), `output_format.txt` | **Fable 5** |
| W3 | Sidecar writer | Phase 2's writer + the always-quote YAML emitter + golden files, built against a stubbed merged record | Wave 0 | `doc_sidecar.py` (new), its tests | **Sonnet 5** |
| W4 | Chunk partitioner | Phase 3's pure partition function: contiguous blocks on part boundaries, variants never straddling, a front and its back never split apart (D3), non-page parts in chunk 1 | Wave 0 | new pure module (or a `utils.py` addition deferred to W6's wiring), its tests | **Sonnet 5** |
| W5 | Flag + gating | Phase 2's `Config.sidecar_md`, `--sidecar-md`, `SIDECAR_AUTO_CATEGORIES`, the reserved-spelling notes (D12), the emit-loop call into W3's writer, fixture runs for all three values | W2 + W3 | `cli.py`, `utils.py` (Config), `core.py` (emit loop only) | **Sonnet 5** |
| W6 | Chunk loop + consolidation | Phase 3b+3c as one stream (the consolidation call site lives inside the chunk loop, so splitting them would put two owners on one region): chunked calls, chunk note, consolidation prompt + tolerant parser, `page_order` verdict plumbing, usage summation | W2 + W4; W1's conventions text is referenced by the chunk note | `core.py` (group-call path), consolidation prompt file | **Opus 5** |
| W7 | Docs | Phase 2's README section, flag table, CHANGELOG, version bump — bound by `test_docs_alignment.py` | W5 (+ W6 if shipping together) | `README.md`, `CHANGELOG` | **Sonnet 5** |
| W8 | Integration verification | Differential fixture runs (unchunked byte-identity, fallback-path byte-identity), adversarial review of W2/W5/W6, live per-provider smoke of the new schema field | everything | none (review) | **Fable 5**, main session |

The waves, and what runs concurrently:

```
Wave 0:  interface freeze                    (main session)
Wave 1:  W1 ‖ W2 ‖ W3 ‖ W4                   (4 parallel builders)
Wave 2:  W5 ‖ W6                             (after W2 lands; W5 rebases first — it
                                              is small and touches only the emit loop)
Wave 3:  W7, then W8                         (W8 gates the release)
```

Phase boundaries and wave boundaries deliberately differ: W3 (a Phase 2 deliverable)
builds in wave 1 because the interface note is all it needs, and W4 (a Phase 3
deliverable) builds in wave 1 because it is pure. What cannot move earlier is
anything that edits `core.py` alongside W2 — that is the serialization constraint,
not the phase numbering. Shipping can still follow §3: land W1+W2 as the 1st PR,
W3+W5 (+W7's README half) as the 2nd, W4+W6 as the 3rd, W8 before each tag.

**Why these models.** The assignment rule is the failure cost of being subtly wrong,
not the line count:

- **Fable 5** takes W2 and W8. W2 is the load-bearing seam: the caption-block
  invariants it must preserve (byte-identical blocks group-wide, the
  lone-scan-carries-no-label rule, the 0.85 near-identical interaction) are exactly
  the class of cross-file behavior this codebase's history shows failing on first
  pass — B1 shipped with five defects and C1's escape scheme was wrong twice before
  brute force settled it (unified-input-pipeline.md). W8 is the adversarial pass
  that caught those; same reasoning.
- **Opus 5** takes the two judgment-heavy streams. W1 is prompt engineering whose
  blast radius is every future run of every user — the deliverable is *words that
  steer four different vision models*, and the paragraph-flow rule especially needs
  taste about where faithful stops and tidy begins. W6 is new model-facing surface
  (the consolidation schema) plus failure-semantics integration, harder than
  mechanical but with a narrower blast radius than W2.
- **Sonnet 5** takes everything with a frozen spec and a testable boundary: W3 and
  W4 are pure functions with golden/property tests, W5 is wiring whose shape the
  plan already dictates, W7 is editorial work bound by an alignment test. These are
  well-specified enough that a stronger model buys nothing but cost.
- **Haiku 4.5** gets nothing. No stream here is boilerplate enough that its speed
  beats Sonnet's reliability margin on a repo whose review culture treats "subtly
  wrong but green" as the main enemy.

Mechanically, each wave-1 workstream is a Claude Code subagent (or teammate session)
on its own branch/worktree with the Wave 0 note as its brief; W2's branch merges
first and the wave-2 streams rebase onto it. The main session stays the integrator
and runs W8 itself rather than delegating it — the reviewer should not be an agent
whose context ends at its own diff.
