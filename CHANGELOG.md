# Changelog

All notable changes to this project are documented here, in the style of
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.6.0]

### Added

- **Rename mode: `--rename PREFIX`.** A grammar-aware mass rename of a
  folder or manifest's files: it keeps every variant tag the naming
  grammar already understands (`-b`/`5b`, `-front`/`-back`/`-negative`,
  `-pageN`, `-crop`), closes the gaps in the numbering, and follows the
  folder's current order.
  ```
  file102.tif          ->  newname-001.tif
  file105.tif          ->  newname-002.tif
  file105b.tif          ->  newname-002b.tif
  file105b-back.tif     ->  newname-002b-back.tif
  ```
  `--rename` alone previews; **photokin renames files on disk only with
  `-w`.** The prefix can be a template — `{date}`, `{today}`, `{folder}`,
  `{orig}` — so `--rename "{date:yymmdd}-bag" -w` restarts the numbering
  per date and `--rename "{orig}" -w` just renumbers and cleans up in
  place. Every apply is journalled before it touches a file, two-phase
  through hidden temporary names so a gap-closing renumber never
  collides, and reversible: `--rename-undo` reverses the latest applied
  run in a folder, `--rename-resume` finishes one an interruption left
  half-done. `--rename-finish` is the one on-disk operation a catalog
  wrapper needs from photokin: it renames only the companions and
  rewrites sidecars for images the catalog application has already
  renamed itself. Companions sharing an image's stem come along, and a
  `.md` transcript two same-stem images share — a TIFF master beside its
  JPEG derivative — follows the same image the analysis half wrote it
  against, so its `source_file:` line is rewritten rather than left
  naming a file the rename moved away. **A folder tracked by a catalog
  application (Lightroom and the like) must be renamed through that
  application, not through photokin directly** — photokin cannot tell
  such a folder apart from an ordinary one on its own, so every
  `--rename` preview says so, and a
  manifest a catalog application exported (`managed_by`) makes `-w` a
  usage error rather than a guess. See "Rename mode: `--rename`" in
  README.md, `docs/rename-mode.md` for the full specification, and
  `docs/rename-contract.md` for the plan and changeset shapes a wrapper
  reads.

### Changed

- **`--rename --dry-run --plan-out PATH` now exits 2 when `PATH` cannot be
  written**, instead of exiting 0 and printing the plan. The dry run used to
  skip that check on the grounds that it writes nothing, which made it a
  weaker rehearsal than the command it stands in for -- `--output-file` and
  `--generate-manifest` have always been checked under `--dry-run`, and
  `--plan-out` now matches them. A dry run that passes is now evidence that
  the real run's destinations are usable.
- **A destination that differs from a file the run needs only by letter case
  is now refused even on a case-sensitive filesystem.** The guard asks
  `utils.paths_are_same_file`, which can only fall back to a case-folded
  comparison when one of the two paths does not exist yet -- and a
  destination that has not been created is the ordinary case. So on Linux,
  `--plan-out scans/BOX3_017.JSON` beside an existing `scans/box3_017.json`
  is now a usage error although those really are two different files there.
  Refusing a contrived spelling is the side to err on when the alternative is
  destroying a file's only copy; spell the destination as something that is
  not a case-variant of a file the run touches.

### Fixed

- **No destination photokin writes may be a file the run depends on, in any
  mode.** Six review rounds each closed one instance of the same defect — a
  write that never asked what was already at its destination — as a one-off
  patch. There is now one guard, asked at the one seam every user-supplied
  destination already passes through: `--plan-out` and the rename changeset
  are checked against every photo and companion the run would rename, a file
  it reports left behind, an earlier run's journal, the input manifest, and
  the names it is about to rename onto; `--output-file`, `--generate-manifest`
  and `--log-file` are checked against the input manifest, `--meta`,
  `--photo-context-file`, an `--output-sidecars` destination and a
  `--sidecar-md` transcript; and two of a run's own destinations landing on
  each other is refused the same way. Matching is filesystem identity, not
  string equality, so a relative path, a `..` detour, a symlink or a hard
  link onto one of those files is caught too. Attempting it is a usage error
  (exit 2) naming both the destination and what it is, whether or not `-w`
  was given and under `--dry-run` alike — previously, `--log-file` reached no
  such check at all and could empty an input photo under a `--dry-run`
  preview that exited 0. The seam covers the `photokin` command only:
  `python -m photokin.exiftool.apply --output PATH` reaches a different
  `main()` in a different module and is deliberately not guarded, so
  `--changeset c.ndjson --output c.ndjson` there still truncates its own
  input.
- **`--rename-undo`/`--rename-resume JOURNAL` resolve a symlinked journal
  before reading it**, so a journal reached through a link is read, appended
  to, and operated on at the folder it really describes rather than the
  folder holding the link — previously, pointing one at a link left its
  bookkeeping and its file moves on two different folders.

## [0.5.0]

### Changed

- **A multipage document's `XMP-dc:Description` now holds each page's own
  transcription, not the whole book.** A group of views of one object — a
  print, its back, a rescan — still gets one shared block written
  byte-identically to every file, exactly as before. A group that is instead
  an ordered sequence of pages gives each file only its own page's text,
  unlabelled, so a `.md` sidecar and its image's Description now cover the
  same page rather than disagreeing about scope the way they did in 0.4.0.
  They agree on scope, not forever on content: Description is merged and a
  sidecar is overwritten, so a later run that rewords a page leaves both
  readings in Description and only the newest in the sidecar.
  A file whose own page never arrived in the model's reply keeps the group's
  whole block as before, and the record now says which of the two it got via
  a new `caption_scope` key (`"part"` or `"group"`). **The migration cost:**
  an archive already processed under an earlier release keeps the
  whole-document caption it already holds — re-running does not clear it —
  so a folder you re-run after upgrading ends up mixed, with newly analyzed
  documents holding per-page captions and previously analyzed ones still
  holding the whole book. See "Documents get their own page, not the whole
  book" in README.md.
- **A long document's transcription now writes.** ExifTool used to receive
  every tag value inline on its own command line, which Windows' roughly
  32,767-character command-line ceiling made unwritable past about 20
  handwritten pages of transcription. A value over 4,000 characters now
  routes through a small temporary file that ExifTool reads directly instead,
  with no change to the value it writes, the flags you pass, or anything
  under that threshold.

## [0.4.0]

### Added

- **Markdown transcript sidecars.** `--sidecar-md {off,auto,all}` (default `off`)
  writes `<stem>.md` beside each analyzed image: YAML frontmatter carrying that
  file's own group, part, page, title, category, keywords, date, location and
  provenance, and a body holding that page's own markdown transcription.
  `all` writes one for every emitted file except crops; `auto` writes one only
  for a group whose category comes back `Document` or `Postcard`. A single-file
  transcript is one flag: `photokin letter.jpg --sidecar-md all`. See
  "A readable transcript beside each scan" and "Markdown transcript sidecars"
  in README.md.
- **Per-part transcription in the response schema.** The model may now return
  `transcriptions`, a map of part label (`"Front"`, `"Back"`, `"Page 1"`, ...)
  to that part's own transcription, alongside or instead of the single
  `caption` string. When present, `caption` is synthesized from it
  deterministically, and it is what lets a sidecar attribute a transcription
  to the one file it belongs to rather than to the whole group. Fully
  optional and tolerant: a response with only `caption` is handled exactly as
  before.
- **Large-document chunking.** `--max-images-per-call N` (default `8`, `0`
  disables) caps the images sent in one model call. A group whose page images
  exceed it is split into contiguous, part-aware chunks (a front/back pair
  and a part's own variant scans are never separated across chunks), each
  sent as its own call, followed by one text-only consolidation call that
  reconciles the chunks' metadata into a single answer and judges page order.
  A group at or under the cap is unaffected. See "Large documents:
  `--max-images-per-call`" in README.md for the full cost/benefit accounting.
- **Page-order findings, recorded not acted on.** For a chunked document, the
  consolidation pass's verdict on page order is written into the record and
  into each sidecar's `page` frontmatter field (with the filename's own
  number kept alongside as `page_from_filename` when the two disagree), and a
  disagreement with the filenames logs a warning naming the group. Nothing is
  ever renamed, reordered, or renumbered.
- `sidecar-xmp` and `sidecar-json` are reserved spellings in the `--sidecar-*`
  family, for standard XMP sidecars and for `--output-sidecars` to fold into
  later — not yet flags photokin accepts.

### Changed

- **A group of more than 8 images now makes more than one model call.**
  `--max-images-per-call` defaults to `8` rather than to no limit, so this
  applies to existing archives without anyone opting in: a 20-page group that
  was one call is now three chunk calls plus one text-only consolidation call.
  The number of images sent is unchanged — what is new is the prompt bundle
  repeated once per chunk, plus the consolidation call's own tokens. Groups at
  or under 8 images, which is nearly every photo group, are untouched and take
  a byte-identical single call. `--max-images-per-call 0` restores the old
  behavior at any size.

- **Transcription conventions now apply to every scan, not just documents.**
  The formatting-aware rules that document mode needed are the same rules
  every transcription now follows: crossed-out text as `~~struck~~` followed
  by its replacement, underlines as `_underlined_`, margin notes as
  blockquotes, footnotes after a `---` rule, and — the sharpest change —
  prose that flows within a paragraph instead of reproducing the object's
  physical line-wrap breaks. Deliberate breaks (lists, poems, addresses,
  salutations, sign-offs, letterheads, forms) are still kept. These marks
  reach `XMP-dc:Description` byte-identically with any `.md` sidecar, because
  they are one transcription, not two.

  **This changes what re-running an already-processed archive produces.**
  Feeding an older caption back in under `-rw` and getting a freshly
  transcribed one back is not new — see "What happens to a caption you
  already have" in README.md — but a re-run under this release is more
  likely to trip it, because the new conventions can genuinely reword a line
  that was transcribed correctly before (a joined paragraph, a newly marked
  strikethrough). Same rule as always decides what happens to the old text:
  where the difference is only in punctuation, spacing or casing, the stored
  line is kept and the new one is dropped; where it changes a word — which a
  flowed paragraph or a newly noticed strikethrough usually does — the old
  line is kept and the new one is added beside it under the same label,
  never silently overwritten. Nothing is lost, but an archive re-run with
  `-rw` after upgrading should expect its captions to grow rather than stay
  byte-identical the way a same-version re-run does.

Earlier history lives in the git log; this file starts recording from 0.4.0
rather than reconstructing releases nobody watched happen.

0.4.0 was merged but never tagged or published, so 0.5.0 is the first release
on PyPI that carries document mode. Its compare link below therefore spans from
0.3.2, and 0.4.0's points at the pull request it landed in rather than at a tag
that does not exist.

[0.6.0]: https://github.com/asielen/photokin/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/asielen/photokin/compare/v0.3.2...v0.5.0
[0.4.0]: https://github.com/asielen/photokin/pull/9
