---
name: manuscript-transcription
description: Full workflow for transcribing a folder of scanned handwritten manuscript pages (memoirs, diaries, journals, letters, family-history drafts) into per-page sidecar transcripts, verifying the page order by narrative flow, building a master transcript, and renaming the scans to archival names. Use this whenever the user has a folder of scanned handwritten pages to transcribe or process — including any mention of transcribing a manuscript, memoir, journal, diary, or letters; "sidecar" transcripts; checking or fixing scan/page order; or another draft of a family manuscript (e.g. "Draft B" of the Church WWII story). Trigger even if the user only asks for part of the pipeline (just transcription, or just ordering) — the conventions here keep partial runs consistent with earlier ones.
---

# Handwritten manuscript scan transcription

Turn a folder of scanned handwritten pages into: (1) one faithful transcript sidecar per page, (2) a verified page order, (3) a single master transcript, (4) archival filenames applied to every image via a one-click rename script, and (5) a findings report. The process was developed and validated on "Draft A" of Robert Church's WWII memoir (62 scans) and is written so a future session can reproduce it exactly on a similar set.

Guiding principle throughout: **the transcript is evidence, not an edition.** Preserve exactly what the author wrote — spelling, grammar, strikethroughs, false starts — and record your own uncertainty explicitly. Every downstream step (ordering, the master file, the report) depends on the per-page transcripts being trustworthy, so fidelity beats fluency everywhere.

## Phase 0 — Intake and decisions

1. `device_list_dir` the connected scans folder. Inventory it: image formats (typically paired `.jpg` + `.tif` per page), file count per format, sizes, and any combined PDF. Confirm the pairing is 1:1 (same stems) and note any stragglers.
2. Work from the JPGs. The TIFFs are usually the same resolution at ~12× the size; there is no transcription benefit to moving them. Open a TIFF only if a specific region of a JPG is unreadable due to compression (this basically never happens at ~300–600 dpi scans).
3. Ask the user (AskUserQuestion, one batch) anything that changes the deliverables. Established defaults from Draft A — reuse them unless the user says otherwise, and skip questions these already answer:
   - **Final name pattern:** `<surname>__<given>_<work>_page-NN.<ext>` (note the double underscore), e.g. `church__robert_wwii_story_draft_page-07.jpg`.
   - **Numbering:** zero-padded two digits (`page-01`), so files sort correctly everywhere.
   - **Sidecar extension:** `.md` (markdown conventions inside, still plain text).
   - **Rename delivery:** a `rename_pages.bat` the user double-clicks. This one is worth restating to the user each time: Claude cannot rename files in place on their computer — it can only write new files — so the honest options are a tiny rename script (no duplicates, instant) or round-tripping renamed copies (slow, and for TIFFs often impossible: `device_commit_files` caps at 20 MB/file). Recommend the script.
   - Leave any combined PDF untouched unless asked.
4. Create the workspace: `work/transcripts/` (drafts keyed to original scan names) and `work/final/` (renamed deliverables). Keep drafts keyed to **original scan names until the order is confirmed** — renaming before verification bakes in an unverified order.
5. Create a task list covering all phases, ending with a verification task.

## Phase 1 — Stage the JPGs

`device_stage_files` in batches of ≤50 files (its hard cap) and comfortably under the transfer budget (~1 MB/page JPGs are no problem; if a call times out, split it — never repeat it unchanged). Record the returned `stagedPath` for every file; agents get those exact paths. Staged files land under `/mnt/user-data/uploads/<folder>/` and are read-only — that is fine, nothing modifies the scans.

## Phase 2 — Parallel transcription

Divide the scans into **contiguous blocks of ~8 pages and launch one general-purpose agent per block, all in a single message** so they run concurrently (62 pages → 8 agents). Contiguous blocks matter: an agent reading consecutive pages notices continuity and flags anomalies within its block for free.

Every agent gets the same prompt with only the file list changed. Use this template — the conventions block must be **identical across agents**, because the sidecars and the master transcript are assembled from their output verbatim:

```
You are transcribing scanned pages of a handwritten <era/author context — e.g. "WWII memoir
written by Robert Church (the user's grandfather) — mid-20th-century American handwriting,
likely a mix of cursive and print, with revisions, marginal notes, and footnotes">. Your
transcription must be faithful and COMPLETE: every word, correction, note, and textual mark.

Your assigned scans (process one at a time, in this order):
<absolute stagedPath per line>

For EACH file:
1. Read the image (the Read tool shows it to you visually).
2. If any region is hard to read, crop/enlarge it: use Python PIL (pip install pillow
   --break-system-packages if missing) to save enlarged crops to /tmp and Read the crops.
   Spend real effort on hard words, but don't loop forever — use the markers below when a
   word truly can't be resolved.
3. Write the transcript to <workspace>/work/transcripts/<basename>.md.

TRANSCRIPT FORMAT — follow EXACTLY (other agents are doing sibling pages with these same
conventions):
- Line 1 (metadata): `[scan: <file>.jpg | scan order: NN | handwritten page number: X]`
  where X is the page number written on the page, exactly as written (e.g. `12`, `-12-`,
  `p. 12`), or `none` if none is visible. Then a blank line.
- Then the full transcription of the page body:
  - Preserve the author's spelling, punctuation, capitalization, and grammar EXACTLY —
    never correct, modernize, or expand abbreviations.
  - Preserve paragraph breaks. Do NOT reproduce the manuscript's physical line-wrap
    breaks — let prose flow within a paragraph. Preserve deliberate line breaks (lists,
    headings, poems, sign-offs).
  - Crossed-out text: `~~struck text~~` at its position, followed by the replacement text
    if one was written.
  - Words inserted above the line or with a caret: place them where the author intended;
    if the position is ambiguous, precede with `[inserted]`.
  - Underlined text: `_underlined_`.
  - Margin/side notes: a blockquote at the point they refer to (or the end if unclear):
    `> [margin note] text of note`
  - Footnotes: after a `---` line at the end, as `[footnote <key>] text`, keeping the
    author's key symbol (*, 1, ...). Keep the in-text reference mark where it appears.
  - Illegible: `[illegible]` for one word, `[illegible ~N words]` for a run. Uncertain
    reading: `{word?}`.
  - Anything else that carries text (printed letterhead, dates, stamps, "over"/"cont'd"
    marks, ink-offset ghosts): include with a bracketed label at the right position.
  - Do not put the handwritten page number in the body — it lives in the metadata line.
  - Blank page: body is just `[blank page]`.
  - No commentary, summaries, or editorial notes outside brackets.

RETURN VALUE (your final message is parsed as raw data — no prose). One line per page:
<basename> | hw#: <X or none> | starts: "<first ~10 words>" | ends: "<last ~10 words>" |
ends-mid-sentence: <yes/no> | notes: <anomalies: blank page, different hand/paper,
non-narrative content, damage, or "-">
```

Handwriting pitfalls to expect (worth knowing so they don't surprise you in review):
- Writers who cross t's with long bars produce false "strikethroughs" — a real deletion usually has a wavy or multiple-stroke line and often a replacement written above.
- Sheets stored stacked transfer **mirror-image ink offsets** from neighboring pages; agents can misread a ghosted corner number as this page's number. Treat oddly duplicated numbers with suspicion (Phase 3 verifies them).
- Pencil pages and overwritten digits (e.g. a year corrected in place) deserve crops before committing to a reading.

**Resilience.** Long multi-agent runs get interrupted (session/usage limits, disconnects). The transcripts directory on disk is the ground truth — after every run or failure, `ls` it and diff against the assignment list; never trust an agent's report over the files. Resume a surviving agent with `SendMessage` to its agent id ("files X, Y still missing — finish per your original conventions"); spawn a fresh agent (full template) for ranges whose agent is gone; transcribe a straggler page or two yourself in the main context, following the same conventions. If an agent said it was mid-correction when it died, re-check the files it claimed to have fixed.

## Phase 3 — Verify the page order (do this yourself, not via agents)

Ordering needs the global view, so read **every transcript in full** in the main context (cat them in batches). For each consecutive pair in scan order, ask: does the text continue?

- **Strongest signal:** a page ends mid-sentence and the next completes it ("...I said Goodbye to grand mère earlier at the | carons."). Most boundaries in a handwritten draft are like this.
- A page that ends on a complete sentence followed by a topic change is fine **if** it reads as a paragraph/section break — a half-blank page ending is a deliberate section break, and an author's topic-outline scribbled in a margin often previews the following pages (confirming order).
- **Handwritten page numbers are evidence, never authority.** On Draft A the corner numbers came from a later revision pass: only every other sheet numbered at first, then ranges skipped where the author reserved room for planned insert pages (35 → 39 → 43 with perfectly continuous text between), scribbled-out renumberings, one number used twice, and one mirror-offset ghost number. Use them to corroborate, and investigate — don't "fix" — every disagreement with the text.
- **Verify ambiguous corner numbers yourself**: crop the top strip of the page image (PIL, full width × ~14% height, ~1.5× enlargement) and Read the crop. Correct the sidecar metadata to what is actually there, using `{55?} (overwritten, unclear)` style honesty when it stays ambiguous.

Classify every sheet:
- **Sequential narrative page** — continues the flow. Gets a number.
- **Insert sheet** — interrupts the narrative (the surrounding pages flow into each other when you skip it) and typically carries the author's own keys: "Insert ¶ #1 …", "insert p. 21", circled passage numbers, an arrow on another page marking the destination. These get **letters a, b, c… in scan order**, and their transcripts stay out of the master. Figure out where the author meant each one to go (the keys may cite a typescript's pagination rather than the manuscript's — a "Typed this far" note is the tell) and say so.
- **Missing sheet** — a broken boundary (mid-sentence end, next page opens mid-topic) plus a number jump means a sheet was skipped in scanning or is missing from the binder. Do not renumber around it silently: note it in place in the master and flag it prominently in the report so the user can check the physical originals.
- **True stranger** — content that belongs to a different document entirely also gets a letter; describe it in the extras file.

If a boundary is genuinely unresolvable from text, look at the two page images yourself before deciding. Only after every boundary has a verdict, fix the final order: narrative pages numbered continuously 1..N in confirmed order; letters for the rest. On Draft A the scan order proved fully correct — expect that, but earn it boundary by boundary.

## Phase 4 — Assemble the deliverables

Write a small build script (Python) so assembly is deterministic and re-runnable; keep the scan→final mapping in one dict it prints and sanity-checks.

1. **Final sidecars** in `work/final/`, named `<pattern>_page-NN.md` / `_page-a.md`, first line rewritten to:
   `[<pattern>_page-NN | original scan: <scan>.jpg | handwritten page number: X]`
   Lettered sidecars get one added line pointing to the extras transcript. Keeping the original scan name in every header preserves provenance after the images are renamed.
2. **Master transcript** `<pattern>_transcript.md`: front matter (provenance; the order verdict; an explanation of the handwritten-numbering quirks so nobody later "corrects" the order from them; the conventions legend), then each narrative page under `## Page N - scan <name> - <hw number>` separators, with any missing-sheet note placed **between** the pages where the gap is, and an appendix table mapping final file ↔ original scan ↔ handwritten number for all pages including letters.
3. **Extras transcript** `<pattern>_transcript_extras.md`: the lettered sheets in full, each preceded by what it is, the author's keys, and where it belongs in the narrative.
4. **Rename script** `rename_pages.bat`, since Windows batch is double-clickable:
   - Write with **CRLF line endings**; start `@echo off` then `cd /d "%~dp0"` (renames run in the script's own folder); one `ren "old.ext" "new.ext"` per file for both `.jpg` and `.tif`; end with a Done message naming what was left alone (the PDF) and `pause` so the window stays open.
   - Sanity-check before shipping: ren-line count = 2 × page count, all targets unique, all sources exactly match the device folder listing.

## Phase 5 — Deliver to the user's folder

1. `SendUserFile` everything in batches of ≤50 files (sidecars first, then master + extras + bat), collecting the returned `file_uuid`s.
2. `device_commit_files` each batch (≤50 files, ≤100 MB per call) with `fileUuid` → full device path in the scans folder.
3. **Bridge failures are normal, not fatal.** If the device is offline: retry once, then stop hammering — the files already delivered via SendUserFile remain downloadable in the conversation, so nothing is lost. Schedule yourself a `send_later` reminder (~30 min, then one more at ~60) to retry the commits unattended, and tell the user they can also just say "commit the files" when their laptop is back. A rejected entry with `HTTP 404 fetching org-scoped file` means that one upload's uuid went stale — re-`SendUserFile` just that file and commit it with the fresh uuid.
4. Verify with `device_list_dir` that every expected file landed, then tell the user the one manual step: double-click `rename_pages.bat` in the folder.

## Phase 6 — Verify and report

Before calling it done: spot-check one or two of the messiest pages (heavy revisions, margin lists) against their images and fold any corrections into sidecar + master; re-audit counts (pages transcribed = scans; master pages + letters = total).

The final report to the user covers, briefly: the order verdict and how it was established; any missing sheets (and where to look in the physical originals); each lettered insert sheet and where the author keyed it; the numbering quirks; where the draft ends (mid-story means there may be another notebook); and a reminder of the uncertainty conventions (`{word?}`, `[illegible]`) so they know what to proofread. Offer, in one line, a clean-reading HTML version (corrections applied, notes tucked away) as a follow-on.