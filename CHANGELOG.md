# Changelog

All notable changes to this project are documented here, in the style of
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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

[0.5.0]: https://github.com/asielen/photokin/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/asielen/photokin/compare/v0.3.2...v0.4.0
