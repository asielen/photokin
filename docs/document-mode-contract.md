# Document Mode — Wave 0 interface freeze

The contract every document-mode workstream builds against. Frozen before any
code was written so W2 (producer) and W3/W5/W6 (consumers) could be built in
parallel against stubs rather than against each other's branches.

Companion to `docs/document-mode.md` (the plan). Where the two disagree, this
note wins — it is the one that was written against the tree.

---

## 1. `transcriptions` — the per-part transcription map

**Record key:** `transcriptions`, on the model's record dict, beside `caption`
and `all_variant_files`.

**Shape:** `dict[str, str]` — part label to that part's markdown transcription.

```json
"transcriptions": {
  "Page 1": "Dear Mother,\n\nWe arrived...",
  "Page 2": "...",
  "Back": "Written on the reverse in pencil."
}
```

**Key vocabulary (frozen).** Exactly the part labels the payload assigned:

| Label | Comes from |
|---|---|
| `Front` | the `("Front", paths)` part, or `analyze_photo`'s front image |
| `Back` | the `("Back", paths)` part, or `analyze_photo`'s back image |
| `Negative` | the `("Negative", paths)` part |
| `Page 1`, `Page 2`, … | the `("Page N", paths)` parts, **after** the page-1 relabel in `process_manifest_stream` |

One label per *part*, never per *file*: variant rescans of one part share that
part's transcription, which is correct — they are scans of the same physical
page.

**Lifecycle.** Set by the analyzer (`analyze_photo` / `analyze_group_parts`)
from the model's response. It rides the canonical record unchanged through the
fan-out in `process_manifest_stream`, so every file of the group holds the whole
map (`merge_record_with_original` starts from `{**record}`, and
`build_canonical_patch` reads only named fields, so the key survives the merge
and never leaks into an ExifTool patch).

**Optional, always.** A response without `transcriptions` is not an error and
costs no retry. Everything downstream must behave as it does today when the key
is absent.

**Normalization on parse.** Drop non-string values, drop keys that are not
strings, strip each value; drop entries whose value is empty after stripping.
If nothing survives, omit the key entirely rather than storing `{}`.

## 2. `resolve_part_label` — the one file to label function

```python
def resolve_part_label(
    entry: dict,
    *,
    multipage_present: bool,
    relabelled_versions: frozenset[str | None],
) -> str:
    """Return the payload part label a manifest grouping entry travelled under."""
```

Lives in `core.py` (it reads `_manifest_part_key`, which is core's). Body:

```python
part_key = _manifest_part_key(entry)
if part_key == "none" and multipage_present and entry["version"] in relabelled_versions:
    part_key = "page:1"
if part_key.startswith("page:"):
    return f"Page {part_key.split(':', 1)[1]}"
return {"front": "Front", "back": "Back", "negative": "Negative", "none": "Front"}[part_key]
```

`relabelled_versions` is the set the emit loop already computes for the crop map
(`core.py`, "the untagged slot of this variant became page 1 above"). Pass it as
a `frozenset`; the caller may hold a `set`.

A label this returns is not guaranteed to be present in `transcriptions` — a
displaced or unseated file was never in the payload under any label. Consumers
must handle a miss (see §3).

## 3. Caption synthesis from `transcriptions`

When `transcriptions` is present, the record's `caption` is **synthesized**
deterministically and replaces whatever `caption` the model sent:

- **A payload of one part** — the bare transcription text, no label. This
  preserves the lone-scan-carries-no-label rule.
- **A payload of two or more parts** — `[Label]\n<text>` sections joined by
  `\n`, in payload part order (the order of the `parts` list, which for a
  multipage group is Page 1..N then Front, Back, Negative). This holds even
  when only one part came back with text.

  The count that decides this is how many parts were **sent**, not how many
  answered. The original wording here said "one part" without saying which
  count it meant, and the literal reading — one surviving section — regressed
  the commonest inscribed-photo shape there is: a front/back pair whose writing
  is all on the back came out unlabelled, where the model's own `caption` path
  always writes `[Back]`. Unlabelled text is prose nobody attributed, so the
  next `-rw` run attributed it to the file it was read off and the back's
  writing became `[Photo] …` on the front, permanently.
- Parts whose transcription is empty after stripping contribute no section.
- Nothing survives to emit → `caption` becomes `""`.

The synthesized string then enters `_absorb_caption` at exactly the point the
model's `caption` does today, so the caption block, its dedup and its merge
behavior are unchanged in shape.

**`_CAPTION_LABEL_RE` is not extended.** `[Page N]` is deliberately *not* added
to it. Today a multipage caption is absorbed as one unlabelled prose section;
synthesizing the same shape keeps that behavior byte-identical rather than
silently re-sectioning captions written by earlier releases.

**`_EMPTY_CAPTION_MARKERS` is not extended.** In particular `[blank page]`
survives into the caption — it is information about a document page, and the
sidecar wants it.

### Supersession note — 2026-08-28 (`docs/per-page-captions.md`, Part A)

This section describes `transcriptions` synthesizing into **one** `caption`
string per group, written to every file. That is no longer the whole story
for a multipage group (`multipage_present`): each file's `XMP-dc:Description`
now carries only its own part's text, read straight out of `transcriptions`
and unlabelled — not the joined multi-part string this section describes.
The synthesized group `caption` this section documents is still built,
unchanged, exactly as written above; it is just no longer what most files in
a multipage group receive. It survives as two things: the transcription a
non-multipage group still absorbs (this section's mechanism was never
document-specific), and, inside a multipage group, the group block a file
falls back to when its own part never arrived. See
`docs/per-page-captions.md` E6-E9 for the file-level rule and E12/R2 for why
an archive already processed under the old rule is not migrated.

What this section got right and what does not change, stated because it is
now MORE load-bearing than it was: **`_CAPTION_LABEL_RE` is not extended**
now guards two absorption sites instead of one — the group's synthesized
`caption` above, and the per-file absorption of a file's own previously
stored caption against its own fresh part text. Either site sectioning on
`[Page N]` reproduces the letterhead-dedup regression `docs/per-page-captions.md`
names as R1, measured and rejected there exactly as it was here. The original
decision recorded above is not rewritten — this note only says what still
holds and what a later reader should know changed.

(Naming note: `_absorb_caption`, named above, is now the private `_absorb`
closure inside `_assemble_caption_block` — the same function, extracted so a
per-file caption and a group caption can both call it. The behavior this
section describes did not change in the move.)

## 4. The markdown sidecar

**Path.** `<image stem>.md` beside the image — the same derivation as
`_write_sidecar_document`'s `<stem>.json`.

**Failure contract.** Identical to `_write_sidecar_document`: catch `OSError`,
log at WARNING naming the basename, return `None`. Never raise; the analysis is
already paid for.

**Entry point.**

```python
def write_markdown_sidecar(
    merged: dict[str, Any],
    item: dict[str, Any],
    group_info: SidecarContext,
    config: utils.Config,
) -> str | None:
```

`merged` is the post-`merge_metadata` per-file record. `item` is the manifest
grouping entry (it supplies `path`). `group_info` is:

```python
@dataclass(frozen=True)
class SidecarContext:
    group_id: str                  # the bucket stem
    part_label: str                # from resolve_part_label
    group_files: tuple[str, ...]   # basenames, group rank order
    page_count: int | None         # count of Page parts; None when not multipage
    page_number: int | None        # filename-derived page number, None when not a page
```

**Frontmatter**, in this order; a key whose value is `None`/empty is omitted
entirely:

| Key | Source |
|---|---|
| `source_file` | `os.path.basename(item["path"])` |
| `group` | `group_info.group_id` |
| `part` | `group_info.part_label` |
| `page` | corrected number from `merged["page_order"]` if present, else `group_info.page_number` |
| `page_from_filename` | only when a correction changed the number |
| `page_order_flags` | only when `page_order` flagged this part |
| `page_count` | `group_info.page_count` |
| `group_files` | `group_info.group_files` |
| `title` | `merged["title"]` |
| `category` | `merged["category"]` |
| `keywords` | `merged["keywords"]` — the merged per-file set, part markers included |
| `date` | `merged["date_guess"]["iso"]` |
| `date_pattern` | `merged["date_guess"]["pattern"]` |
| `date_confidence` | `merged["date_guess"]["confidence"]` |
| `location` | flat map of the non-null members of `merged["location_guess"]` |
| `analyzed_by` | see below |
| `transcription_scope` | the literal `group`, only on the fallback path |

`analyzed_by` is `"<provider display name> <model> (<analysis date>)"`, e.g.
`Claude claude-sonnet-4-6 (2026-08-27)`. Provider from
`utils.provider_display_name(config.provider)`, model from
`merged["_usage"]["model"]` (fall back to `utils.resolve_model_for_provider`),
date parsed out of `ai_caption`'s `[AI Analysis on YYYY-MM-DD]:` prefix when
present and `date.today()` otherwise — parsing it keeps the writer deterministic
under a stubbed model.

**Body.**

```markdown
# <merged["title"], or group_id when the title is null>

<merged["ai_caption"], verbatim, when present>

## Transcription — <part label>

<transcriptions[part_label], verbatim>
```

**Fallback path.** When the record has no `transcriptions`, or the resolved part
label is not a key in it: the body's transcription section is the whole caption
block under a bare `## Transcription` heading, and frontmatter carries
`transcription_scope: group`. Honest about what it is rather than claiming an
attribution that was never made.

**YAML emitter.** No YAML dependency; the emitted subset does not need one.

- Strings: always double-quoted. Escape `\` and `"`; encode newline as `\n`,
  tab as `\t`, carriage return as `\r`, and any other C0 control character as
  `\uXXXX`.
- `int` / `float`: bare. `bool`: `true` / `false`.
- Lists: flow style, elements serialized by the same rules —
  `keywords: ["Document", "1944"]`. An empty list omits the key.
- The one nested map (`location`): flow style with bare keys —
  `location: {country: "France", city: "Le Mans", confidence: 0.9}`.
- Frontmatter is fenced by a `---` line before and after.

## 5. Chunk partitioning

```python
def partition_parts(
    parts: list[tuple[str, list[str]]],
    chunk_size: int,
) -> list[list[tuple[str, list[str]]]]:
    """Split ordered parts into contiguous blocks of at most chunk_size images."""
```

Pure function, new module `photokin/chunking.py`, no imports from `core`.

Rules, in order:

1. `chunk_size <= 0`, or the group's total image count is `<= chunk_size`, or
   there are no page parts → `[list(parts)]`. One chunk, today's behavior.
2. Split `parts` into `pages` (label matching `^Page\s+\d+$`, case-insensitive)
   and `others`, each preserving its relative order.
3. Pack `pages` greedily into contiguous chunks, **a part is atomic** — its
   variants never straddle a chunk. A single part holding more than
   `chunk_size` images becomes its own oversized chunk.
4. Chunk 1's page budget is reduced by the images `others` will ride with, so
   the first call is not systematically the largest — but chunk 1 always gets
   at least one page part however large `others` is.
5. `others` are appended to chunk 1, after its pages. That is the source order
   (pages precede Front/Back/Negative in the list `process_manifest_stream`
   builds), so the concatenation of the returned chunks is exactly `parts`.

Consequences worth stating rather than discovering: a front and its back are
never split, because every non-page part rides chunk 1 together; and a group
with no page parts is never chunked at all, however many variants it holds.
W6 logs at WARNING when a returned chunk exceeds `chunk_size`, so a bound the
partitioner could not honor is never silent.

## 6. Consolidation (Phase 3)

One text-only call per chunked group, after the last chunk call.

**Input:** every part's label, its filenames, its filename-derived page number
and its transcription; plus each chunk's provisional `keywords`, `title`,
`category`, `ai_caption`, `date_guess` and `location_guess`. No images.

**Output**, same strict-JSON discipline as the main schema:

```json
{
  "result": { "<main_image_path>": { "keywords": [...], "title": ..., "category": ...,
                                     "ai_caption": ..., "location_guess": {...},
                                     "date_guess": {...}, "proposed_new_keywords": [] } },
  "page_order": { "Page 3": {"page": 5, "flags": ["out_of_order"]} },
  "page_order_notes": ["Page 3 completes the sentence Page 4 begins."]
}
```

- `page_order` is keyed by **part label**, the same frozen vocabulary as
  `transcriptions`. Values carry `page` (int) and an optional `flags` list drawn
  from `out_of_order`, `missing_page_before`, `missing_page_after`,
  `duplicate_page`.
- Transcriptions are **not** re-emitted and never revised by this pass.

**On the record:** `page_order` and `page_order_notes` land as additive keys
beside `all_variant_files`. Merged `transcriptions` is the union of the chunk
results, in part order.

**Tolerant parse.** A missing or malformed `page_order` falls back to filename
order and logs it. A missing `result` block falls back to the existing
`_best_guess` pick across the chunk records. Neither fails the group.

**Verdict is data, never action (D11).** photokin writes the corrected number
into the record and the sidecar and logs a WARNING naming the group when the
corrected order disagrees with the filenames. It does not rename, reorder or
renumber anything.

## 7. Flags and config

| Name | Home | Default |
|---|---|---|
| `Config.sidecar_md: str` | `utils.Config` | `"off"` |
| `Config.max_images_per_call: int` | `utils.Config` | `8` |
| `SIDECAR_MD_VALUES: tuple[str, ...]` | `utils` | `("off", "auto", "all")` |
| `SIDECAR_AUTO_CATEGORIES: frozenset[str]` | `utils` | `{"Document", "Postcard"}` |

`--sidecar-md {off,auto,all}` and `--max-images-per-call N` on the CLI. Named by
format and by what they constrain, not by feature (D12): `--sidecar-xmp` and
`--sidecar-json` are reserved spellings and must be recorded as reserved in the
README flag table, the way `-R` was.

`SIDECAR_AUTO_CATEGORIES` is format-neutral on purpose — a future
`--sidecar-xmp auto` reads the same gate rather than growing its own.

## 8. Where the sidecar is written

In the per-file emit loop of `process_manifest_stream`, after
`merged["all_variant_files"]` is attached and before `_emit`. That is the one
point that is per-file, post-merge and after `category` is known, which makes
manual and auto mode the same writer with one gate.

The gate:

```python
config.sidecar_md == "all"
or (config.sidecar_md == "auto"
    and (merged.get("category") or "") in utils.SIDECAR_AUTO_CATEGORIES)
```

and, either way, `not item["is_crop"]` (D9).
