"""
photokin.core
===================

Library entrypoint and orchestration logic.

Responsibilities:
- Normalize/validate paths.
- Load vocab and known keywords.
- Upload images to Files API for archival (logging file_ids).
- Convert images to data URLs and measure the exact byte payload sent to the model.
- Assemble prompts, call the model, and parse/clean JSON (with one retry).
- Post-process: warnings, new keyword appends, optional sidecar JSON.
- Batch helpers: folder mode, manifest streaming (NDJSON) or aggregate (JSON).

This is the orchestration hub: the single-photo path (:func:`analyze_photo`), the
group/variant path (:func:`analyze_group_parts`), and the batch path
(:func:`process_manifest_stream`) all live here and share the same prompt/model/
parse/merge machinery. ``process_manifest_stream`` is what the Lightroom plugin
drives; it groups manifest items by photo, analyzes each group, and emits NDJSON
incrementally while also returning an aggregate snapshot.

Code map (public-facing entry points marked PUBLIC):
- _is_run_fatal                 does this error describe the run, not one photo?
- _build_llm_dump_writer        optional debug dumper for raw LLM requests
- inject_analysis_date          stamp a date into the '[AI Analysis]:' caption prefix
- _strip_empty_caption_sections drop empty [Front]/[Back] caption sections
- _normalize_caption_text       reduce a caption to what two copies must share
- _captions_are_near_identical  is this caption a restatement of that one?
- _split_caption_sections       read one file's caption back as labelled sections
- _assemble_caption_block       fold captions and a transcription into one block
- _normalize_transcriptions     keep only the per-part entries a reply's map may carry
- _synthesize_caption           build the caption string from per-part transcriptions
- _build_provider_client        construct the SDK client for the active provider
- _ensure_provenance_keyword    guarantee one provider/model provenance keyword
- _should_run_archival_upload   gate Files-API upload by provider
- _normalized_error_payload     build a provider-normalized error record
- analyze_photo                 PUBLIC: full pipeline for one front(+back) photo
- analyze_group_parts           PUBLIC: analyze ordered parts (front/back/pages)
- analyze_group_front_back      PUBLIC: convenience wrapper over analyze_group_parts
- build_folder_manifest         PUBLIC: a folder as in-memory manifest items
- build_single_photo_manifest   PUBLIC: image + --back + --meta as manifest items
- analyze_folder                PUBLIC: batch a whole folder
- _coerce_manifest_bool         read a tri-state boolean flag off a manifest item
- _log_manifest_override        warn that an explicit flag beat the filename
- _manifest_group_override      resolve an item's explicit bucket key
- _resolve_manifest_entry       build one grouping entry, filename plus overrides
- _item_part_marker             the per-file part keyword an entry earns, if any
- _escape_pair_half             make one half of a pair key free of bare separators
- _pair_bucket_key              the escaped group-key/variant join '--group-by pair' uses
- build_manifest_buckets        PUBLIC: group items the way the stream will
- _manifest_part_key            the slot an entry competes for in its variant
- resolve_part_label            PUBLIC: the payload label a grouping entry travelled under
- _slot_rank_key                the one ordering every grouping tie-break uses
- analyze_manifest              PUBLIC: aggregate wrapper over the stream
- process_manifest_stream       PUBLIC: streaming NDJSON batch (the plugin path)
"""

import difflib
import json
import logging
import os
import re
import traceback
from pathlib import Path
from datetime import date
from typing import Callable, Dict, Any, List
from copy import deepcopy

from . import chunking, doc_sidecar, utils
from .api import call_model, extract_output_text, get_response_model
from .errors import ProviderApiError, SELF_EXPLANATORY_ERROR_TYPES
from .merge import merge_record_with_original as merge_metadata
from .canonical import (
    build_canonical_patch,
    canonical_values_from_metadata,
    canonical_values_from_patch,
)
from .changeset import (
    make_run_id,
    ordered_group_keys,
    select_forwarded_metadata,
    diff_canonical_metadata,
    emit_changeset_record,
)

logger = logging.getLogger(__name__)

_EMPTY_CAPTION_MARKERS = (
    "no text visible",
    "none",
    "blank",
    "empty",
    "n/a",
)
# ProviderApiError types that describe the run rather than one photo, so a batch
# loop must abort on them instead of isolating the same failure per group. The
# first two are raised before any request; model_not_found is only discoverable
# on the first call, but the model is constant for the run, so every later group
# would fail the same way.
_RUN_FATAL_ERROR_TYPES = frozenset({"missing_api_key", "missing_dependency", "model_not_found"})

# A 401/403 means the credential itself was rejected -- just as constant
# across every remaining call as a missing key is. Checked by status code
# rather than folded into _RUN_FATAL_ERROR_TYPES above because which
# error_type wraps an auth rejection varies by provider SDK (api_status
# today for OpenAI/Anthropic/OpenRouter); the HTTP status is the one
# consistent signal across all of them.
_RUN_FATAL_STATUS_CODES = frozenset({401, 403})


def _is_run_fatal(exc: ProviderApiError) -> bool:
    """Whether ``exc`` describes the run rather than one photo."""
    return exc.error_type in _RUN_FATAL_ERROR_TYPES or exc.status_code in _RUN_FATAL_STATUS_CODES


def _build_llm_dump_writer(
    config: utils.Config,
    source_path: str,
    phase: str,
) -> Callable[[Dict[str, Any]], None] | None:
    if not config.debug_dump_llm_request:
        return None

    batch_id = (config.run_batch_id or "batch").strip() or "batch"
    photo_stem = Path(source_path).stem or "photo"
    dump_dir = Path(config.debug_dump_dir or os.path.join(os.getcwd(), "debug"))

    def _writer(request_payload: Dict[str, Any]) -> None:
        dump_dir.mkdir(parents=True, exist_ok=True)
        base_name = f"{batch_id}_llm_request_{photo_stem}_{phase}"
        dump_path = dump_dir / f"{base_name}.json"
        suffix = 1
        while dump_path.exists():
            dump_path = dump_dir / f"{base_name}_{suffix}.json"
            suffix += 1

        try:
            with open(dump_path, "w", encoding="utf-8") as fh:
                json.dump(request_payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            logger.info("Wrote LLM request dump: %s", dump_path)
        except OSError as exc:
            logger.warning("Could not write LLM request dump %s: %s", dump_path, exc)

    return _writer


def _build_hydration_dump_writer(
    config: utils.Config,
    source_path: str,
    phase: str,
) -> Callable[[Dict[str, Any]], None] | None:
    """Build a writer for the group's assembled metadata, before it becomes a prompt.

    The mirror of :func:`_build_llm_dump_writer` for the other half of "what
    did this run actually send" -- the LLM-request dump shows the *prompt*,
    already merged into text; this shows the *metadata* the merge started
    from (whatever ``-r`` read plus whatever the manifest supplied), one
    level upstream of that merge, in the shape ``combine_group_metadata``
    produced it. A hydration bug is a bug in that data, and finding it by
    eye inside a fully assembled prompt is far harder than reading the
    dict directly.

    Args:
        config: Run configuration; gated on ``debug_dump_hydration``.
        source_path: The group's representative path, for the dump's stem.
        phase: ``"single"`` or ``"group"`` -- same vocabulary as the
            LLM-request dump, and for the same reason: which analyzer
            produced this group is part of what a debugging session needs.

    Returns:
        A callable that writes one dump per call, or ``None`` when
        ``debug_dump_hydration`` is off.
    """
    if not config.debug_dump_hydration:
        return None

    batch_id = (config.run_batch_id or "batch").strip() or "batch"
    photo_stem = Path(source_path).stem or "photo"
    dump_dir = Path(config.debug_dump_dir or os.path.join(os.getcwd(), "debug"))

    def _writer(metadata: Dict[str, Any]) -> None:
        dump_dir.mkdir(parents=True, exist_ok=True)
        base_name = f"{batch_id}_hydration_{photo_stem}_{phase}"
        dump_path = dump_dir / f"{base_name}.json"
        suffix = 1
        while dump_path.exists():
            dump_path = dump_dir / f"{base_name}_{suffix}.json"
            suffix += 1

        try:
            with open(dump_path, "w", encoding="utf-8") as fh:
                json.dump(metadata, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            logger.info("Wrote hydration dump: %s", dump_path)
        except OSError as exc:
            logger.warning("Could not write hydration dump %s: %s", dump_path, exc)

    return _writer


def inject_analysis_date(ai_caption: Any, analysis_date: date | None = None) -> Any:
    """
    If the caption starts with the undated '[AI Analysis]:' prefix, replace it
    with a dated prefix using the provided date (defaults to today).
    """
    if ai_caption is None:
        return ai_caption
    text = str(ai_caption)
    if not text.strip():
        return ai_caption
    analysis_date = analysis_date or date.today()
    prefix = "[AI Analysis]:"
    dated_prefix = f"[AI Analysis on {analysis_date.isoformat()}]:"
    leading_ws_len = len(text) - len(text.lstrip())
    leading_ws = text[:leading_ws_len]
    trimmed = text.lstrip()
    if trimmed.startswith(prefix):
        return leading_ws + dated_prefix + trimmed[len(prefix):]
    return ai_caption


def _strip_empty_caption_sections(caption: str) -> str:
    """Remove empty [Front]/[Back] caption sections that only contain placeholders.

    The model may emit structural headers even when no transcription exists for
    that side of the photo. Removing placeholder-only sections keeps captions
    concise and prevents Lightroom from showing noisy blocks like
    ``[Front]\n[No text visible]``.
    """
    text = (caption or "").strip()
    if not text:
        return ""

    section_re = re.compile(r"\[(Front|Back)\]\s*(.*?)(?=(?:\n\[(?:Front|Back)\])|$)", re.IGNORECASE | re.DOTALL)
    matches = list(section_re.finditer(text))
    if not matches:
        return text

    kept_sections: list[str] = []
    for match in matches:
        label = match.group(1).strip().title()
        body = (match.group(2) or "").strip()
        normalized = re.sub(r"[\s\[\]\(\)\-_:]+", " ", body).strip().lower()
        is_empty_marker = (not normalized) or (normalized in _EMPTY_CAPTION_MARKERS)
        if is_empty_marker:
            continue
        kept_sections.append(f"[{label}]\n{body}")

    return "\n".join(kept_sections).strip()


#: A caption an older photokin release wrote may still carry an "[AI Analysis]"
#: tail glued on from a previous run -- that release's own interpretation, not a
#: human's words. ``_split_caption_sections`` matches on this to drop that tail
#: when reading an existing caption back in, so an already-contaminated file is
#: cleaned up the next time it is read rather than having the stale analysis
#: treated as a caption section and merged forward forever.
#:
#: Matched loosely: ``inject_analysis_date`` rewrites the marker to
#: "[AI Analysis on 1952-06-01]:" in ``ai_caption``, so both spellings have to be
#: recognized.
_CAPTION_AI_MARKER_RE = re.compile(r"^\s*\[AI Analysis\b[^\]]*\]\s*:?", re.IGNORECASE)

#: A caption line that already carries one of our own section labels. ``Front``
#: is read but never written: it is what photokin wrote before the wording
#: became ``Photo``, and an archive enriched by an older release must keep those
#: lines as they are rather than have them attributed a second time.
_CAPTION_LABEL_RE = re.compile(r"^\s*\[(Photo|Front|Back)(\s+[^\]]+)?\]\s*:?\s*", re.IGNORECASE)

#: The bare prefix a model sometimes puts on a caption of its own accord, and
#: which an older tool may have written into a file. Stripped only when it names
#: the same side as the label being applied, so "[Back] Back: pencil note"
#: cannot happen.
_CAPTION_ROLE_PREFIX_RE = re.compile(r"^\s*(photo|front|back)\s*:\s*", re.IGNORECASE)

#: How alike two captions must read before the second is treated as a
#: restatement of the first and dropped rather than appended.
#:
#: This is the dangerous knob -- too loose silently discards a caption someone
#: typed, which is unrecoverable; too tight and the block grows a near-twin line
#: every run -- so it was set by measuring ``difflib`` against real caption pairs
#: rather than by taste. The measurement (scored on the normalized text below)
#: says something sharper than "pick carefully": no ratio can do this job.
#:
#:   must SKIP  trailing period / case / spacing .................. 1.0000
#:   must SKIP  "Ruth and Sam, outside" vs "Ruth and Sam outside" . 0.9841
#:   must SKIP  "Grandma’s porch" vs "Grandma's porch" ............ 0.9643
#:   must SKIP  "Ohio - summer" vs "Ohio — summer" ................ 0.9444
#:   must SKIP  '"hello"' vs "'hello'" ............................ 0.9091
#:   must KEEP  "...bakery, 1948" vs "...bakery, 1949" ............ 0.9730
#:   must KEEP  "Ruth and Sam" vs "Ruth and Edith" ................ 0.8750
#:   must KEEP  one digit of a year inside a 300-char analysis .... 0.9967
#:
#: Skipping needs ``ratio >= T``, so the rows that must be skipped want
#: ``T <= 0.9091`` and the rows that must be kept want ``T > 0.9967``. There is
#: no such T: the two ranges overlap almost completely, because ``ratio`` is
#: relative to length and a changed *year* in a long block moves it less than a
#: changed *quote mark* in a short one.
#:
#: What does separate them cleanly, on every row above, is whether any WORD
#: changed. So the word sequence carries the decision -- a difference that
#: changes no word is a difference in punctuation, quoting or spacing, which is
#: the same caption typed twice.
#:
#: The word gate is NECESSARY, not an alternative. Reaching the ratio only when
#: the words already differ means the ratio can only ever skip something
#: materially different, which is precisely the data loss above. Measured: a
#: 656-character postcard-back transcription -- the shape README.md:27 ships as
#: its worked example -- with the year corrected 44 -> 45 scores 0.99847 and was
#: dropped, writing the stale year back over the file the archivist had just
#: fixed. One substituted character in a length-n block scores (n-1)/n, so any
#: high ratio is a length test wearing a similarity test's clothes.
#:
#: So: same words is required, and the ratio is a FLOOR under it, guarding the
#: case where identical words are re-punctuated so heavily the line no longer
#: reads the same way. Measured with the tokens held equal, realistic
#: re-punctuation spans 0.86-1.00 (curly quotes 0.88, semicolons for commas
#: 0.86, added parentheses 0.98) while a punctuation dump sits at 0.43-0.69
#: (dashes for spaces 0.43, an appended ASCII divider 0.69). 0.85 sits in that
#: gap, and above the word gate it cannot discard a changed name, year or place
#: at any length -- which the previous 0.998 provably could.
_CAPTION_NEAR_IDENTICAL_RATIO = 0.85

#: Trailing noise that says nothing about whether two captions are the same one.
_CAPTION_TRAILING_NOISE = " \t.,;:!?-–—\"'`)]}"

#: A run of word characters. Comparing these instead of the raw text is what
#: folds away punctuation, quote style and dashes; see the table above.
_CAPTION_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _normalize_caption_text(text: str) -> str:
    """Reduce a caption to what two copies of it have to share to be one caption.

    Args:
        text: A caption, or one section of one.

    Returns:
        The text with runs of whitespace collapsed, case folded and trailing
        punctuation removed -- so a caption that came back from a round trip
        through a metadata tag compares equal to the one that went in.
    """
    return " ".join((text or "").split()).casefold().rstrip(_CAPTION_TRAILING_NOISE)


def _captions_are_near_identical(existing: str, candidate: str) -> bool:
    """Is *candidate* a restatement of *existing* rather than something new?

    Args:
        existing: A caption section already accepted into the block.
        candidate: One offered for the same label.

    Returns:
        True when the two are the same caption written slightly differently, and
        the candidate adds nothing by being kept. False for anything that reads
        as a real edit, which is kept -- the failure this asymmetry is chosen
        against is losing a correction someone typed, not carrying an extra line.
    """
    left = _normalize_caption_text(existing)
    right = _normalize_caption_text(candidate)
    if left == right:
        return True
    if not left or not right:
        return False
    # A changed word is a real edit and is always kept, however small it looks
    # against a long block. This test comes first and is necessary: run the
    # other way round, the ratio is only ever consulted once the words already
    # differ, so it could only ever discard a genuine correction.
    if _CAPTION_WORD_RE.findall(left) != _CAPTION_WORD_RE.findall(right):
        return False
    # Same words, then: one caption typed twice, unless the punctuation was
    # changed so heavily the line no longer reads the same way.
    return (
        difflib.SequenceMatcher(None, left, right).ratio() >= _CAPTION_NEAR_IDENTICAL_RATIO
    )


def _caption_label_key(line: str) -> str:
    """Return the section key a labelled caption line is filed under.

    Case and the legacy ``Front`` spelling are folded away, so one section of the
    block is one key however it was written. The key is what makes the merge
    per-label: two files offering ``[Photo A]`` are the same section and settle
    against each other, while ``[Photo B]`` is a different one and cannot be
    disturbed by either.

    Args:
        line: A caption line matching :data:`_CAPTION_LABEL_RE`.

    Returns:
        A folded ``"role letter"`` key, e.g. ``"photo a"`` or ``"back"``.
    """
    match = _CAPTION_LABEL_RE.match(line)
    if not match:
        return ""
    role = match.group(1).lower()
    if role == "front":
        role = "photo"
    return f"{role} {(match.group(2) or '').strip().lower()}".strip()


def _caption_section_text(body: list[str]) -> str:
    """Return a section's text with its own label removed.

    Comparison is on the text and not on the label, so one caption that two
    files of a group both hold -- an archivist copied a note onto the print and
    its back -- is written once rather than once per side. That is the
    de-duplication the old per-variant branch existed for, kept; what is not kept
    is how it used to get there, which was to file the front's caption under the
    back's label.

    Args:
        body: One section, including its label line.

    Returns:
        The section's text, label stripped.
    """
    lines = list(body)
    if lines:
        lines[0] = _CAPTION_LABEL_RE.sub("", lines[0], count=1)
    return "\n".join(lines).strip()


def _split_caption_sections(caption: str, label: str) -> list[tuple[str, list[str]]]:
    """Read one file's existing caption back as labelled sections.

    A section starts at a labelled line and runs to the next one, so a multi-line
    entry stays a single section and keeps the blank lines its author put in it.
    Lines before the first label are prose nobody attributed -- a caption typed
    straight into Lightroom, or one an older release wrote -- and they are
    attributed to *label*, the file they were read off, which is the one moment
    that attribution is free rather than guesswork.

    That whole run of prose takes ONE label, on its first line, rather than a
    label per line: the run is one thought, and a note whose paragraphs were
    labelled separately would be several sections that later runs could
    de-duplicate and reorder independently of each other.

    Everything from an ``[AI Analysis]`` marker to the end is a previous run's
    analysis. It is dropped here and regenerated from this run's answer, which is
    what stops the block accumulating one analysis per pass; the model is told
    the same thing about a caption it is shown (see the CAPTION MERGE BEHAVIOR
    rules in ``prompts_photo_ai/instructions_front_back.txt``).

    Args:
        caption: The file's existing caption, verbatim.
        label: The label unattributed prose earns, e.g. ``"[Photo A]"``, or
            ``""`` for a group whose files are not labelled at all.

    Returns:
        ``(key, lines)`` pairs in the order they appear, where *key* comes from
        :func:`_caption_label_key` and *lines* is the section including its own
        label line.
    """
    kept: list[str] = []
    for raw in (caption or "").strip().splitlines():
        if _CAPTION_AI_MARKER_RE.match(raw):
            break
        kept.append(raw.rstrip())

    first_labelled = next(
        (i for i, line in enumerate(kept) if _CAPTION_LABEL_RE.match(line)), len(kept)
    )

    sections: list[tuple[str, list[str]]] = []
    prose = list(kept[:first_labelled])
    while prose and not prose[0].strip():
        prose.pop(0)
    while prose and not prose[-1].strip():
        prose.pop()
    if prose:
        if label:
            head = _CAPTION_ROLE_PREFIX_RE.match(prose[0])
            if head:
                role = head.group(1).lower()
                # Strip it only when it names the side the label is about to
                # say, so "[Back] Back: pencil note" cannot happen and a
                # "Front:" a human meant as part of their own note survives on
                # a section that is not about the front.
                role = "photo" if role == "front" else role
                if label.lower().startswith(f"[{role}"):
                    prose[0] = prose[0][head.end():]
            prose[0] = f"{label} {prose[0].strip()}".strip()
        sections.append((_caption_label_key(prose[0]) if label else "", prose))

    for line in kept[first_labelled:]:
        if _CAPTION_LABEL_RE.match(line):
            sections.append((_caption_label_key(line), [line]))
        elif sections:
            sections[-1][1].append(line)
    return [(key, body) for key, body in sections if any(l.strip() for l in body)]


def _assemble_caption_block(intake: list[tuple[str, str]], fresh_caption: str) -> str | None:
    """Fold a set of existing captions and one fresh transcription into a block.

    The one place a caption block is built, and it is now called in two
    regimes. For a group of views of one object it is called ONCE, with every
    member's stored caption filed under the label of the file it came off --
    the print, its back and its rescan share one block, which is what lets
    whichever file someone opens tell the whole story of the object. For a
    multipage document it is called once PER FILE, with that file's own stored
    caption alone and that file's own page as the fresh text.

    Which captions go in is the whole of the difference between the two: the
    folding, the section de-duplication and the line de-duplication below are
    identical either way, deliberately, so that "running it twice does not grow
    your captions" is one proof rather than two.

    Being keyed by section is what makes the result safe to re-read, which is
    not optional: under ``-rw`` the block written here is exactly what the next
    run reads back as a file's existing caption. Intake recognizes its own
    output and takes it verbatim; attributing it a second time is how you get
    "[Photo A] [Photo A] Caption A" and a caption that grows on every pass.

    Args:
        intake: ``(existing caption, label)`` pairs in the order they are to be
            absorbed. *label* is what unattributed prose in that caption earns
            -- ``"[Photo A]"``, ``"[Back]"``, or ``""`` where the text needs no
            attribution because the block covers exactly one thing. An empty
            caption contributes nothing.
        fresh_caption: This run's transcription of whatever the block covers,
            absorbed last and always unlabelled.

    Returns:
        The block, or ``None`` when nothing survived to write.
    """
    caption_sections: list[list[str]] = []
    accepted_texts: list[str] = []

    def _absorb(text: str, label: str) -> None:
        """Fold *text* in section by section, never whole-string.

        Filling in a missing "[Photo B]" therefore cannot disturb the
        "[Photo A]" already accepted, and a source holding the same caption --
        or a trivially reworded copy of it -- adds nothing rather than adding a
        near-twin line.

        Args:
            text: One source caption, verbatim.
            label: The label its unattributed prose earns, or ``""``.
        """
        for _key, body in _split_caption_sections(text, label):
            section_text = _caption_section_text(body)
            if any(
                _captions_are_near_identical(seen, section_text) for seen in accepted_texts
            ):
                continue
            accepted_texts.append(section_text)
            caption_sections.append(body)

    for existing_caption, label in intake:
        if existing_caption:
            _absorb(existing_caption, label)
    if fresh_caption:
        _absorb(fresh_caption, "")

    # De-duplicated line by line as well as section by section. The section
    # pass settles what each label says; this one is the last net, and it is
    # what stops a model that echoed a caption it was shown from landing that
    # line twice.
    #
    # ACROSS sections only, never within one. That distinction is what makes
    # the block converge, and it is load-bearing now in a way it was not before
    # per-part transcription existed. A multi-page transcription absorbed as a
    # group block is ONE section (``_CAPTION_LABEL_RE`` does not match
    # "[Page N]"), and inside it a repeated line is the document's own content,
    # not an echo: a letterhead printed on every sheet, a recurring "Dear
    # Mother,", a second "[blank page]". De-duplicating those against each
    # other dropped real transcription -- and worse, it did not settle, because
    # the block then no longer matched what the next run synthesized fresh, so
    # the near-identical section gate stopped firing and every ``-rw`` pass
    # appended another structural tail. Scoping the key to its section keeps the
    # original purpose (an echo arrives in a *different* section) and restores
    # "running it twice does not grow your captions".
    caption_block_lines: list[str] = []
    first_seen_in: dict[str, int] = {}
    for section_index, body in enumerate(caption_sections):
        # Buffered per section rather than appended as we go, because a section
        # every one of whose content lines was already said elsewhere is an echo
        # whole, and its blank lines and rule lines are then framing nothing.
        # Emitting them anyway is not cosmetic: a page's own text, read back
        # beside a group block an earlier release wrote, repeats every word of
        # it but none of its layout, so the layout survived the dedup, was
        # stored, and came back again on the pass after -- one "---" of
        # unbounded growth per run, on exactly the archives E12 declines to
        # migrate. A section that never had a content line to lose is layout
        # somebody wrote on purpose and is kept as it always was.
        section_lines: list[str] = []
        section_had_content = False
        section_kept_content = False
        for line in body:
            key = " ".join(line.split()).lower()
            if not key:
                # A blank line is the author's paragraph break, not a caption;
                # it is kept as written and never counted as a duplicate, which
                # is what leaves a multi-paragraph note byte-identical after a
                # re-read.
                section_lines.append(line)
                continue
            if not _CAPTION_WORD_RE.search(key):
                # A wordless line is markdown structure, not content: the
                # transcription conventions put footnotes after a "---" rule, so
                # two files whose captions both carry footnotes hold that rule
                # line twice on purpose, and treating the second as a repeat
                # would glue one file's footnotes onto the other's prose. Like
                # the blank line above, it is layout and is kept as written.
                section_lines.append(line)
                continue
            section_had_content = True
            if first_seen_in.setdefault(key, section_index) != section_index:
                continue
            section_kept_content = True
            section_lines.append(line)
        if section_kept_content or not section_had_content:
            caption_block_lines.extend(section_lines)
    return "\n".join(caption_block_lines).strip("\n") or None


def _normalize_transcriptions(raw: object) -> dict[str, str] | None:
    """Reduce a reply's ``transcriptions`` value to the entries the contract keeps.

    The field is optional and model-written, so it arrives in whatever shape the
    model chose: possibly not a dict at all, possibly holding non-string keys or
    values, possibly padded with whitespace. Only string-to-string entries whose
    value still holds text after stripping survive.

    Args:
        raw: The reply's ``transcriptions`` value, verbatim.

    Returns:
        The surviving label-to-text entries, values stripped, or ``None`` when
        nothing survives -- never an empty dict, so a record either carries a
        usable map or carries no key at all.
    """
    if not isinstance(raw, dict):
        return None
    cleaned = {
        key: text
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, str) and (text := value.strip())
    }
    return cleaned or None


#: Heads a block of transcription that could not be attributed to one part --
#: the text of a chunk that answered with ``caption`` while its siblings
#: answered with ``transcriptions``. Deliberately not a part label, so nothing
#: files it to a file, and recognized by :func:`_reorder_caption_sections` so it
#: cannot be swallowed by the section it follows.
_UNATTRIBUTED_LABEL = "[Unattributed]"


def _reorder_caption_sections(caption: str, part_order: list[str]) -> str:
    """Sort a caption's bracket-labelled sections back into payload part order.

    Only ever used on a chunked group, and only for the sections whose labels
    this program assigned itself. Each chunk writes a caption covering the parts
    that chunk saw, and the partitioner does not hand out the parts in payload
    order: it puts ``Front``/``Back``/``Negative`` at the end of chunk 1 so a
    front and its back are never split (its rules 4 and 5). Concatenating the
    chunk captions therefore reads ``Page 1``..``Page 6``, ``Front``, ``Back``,
    ``Page 7``.., which is not the object's own order and is not what an
    unchunked call would have written.

    Sorting the CHUNKS does not fix that and cannot: chunk 1's caption is a
    single string that already holds pages 1-6 and both sides, so the
    out-of-order sections are inside it. The sections themselves are what has to
    move.

    This is a reordering and nothing else -- no line is edited, dropped or
    re-attributed. It is safe here, where the whole-caption-parsing the design
    otherwise avoids (D6) is not being attempted: labels are only recognized
    when they match a part this payload named, so a ``[Letter]`` or
    ``[Address]`` sub-label the model chose stays exactly where the model put
    it, inside its own part's section.

    Args:
        caption: The concatenated chunk captions.
        part_order: The part labels the payload sent, in payload order.

    Returns:
        The caption with its recognized sections in part order, or *caption*
        unchanged when it carries no recognized label.
    """
    rank = {label.casefold(): index for index, label in enumerate(part_order)}
    # Always last, and recognized even though it is not a part: it is the
    # boundary this module writes itself to fence off text that belongs to no
    # single file. Without it, unattributed text appended after the sections
    # would read as the continuation of whichever section happened to end up
    # last -- inventing exactly the attribution the warning beside it denies.
    rank[_UNATTRIBUTED_LABEL.strip("[]").casefold()] = len(part_order)
    preamble: list[str] = []
    sections: list[tuple[int, int, list[str]]] = []
    current: list[str] | None = None
    for line in caption.splitlines():
        stripped = line.strip()
        label = (
            stripped[1:-1].strip().casefold()
            if stripped.startswith("[") and stripped.endswith("]")
            else None
        )
        if label is not None and label in rank:
            current = [line]
            # Arrival index breaks ties, so two chunks that both wrote a
            # "[Front]" section stay in the order they were called rather than
            # being swapped by an unstable sort.
            sections.append((rank[label], len(sections), current))
            continue
        (preamble if current is None else current).append(line)
    if not sections:
        return caption
    ordered = [line for _, _, body in sorted(sections) for line in body]
    return "\n".join(preamble + ordered).strip("\n")


def _synthesize_caption(transcriptions: dict[str, str], part_order: list[str]) -> str:
    """Build the record's caption string from a per-part transcription map.

    Reproduces the shape the model used to write when it merged the parts
    itself, so the synthesized string enters ``_absorb_caption`` exactly where
    the model's own ``caption`` does and the caption block keeps its grammar:
    a lone-part payload is the bare text with no label (the
    lone-scan-carries-no-label rule), and a payload of two or more parts becomes
    ``[Label]\\n<text>`` sections joined by newlines in payload part order. A
    label the payload never named is appended after the ordered parts, in the
    map's own order, rather than dropped -- the model answered about something,
    and silently losing that answer is worse than an unexpected section.

    The bare form is decided by how many parts the payload SENT, not by how many
    came back with text. Those differ in the commonest inscribed-photo shape
    there is: a front/back pair whose writing is all on the back. Deciding on
    the survivor count dropped the ``[Back]`` label there, which the model's own
    ``caption`` path never does ("If there is text only on the back, only
    include a [Back] section"), and the loss did not stay cosmetic -- an
    unlabelled block is prose nobody attributed, so the next ``-rw`` run
    attributed it to the file it was read off and the back's writing became
    ``[Photo] ...`` on the front, permanently.

    Args:
        transcriptions: Part label to transcription text, normally as returned
            by :func:`_normalize_transcriptions`.
        part_order: The part labels the payload sent, in payload order.

    Returns:
        The synthesized caption, or ``""`` when no part contributes a section.
    """
    ordered = [label for label in part_order if label in transcriptions]
    ordered += [label for label in transcriptions if label not in part_order]
    sections = [(label, text) for label in ordered if (text := transcriptions[label].strip())]
    if not sections:
        return ""
    if len(sections) == 1 and len(part_order) <= 1:
        return sections[0][1]
    return "\n".join(f"[{label}]\n{text}" for label, text in sections)


def _missing_api_key_message(provider_label: str, env_var: str) -> str:
    return (
        f"{provider_label} provider selected but {env_var} is not set. "
        f'Set it for this terminal session and retry: $env:{env_var} = "..." (PowerShell) '
        f"or export {env_var}=... (macOS/Linux), then run the command again."
    )


def _missing_sdk_message(display_name: str, package: str, extra: str) -> str:
    """Message for a selected provider whose SDK is not importable.

    Names the SDKs that ARE installed when there are any: the user who
    installed only ``[anthropic]`` and landed on another provider needs
    ``--provider anthropic`` far more often than a second SDK.

    Args:
        display_name: The provider's user-facing name.
        package: The pip distribution the provider needs.
        extra: The photokin extra that installs it.

    Returns:
        The full missing-dependency message.
    """
    message = (
        f"{display_name} provider selected but the {package} package is not installed. "
        f'Run: pip install "photokin[{extra}]"'
    )
    installed = utils.installed_provider_sdks()
    if installed:
        alternatives = " or ".join(f"--provider {name}" for name in installed)
        message += f" - or switch to the SDK you already have: {alternatives}"
    return message


def _build_provider_client(config: utils.Config):
    """Build provider SDK client using the selected provider and API key env var.

    Fails fast with a normalized ``missing_api_key``/``missing_dependency``
    ``ProviderApiError`` rather than letting the underlying SDK raise deep
    inside the first request -- that's how a bare, unhelpful auth error from
    the SDK ends up as the top-level failure instead of a clear one naming
    the exact env var to set.
    """
    provider = utils.normalize_provider(config.provider)
    if provider == "anthropic":
        try:
            import anthropic
        except ImportError as exc:
            raise ProviderApiError(
                "missing_dependency",
                _missing_sdk_message("Anthropic", "anthropic", "anthropic"),
            ) from exc
        api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
        if not api_key:
            raise ProviderApiError("missing_api_key", _missing_api_key_message("Anthropic", "ANTHROPIC_API_KEY"))
        return anthropic.Anthropic(api_key=api_key)
    if provider == "gemini":
        try:
            from google import genai
            from google.genai import types as genai_types
        except ImportError as exc:
            raise ProviderApiError(
                "missing_dependency",
                _missing_sdk_message("Gemini", "google-genai", "gemini"),
            ) from exc
        api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
        if not api_key:
            raise ProviderApiError("missing_api_key", _missing_api_key_message("Gemini", "GEMINI_API_KEY"))
        # Unlike the Anthropic/OpenAI SDKs used here, google-genai has no
        # default request timeout -- observed in practice as a single
        # generate_content() call hanging indefinitely (over an hour, no
        # error, no response) with no way to detect or recover from it
        # short of killing the whole process. 3 minutes comfortably covers
        # every real per-photo response time seen in this pipeline (even
        # multi-image groups), while still failing well before a silent
        # hang can block an entire batch run.
        return genai.Client(api_key=api_key, http_options=genai_types.HttpOptions(timeout=180_000))
    if provider == "openrouter":
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ProviderApiError(
                "missing_dependency",
                _missing_sdk_message("OpenRouter", "openai", "openai"),
            ) from exc
        api_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
        if not api_key:
            # Do NOT pass api_key=None here: the OpenAI SDK would fall back to
            # OPENAI_API_KEY from the environment while still targeting the
            # OpenRouter base_url, leaking the wrong provider's secret to
            # OpenRouter. Require the OpenRouter key explicitly instead.
            raise ProviderApiError("missing_api_key", _missing_api_key_message("OpenRouter", "OPENROUTER_API_KEY"))
        base_url = (os.getenv("OPENROUTER_BASE_URL") or "").strip() or "https://openrouter.ai/api/v1"
        return OpenAI(api_key=api_key, base_url=base_url)
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise ProviderApiError(
            "missing_dependency",
            _missing_sdk_message("OpenAI", "openai", "openai"),
        ) from exc
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise ProviderApiError("missing_api_key", _missing_api_key_message("OpenAI", "OPENAI_API_KEY"))
    return OpenAI(api_key=api_key)


def _ensure_provenance_keyword(record: dict[str, Any], provider_name: str, model_name: str) -> None:
    """Ensure keywords include exactly one provider/model provenance marker."""
    keywords_raw = record.get("keywords")
    if not isinstance(keywords_raw, list):
        record["keywords"] = [f"{provider_name} {model_name} Analyzed"]
        return

    provenance = f"{provider_name} {model_name} Analyzed"
    filtered: List[str] = []
    for kw in keywords_raw:
        if not isinstance(kw, str):
            continue
        if kw.endswith(" Analyzed"):
            continue
        filtered.append(kw)
    filtered.append(provenance)
    record["keywords"] = filtered



def _should_run_archival_upload(provider: str) -> bool:
    """Return whether archival upload should run for the selected provider."""
    return utils.normalize_provider(provider) == "openai"


def _normalized_error_payload(exc: Exception) -> Dict[str, Any]:
    """Build an error payload with provider-normalized types where possible."""
    error_type = exc.__class__.__name__
    status_code = None
    provider_message = None
    retry_after = None

    if isinstance(exc, ProviderApiError):
        error_type = exc.error_type
        status_code = exc.status_code
        provider_message = exc.provider_message
        retry_after = exc.retry_after

    payload: Dict[str, Any] = {"type": error_type, "message": str(exc)}
    if status_code is not None:
        payload["status_code"] = int(status_code)
    # Both are extras beside ``message``, not replacements for it: a consumer
    # that only reads ``message`` keeps working unchanged, and one that wants
    # the provider's own wording without re-parsing a Python dict repr (see
    # ProviderApiError.provider_message) or the raw retry-after header reads
    # these instead.
    if provider_message is not None:
        payload["provider_message"] = provider_message
    if retry_after is not None:
        payload["retry_after"] = retry_after
    return payload


def _write_sidecar_document(data: Dict[str, Any], image_path: str, config: utils.Config) -> str | None:
    """Write an analysis document beside its image, warning instead of raising.

    The analysis is already paid for by the time this runs, so a sidecar that
    cannot be written must not take the record down with it and be reported as a
    model failure. An ``OSError`` escaping here reaches the batch loop's
    per-group handler, which discards the model's output, writes an error
    payload for every file of the group, and -- under
    ``strict_run_failures``, once no group has succeeded -- re-raises and loses
    the whole run. A read-only sidecar left by a previous run, a lock held by a
    sync client, a path over ``MAX_PATH`` or a full disk is enough to trigger it.

    Args:
        data: The analysis document to serialize.
        image_path: Image the sidecar belongs to; it supplies the destination
            directory and the ``.json`` stem.
        config: Run configuration, read for ``pretty_json``.

    Returns:
        The path written, or ``None`` when it could not be written, which has
        already been logged at WARNING.
    """
    img_dir = os.path.dirname(os.path.abspath(image_path))
    img_base = os.path.splitext(os.path.basename(image_path))[0]
    json_path = os.path.join(img_dir, f"{img_base}.json")
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2 if config.pretty_json else None, ensure_ascii=False)
    except (OSError, UnicodeError) as exc:
        # ``UnicodeError`` beside ``OSError`` for the same reason its markdown
        # twin catches it (``doc_sidecar.write_markdown_sidecar``): what is
        # being written is model-authored text, and a lone surrogate in it
        # raises UnicodeEncodeError -- a ValueError -- which an OSError-only
        # guard lets past, into the batch loop's per-group handler, discarding
        # the paid-for analysis this function exists to protect. ``ensure_ascii``
        # is False here, so the surrogate reaches the encoder rather than being
        # escaped on the way out.
        logger.warning(
            "Sidecar not written for %s (%s): the analysis is kept in the results.",
            os.path.basename(image_path),
            exc,
        )
        return None
    return json_path


def analyze_photo(
    front_path: str,
    back_path: str | None = None,
    config: utils.Config = utils.Config(),
    *,
    original_meta: dict | None = None,
    write_sidecar: bool = False
) -> Dict[str, Any]:
    """Run the full analysis pipeline for one photo (front + optional back).

    The function centralizes all validation and uploads so every entry point
    (CLI, manifest, or Lightroom plug-in) benefits from the same guardrails.
    The intentional ordering—normalize paths → upload lossless originals →
    downscale for model calls → call + parse → post-process—matches the data
    ownership requirements of the workflow.  Reordering these steps would make
    it harder to reason about failures (e.g., parsing errors would hide upload
    issues), so the docstring highlights why the structure is fixed.
    """
    # Normalize & verify
    front = utils.normalize_path(front_path) or ""
    back = utils.normalize_path(back_path) if back_path else None
    paths = [front] + ([back] if back else [])
    utils.ensure_paths_exist([p for p in paths if p])

    if not (1 <= config.jpeg_quality <= 100):
        raise ValueError("--jpeg-quality must be between 1 and 100.")

    # Resolve package-internal prompt resources & defaults
    utils.resolve_default_paths(config)

    # Load vocabulary (for new keyword detection)
    sections, new_keywords_log = utils.load_vocab_sections(config.vocab_path)
    known_keywords = utils.flatten_known_keywords(sections, new_keywords_log)

    provider = utils.normalize_provider(config.provider)
    provider_name = utils.provider_display_name(provider)
    client = _build_provider_client(config)
    model_name = utils.resolve_model_for_provider(config)
    today = date.today().isoformat()

    # Archival upload (lossless path; model call uses data URLs below)
    if _should_run_archival_upload(provider):
        for idx, p in enumerate(paths):
            if not p:
                continue
            fid = utils.archival_upload(client, p, config.jpeg_quality, purpose="user_data")
            label = "front" if idx == 0 else "back"
            logger.info("Uploaded %s image (file_id=%s)", label, fid)
    else:
        logger.info("Skipping archival upload for provider %s (Files API unsupported).", provider)

    # Data URLs + sizes (for the multimodal call)
    image_data_urls: List[str] = []
    image_byte_sizes: List[int] = []
    image_meta: List[dict] = []
    for p in paths:
        if not p:
            image_data_urls.append("")
            image_byte_sizes.append(0)
            image_meta.append({"mime": None, "width": None, "height": None, "resized": False})
            continue
        url, nbytes, meta = utils.build_data_url_and_size(p, config.jpeg_quality, config.max_edge)
        image_data_urls.append(url)
        image_byte_sizes.append(nbytes)
        image_meta.append(meta)

    # Console note about payload sizes
    labels = ["front", "back"]
    for i, sz in enumerate(image_byte_sizes):
        if i >= len(paths) or not paths[i]:
            continue
        dims = image_meta[i]
        wh = f"{dims.get('width')}x{dims.get('height')}" if dims.get("width") and dims.get("height") else "unknown"
        logger.info(
            "Payload bytes for %s image sent to model: %d bytes (%s, %s)",
            labels[i],
            sz,
            wh,
            dims.get("mime"),
        )

    # Prompts (include forwarded metadata if present and allowed by metadata_forward_path)
    forward_fields = None
    try:
        if config.metadata_forward_path and os.path.isfile(config.metadata_forward_path):
            with open(config.metadata_forward_path, "r", encoding="utf-8") as fh:
                mp = json.load(fh)
            forward_fields = mp.get("forward_fields")
    except (OSError, json.JSONDecodeError) as exc:
        if os.getenv("MEL_VERBOSE") or os.getenv("MEL_DEBUG"):
            logger.warning("Failed to load forwarded metadata: %s", exc)

    prompt_items = utils.build_prompt_bundle(
            model_name,
            today,
            provider_name = provider_name,
            forwarded_meta = original_meta,
            forward_fields = forward_fields,
            cfg = config,
    )

    dump_request_writer = _build_llm_dump_writer(config, front, "single")

    # Call model + robust JSON parsing (cleanup + retries)
    def _retry_once_resend_images(extra_instruction: str):
        prompts2 = list(prompt_items) + [{"type": "input_text", "text": extra_instruction}]
        r2 = call_model(client, model_name, prompts2, image_data_urls, provider=provider, dump_request=dump_request_writer)  # re-send images
        return extract_output_text(r2, provider=provider)

    # 1st attempt
    resp = call_model(client, model_name, list(prompt_items), image_data_urls, provider=provider, dump_request=dump_request_writer)
    usage = utils.extract_usage(resp)
    resolved_model_name = get_response_model(resp, model_name)
    raw = extract_output_text(resp, provider=provider)

    # If empty/whitespace, retry immediately with images
    if not raw or not raw.strip():
        raw = _retry_once_resend_images(
            "You MUST return strictly valid JSON only — no markdown, no code fences, no triple quotes. Use \\n inside JSON strings."
        )

    # Parse with cleanup + one more retry (text+images) if needed
    def _retry_once():
        return _retry_once_resend_images(
            "Final attempt: Return ONLY valid JSON. No commentary. If unsure, return an empty JSON object with the correct keys and nulls."
        )

    data, _raw_used = utils.parse_with_retry(
        raw, _retry_once, config=config, source_path=front,
    )

    # Normalize main key to front path
    main_key = front
    result_obj = data.get("result", {})
    if main_key not in result_obj:
        if isinstance(result_obj, dict) and len(result_obj) == 1:
            only_key = next(iter(result_obj.keys()))
            data["result"] = {main_key: result_obj[only_key]}
        else:
            data = {"result": {main_key: result_obj}}
    result_obj = data["result"]

    if isinstance(result_obj, dict):
        for rec in result_obj.values():
            if isinstance(rec, dict) and "ai_caption" in rec:
                rec["ai_caption"] = inject_analysis_date(rec.get("ai_caption"), date.fromisoformat(today))

    record = result_obj[main_key]
    _ensure_provenance_keyword(record, provider_name, resolved_model_name)

    # Per-part transcriptions, when the model chose to send them. Optional,
    # always: a reply without the map -- or with one nothing survives from --
    # takes today's path verbatim, and no retry is spent demanding the field.
    # When the map survives, the caption is synthesized from it rather than
    # trusted from the reply: per-part attribution exists only at generation
    # time, and the model is told it may omit ``caption`` beside the map.
    transcriptions = _normalize_transcriptions(record.pop("transcriptions", None))
    if transcriptions is not None:
        record["transcriptions"] = transcriptions
        record["caption"] = _synthesize_caption(
            transcriptions, ["Front", "Back"] if back else ["Front"]
        )

    # Attach transport info for auditing
    sent = {
        "front": {
            "bytes": int(image_byte_sizes[0]) if len(image_byte_sizes) > 0 else None,
            "mime": image_meta[0].get("mime") if image_meta else None,
            "width": image_meta[0].get("width") if image_meta else None,
            "height": image_meta[0].get("height") if image_meta else None,
            "resized": image_meta[0].get("resized") if image_meta else None,
        }
    }
    if len(image_byte_sizes) > 1 and back:
        sent["back"] = {
            "bytes": int(image_byte_sizes[1]),
            "mime": image_meta[1].get("mime"),
            "width": image_meta[1].get("width"),
            "height": image_meta[1].get("height"),
            "resized": image_meta[1].get("resized"),
        }
    record["_transport"] = {"max_edge": config.max_edge, "jpeg_quality": config.jpeg_quality, "sent": sent}

    # Forbidden-ish warnings
    kws = record.get("keywords", []) or []
    warn_list = utils.warn_forbiddenish_keywords(kws)
    if warn_list:
        for w in warn_list:
            logger.warning("%s", w)
        if config.fail_on_forbidden:
            raise SystemExit(2)

    # New keywords → TOML (reuse the already-loaded vocab data). The provenance
    # tag is auto-added by this tool, not proposed content, so (like the
    # forbidden-word check above) it is exempt rather than treated as an
    # unproposed keyword every single run.
    new_kws = [
        k for k in (record.get("keywords") or [])
        if k not in known_keywords and not (isinstance(k, str) and k.strip().endswith(" Analyzed"))
    ]
    proposed_raw = record.get("proposed_new_keywords")
    if not isinstance(proposed_raw, list):
        logger.warning('"proposed_new_keywords" missing or invalid; skipping vocab updates.')
        proposed = []
        skip_vocab_updates = True
    else:
        proposed = proposed_raw
        skip_vocab_updates = False
    proposed_map = {p.get("keyword"): p for p in proposed if isinstance(p, dict) and p.get("keyword")}
    record["_usage"] = usage

    inserted_count = 0
    if new_kws and not config.no_update_vocab:
        utils.safe_backup(config.vocab_path)
        try:
            for k in new_kws:
                if skip_vocab_updates:
                    logger.warning(
                        'Skipping keyword "%s" because proposed_new_keywords is missing.', k
                    )
                    continue
                if not isinstance(k, str):
                    logger.warning("Skipping non-string keyword in new keyword list.")
                    continue
                if k.upper().startswith("PC-"):
                    logger.warning('Skipping keyword "%s" (PC- prefix not allowed).', k)
                    continue
                if k.strip().lower() in utils.PART_MARKER_KEYWORDS:
                    # Approving one would teach the model to propose a token the
                    # fan-out then strips from every file it does not describe.
                    logger.warning('Skipping keyword "%s" (part marker, not a vocabulary keyword).', k)
                    continue

                p = proposed_map.get(k)
                if not p:
                    logger.info('Keyword "%s" was used on this photo but not added to the vocabulary.', k)
                    continue

                section = (p.get("section") or "").strip()
                note = (p.get("note") or "").strip()
                if not section:
                    logger.warning(
                        'Skipping keyword "%s" (missing section in proposed_new_keywords).', k
                    )
                    continue
                if utils.note_looks_placeholder(note):
                    logger.warning('Skipping keyword "%s" (note is missing or placeholder).', k)
                    continue

                if utils.insert_keyword_into_vocab_file(config.vocab_path, section, k, note):
                    inserted_count += 1

            if inserted_count:
                logger.info(
                    "Vocabulary updated (%d new keyword(s) inserted into %s)",
                    inserted_count,
                    config.vocab_path,
                )
        except Exception as e:
            msg = (
                "Vocabulary update failed TOML validation. "
                f"Review {config.vocab_path} and restore {config.vocab_path}.bak if needed."
            )
            logger.error("%s (%s)", msg, e)
            raise RuntimeError(msg) from e

    # Optional per-photo sidecar
    json_path = _write_sidecar_document(data, front, config) if write_sidecar else None
    if json_path:
        logger.info("Analysis completed for %s; JSON saved as %s", os.path.basename(front), json_path)
    else:
        # Deliberately silent about whether a sidecar exists: batch callers turn
        # this off so they can write the variant-enriched record themselves, and
        # a write that failed has already said so.
        logger.info("Analysis completed for %s", os.path.basename(front))

    return data


# === Chunked group calls and the consolidation pass ===
#
# A group's payload has no upper bound: a sixty-page memoir is sixty images in
# one request. ``chunking.partition_parts`` splits that into bounded, contiguous
# blocks; the loop in :func:`analyze_group_parts` sends one call per block and
# then one final text-only call that reconciles what the blocks each concluded
# on their own. The helpers below are the parts of that path pure enough to live
# outside the analyzer and be tested without a provider.

#: A ``Page N`` part label, with its number captured. The part-label vocabulary
#: is frozen by ``docs/document-mode-contract.md`` section 1, so this is
#: deliberately narrow rather than guessing at other spellings.
_GROUP_PAGE_LABEL_RE = re.compile(r"^Page\s+(\d+)$", re.IGNORECASE)

#: The record fields the consolidation pass may replace on a chunked group.
#: ``caption`` and ``transcriptions`` are pointedly absent: that call never saw
#: the pages, so letting it rewrite a transcription would launder away exactly
#: the fidelity the transcription conventions exist to protect.
#:
#: ``proposed_new_keywords`` is absent for the same reason, and its absence is
#: load-bearing rather than tidy. A proposal is a claim about a keyword's
#: justification -- the section it belongs in and a note saying what it captures
#: -- made by a call that looked at the page the keyword came off. The
#: consolidation call looked at no page. Letting it replace the list meant the
#: empty array it is asked for overwrote the proposals ``_fold_chunk_records``
#: had just folded from the chunks, and the vocabulary block downstream then
#: rejected every new keyword for want of a proposal: a chunked group could
#: never teach the vocabulary anything, which it could before chunking existed.
_CONSOLIDATED_FIELDS = (
    "keywords",
    "title",
    "category",
    "ai_caption",
    "location_guess",
    "date_guess",
)

#: The page-order flags a consolidation reply may set, per
#: ``docs/document-mode-contract.md`` section 6. Anything else is dropped: these
#: reach the record and the sidecar's frontmatter, where only a value from this
#: set means anything to a reader or a downstream tool.
_PAGE_ORDER_FLAGS = frozenset(
    {"out_of_order", "missing_page_before", "missing_page_after", "duplicate_page"}
)

#: The shape each consolidated field must arrive in before it may replace what
#: the chunks concluded. The consolidation reply is model-written JSON, so it
#: can be perfectly valid JSON and still give a field the wrong type -- and the
#: chunk answer it would displace was built from calls that actually saw the
#: pages. A mistyped field is dropped in favour of that, rather than accepted
#: and left to break something further downstream.
_CONSOLIDATED_FIELD_TYPES: dict[str, type] = {
    "keywords": list,
    "title": str,
    "category": str,
    "ai_caption": str,
    "location_guess": dict,
    "date_guess": dict,
}


def _page_number_from_label(label: str) -> int | None:
    """Return the page number a ``Page N`` part label carries.

    Args:
        label: A payload part label, e.g. ``"Page 3"`` or ``"Front"``.

    Returns:
        The number for a page label, or ``None`` for any other part.
    """
    match = _GROUP_PAGE_LABEL_RE.match(label.strip())
    return int(match.group(1)) if match else None


def _part_counts(parts: list[tuple[str, list[str]]]) -> list[dict[str, Any]]:
    """Return one ``{"label", "count"}`` entry per part, in payload order.

    Args:
        parts: Ordered ``(label, [paths])`` parts.

    Returns:
        The per-part image counts, in the shape both the prompt note and the
        ``_transport`` record describe them in.
    """
    return [{"label": label, "count": len(paths)} for label, paths in parts]


def _build_group_variants_note(parts: list[tuple[str, list[str]]], image_count: int) -> str:
    """Build the GROUP VARIANTS NOTE one group call prefixes to its prompt.

    A group reaches this analyzer for its part labels as much as for its size:
    a lone negative or a lone album page is one image that still has to be
    named as the part it is. Telling the model it is seeing several would be
    contradicted by the payload it can count for itself -- so the claim is made
    from the payload actually being sent, which for a chunked group is that
    chunk's parts rather than the whole object's.

    Args:
        parts: The parts this call carries, in payload order.
        image_count: How many images this call carries.

    Returns:
        The note text.
    """
    extra_lines: list[str] = [
        "GROUP VARIANTS NOTE:",
        "You are seeing multiple scans or variants of the same physical photograph or document."
        if image_count > 1
        else "You are seeing a single scan of one part of a physical photograph or document.",
    ]

    for idx, entry in enumerate(_part_counts(parts)):
        prefix = "The first" if idx == 0 else "The next"
        extra_lines.append(f"{prefix} {entry['count']} image(s) are {entry['label']} variants of the item.")

    extra_lines.extend(
        [
            "Analyze all provided images together as one unified item, preserving the part order given.",
            # This note is the LAST thing the model reads before the images, so
            # an unqualified "preserving line breaks" here outranked the LINE
            # BREAKS rules stated tens of thousands of characters earlier and
            # undid the whole flowed-prose convention. It points at them now
            # instead of contradicting them.
            (
                "When filling the caption field, transcribe all visible text from each part "
                "across all variants, merging duplicates and applying the LINE BREAKS rules "
                "above: paragraph and deliberate breaks are kept, and the wrapped lines of "
                "running prose are joined."
            ),
            "Do NOT describe the scene in the caption field; only transcribed text (with [ ] for guesses and semi-illegible text).",
            "Describe the visual scene and give 3–6 sentences of cautious but comprehensive historical analysis ONLY in the ai_caption field, starting with '[AI Analysis]:'.",
        ]
    )
    return "\n".join(extra_lines)


def _build_chunk_note(
    chunk_parts: list[tuple[str, list[str]]],
    chunk_index: int,
    chunk_count: int,
    total_pages: int,
) -> str:
    """Build the note telling one chunk call that it is seeing part of an object.

    A chunked call is otherwise indistinguishable from a complete one: handed
    eight pages of a sixty-page memoir and told nothing, a model describes an
    eight-page document, dates it from the only dateline it can see, and tries
    to finish the sentence that runs off the last page. The note supplies the
    three things it cannot work out for itself -- which parts of how many these
    are, that the object continues past them, and that everything it concludes
    about the object as a whole is provisional -- and forecloses the one thing
    it might wrongly conclude from those: that the transcription conventions
    relax because the payload is partial.

    Args:
        chunk_parts: The parts this call carries, in payload order.
        chunk_index: Zero-based index of this chunk among the group's chunks.
        chunk_count: How many chunk calls the group takes in total.
        total_pages: How many page parts the whole group holds; ``0`` when it
            holds none, in which case the total is left unstated rather than
            claimed as zero.

    Returns:
        The note text, ready to append to the call's prompt items.
    """
    labels = ", ".join(label for label, _ in chunk_parts)
    of_pages = f", of {total_pages} pages in the whole object" if total_pages else ""
    return "\n".join(
        [
            "CHUNK NOTE:",
            f"This request carries block {chunk_index + 1} of {chunk_count} of ONE physical object.",
            f"It holds these parts: {labels}{of_pages}.",
            # The last block is told the truth about being last. Telling it the
            # object continues, and that some other block finishes its trailing
            # sentence, is false there -- and actively harmful: a document whose
            # final page genuinely stops mid-sentence, or is followed by a
            # missing sheet, is exactly the evidence the page-order pass needs,
            # and no later call exists to recover it if this one is talked out
            # of reporting what it sees.
            (
                "The object continues beyond these images. The other blocks are being read in "
                "separate requests and the calling program joins the results afterwards."
                if chunk_index < chunk_count - 1
                else "This is the LAST block of the object. The blocks before it are being read "
                "in separate requests and the calling program joins the results afterwards; "
                "nothing follows this one."
            ),
            (
                "Transcribe only what is in front of you. Do not summarize or invent the pages "
                "you were not sent, and do not complete a sentence that runs off the last page "
                "here -- the page that finishes it is in another block."
                if chunk_index < chunk_count - 1
                else "Transcribe only what is in front of you. Do not summarize or invent the "
                "pages you were not sent. If the text simply stops part-way through a sentence "
                "on the last page here, transcribe it exactly as it stops -- that is evidence "
                "about the object, not a mistake to tidy up."
            ),
            "The transcription conventions are unchanged. Apply them exactly as you would to a "
            + "complete object; a partial payload changes nothing about how text is recorded.",
            "Your metadata for this request -- keywords, title, category, date_guess and "
            + "location_guess -- is PROVISIONAL. A consolidation step reconciles it with the "
            + "other blocks afterwards, so answer from the evidence in these images alone rather "
            + "than guessing at what the pages you cannot see contain.",
        ]
    )


def _build_consolidation_payload(
    parts: list[tuple[str, list[str]]],
    transcriptions: dict[str, str],
    chunks: list[list[tuple[str, list[str]]]],
    chunk_records: list[dict[str, Any]],
    main_key: str,
) -> str:
    """Serialize the text-only evidence the consolidation call reasons over.

    JSON rather than prose because every field of it is already structured and
    every transcription in it is free to hold blank lines, blockquotes and
    ``---`` rules of its own -- a prose framing would have to invent a
    delimiter that the transcriptions are entitled to contain.

    Args:
        parts: The whole group's ordered parts.
        transcriptions: The union of the chunks' transcription maps.
        chunks: The parts each chunk call carried, in call order.
        chunk_records: Each chunk's parsed record, aligned with ``chunks``.
        main_key: The path the consolidated ``result`` must be keyed by.

    Returns:
        The payload as pretty-printed JSON.
    """
    payload = {
        "main_image_path": main_key,
        "parts": [
            {
                "label": label,
                "files": [os.path.basename(path) for path in paths],
                "page_from_filename": _page_number_from_label(label),
                "transcription": transcriptions.get(label),
            }
            for label, paths in parts
        ],
        "provisional_metadata_by_block": [
            {
                "block": idx + 1,
                "parts_seen": [label for label, _ in chunk],
                "keywords": record.get("keywords"),
                "title": record.get("title"),
                "category": record.get("category"),
                "ai_caption": record.get("ai_caption"),
                "date_guess": record.get("date_guess"),
                "location_guess": record.get("location_guess"),
                # A block that answered with ``caption`` rather than
                # ``transcriptions`` contributes nothing to ``parts`` above --
                # every part it saw shows a null transcription there. Without
                # its caption here, this call would judge the title, the date
                # and above all the page order of a document whose middle it
                # was never shown, while its verdict outranks the answers from
                # the calls that did see those pages. Present only when it is
                # the block's sole record of the text.
                **(
                    {"unattributed_transcription": record.get("caption")}
                    if not _normalize_transcriptions(record.get("transcriptions"))
                    and isinstance(record.get("caption"), str)
                    and record.get("caption", "").strip()
                    else {}
                ),
            }
            for idx, (chunk, record) in enumerate(zip(chunks, chunk_records))
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _normalize_page_order(raw: object, part_labels: set[str]) -> dict[str, dict[str, Any]] | None:
    """Reduce a consolidation reply's ``page_order`` to what the record may carry.

    As tolerant as the main parser, and for the same reason: a page-order
    verdict is a finding about the scan order, never something the group's
    metadata depends on, so a reply that garbles it must cost the group
    nothing. Entries naming a part the payload never sent are dropped -- the
    map is looked up by part label downstream, so a label no file resolves to
    could only ever be dead weight in a sidecar.

    Args:
        raw: The reply's ``page_order`` value, verbatim.
        part_labels: The part labels this group actually sent.

    Returns:
        Label to ``{"page": int}`` plus an optional ``"flags"`` list, or
        ``None`` when nothing usable survives -- never an empty dict.
    """
    if not isinstance(raw, dict):
        return None
    cleaned: dict[str, dict[str, Any]] = {}
    for label, entry in raw.items():
        if not isinstance(label, str) or label not in part_labels or not isinstance(entry, dict):
            continue
        page = entry.get("page")
        if isinstance(page, str) and page.strip().isdigit():
            page = int(page.strip())
        if isinstance(page, bool) or not isinstance(page, int):
            continue
        raw_flags = entry.get("flags")
        # Filtered against the frozen vocabulary, not merely against emptiness:
        # a flag becomes record data and sidecar frontmatter, and a consumer can
        # only act on the four values the contract names. A hallucinated fifth
        # would be persisted as though it meant something.
        flags = (
            [
                flag.strip()
                for flag in raw_flags
                if isinstance(flag, str) and flag.strip() in _PAGE_ORDER_FLAGS
            ]
            if isinstance(raw_flags, list)
            else []
        )
        cleaned[label] = {"page": page, "flags": flags} if flags else {"page": page}
    return cleaned or None


def _fold_chunk_records(chunk_records: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine per-chunk records into one group record without the model's help.

    The consolidation pass is the intended author of a chunked group's final
    metadata; this is what stands in when its reply is missing or unusable. It
    keeps the first chunk's answer as the base -- chunk 1 holds the opening
    pages, which is where a letterhead, a salutation and a dateline almost
    always are -- then widens it exactly the way the group merge in
    ``process_manifest_stream`` widens a multi-analysis group: union the
    keywords, take the highest-confidence date and location guess.

    Args:
        chunk_records: Each chunk's parsed record, in call order. Never empty;
            not mutated.

    Returns:
        A new record dict. ``proposed_new_keywords`` is set only when at least
        one chunk sent a list for it, so a group whose model never sent the
        field still reaches the vocabulary block as the missing field it is
        rather than as an empty approval list.
    """
    folded = deepcopy(chunk_records[0])
    folded["keywords"] = utils.union_keywords(
        *[record.get("keywords") or [] for record in chunk_records]
    )

    for field in ("location_guess", "date_guess"):
        best = None
        best_conf = -1.0
        for record in chunk_records:
            guess = record.get(field)
            if not isinstance(guess, dict):
                continue
            conf = guess.get("confidence")
            if isinstance(conf, (int, float)) and float(conf) > best_conf:
                best_conf = float(conf)
                best = guess
        if best is not None:
            folded[field] = deepcopy(best)

    if any(isinstance(record.get("proposed_new_keywords"), list) for record in chunk_records):
        proposed: list[Any] = []
        seen: set[str] = set()
        for record in chunk_records:
            for entry in record.get("proposed_new_keywords") or []:
                keyword = entry.get("keyword") if isinstance(entry, dict) else None
                if isinstance(keyword, str) and keyword not in seen:
                    seen.add(keyword)
                    proposed.append(entry)
        folded["proposed_new_keywords"] = proposed

    return folded


def _sum_usages(usages: list[dict | None]) -> dict[str, Any]:
    """Sum the token usage of every call one chunked group made.

    Matches the summation shape ``process_manifest_stream`` already uses for a
    group holding more than one analysis, including its habit of reporting a
    zero as ``None`` so an absent count is never read as a free call.

    Args:
        usages: One entry per call -- every chunk call plus the consolidation
            call. A ``None`` entry (a provider that reported no usage) counts
            as zero.

    Returns:
        The combined usage dict, carrying the first model string any call
        reported.
    """

    def _tok(usage: dict | None, key: str) -> int:
        value = usage.get(key) if usage else None
        return int(value) if isinstance(value, int) else 0

    prompt_tokens = sum(_tok(usage, "prompt_tokens") for usage in usages)
    completion_tokens = sum(_tok(usage, "completion_tokens") for usage in usages)
    model_name = next(
        (usage.get("model") for usage in usages if usage and usage.get("model")), None
    )
    return {
        "prompt_tokens": prompt_tokens or None,
        "completion_tokens": completion_tokens or None,
        "input_tokens": prompt_tokens or None,
        "output_tokens": completion_tokens or None,
        "total_tokens": (prompt_tokens + completion_tokens) or None,
        "model": model_name,
    }


def analyze_group_parts(
    parts: list[tuple[str, list[str]]],
    config: utils.Config = utils.Config(),
    *,
    original_meta: dict | None = None,
    write_sidecar: bool = False,
) -> Dict[str, Any]:
    """
    Group-aware analysis for ordered document/photo parts (front/back or multi-page).

    ``parts`` is a list of (label, [paths]) tuples. Labels describe the logical
    part ("Front", "Back", "Page 1", "Page 2", ...). Paths inside a label are
    variant scans of that part. All images are analyzed together in the order
    provided by ``parts``.

    A payload larger than ``config.max_images_per_call`` is split into
    contiguous chunks (``chunking.partition_parts``), sent as one sequential
    call each, and reconciled by a final text-only consolidation call whose
    verdict on the page order lands on the record as ``page_order`` /
    ``page_order_notes``. Chunking happens here rather than at the call site so
    that this function's signature, and every caller of it, are unchanged --
    and a group that fits in one payload takes exactly the path it did before
    chunking existed: one call, the same prompt items in the same order.
    """
    norm_parts: list[tuple[str, list[str]]] = []
    for label, paths in parts:
        lbl = str(label).strip() or "Part"
        normalized: list[str] = []
        for p in (paths or []):
            np = utils.normalize_path(p)
            if np and np not in normalized:
                normalized.append(np)
        if normalized:
            norm_parts.append((lbl, normalized))

    if not norm_parts:
        raise ValueError("analyze_group_parts requires at least one image")

    flat_paths: list[str] = []
    path_labels: list[str] = []
    for lbl, plist in norm_parts:
        for p in plist:
            if p not in flat_paths:
                flat_paths.append(p)
                path_labels.append(lbl)

    utils.ensure_paths_exist(flat_paths)

    if not (1 <= config.jpeg_quality <= 100):
        raise ValueError("--jpeg-quality must be between 1 and 100.")

    main_key = flat_paths[0]

    utils.resolve_default_paths(config)

    sections, new_keywords_log = utils.load_vocab_sections(config.vocab_path)
    known_keywords = utils.flatten_known_keywords(sections, new_keywords_log)

    provider = utils.normalize_provider(config.provider)
    provider_name = utils.provider_display_name(provider)
    client = _build_provider_client(config)
    model_name = utils.resolve_model_for_provider(config)
    today = date.today().isoformat()

    if _should_run_archival_upload(provider):
        for idx, p in enumerate(flat_paths):
            if not p:
                continue
            fid = utils.archival_upload(client, p, config.jpeg_quality, purpose="user_data")
            role = path_labels[idx] if idx < len(path_labels) else "part"
            logger.info("Uploaded %s variant image (file_id=%s)", role, fid)
    else:
        logger.info("Skipping archival upload for provider %s (Files API unsupported).", provider)

    image_data_urls: List[str] = []
    image_byte_sizes: List[int] = []
    image_meta: List[dict] = []

    for p in flat_paths:
        url, nbytes, meta = utils.build_data_url_and_size(p, config.jpeg_quality, config.max_edge)
        image_data_urls.append(url)
        image_byte_sizes.append(nbytes)
        image_meta.append(meta)

    for idx, sz in enumerate(image_byte_sizes):
        if idx >= len(flat_paths) or not flat_paths[idx]:
            continue
        dims = image_meta[idx]
        wh = (
            f"{dims.get('width')}x{dims.get('height')}"
            if dims.get("width") and dims.get("height")
            else "unknown"
        )
        role = path_labels[idx] if idx < len(path_labels) else "part"
        logger.info(
            "Payload bytes for %s variant %d sent to model: %d bytes (%s, %s)",
            role,
            idx + 1,
            sz,
            wh,
            dims.get("mime"),
        )

    forward_fields = None
    try:
        if config.metadata_forward_path and os.path.isfile(config.metadata_forward_path):
            with open(config.metadata_forward_path, "r", encoding="utf-8") as fh:
                mp = json.load(fh)
            forward_fields = mp.get("forward_fields")
    except (OSError, json.JSONDecodeError) as exc:
        if os.getenv("MEL_VERBOSE") or os.getenv("MEL_DEBUG"):
            logger.warning("Failed to load forwarded metadata: %s", exc)

    prompt_bundle = utils.build_prompt_bundle(
            model_name,
            today,
            provider_name = provider_name,
            forwarded_meta = original_meta,
            forward_fields = forward_fields,
            cfg = config,
    )

    group_label = os.path.basename(main_key)
    url_by_path = dict(zip(flat_paths, image_data_urls))
    dump_request_writer = _build_llm_dump_writer(config, main_key, "group")

    def _analyze_parts_once(
        chunk_parts: list[tuple[str, list[str]]],
        chunk_note: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict | None, str]:
        """Make one model call over ``chunk_parts`` and parse what came back.

        The whole of one group call lives here so that a group small enough to
        fit in a single payload takes a path byte-identical to the unchunked
        one: same prompt items in the same order, one call, and -- with
        ``chunk_note`` ``None`` -- nothing appended that says otherwise.

        Args:
            chunk_parts: The parts this call carries, in payload order.
            chunk_note: The note appended after the group note for a chunked
                call, or ``None`` for a call carrying the whole group.

        Returns:
            ``(data, record, usage, resolved_model_name)``: the parsed reply,
            the record under this call's main key, its token usage (``None``
            when the provider reported none) and the model string the provider
            answered with.

        Raises:
            ValueError: The reply held no usable ``result`` for the main key.
        """
        chunk_flat: list[str] = []
        for _label, plist in chunk_parts:
            for path in plist:
                if path not in chunk_flat:
                    chunk_flat.append(path)
        chunk_urls = [url_by_path[path] for path in chunk_flat]
        chunk_key = chunk_flat[0]

        call_items = list(prompt_bundle) + [
            {
                "type": "input_text",
                "text": _build_group_variants_note(chunk_parts, len(chunk_flat)),
            }
        ]
        if chunk_note:
            call_items.append({"type": "input_text", "text": chunk_note})

        def _retry_once_resend_images(extra_instruction: str) -> str:
            prompts2 = list(call_items) + [{"type": "input_text", "text": extra_instruction}]
            r2 = call_model(client, model_name, prompts2, chunk_urls, provider=provider, dump_request=dump_request_writer)
            return extract_output_text(r2, provider=provider)

        resp = call_model(client, model_name, list(call_items), chunk_urls, provider=provider, dump_request=dump_request_writer)
        call_usage = utils.extract_usage(resp)
        call_model_name = get_response_model(resp, model_name)
        raw = extract_output_text(resp, provider=provider)

        if not raw or not raw.strip():
            raw = _retry_once_resend_images(
                "You MUST return strictly valid JSON only — no markdown, no code fences, "
                "no commentary. Use literal \\n characters inside JSON strings for line breaks."
            )

        def _retry_once() -> str:
            return _retry_once_resend_images(
                "Final attempt: Return ONLY valid JSON. No commentary, no markdown. "
                "If you cannot comply, return an empty JSON object with the correct keys and nulls."
            )

        call_data, _raw_used = utils.parse_with_retry(
            raw, _retry_once, config=config, source_path=chunk_key,
        )

        result_obj = call_data.get("result", {}) or {}
        if chunk_key not in result_obj:
            if isinstance(result_obj, dict) and len(result_obj) == 1:
                only_key = next(iter(result_obj.keys()))
                call_data["result"] = {chunk_key: result_obj[only_key]}
                result_obj = call_data["result"]
            else:
                raise ValueError(
                    f"Model output did not contain expected main key {chunk_key!r} "
                    f"and could not be normalized."
                )

        if isinstance(result_obj, dict):
            for rec in result_obj.values():
                if isinstance(rec, dict) and "ai_caption" in rec:
                    rec["ai_caption"] = inject_analysis_date(rec.get("ai_caption"), date.fromisoformat(today))

        call_record = result_obj.get(chunk_key) or {}
        if not isinstance(call_record, dict):
            raise ValueError("Model output for main result was not an object/dict.")

        _ensure_provenance_keyword(call_record, provider_name, call_model_name)

        # Per-part transcriptions, when the model chose to send them. Optional,
        # always: a reply without the map -- or with one nothing survives from --
        # takes today's path verbatim, and no retry is spent demanding the field.
        # When the map survives, the caption is synthesized from it in payload part
        # order rather than trusted from the reply: per-part attribution exists
        # only at generation time, and the model is told it may omit ``caption``
        # beside the map.
        call_transcriptions = _normalize_transcriptions(call_record.pop("transcriptions", None))
        if call_transcriptions is not None:
            call_record["transcriptions"] = call_transcriptions
            call_record["caption"] = _synthesize_caption(
                call_transcriptions, [label for label, _ in chunk_parts]
            )

        return call_data, call_record, call_usage, call_model_name

    def _run_consolidation(payload_text: str) -> tuple[dict[str, Any] | None, dict | None]:
        """Make the group's one text-only consolidation call, parsed tolerantly.

        No images are re-sent: the per-chunk transcriptions are the evidence,
        and paying for the pixels a second time would buy a pass that is only
        allowed to reason over the text anyway. Nothing here may fail the
        group, so every way the reply can be unusable ends in ``None`` and an
        INFO line rather than an exception -- the chunk results are already a
        complete answer, just an unreconciled one.

        Args:
            payload_text: The consolidation input, as JSON.

        Returns:
            ``(parsed_reply, usage)``. The reply is ``None`` when it was
            empty or not usable JSON; the usage is whatever the call reported
            either way, since the call was made and billed regardless.
        """
        consolidation_items = list(prompt_bundle) + [
            {
                "type": "input_text",
                "text": utils._read_text(utils._resolve_prompt_file("consolidation.txt", config)),
            },
            {"type": "input_text", "text": "CONSOLIDATION INPUT (JSON)\n" + payload_text},
        ]
        try:
            resp = call_model(
                client,
                model_name,
                consolidation_items,
                [],
                provider=provider,
                dump_request=_build_llm_dump_writer(config, main_key, "consolidation"),
            )
        except ProviderApiError as exc:
            # The one failure that must not be allowed to cost anything. By the
            # time this call is made the group has already spent every chunk
            # call it was going to spend and holds a complete set of
            # transcriptions; letting a rate limit, a context-window rejection
            # or a blip on this last cheap text-only request escape would send
            # the whole group to the batch loop's failure handler and throw all
            # of that away. The chunk answers are a complete result, merely an
            # unreconciled one, so fall back to them exactly as for a reply that
            # came back unusable.
            #
            # A credential or model error that is fatal to the RUN still stops
            # it: the next group's very first chunk call raises the same thing
            # before anything has been paid for.
            logger.warning(
                "Group %s: the consolidation pass could not be made (%s: %s); the chunk "
                "answers stand and no work is lost.",
                group_label,
                exc.error_type,
                exc,
            )
            return None, None
        consolidation_usage = utils.extract_usage(resp)
        raw = extract_output_text(resp, provider=provider)
        if not raw or not raw.strip():
            logger.info(
                "Group %s: the consolidation pass returned nothing; the chunk answers stand.",
                group_label,
            )
            return None, consolidation_usage
        try:
            # No retry function: a chunked group has already made several calls
            # and holds a complete set of transcriptions, so spending another
            # one to re-ask for a reconciliation is worse value than falling
            # back to what the chunks said.
            parsed, _consolidation_raw = utils.parse_with_retry(
                raw, lambda: "", config=config, source_path=main_key,
            )
        except json.JSONDecodeError as exc:
            logger.info(
                "Group %s: the consolidation reply was not usable JSON (%s); "
                "the chunk answers stand.",
                group_label,
                exc,
            )
            return None, consolidation_usage
        if not isinstance(parsed, dict):
            logger.info(
                "Group %s: the consolidation reply was not a JSON object; the chunk answers stand.",
                group_label,
            )
            return None, consolidation_usage
        return parsed, consolidation_usage

    chunks = chunking.partition_parts(norm_parts, config.max_images_per_call)
    if config.max_images_per_call > 0:
        for idx, chunk in enumerate(chunks):
            chunk_images = sum(len(paths) for _, paths in chunk)
            if chunk_images > config.max_images_per_call:
                # A bound the partitioner could not honor must not be silent:
                # a part with more variant scans than the budget is never split,
                # and every non-page part rides the first call, so either can
                # push one call over. The call is still made -- an oversized
                # payload is better than a dropped page -- but the run says so.
                logger.warning(
                    "Group %s: call %d of %d carries %d images, over the "
                    "--max-images-per-call budget of %d.",
                    group_label,
                    idx + 1,
                    len(chunks),
                    chunk_images,
                    config.max_images_per_call,
                )

    part_order = [label for label, _ in norm_parts]

    if len(chunks) == 1:
        data, record, record_usage, resolved_model_name = _analyze_parts_once(chunks[0], None)
    else:
        page_part_count = len(
            [label for label in part_order if _page_number_from_label(label) is not None]
        )
        logger.info(
            "Group %s: %d images exceed the per-call budget of %d; sending %d chunked calls "
            "and one text-only consolidation call.",
            group_label,
            len(flat_paths),
            config.max_images_per_call,
            len(chunks),
        )

        chunk_records: list[dict[str, Any]] = []
        usages: list[dict | None] = []
        resolved_model_name = model_name
        # Sequential (D10): deterministic, debuggable, and rate-limit-safe
        # across four providers with four different limit models. A chunk that
        # fails after its own retries fails the group exactly as a failed
        # single call does today -- per-group isolation in the batch loop
        # already handles that -- so nothing is caught here.
        for idx, chunk in enumerate(chunks):
            _chunk_data, chunk_record, chunk_usage, chunk_model = _analyze_parts_once(
                chunk, _build_chunk_note(chunk, idx, len(chunks), page_part_count)
            )
            chunk_records.append(chunk_record)
            usages.append(chunk_usage)
            if idx == 0:
                resolved_model_name = chunk_model

        # The union is taken in payload part order rather than call order, so
        # the map reads as the object does however the partitioner grouped it.
        merged_transcriptions: dict[str, str] = {}
        for label in part_order:
            for chunk_record in chunk_records:
                text = (chunk_record.get("transcriptions") or {}).get(label)
                if isinstance(text, str) and text.strip():
                    merged_transcriptions[label] = text
                    break

        # The merge above is an exact-key lookup, so a chunk that spelled a
        # label even slightly differently ("page 9" for "Page 9") contributes
        # nothing and its page would leave the record without a word. On the
        # unchunked path a stray label merely lands in the caption under its own
        # bogus heading, which is visible; here it is invisible, so say it.
        known = set(part_order)
        stray_labels = sorted(
            {
                label
                for chunk_record in chunk_records
                for label in (chunk_record.get("transcriptions") or {})
                if isinstance(label, str) and label not in known
            }
        )
        for label in stray_labels:
            # Carried, not dropped: ``_synthesize_caption`` appends a label the
            # payload never named rather than losing it, and the chunked path
            # has no business being stricter than the single-call one. The text
            # reaches the caption; only its attribution to a file is lost.
            for chunk_record in chunk_records:
                text = (chunk_record.get("transcriptions") or {}).get(label)
                if isinstance(text, str) and text.strip():
                    merged_transcriptions[label] = text
                    break
        if stray_labels:
            logger.warning(
                "Group %s: the model returned transcriptions under %d label(s) this payload "
                "never named (%s); the text is kept in the caption but cannot be filed to a "
                "file, so those parts get the group transcript in their sidecar rather than "
                "their own. Expected exactly: %s.",
                group_label,
                len(stray_labels),
                ", ".join(repr(label) for label in stray_labels),
                ", ".join(repr(label) for label in part_order),
            )

        consolidation, consolidation_usage = _run_consolidation(
            _build_consolidation_payload(
                norm_parts, merged_transcriptions, chunks, chunk_records, main_key
            )
        )
        usages.append(consolidation_usage)
        record_usage = _sum_usages(usages)

        record = _fold_chunk_records(chunk_records)

        consolidated_result: dict[str, Any] | None = None
        page_order: dict[str, dict[str, Any]] | None = None
        page_order_notes: list[str] = []
        if consolidation is not None:
            result_block = consolidation.get("result")
            if isinstance(result_block, dict) and result_block:
                candidate = result_block.get(main_key)
                if not isinstance(candidate, dict) and len(result_block) == 1:
                    candidate = next(iter(result_block.values()))
                if isinstance(candidate, dict):
                    consolidated_result = candidate
            page_order = _normalize_page_order(consolidation.get("page_order"), set(part_order))
            raw_notes = consolidation.get("page_order_notes")
            if isinstance(raw_notes, list):
                page_order_notes = [
                    note.strip() for note in raw_notes if isinstance(note, str) and note.strip()
                ]

        if consolidated_result is None:
            logger.info(
                "Group %s: the consolidation pass produced no usable result block; the group "
                "metadata is folded from the chunk answers instead.",
                group_label,
            )
        else:
            # This replaces the per-analysis best-confidence pick for chunked
            # groups; an unchunked group's _best_guess path is untouched.
            if "ai_caption" in consolidated_result:
                consolidated_result["ai_caption"] = inject_analysis_date(
                    consolidated_result.get("ai_caption"), date.fromisoformat(today)
                )
            for field in _CONSOLIDATED_FIELDS:
                value = consolidated_result.get(field)
                if value is None:
                    continue
                if not isinstance(value, _CONSOLIDATED_FIELD_TYPES[field]):
                    # The folded chunk answer came from calls that saw the
                    # pages; a mistyped consolidated field has not earned the
                    # right to displace it. "keywords": "Document" is the one
                    # that bites -- a bare string is truthy, so it would replace
                    # the whole list and leave the record with a keyword field
                    # nothing downstream can read.
                    logger.warning(
                        "Group %s: the consolidation pass returned %s as %s rather than %s; "
                        "keeping what the chunk answers concluded for that field.",
                        group_label,
                        field,
                        type(value).__name__,
                        _CONSOLIDATED_FIELD_TYPES[field].__name__,
                    )
                    continue
                record[field] = value
            _ensure_provenance_keyword(record, provider_name, resolved_model_name)

        # A chunk that answered with ``caption`` instead of ``transcriptions``.
        # Compliance is per call, not per run, so one group can genuinely hold
        # both: the field is optional by design and the plan's own fallback
        # promise is that ignoring it degrades to current behavior. Reading only
        # the map when ANY chunk filled it dropped every non-complying chunk's
        # pages out of the record, the caption block and the sidecars without a
        # word -- a silent loss of the transcription the group was billed for.
        #
        unmapped_captions = [
            text.strip()
            for chunk_record in chunk_records
            if not _normalize_transcriptions(chunk_record.get("transcriptions"))
            and isinstance(text := chunk_record.get("caption"), str)
            and text.strip()
        ]

        if merged_transcriptions:
            record["transcriptions"] = merged_transcriptions
            synthesized = _synthesize_caption(merged_transcriptions, part_order)
            if unmapped_captions:
                logger.warning(
                    "Group %s: %d of %d chunk call(s) answered with 'caption' instead of "
                    "'transcriptions'; their text is appended to the caption, but the pages "
                    "they cover cannot be attributed to a file and take the group transcript "
                    "in their sidecar.",
                    group_label,
                    len(unmapped_captions),
                    len(chunks),
                )
            # Fenced, not merely appended. A caption-only chunk's text carries
            # no part label of its own, so concatenating it after the last
            # synthesized section would file it under that section's page --
            # asserting an attribution the warning above explicitly denies.
            fenced = [
                f"{_UNATTRIBUTED_LABEL}\n{text}" for text in unmapped_captions
            ]
            record["caption"] = _reorder_caption_sections(
                "\n".join(text for text in [synthesized, *fenced] if text),
                part_order,
            )
        else:
            # The fallback path: a model that ignored ``transcriptions`` still
            # wrote a caption per chunk, and each already carries its own
            # bracket-labelled sections, so putting those sections back in
            # payload order rebuilds what a single call would have written.
            record.pop("transcriptions", None)
            record["caption"] = _reorder_caption_sections(
                "\n".join(unmapped_captions), part_order
            )

        if page_order:
            # Data, never action (D11). photokin records the corrected number
            # and warns; it renames, reorders and renumbers nothing, because
            # renaming is destructive and cross-tool -- a Lightroom catalog
            # references these paths.
            record["page_order"] = page_order
            if page_order_notes:
                record["page_order_notes"] = page_order_notes
            disagreements = [
                f"{label} reads as page {entry['page']}"
                for label, entry in page_order.items()
                if (filename_page := _page_number_from_label(label)) is not None
                and entry["page"] != filename_page
            ]
            if disagreements:
                logger.warning(
                    "Group %s: the pages do not read in filename order (%s). "
                    "The corrected numbers are recorded; no file is renamed.",
                    group_label,
                    "; ".join(disagreements),
                )
        else:
            logger.info(
                "Group %s: the consolidation pass returned no usable page order; "
                "the filename order stands.",
                group_label,
            )

        data = {"result": {main_key: record}}

    part_counts = _part_counts(norm_parts)
    sent: Dict[str, Any] = {
        "max_edge": config.max_edge,
        "jpeg_quality": config.jpeg_quality,
        "part_count": len(norm_parts),
        "parts": part_counts,
    }
    if len(chunks) > 1:
        sent["chunk_count"] = len(chunks)
    front_count = next((p["count"] for p in part_counts if p["label"].strip().lower() == "front"), 0)
    back_count = next((p["count"] for p in part_counts if p["label"].strip().lower() == "back"), 0)
    if front_count:
        sent["front_count"] = front_count
    if back_count:
        sent["back_count"] = back_count

    variant_payloads: List[dict] = []
    for idx, p in enumerate(flat_paths):
        dims = image_meta[idx]
        lbl = path_labels[idx] if idx < len(path_labels) else "part"
        entry = {
            "path": p,
            "part": lbl,
            "bytes": int(image_byte_sizes[idx]),
            "mime": dims.get("mime"),
            "width": dims.get("width"),
            "height": dims.get("height"),
            "resized": dims.get("resized"),
        }
        variant_payloads.append(entry)
    sent["variants"] = variant_payloads

    record["_transport"] = sent
    record["_usage"] = record_usage

    kws = record.get("keywords", []) or []
    warn_list = utils.warn_forbiddenish_keywords(kws)
    if warn_list:
        for w in warn_list:
            logger.warning("%s", w)
        if config.fail_on_forbidden:
            raise SystemExit(2)

    # The provenance tag is auto-added by this tool, not proposed content, so
    # (like the forbidden-word check above) it is exempt rather than treated
    # as an unproposed keyword every single run.
    new_kws = [
        k for k in kws
        if isinstance(k, str) and k not in known_keywords and not k.strip().endswith(" Analyzed")
    ]
    proposed_raw = record.get("proposed_new_keywords")
    if not isinstance(proposed_raw, list):
        logger.warning('"proposed_new_keywords" missing or invalid; skipping vocab updates.')
        proposed = []
        skip_vocab_updates = True
    else:
        proposed = proposed_raw
        skip_vocab_updates = False
    proposed_map = {
        p.get("keyword"): p
        for p in proposed
        if isinstance(p, dict) and p.get("keyword")
    }

    inserted_count = 0
    if new_kws and not config.no_update_vocab:
        utils.safe_backup(config.vocab_path)
        try:
            for k in new_kws:
                if skip_vocab_updates:
                    logger.warning(
                        'Skipping keyword "%s" because proposed_new_keywords is missing.', k
                    )
                    continue
                if not isinstance(k, str):
                    logger.warning("Skipping non-string keyword in new keyword list.")
                    continue
                if k.upper().startswith("PC-"):
                    logger.warning('Skipping keyword "%s" (PC- prefix not allowed).', k)
                    continue
                if k.strip().lower() in utils.PART_MARKER_KEYWORDS:
                    # Approving one would teach the model to propose a token the
                    # fan-out then strips from every file it does not describe.
                    logger.warning('Skipping keyword "%s" (part marker, not a vocabulary keyword).', k)
                    continue

                p = proposed_map.get(k)
                if not p:
                    logger.info('Keyword "%s" was used on this photo but not added to the vocabulary.', k)
                    continue

                section = (p.get("section") or "").strip()
                note = (p.get("note") or "").strip()
                if not section:
                    logger.warning(
                        'Skipping keyword "%s" (missing section in proposed_new_keywords).', k
                    )
                    continue
                if utils.note_looks_placeholder(note):
                    logger.warning('Skipping keyword "%s" (note is missing or placeholder).', k)
                    continue

                if utils.insert_keyword_into_vocab_file(config.vocab_path, section, k, note):
                    inserted_count += 1

            if inserted_count:
                logger.info(
                    "Vocabulary updated (%d new keyword(s) inserted into %s)",
                    inserted_count,
                    config.vocab_path,
                )
        except Exception as e:
            logger.exception("Failed to insert new keywords: %s", e)

    json_path = _write_sidecar_document(data, main_key, config) if write_sidecar else None
    if json_path:
        logger.info(
            "Group analysis completed for %s; JSON saved as %s",
            os.path.basename(main_key),
            json_path,
        )
    else:
        logger.info("Group analysis completed for %s", os.path.basename(main_key))

    return data

def analyze_group_front_back(
    front_paths: List[str] | None,
    back_paths: List[str] | None,
    config: utils.Config = utils.Config(),
    *,
    original_meta: dict | None = None,
    write_sidecar: bool = False,
) -> Dict[str, Any]:
    """Analyze a photo from separate front/back path lists.

    Convenience wrapper over :func:`analyze_group_parts` for the common case
    where parts are already split into fronts and backs (rather than the generic
    ``(label, paths)`` form). Empty lists are skipped so a front-only or
    back-only set still works.
    """
    parts: list[tuple[str, list[str]]] = []
    if front_paths:
        parts.append(("Front", front_paths))
    if back_paths:
        parts.append(("Back", back_paths))
    return analyze_group_parts(
        parts,
        config=config,
        original_meta=original_meta,
        write_sidecar=write_sidecar,
    )


def build_folder_manifest(folder_path: str, *, photo_context_text: str | None = None) -> Dict[str, Any]:
    """Describe a folder as an in-memory manifest.

    Each item carries ``path`` and nothing else. Folder mode has no source of
    truth beyond the filename, so an explicit ``is_back``/``version``/``group``
    would only hand :func:`_resolve_manifest_entry` back the answer it is about
    to derive from the same parser -- and would then freeze that answer, so any
    later change to the grammar would be silently overridden and reported as an
    override on every ordinary folder.

    Args:
        folder_path: Directory to describe.
        photo_context_text: Resolved, sanitized photo context, emitted inline so
            the manifest round-trips through ``utils.resolve_photo_context``.
            Omitted from the document when empty.

    Returns:
        A manifest dict whose ``items`` are exactly what the analysis path
        processes, in exactly that order.

    Raises:
        NotADirectoryError: If ``folder_path`` is not an existing directory.
    """
    folder = utils.normalize_path(folder_path) or ""
    manifest: Dict[str, Any] = {
        "schema_version": 1,
        "generated_by": "photokin --generate-manifest",
        "source": {"type": "folder", "path": folder},
    }
    if photo_context_text:
        manifest["photo_context_text"] = photo_context_text
    manifest["items"] = [{"path": path} for path in utils.list_folder_images(folder)]
    return manifest


def build_single_photo_manifest(
    image_path: str,
    back_path: str | None = None,
    *,
    meta: dict | None = None,
    photo_context_text: str | None = None,
) -> Dict[str, Any]:
    """Describe an image plus its optional back and metadata as a manifest.

    Unlike a folder, single-photo input carries real assertions the filename
    cannot make, so this one does set overrides. ``--back`` says "this file is
    the reverse of that one" whatever it is called, so the back is given the
    front's whole address -- ``is_back``, the shared ``group`` and the front's
    ``version`` -- rather than only the group key. Without the group key
    ``photo.jpg --back reverse.jpg`` splits into two objects and two model
    calls; without the version the same split happens under ``--group-by pair``
    alone, whose bucket key carries the variant letter, for a back named
    ``IMG_0042b.jpg`` -- which is exactly the sort of unreadable name ``--back``
    exists to handle. (Under ``--group-by none`` neither key is consulted, so
    the pair does split in two; that is what the escape hatch is for.)
    ``--meta`` rides inline on the front only, matching the single
    ``original_meta`` blob the old call site forwarded; the group's other item
    still receives it through ``merge_original_sources``.

    Args:
        image_path: The front image.
        back_path: The reverse side, or ``None``.
        meta: Already-loaded original metadata, or ``None``. Inline rather than
            a ``metadata_path`` so a malformed file has already failed loudly at
            load time and the manifest stays self-contained.
        photo_context_text: Resolved, sanitized photo context, emitted inline.

    Returns:
        A manifest dict with the front first and the back, if any, second.
    """
    front = utils.normalize_path(image_path) or ""
    # Empty only for an empty path, which the caller is expected to have refused
    # already; emitting ``group: ""`` would be an unusable override rather than a
    # grouping instruction, and would be warned about as one.
    parsed_front = utils.parse_media_filename(front) if front else None
    group_key = parsed_front.base_id if parsed_front else ""
    # The front keeps the version its own name yields, which is the value the
    # back is pinned to. ``""`` is the documented spelling of "no variant
    # letter"; leaving the key out would fall back to the back's own filename.
    back_address = (
        {"group": group_key, "version": parsed_front.variant_id or ""}
        if group_key and parsed_front
        else {}
    )
    front_item: Dict[str, Any] = {"path": front}
    if group_key:
        front_item["group"] = group_key
    if meta:
        front_item["metadata"] = meta
    items = [front_item]
    if back_path:
        items.append({"path": utils.normalize_path(back_path), **back_address, "is_back": True})

    manifest: Dict[str, Any] = {
        "schema_version": 1,
        "generated_by": "photokin --generate-manifest",
        "source": {"type": "single", "path": front},
    }
    if photo_context_text:
        manifest["photo_context_text"] = photo_context_text
    manifest["items"] = items
    return manifest


def analyze_folder(
    folder_path: str,
    config: utils.Config = utils.Config(),
    *,
    write_sidecars: bool = False
) -> Dict[str, Any]:
    """
    Batch mode for entire folders.

    The folder is translated into manifest items and handed to
    :func:`process_manifest_stream`, so folder and manifest input group
    identically: album pages, negative-only sets, crops and variant scans are
    all analyzed rather than skipped, and ``config.group_by`` selects the
    grouping granularity here exactly as it does for a manifest.  Failure
    handling is folder mode's own: a group that raises is
    recorded under ``errors`` and the batch carries on, a run-fatal provider
    error aborts immediately, and a run in which nothing succeeded re-raises its
    first failure rather than exiting 0 with an empty result set.

    Returns:
        ``{"results": {file_path: record}, "errors": {file_path: payload}}`` --
        one entry per FILE, not per group. Every image the folder holds appears
        in exactly one of the two. Records carry the merge report under
        ``_merge``, per-file scoped ``keywords`` and ``caption``, and the full
        ``all_variant_files`` map (``front``/``back``/``variants``/``all``, plus
        ``pages``/``crops``/``negatives``/``displaced`` where they apply). See
        Breaking change #2 in ``docs/unified-input-pipeline.md``.

    Raises:
        NotADirectoryError: If ``folder_path`` is not an existing directory.
        Exception: The first per-group failure, when no group succeeded.
    """
    manifest = build_folder_manifest(folder_path, photo_context_text=config.photo_context_text)
    if not manifest["items"]:
        logger.warning("No image files found in folder: %s", manifest["source"]["path"])
        return {"results": {}, "errors": {}}

    return process_manifest_stream(
        manifest=manifest,
        cfg=config,
        write_sidecars=write_sidecars,
        strict_run_failures=True,
    )


# === Manifest grouping ===

# One canonical ordering for every grouping tie-break in the manifest path. The
# crop flag leads it (see ``_slot_rank_key``) so a derivative can never take the
# slot of the scan it was cropped from, whatever order the manifest listed them in.
# Defined in ``utils`` because ``combine_group_metadata`` ranks the same entries
# by the same order, so which file stands for the object is decided once.
_PART_RANK = utils.PART_RANK

# Fidelity order for same-stem files that differ only by extension, e.g. a TIFF
# master beside the JPEG derivative an archivist keeps for browsing. Only one of
# them is sent to the model, so send the one that lost the least: lossless first,
# then PNG, then the lossy formats. Alphabetical order -- the fallback this sits
# in front of -- picks the opposite, since ".jpg" sorts before ".tif", and the
# compression artifacts it hands the model are exactly what costs a transcription
# of faint pencil on the back of a card.
_FORMAT_RANK = {".tif": 0, ".tiff": 0, ".png": 1, ".jpg": 2, ".jpeg": 2}
_UNRANKED_FORMAT = 3

_GROUP_BY_VALUES = frozenset(utils.GROUP_BY_VALUES)

# Joins the two halves of a ``pair`` bucket key. Illegal in a Windows filename,
# so a key the grammar derived there never contains one -- but an explicit
# manifest ``group`` may, on any platform, which is why the halves are escaped.
_PAIR_KEY_SEPARATOR = "|"
_PAIR_KEY_ESCAPE = "\\"

# The plug-in writes manifests from Lua, which passes literal true/false strings.
_MANIFEST_TRUE = frozenset({"true", "1", "yes"})
_MANIFEST_FALSE = frozenset({"false", "0", "no"})

# Only a separator or a digit may precede the token, so 'feedback.jpg' is never
# read as the back of 'feed'.
_EXPLICIT_BACK_SUFFIX_RE = re.compile(r"(?:[-_. ]|(?<=\d))back$", re.IGNORECASE)


def _coerce_manifest_bool(raw: dict, key: str, path: str, *, log: bool = True) -> bool | None:
    """Read a tri-state boolean flag from a manifest item.

    Args:
        raw: One entry of the manifest's ``items`` array.
        key: Flag name to read.
        path: Normalized item path, used only for the warning message.
        log: Whether to report an unreadable value. Off for a caller that is
            resolving the same items a second time purely to count them.

    Returns:
        The flag value, or ``None`` when it is absent, null or unreadable -- all
        of which mean "no override". Note that ``False`` is an override and must
        therefore never be tested for truthiness by the caller.
    """
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _MANIFEST_TRUE:
            return True
        if token in _MANIFEST_FALSE:
            return False
    if log:
        logger.warning("Manifest item %s: ignoring unrecognized %s value %r", path, key, value)
    return None


def _log_manifest_override(path: str, key: str, value: object, field: str, derived: object) -> None:
    """Warn that an explicit manifest flag contradicted, and beat, the filename."""
    logger.warning(
        "Manifest item %s: explicit %s=%r overrides filename-derived %s=%r",
        path,
        key,
        value,
        field,
        derived,
    )


def _manifest_group_override(raw: dict, path: str, *, log: bool = True) -> str | None:
    """Resolve an item's explicit bucket key.

    ``group`` is canonical and ``base_id`` an accepted alias; when both are given
    and disagree, ``group`` wins.

    Args:
        raw: One entry of the manifest's ``items`` array.
        path: Normalized item path, used only for warning messages.
        log: Whether to report an unusable or conflicting value. Off for a
            caller resolving the same items a second time purely to count them.

    Returns:
        The explicit bucket key, or ``None`` to fall back to the filename.
    """
    resolved: str | None = None
    for key in ("group", "base_id"):
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            if log:
                logger.warning("Manifest item %s: ignoring unusable %s value %r", path, key, value)
            continue
        candidate = value.strip()
        if resolved is None:
            resolved = candidate
        elif candidate != resolved and log:
            logger.warning(
                "Manifest item %s: base_id=%r conflicts with group=%r; using group.",
                path,
                candidate,
                resolved,
            )
    return resolved


def _resolve_manifest_entry(raw: dict, *, log_overrides: bool = True) -> dict | None:
    """Build one grouping entry from a raw manifest item.

    Everything starts from the filename grammar and is then corrected by whatever
    the caller stated explicitly. ``is_back``, ``is_crop``, ``version`` and
    ``group`` (alias ``base_id``) always beat the filename, in both directions:
    they exist precisely for files whose names do not follow the grammar, so a
    filename that overruled them would leave them inert exactly where they are
    needed. Every override that actually changes a derived value is logged.

    Args:
        raw: One entry of the manifest's ``items`` array.
        log_overrides: Whether to report the overrides this item applies. The
            CLI resolves the same items a second time to count the groups for
            its plan summary, and every override line would otherwise be
            printed twice.

    Returns:
        The grouping entry, or ``None`` when the item carries no usable path.
    """
    path = utils.normalize_path(raw.get("path") or "")
    if not path:
        return None

    parsed = utils.parse_media_filename(path)
    part_kind = parsed.part_kind
    page_num = parsed.page_num
    version = parsed.variant_id
    is_crop = parsed.is_crop

    explicit_back = _coerce_manifest_bool(raw, "is_back", path, log=log_overrides)
    if explicit_back is True and part_kind != "back":
        if log_overrides:
            _log_manifest_override(path, "is_back", raw.get("is_back"), "part_kind", part_kind)
        # An item cannot be both a page and a back.
        part_kind, page_num = "back", None
    elif explicit_back is False and part_kind == "back":
        # "front" rather than "none": the caller asserted the front side, and an
        # untagged file can still be promoted to page 1 in a multipage group.
        if log_overrides:
            _log_manifest_override(path, "is_back", raw.get("is_back"), "part_kind", part_kind)
        part_kind = "front"

    explicit_crop = _coerce_manifest_bool(raw, "is_crop", path, log=log_overrides)
    if explicit_crop is not None and explicit_crop != is_crop:
        if log_overrides:
            _log_manifest_override(path, "is_crop", raw.get("is_crop"), "is_crop", is_crop)
        is_crop = explicit_crop

    if raw.get("version") is not None:
        explicit_version = str(raw["version"]).strip().lower() or None
        if explicit_version != version and log_overrides:
            _log_manifest_override(path, "version", raw.get("version"), "version", version)
        version = explicit_version

    group_key = _manifest_group_override(raw, path, log=log_overrides)
    if group_key is None:
        group_key = parsed.base_id
        if explicit_back is True and parsed.part_kind != "back":
            # The parser reads only the hyphenated '-back', so an explicitly
            # flagged 'box3_017_back.jpg' would otherwise bucket on its own.
            repaired = _EXPLICIT_BACK_SUFFIX_RE.sub("", group_key, count=1)
            if repaired and repaired != group_key:
                if log_overrides:
                    logger.info(
                        "Manifest item %s: is_back is set, grouping under '%s' rather than '%s'.",
                        path,
                        repaired,
                        group_key,
                    )
                group_key = repaired
    elif group_key != parsed.base_id and log_overrides:
        _log_manifest_override(path, "group", group_key, "group", parsed.base_id)

    return {
        "path": path,
        "is_back": part_kind == "back",
        "version": version,
        "part_kind": part_kind,
        "page_num": page_num,
        "is_crop": is_crop,
        "group_key": group_key,
        "preferred": bool(raw.get("preferred")),
        "metadata": raw.get("metadata"),
        "metadata_path": raw.get("metadata_path"),
        # Set by the ExifTool hydrator (which runs before bucketing) when -r
        # asked for this file's metadata and the read could not be confirmed;
        # the changeset emitter proposes no writes for such a file.
        utils.HYDRATION_FAILED_KEY: bool(raw.get(utils.HYDRATION_FAILED_KEY)),
    }


def _item_part_marker(entry: dict) -> str | None:
    """Return the part-marker keyword a grouping entry earns, if any.

    ``is_back`` is defined as ``part_kind == "back"``, so the two kinds are
    mutually exclusive and at most one marker ever applies to a file.

    Args:
        entry: One :func:`_resolve_manifest_entry` result.

    Returns:
        ``"back"``, ``"negative"``, or ``None`` for a file that is neither.
    """
    if entry["is_back"]:
        return "back"
    return "negative" if entry["part_kind"] == "negative" else None


def _escape_pair_half(half: str) -> str:
    """Escape one half of a ``pair`` bucket key so its separators are inert.

    The escape character is doubled first, then the separator is prefixed with
    it, so every separator in the result is preceded by an odd-length run of
    escape characters. The escaped half therefore ends in an even-length run,
    possibly empty, and the separator :func:`_pair_bucket_key` joins on is the
    only one in the whole key that an odd run does not precede.

    Args:
        half: The group key or the variant letter, verbatim.

    Returns:
        The escaped half, which contains no bare separator.
    """
    return half.replace(_PAIR_KEY_ESCAPE, _PAIR_KEY_ESCAPE * 2).replace(
        _PAIR_KEY_SEPARATOR, _PAIR_KEY_ESCAPE + _PAIR_KEY_SEPARATOR
    )


def _pair_bucket_key(group_key: str, version: str | None) -> str:
    """Join a group key and a variant letter into one ``pair`` bucket key.

    Each half is escaped by :func:`_escape_pair_half` and the two are joined on
    a bare separator. The encoding is injective. Reading left to right, an
    escape character consumes the character after it and a separator that is not
    consumed that way is the join, so a key holding no bare separator came from
    a ``None`` version and one holding a bare separator splits at exactly the
    first: the two halves are recovered unambiguously, and only one input can
    spell any given key.

    Doubling the separator instead -- the first attempt -- is not injective,
    because a half's escaped trailing run merges with the joining separator:
    ``("a|", "a")`` and ``("a", "|a")`` both spell ``a|||a``.

    A plain join is worse still: an explicit manifest ``group`` of ``"album|b"``
    and a filename-derived ``("album", "b")`` both spell ``album|b``, which puts
    two unrelated objects in one model call and writes both of them the same
    caption, date and location. A key holding neither the separator nor the
    escape character keeps its exact spelling when there is no variant letter,
    which is every key the grammar derives on Windows, so the ordinary shape
    carries the same changeset ``group_id`` under ``pair`` as under ``object``.

    Args:
        group_key: The entry's resolved group key.
        version: The entry's variant letter, or ``None`` when it has none.

    Returns:
        The bucket key.
    """
    escaped = _escape_pair_half(group_key)
    if version is None:
        return escaped
    return f"{escaped}{_PAIR_KEY_SEPARATOR}{_escape_pair_half(version)}"


def build_manifest_buckets(
    items: List[dict],
    *,
    group_by: str = utils.GROUP_BY_OBJECT,
    log_overrides: bool = True,
) -> Dict[str, List[dict]]:
    """Bucket manifest items by resolved group key, dropping items with no usable path.

    The single implementation of the grouping every input mode sees, so a count
    taken from it -- ``--generate-manifest`` reports one -- cannot drift from the
    grouping the run actually performs. Resolving the entries here is also what
    surfaces the explicit-override warnings, which is why the flag can report a
    disagreeing ``--back`` before it writes the file.

    The key stays a string at every granularity, because it is the ``stem`` the
    stream logs every per-group message against and the changeset's
    ``group_id``/``group_key``; a tuple would ripple into all of those. Under
    ``pair`` the two halves are joined by :func:`_pair_bucket_key`, which
    escapes its separator rather than assuming neither half can contain one.

    Args:
        items: The manifest's ``items`` array.
        group_by: One of :data:`utils.GROUP_BY_VALUES`. ``object`` keys on the
            resolved group key, ``pair`` on the group key plus the variant
            letter, and ``none`` on the file itself.
        log_overrides: Whether resolving the entries reports the overrides they
            apply. The CLI's plan summary buckets the same items a second time
            purely for a count and passes ``False``, so no diagnostic is
            printed twice; every other caller keeps the default.

    Returns:
        ``{group_key: [entry, ...]}`` in first-seen key order, entries in item
        order.

    Raises:
        ValueError: If *group_by* is not one of :data:`utils.GROUP_BY_VALUES`.
            argparse already guards the CLI; this guards a library caller who
            sets ``cfg.group_by`` by hand.
    """
    if group_by not in _GROUP_BY_VALUES:
        raise ValueError(f"Unknown group_by value: {group_by!r}")
    buckets: Dict[str, List[dict]] = {}
    for raw in items:
        entry = _resolve_manifest_entry(raw, log_overrides=log_overrides)
        if entry is None:
            continue
        if group_by == utils.GROUP_BY_OBJECT:
            key = entry["group_key"]
        elif group_by == utils.GROUP_BY_PAIR:
            key = _pair_bucket_key(entry["group_key"], entry["version"])
        else:
            key = entry["path"]
        buckets.setdefault(key, []).append(entry)
    return buckets


def _manifest_part_key(entry: dict) -> str:
    """Return the slot an entry competes for within its variant.

    The slot address is the ``(version, part_key)`` pair. Crop-ness deliberately
    stays out of the address so a crop contends for its parent's slot and loses
    on rank rather than quietly occupying a slot of its own.
    """
    part_kind = entry["part_kind"]
    if part_kind == "page":
        # ``or 1`` would be wrong here: '-pageN' accepts any run of digits, so
        # '-page0' is a legal name whose slot must stay distinct from page 1's.
        page_num = entry["page_num"]
        return f"page:{1 if page_num is None else page_num}"
    if part_kind in ("front", "back", "negative"):
        return part_kind
    return "none"


def resolve_part_label(
    entry: dict,
    *,
    multipage_present: bool,
    relabelled_versions: frozenset[str | None],
) -> str:
    """Return the payload part label a manifest grouping entry travelled under.

    The one file-to-label function in the system: a consumer that needs to look
    a file up in a record's ``transcriptions`` map resolves its label here
    rather than re-deriving it, so the label vocabulary stays exactly the one
    the payload used. The untagged front of a multipage variant was relabelled
    to Page 1 before the payload was built, so the same relabel is applied
    before mapping; ``none`` otherwise resolves to ``Front``, because the front
    side is what an untagged file travels as.

    A label this returns is not guaranteed to appear in ``transcriptions``: a
    displaced or unseated file was never in the payload under any label, so
    callers must handle a miss.

    Args:
        entry: A manifest grouping entry, as built by
            :func:`_resolve_manifest_entry`.
        multipage_present: Whether the entry's group holds explicit page parts.
        relabelled_versions: The variant letters whose untagged slot became
            Page 1, as the emit loop computes for the crop map.

    Returns:
        The part label the entry's file was sent under: ``"Front"``,
        ``"Back"``, ``"Negative"`` or ``"Page N"``.
    """
    part_key = _manifest_part_key(entry)
    if part_key == "none" and multipage_present and entry["version"] in relabelled_versions:
        part_key = "page:1"
    return part_label_for_slot(part_key)


def part_label_for_slot(part_key: str) -> str:
    """Return the payload part label a slot key travels under.

    Split out of :func:`resolve_part_label` because attribution sometimes has
    to start from a slot rather than from an entry: a path a manifest listed
    twice under contradicting roles keeps only one of its addresses, and the
    label that matters is the retained one, not the one the losing entry would
    resolve to on its own.

    Args:
        part_key: A slot key as produced by :func:`_manifest_part_key`, or
            ``"page:N"`` after the page-1 relabel.

    Returns:
        ``"Front"``, ``"Back"``, ``"Negative"`` or ``"Page N"``.
    """
    if part_key.startswith("page:"):
        return f"Page {part_key.split(':', 1)[1]}"
    return {"front": "Front", "back": "Back", "negative": "Negative", "none": "Front"}[part_key]


def _slot_address_rank(version: str | None, part_key: str) -> tuple[int, int, int, str]:
    """Rank a ``(version, part_key)`` slot address the way entries are ranked.

    Mirrors the part-kind, page-number and unversioned-first components of
    :func:`_slot_rank_key`. Used when one path has won more than one address and
    only its best claim may travel in the payload.

    Args:
        version: The variant letter the address belongs to, or ``None``.
        part_key: The slot key, as produced by :func:`_manifest_part_key`.

    Returns:
        A sort key placing the address a path should keep first.
    """
    if part_key.startswith("page:"):
        kind, page_num = "page", int(part_key.split(":", 1)[1])
    else:
        kind, page_num = part_key, 0
    return (_PART_RANK[kind], page_num, 0 if version is None else 1, version or "")


def _slot_rank_key(entry: dict) -> tuple[int, int, int, int, int, str, int, str, str]:
    """Order grouping entries so no choice in the bucket loop depends on manifest order.

    Crop-ness leads, so a real scan beats a crop of it unconditionally -- including
    a crop the caller marked ``preferred``, since a derivative cannot stand in for
    the original listed beside it. ``preferred`` comes next, so an explicit choice
    takes any slot it is actually allowed to take. Then part kind, page number and
    unversioned-before-versioned.

    Format fidelity comes after those and before the path, so it settles only the
    case the path would otherwise settle alphabetically: two files of the same
    stem and part differing by extension. The path itself stays last, so even two
    indistinguishable candidates resolve the same way every run.
    """
    page_num = entry["page_num"]
    extension = os.path.splitext(entry["path"])[1].lower()
    return (
        1 if entry["is_crop"] else 0,
        0 if entry["preferred"] else 1,
        _PART_RANK[entry["part_kind"]],
        0 if page_num is None else page_num,
        0 if entry["version"] is None else 1,
        entry["version"] or "",
        _FORMAT_RANK.get(extension, _UNRANKED_FORMAT),
        entry["path"].lower(),
        entry["path"],
    )


def analyze_manifest(
    manifest: dict | str,
    config: utils.Config = utils.Config(),
    *,
    write_sidecars: bool = False,
    ndjson_writer=None,
    changeset_writer=None,
    changeset_run_id: str | None = None,
    metadata_hydrator: Callable[[List[dict]], None] | None = None,
    titles_may_be_from_files: bool = False,
) -> dict:
    """
    Convenience wrapper around :func:`process_manifest_stream` that preserves the
    historically non-streaming signature.

    Kept for external callers that want the whole snapshot and no streaming
    callbacks; ``public.analyze_manifest`` is the narrowest of them. The CLI does
    not use it -- it calls :func:`process_manifest_stream` directly, because it
    needs the NDJSON and changeset writers this signature does not carry.

    ``titles_may_be_from_files`` is forwarded rather than left out: a wrapper that
    silently drops a keyword is worse than one that never offered it, because the
    caller's title precedence would quietly differ from the callee's with nothing
    to show for it. See :func:`process_manifest_stream` for what it means.
    """
    return process_manifest_stream(
        manifest=manifest,
        cfg=config,
        write_sidecars=write_sidecars,
        ndjson_writer=ndjson_writer,
        changeset_writer=changeset_writer,
        changeset_run_id=changeset_run_id,
        metadata_hydrator=metadata_hydrator,
        titles_may_be_from_files=titles_may_be_from_files,
    )


def process_manifest_stream(
    manifest: dict | str,
    cfg: utils.Config,
    *,
    write_sidecars: bool = False,
    ndjson_writer=None,
    changeset_writer=None,
    changeset_run_id: str | None = None,
    metadata_hydrator: Callable[[List[dict]], None] | None = None,
    titles_may_be_from_files: bool = False,
    strict_run_failures: bool = False,
    should_cancel: Callable[[], bool] | None = None,
    run_event_writer: Callable[[dict], None] | None = None,
) -> dict:
    """Stream manifest processing results while still returning a full snapshot.

    Lightroom drives large batches and needs partial feedback to stay responsive,
    so we stream NDJSON records as soon as each group finishes *and* build the
    aggregate result that older callers expect.  This dual behavior is the core
    design constraint worth documenting.

    Grouping granularity comes off the config as ``cfg.group_by``, one of
    :data:`utils.GROUP_BY_VALUES`; which analyzer each group then reaches
    follows the group's own contents rather than any flag.

    Args:
        titles_may_be_from_files: Whether the values ``metadata_hydrator``
            supplies were read out of the files' own tags. It narrows exactly one
            rule -- a title in an item's metadata stops outranking one the model
            transcribed off the print -- and nothing else; in particular it reads
            nothing itself. Off by default, so an embedder hydrating from a
            database or a sidecar format keeps full title precedence for the
            human words it supplies. The CLI sets it from ``-r``.
        strict_run_failures: Folder mode's Phase A failure contract, off by
            default so manifest mode -- the plug-in contract -- keeps behaving
            exactly as it did. When on, a ``ProviderApiError`` describing the run
            rather than one photo aborts immediately instead of being repeated
            per group, and a run in which every group failed re-raises its first
            failure rather than returning an empty result the caller exits 0 on.
            The asymmetry between the two modes is deliberate and owned by
            Phase C; see ``docs/unified-input-pipeline.md``.
        should_cancel: Polled once before each group starts (never mid-group --
            groups make one model call each under the default ``object``
            grouping, so there is no narrower seam to check between). A
            cooperative stop: the batch returns cleanly with whatever
            ``results``/``errors`` it has already banked, rather than raising,
            since stopping on request is not a failure. ``None`` disables
            polling entirely, which is the default for every caller that has
            no way to be asked to cancel.
        run_event_writer: Called once per group, right before it starts, with
            a ``{"group": ..., "index": ..., "of": ...}`` liveness signal --
            deliberately a *separate* channel from ``ndjson_writer``, which
            every existing caller already treats as "one ``path``/``status``
            record per finished file" and asserts on exactly that shape. A
            caller that wants these events folded into the same NDJSON stream
            (the CLI does) does that itself; ``None`` means no one is
            listening, which is the default.

    Returns:
        ``{"results": {path: record}, "errors": {path: payload}, "groups_failed":
        int, "files_unsent": int, "cancelled": bool}``. ``results``/``errors``
        are one entry per file and disjoint -- every file of a failed group
        carries that group's error payload, bar one already banked when the
        group raised part-way through its per-file loop, since that record is
        complete and its ``ok`` line is already on the stream. ``cancelled`` is
        ``True`` only when ``should_cancel`` returned ``True`` before every
        group had run; a batch that finished on its own always reports
        ``False``, even if every group failed.
    """
    if isinstance(manifest, str):
        man = utils.load_manifest(utils.normalize_path(manifest))
    else:
        man = manifest

    utils.resolve_default_paths(cfg)

    forward_fields = None
    try:
        if cfg.metadata_forward_path and os.path.isfile(cfg.metadata_forward_path):
            with open(cfg.metadata_forward_path, "r", encoding="utf-8") as fh:
                mp = json.load(fh)
            forward_fields = mp.get("forward_fields")
    except (OSError, json.JSONDecodeError) as exc:
        if os.getenv("MEL_VERBOSE") or os.getenv("MEL_DEBUG"):
            logger.warning("Failed to load forwarded metadata: %s", exc)
        forward_fields = None

    items = man.get("items", [])
    # Provenance is the caller's fact to state, not ours to infer. This used to
    # read ``metadata_hydrator is not None``, but "a hydrator ran" is not "these
    # values came out of the files' own tags": the README invites embedders to
    # supply their own hydrator reading a database or a sidecar format, where a
    # title is a human's words and must keep beating the model's transcription
    # exactly as an inline one does. Inferring it re-opened, through the public
    # seam, the very data loss the -r title rule was narrowed to avoid.
    # Marking photokin's own hydrator instead would answer for the one callable
    # we ship and lie for every wrapper around it -- a decorator, a
    # functools.partial, a lambda closing over two hydrators -- so the honest
    # signal is the parameter: the caller knows, and the callee cannot.
    if metadata_hydrator is not None:
        metadata_hydrator(items)
    # A per-item narrowing of titles_may_be_from_files, when the hydrator can
    # state it precisely: photokin's own (exposed via make_manifest_hydrator's
    # title_from_file attribute) knows exactly which items' titles it filled
    # from the file, as opposed to items that already carried a manifest- or
    # --meta-supplied title and so were never touched. Without it every title
    # in the run would be treated as possibly-from-file merely because -r was
    # given somewhere in the run, which is the run-wide bool's known blind
    # spot -- see titles_may_be_from_files above. An arbitrary external
    # hydrator carries no such attribute and falls back to that bool exactly
    # as before.
    title_from_file_ids: set[int] | None = getattr(metadata_hydrator, "title_from_file", None)
    group_by = cfg.group_by
    buckets = build_manifest_buckets(items, group_by=group_by)

    results: dict[str, dict] = {}
    errors: dict[str, dict] = {}
    run_id = changeset_run_id or (make_run_id() if changeset_writer else None)

    def _emit(path: str, status: str, payload: dict):
        if ndjson_writer:
            rec = {"path": path, "status": status}
            rec.update(payload)
            if cfg.dry_run:
                rec["dry_run"] = True
            ndjson_writer(json.dumps(rec, ensure_ascii=False))
        if status == "ok":
            results[path] = payload.get("result") or payload
        elif status == "error":
            errors[path] = payload.get("error") or payload

    failed_groups = 0
    first_error: Exception | None = None
    # Files a group listed that no model call carried. Taken from the payload
    # rather than accumulated as each displacement rule fires: an accumulator has
    # to be updated at every site that drops a file, and one of them -- the slot
    # collision below -- warned without doing so, which is how the completion
    # line came to report zero directly under a WARNING saying otherwise.
    unsent_paths: set[str] = set()
    # Markdown sidecar destinations already written, for the whole run rather
    # than for one group. Two files whose names differ only by extension do not
    # have to be in the same bucket to collide on one ``<stem>.md``: under
    # ``--group-by none`` every file is its own group, and a manifest may put
    # them in different groups explicitly, so a per-group guard alone would let
    # the second group overwrite the first group's sidecar without a word.
    sidecar_written: dict[str, str] = {}

    group_keys = ordered_group_keys(buckets)
    for group_index, stem in enumerate(group_keys):
        if should_cancel is not None and should_cancel():
            logger.warning(
                "Cancelled after %d of %d group(s); %d file(s) already recorded.",
                group_index, len(group_keys), len(results),
            )
            return {
                "results": results,
                "errors": errors,
                "groups_failed": failed_groups,
                "files_unsent": len(unsent_paths),
                "cancelled": True,
            }
        if run_event_writer:
            # A liveness signal for a caller that only watches the results
            # stream: the last thing it saw otherwise might be one photo's
            # ``ok`` line from several groups ago, with nothing to show
            # whether the run is working through a slow group or has
            # silently died. Announced at the group boundary rather than via
            # a background heartbeat during the model call itself -- simpler
            # and safe to call from the same thread that is about to block on
            # that call, at the cost of no signal *within* one unusually slow
            # group.
            run_event_writer({"group": stem, "index": group_index + 1, "of": len(group_keys)})
        group = buckets[stem]
        # Bound before the try so the failure log always has a subject: a group
        # can fail well before primary selection has run.
        subject = group[0]["path"] if group else stem
        # Paths already banked when a group raises part-way through its per-file
        # emit loop below. Their records are complete and their ``ok`` line is
        # already on the stream, so re-reporting them in the handler would key
        # one path under both ``results`` and ``errors`` and contradict a line
        # the consumer may have acted on.
        emitted_ok: set[str] = set()
        try:
            multipage_present = any(it["part_kind"] == "page" for it in group)
            # Rank order, not arrival order, so the warnings below and the
            # recorded crop map read the same whatever order the manifest used,
            # and one entry per resolved path so a manifest that lists the same
            # crop twice is not described twice as standing in for the object.
            crops_by_path: dict[str, dict] = {}
            for it in sorted((c for c in group if c["is_crop"]), key=_slot_rank_key):
                crops_by_path.setdefault(it["path"], it)
            crops = list(crops_by_path.values())

            variant_order: list[str | None] = []
            slot_candidates: dict[tuple[str | None, str], list[dict]] = {}
            for it in group:
                ver = it["version"]
                if ver not in variant_order:
                    variant_order.append(ver)
                slot_candidates.setdefault((ver, _manifest_part_key(it)), []).append(it)

            # A crop is a supporting view of its parent, so it yields the slot
            # whenever the parent is listed -- matching folder mode. That has to
            # be decided per slot rather than per group: a group holding a
            # cropped front and an uncropped back has an uncropped file in it,
            # yet dropping the crop would leave the group with no front at all.
            orphan_crops: set[str] = set()
            for address, claimants in slot_candidates.items():
                uncropped = [c for c in claimants if not c["is_crop"]]
                if uncropped:
                    slot_candidates[address] = uncropped
                else:
                    orphan_crops.update(c["path"] for c in claimants)

            # Every file that lost a claim on the payload, addressed by the slot
            # it lost, whichever of the three rules below took it. This is what
            # the record discloses under ``all_variant_files.displaced``, and the
            # three rules fill in one map so that two collisions of the same kind
            # cannot be accounted for differently.
            displaced_slots: dict[str, list[str]] = {}

            # One winner per (version, part) address, chosen by rank rather than
            # by arrival, so the file sent to the model is the same one in every
            # permutation of the manifest.
            variant_parts: dict[str | None, dict[str, str]] = {}
            slot_winners: list[dict] = []
            for (ver, part_key), claimants in slot_candidates.items():
                ranked = sorted(claimants, key=_slot_rank_key)
                winner = ranked[0]
                # A manifest listing one path twice repeats a file rather than
                # contesting a slot, so address the claimants by resolved path:
                # an exact duplicate is not a collision and must not be reported
                # as one.
                losers: dict[str, dict] = {}
                for claimant in ranked[1:]:
                    if claimant["path"] != winner["path"]:
                        losers.setdefault(claimant["path"], claimant)
                if losers:
                    # The commonest shape here is a TIFF master beside its JPEG
                    # derivative: same stem, same slot, one analysis fanned out
                    # over both. Sending one of them is the saving, so the loser
                    # is disclosed rather than sent -- and disclosed the same way
                    # the two rules below disclose theirs.
                    displaced_slots.setdefault(f"{ver or ''}:{part_key}", []).extend(losers)
                    logger.warning(
                        "Group '%s': %d file(s) claim the same %s slot; analyzing %s "
                        "and recording the rest: %s",
                        stem,
                        len(losers) + 1,
                        part_key,
                        winner["path"],
                        ", ".join(os.path.basename(c["path"]) for c in losers.values()),
                    )
                variant_parts.setdefault(ver, {})[part_key] = winner["path"]
                slot_winners.append(winner)

            relabelled_versions: set[str | None] = set()
            if multipage_present:
                # Guardrail: only treat an untagged file as Page 1 when the overall
                # base_id has explicit -pageN entries, so single unrelated photos
                # don't accidentally become page docs.
                for ver, parts in variant_parts.items():
                    if "page:1" not in parts and "none" in parts:
                        parts["page:1"] = parts.pop("none")
                        relabelled_versions.add(ver)

            # Invariant: a listed file is never dropped from the payload in
            # silence. An untagged file reaches the model through the front side
            # of its variant -- as Page 1 in a multipage group, as the front
            # otherwise -- and that role holds exactly one file. When something
            # more specific already holds it, the untagged file has no part to
            # travel in, so say so and record it rather than letting a later
            # assignment overwrite the earlier one and lose it without a word.
            unseated_fronts: set[str] = set()
            for ver, parts in variant_parts.items():
                untagged = parts.get("none")
                if untagged is None:
                    continue
                holder = parts.get("page:1" if multipage_present else "front")
                if holder is None:
                    continue
                parts.pop("none")
                unseated_fronts.add(untagged)
                displaced_slots.setdefault(f"{ver or ''}:none", []).append(untagged)
                logger.warning(
                    "Group '%s': %s and %s both claim the front side of variant "
                    "%s; analyzing %s and recording %s without sending it.",
                    stem,
                    untagged,
                    holder,
                    ver or "(unversioned)",
                    holder,
                    os.path.basename(untagged),
                )
            if unseated_fronts:
                # Only what this rule unseated, not the whole of
                # ``displaced_slots``: a slot-collision loser never entered
                # ``slot_winners``, and a path that lost one address may still
                # hold another, so filtering on every displaced path would strike
                # a file the payload does carry off the candidate list.
                slot_winners = [w for w in slot_winners if w["path"] not in unseated_fronts]

            # Invariant: one path is never sent under two labels. A manifest
            # listing the same file twice under contradicting flags wins it two
            # addresses, and the group payload would then upload, bill and
            # describe it once per address. Keep its best claim -- it stays a
            # candidate for the primary, since it is still sent -- and disclose
            # the rest.
            # The full ``(version, part_key)`` ADDRESS, not just the slot: a
            # manifest can repeat one path under different explicit versions as
            # readily as under different roles, and the caption path resolves
            # attribution by looking the address up in ``variant_parts``. Half
            # an address sends it to the losing entry's own version, where the
            # slot is no longer held, so the file falls back and then overwrites
            # the correctly attributed record for the same path.
            carried_as: dict[str, tuple[str | None, str]] = {}
            for ver, part_key in sorted(
                (
                    (ver, part_key)
                    for ver, parts in variant_parts.items()
                    for part_key in parts
                ),
                key=lambda address: _slot_address_rank(*address),
            ):
                path = variant_parts[ver][part_key]
                if path not in carried_as:
                    carried_as[path] = (ver, part_key)
                    continue
                del variant_parts[ver][part_key]
                displaced_slots.setdefault(f"{ver or ''}:{part_key}", []).append(path)
                kept_slot = carried_as[path][1]
                logger.warning(
                    "Group '%s': %s claims the %s slot as well as the %s slot; "
                    "sending it once, as %s.",
                    stem,
                    path,
                    part_key,
                    kept_slot,
                    kept_slot,
                )

            variant_pairs: dict[str | None, dict[str, str]] = {}
            for ver, parts in variant_parts.items():
                if parts.get("front"):
                    variant_pairs.setdefault(ver, {})["front"] = parts["front"]
                if parts.get("back"):
                    variant_pairs.setdefault(ver, {})["back"] = parts["back"]
                if parts.get("none") and not multipage_present:
                    variant_pairs.setdefault(ver, {})["front"] = parts["none"]
                if multipage_present and parts.get("page:1"):
                    variant_pairs.setdefault(ver, {})["front"] = parts["page:1"]

            order_index = {v: i for i, v in enumerate(variant_order)}
            variant_list_sorted = sorted(
                variant_order,
                key=lambda v: (v is None, v or "", order_index.get(v, 0)),
            )
            page_nums_all: list[int] = []
            if multipage_present:
                page_set: set[int] = set()
                for parts in variant_parts.values():
                    for key in parts.keys():
                        if key.startswith("page:"):
                            try:
                                page_set.add(int(key.split(":", 1)[1]))
                            except (ValueError, TypeError):
                                continue
                page_nums_all = sorted(page_set)

            all_negatives: list[str] = []
            for ver in variant_list_sorted:
                neg = variant_parts.get(ver, {}).get("negative")
                if neg and neg not in all_negatives:
                    all_negatives.append(neg)

            # Primary selection reads the slot map rather than the arrival order,
            # so the analyzed file is always one the payload actually carries.
            # ``preferred`` is honored through ``_slot_rank_key``, which lets it
            # take any slot it contends for; an item that still owns no slot is
            # never the primary, because the primary is by definition the file
            # sent. That is what keeps a ``preferred`` crop -- which loses its
            # parent's slot on crop-ness -- from being named as the analyzed file
            # of a payload it is not in.
            candidates: list[dict] = []
            seen_candidate_paths: set[str] = set()
            for entry in sorted(slot_winners, key=_slot_rank_key):
                if entry["path"] not in seen_candidate_paths:
                    seen_candidate_paths.add(entry["path"])
                    candidates.append(entry)

            # ``candidates`` is already in rank order, so its head IS "the best
            # non-crop, preferred-if-any, front-side, unversioned file" -- the
            # thing the retired ``pick_master_index`` approximated by scanning
            # arrival order, and the second of the two order-dependent choices
            # behind the B1 crop bug. Rank puts a negative last on its own
            # (``_PART_RANK["negative"] == 4``), so the separate negative filter
            # that used to guard the master pick is gone with it; a ``preferred``
            # negative still wins, since ``preferred`` outranks part kind.
            primary_item = candidates[0]
            primary_front = primary_item["path"] if not primary_item["is_back"] else None
            primary_version = primary_item["version"]
            # A back is only ever chosen here because the caller preferred it or
            # because the group holds nothing else, and either way it is the back
            # to send: resolving it from the slot map instead would let the
            # version lookup below hand the model a different file than the one
            # the caller named.
            primary_back = primary_item["path"] if primary_item["is_back"] else None
            if primary_front is None:
                # Search the whole candidate list, negatives included: a group
                # holding only a negative and a back has exactly one front-side
                # file, and it is the negative. Fallback-safe even when the group
                # holds nothing but backs.
                front_entry = next((c for c in candidates if not c["is_back"]), None)
                if front_entry is None:
                    primary_front = primary_item["path"]
                else:
                    primary_front = front_entry["path"]
                    # ``primary_version`` addresses the back slot below and scopes
                    # the analysis's PC* keywords, so it has to describe the file
                    # actually sent as the front, not the item that won a master
                    # pick it then lost the front role to.
                    primary_version = front_entry["version"]
            subject = primary_front

            # Read the back out of the slot map so the back sent to the model is
            # always the file that owns the slot: same version as the primary
            # first, then any unversioned back, then the lowest-sorting one.
            if primary_back is None:
                primary_back = variant_pairs.get(primary_version, {}).get("back")
            if primary_back is None:
                primary_back = variant_pairs.get(None, {}).get("back")
            if primary_back is None:
                primary_back = next(
                    (
                        variant_pairs[ver]["back"]
                        for ver in variant_list_sorted
                        if "back" in variant_pairs.get(ver, {})
                    ),
                    None,
                )

            if primary_back is not None and primary_back == primary_front:
                # Invariant: one path is never sent under two labels. A group with
                # no front side resolves both roles to the same file, as does a
                # manifest listing one path twice under conflicting flags, and
                # ``analyze_photo`` would then upload it, bill for it and describe
                # it twice -- once as a side it demonstrably is not.
                logger.info(
                    "Group '%s': %s is the only file standing for both sides; "
                    "sending it once rather than as its own back.",
                    stem,
                    primary_front,
                )
                primary_back = None

            all_fronts: list[str] = []
            all_backs: list[str] = []
            for ver in variant_list_sorted:
                slot_pair = variant_pairs.get(ver, {})
                front_path = slot_pair.get("front")
                if front_path and front_path not in all_fronts:
                    all_fronts.append(front_path)
                back_path = slot_pair.get("back")
                if back_path and back_path not in all_backs:
                    all_backs.append(back_path)

            # The one predicate the whole payload hangs on, computed from the
            # group's own contents rather than from a flag. There is no primary
            # any more, so "send the whole group" is decided by whether the group
            # holds anything a single front/back pair cannot describe: a page, a
            # negative, a second front-side scan or a second back.
            group_payload = (
                multipage_present
                or bool(all_negatives)
                or len(all_fronts) > 1
                or len(all_backs) > 1
            )

            # Warn only once the file set bound for the model is known, and test
            # against that set rather than against ``primary_front``: the
            # group-aware path sends more than the primary, and a crop the caller
            # marked ``preferred`` can be the primary yet still miss the payload.
            if group_payload:
                analyzed_paths = {p for parts in variant_parts.values() for p in parts.values()}
            else:
                analyzed_paths = {p for p in (primary_front, primary_back) if p}

            # The ``group_by`` guard: under ``none`` every crop is an orphan by
            # construction -- its group holds one file, so there is no parent for
            # it to be a supporting view of -- and this warning is written to
            # flag a surprising input, not a property of the mode.
            for it in crops:
                if (
                    group_by != utils.GROUP_BY_NONE
                    and it["path"] in orphan_crops
                    and it["path"] in analyzed_paths
                ):
                    # Nothing uncropped claimed this slot, so the crop is all
                    # that stands for the object -- manifest mode owes every
                    # listed file a result, so it is analyzed rather than skipped.
                    logger.warning(
                        "Group '%s': %s has no uncropped original in the manifest; "
                        "analyzing the crop as the object itself.",
                        stem,
                        it["path"],
                    )

            unanalyzed_crops = [c for c in crops if c["path"] not in analyzed_paths]
            if unanalyzed_crops:
                logger.warning(
                    "Group '%s': %d crop file(s) are recorded but not analyzed: %s",
                    stem,
                    len(unanalyzed_crops),
                    ", ".join(os.path.basename(c["path"]) for c in unanalyzed_crops),
                )
            # The completion line's count, read off the payload the group is
            # about to send. Every warning above names a file this set holds, and
            # it holds nothing a warning did not name, so the summary cannot
            # contradict them. It also leaves out the one file a warning says
            # *was* sent: a path that won two addresses still travels, under the
            # better of them.
            unsent_paths.update(
                it["path"] for it in group if it["path"] not in analyzed_paths
            )

            combined_meta = utils.combine_group_metadata(group)
            sent_to_model_snapshot = select_forwarded_metadata(combined_meta, forward_fields)
            hydration_dump_writer = _build_hydration_dump_writer(cfg, subject, "group")
            if hydration_dump_writer:
                hydration_dump_writer(combined_meta)

            # At this point we have:
            #   - ``group``: all manifest entries for this logical photo (same stem)
            #   - ``primary_front`` / ``primary_back``: the chosen canonical pair
            #   - ``variant_pairs``: {version -> {"front": ..., "back": ...}}
            #
            # We now call into the model. A group payload sends every file that
            # owns a slot in one call, so the model can write a single natural
            # caption that applies across the set; a group a single pair fully
            # describes takes the pair call instead. The two analyzers are not
            # interchangeable -- the group one prefixes a "multiple scans or
            # variants" note to the prompt, tags its dump differently and raises
            # where the pair one rewraps -- so binding the callee to the payload
            # shape is what keeps an ordinary front/back run byte-identical.

            analyses: list[tuple[dict, str, str | None]] = []

            if group_payload:
                # --- Group-aware path: send all variants in one call -----------------
                if multipage_present:
                    parts_for_analysis: list[tuple[str, list[str]]] = []

                    for num in page_nums_all:
                        part_paths: list[str] = []
                        for ver in variant_list_sorted:
                            pth = variant_parts.get(ver, {}).get(f"page:{num}")
                            if pth and pth not in part_paths:
                                part_paths.append(pth)
                        if part_paths:
                            parts_for_analysis.append((f"Page {num}", part_paths))

                    def _collect_part(key: str, label: str):
                        # Preserve supplemental sides (front/back) after pages, since
                        # multipage mode prioritizes ordered pages first.
                        part_paths: list[str] = []
                        for ver in variant_list_sorted:
                            pth = variant_parts.get(ver, {}).get(key)
                            if pth and pth not in part_paths:
                                part_paths.append(pth)
                        if part_paths:
                            parts_for_analysis.append((label, part_paths))

                    _collect_part("front", "Front")
                    _collect_part("back", "Back")
                    _collect_part("negative", "Negative")

                    if not parts_for_analysis:
                        raise ValueError(f"No parts collected for multipage group {stem}")

                    data_group = analyze_group_parts(
                        parts=parts_for_analysis,
                        config=cfg,
                        original_meta=combined_meta,
                        write_sidecar=write_sidecars,
                    )
                elif all_negatives:
                    # A negative is neither a front nor a back, so it needs the
                    # generic part form. The ordinary front/back call site is
                    # left exactly as it was.
                    data_group = analyze_group_parts(
                        parts=[
                            (label, paths)
                            for label, paths in (
                                ("Front", all_fronts),
                                ("Back", all_backs),
                                ("Negative", all_negatives),
                            )
                            if paths
                        ],
                        config=cfg,
                        original_meta=combined_meta,
                        write_sidecar=write_sidecars,
                    )
                else:
                    data_group = analyze_group_front_back(
                        all_fronts,
                        all_backs,
                        cfg,
                        original_meta = combined_meta,
                        write_sidecar = write_sidecars,
                    )

                # The group helper uses the same JSON shape as ``analyze_photo`` but
                # we still normalize to ``primary_front`` for consistency with the
                # rest of this function.
                result_obj = (data_group.get("result") or {}) if isinstance(data_group, dict) else {}
                if primary_front not in result_obj:
                    if isinstance(result_obj, dict) and len(result_obj) == 1:
                        only_key = next(iter(result_obj.keys()))
                        canonical = result_obj[only_key]
                    else:
                        raise KeyError(
                                f"Group analysis result did not contain expected key {primary_front!r}"
                        )
                else:
                    canonical = result_obj[primary_front]

                analyses.append((canonical, primary_front, primary_version))

            else:
                # --- Pair path: one front and, at most, its own back ------------------
                data_primary = analyze_photo(
                        primary_front,
                        primary_back,
                        cfg,
                        original_meta = combined_meta,
                        write_sidecar = write_sidecars,
                )
                canonical = data_primary["result"][primary_front]
                analyses.append((canonical, primary_front, primary_version))

            multiple_fronts = len([it for it in group if not it["is_back"]]) > 1
            multiple_backs = len([it for it in group if it["is_back"]]) > 1

            # Variant merge rules:
            # - Combine keywords from every photo but keep the part markers on the
            #   files they describe and share PC* codes across the whole group.
            # - Preserve existing captions, then append generated captions labeled by
            #   front/back and variant letter when multiples exist.
            # - Share AI analysis notes across the set.
            # - Pick the highest-confidence location/date guess across analyses.

            def _split_keywords_for_merge(keywords: list[str] | None) -> tuple[list[str], list[str]]:
                base: list[str] = []
                pc_only: list[str] = []
                for raw in keywords or []:
                    if not isinstance(raw, str):
                        continue
                    kw = raw.strip()
                    if not kw:
                        continue
                    # A part marker describes one file, so it must not reach the
                    # group-wide pool that lands on all of them. The model emits
                    # "Negative" now that it is told a ``Negative`` part is
                    # present, and it would otherwise spread to the print.
                    if kw.lower() in utils.PART_MARKER_KEYWORDS:
                        continue
                    if kw.upper().startswith("PC"):
                        pc_only.append(kw)
                        continue
                    base.append(kw)
                return base, pc_only

            keyword_bases: list[list[str]] = []
            pc_codes: list[str] = []
            combined_base, _ = _split_keywords_for_merge(combined_meta.get("keywords"))
            if combined_base:
                keyword_bases.append(combined_base)
            for rec, _, _ver in analyses:
                base_kw, pc_kw = _split_keywords_for_merge(rec.get("keywords"))
                if base_kw:
                    keyword_bases.append(base_kw)
                if pc_kw:
                    pc_codes.extend(pc_kw)
            # Rule 1: union all shared keywords; "back" only applies when we later
            # emit a back record.
            shared_keywords = utils.union_keywords(*keyword_bases)
            # A PC* code is a short identifier the model transcribes off the object
            # itself (image_rules.txt:97), so it describes the physical print rather
            # than the one scan that happened to be legible. Every variant in a group
            # is another scan of that same print, so the codes are shared across the
            # group. Scoping them to the analyzed variant meant a -b rescan silently
            # lost the code its sibling gave up, since only one analysis runs per
            # group and only files sharing its variant letter ever saw the codes.
            group_pc_codes = utils.union_keywords(pc_codes) if pc_codes else []

            def _best_guess(field: str):
                best = None
                best_conf = -1.0
                for rec, _, _ in analyses:
                    guess = rec.get(field) or {}
                    conf = guess.get("confidence") if isinstance(guess, dict) else None
                    if isinstance(conf, (int, float)) and conf > best_conf:
                        best_conf = conf
                        best = guess
                return best

            # Rule 4: use the location/date guess with the highest confidence across analyses.
            best_location = _best_guess("location_guess")
            best_date = _best_guess("date_guess")
            if best_location:
                canonical["location_guess"] = best_location
            if best_date:
                canonical["date_guess"] = best_date

            # relabelled_versions-as-frozenset, hoisted out of the sidecar gate
            # further down: resolve_part_label needs it to map a file to its
            # part label, and the per-file caption build below calls
            # resolve_part_label too -- on the default path, where sidecars are
            # off. Left inside that gate it would silently stay the empty
            # frozenset whenever sidecars are off, and the untagged file that
            # became Page 1 would resolve to the wrong label, which is now a
            # wrong caption on the commonest run there is. The mutable
            # ``relabelled_versions`` it freezes is already computed
            # unconditionally above (the crop map needs it); only the freeze was
            # gated, so hoisting it costs an ordinary run one frozenset() call,
            # not the sidecar-only work. Do not move this back inside the gate.
            relabelled_versions_frozen: frozenset[str | None] = frozenset(relabelled_versions)

            # === Rule 2: one caption block per object, written to every file ===
            #
            # Still built for every group, including a document, where it is
            # what a file the model did not transcribe falls back to -- but for
            # a document it is no longer what most files receive. The per-file
            # rule is below this block, and reads it.
            #
            # A print, its back and a rescan of it are one object, so whichever
            # of them someone opens in Lightroom should tell the whole story of
            # that object rather than a third of it. Every file of the group
            # therefore ends up holding the SAME caption: each scan's own
            # caption, labelled with the file it came off, then this run's
            # analysis.
            #
            #     [Photo A] Caption A
            #     [Photo B] Caption B
            #     [Back] Back of Photo B
            #     [AI Analysis]: Two people outside a bakery.
            #
            # It has to be built for the group rather than per file, and that is
            # the whole architecture: a per-file block would carry that file's
            # own caption as a personal preamble, no two files would match, and
            # the point would be lost. So the intake below sweeps the group while
            # it is still known WHICH file each caption came off -- the one
            # moment attribution is free rather than guesswork -- and everything
            # after it is keyed by label.
            #
            # Being keyed is also what makes the block safe to re-read, which is
            # not optional: under ``-rw`` the block written here is exactly what
            # the next run reads back as each file's existing caption. Intake
            # recognizes its own output and takes it verbatim; attributing it a
            # second time is how you get "[Photo A] [Photo A] Caption A" and a
            # caption that grows on every pass.

            # --- Which label each file's caption is filed under -----------------

            has_front_side = any(not it["is_back"] for it in group)
            has_back_side = any(it["is_back"] for it in group)
            # "[Back]" only says anything opposite a "[Photo]", and a lone file
            # has nothing to be told apart from, so the overwhelmingly common
            # trivial case -- one scan, no back -- carries no label at all.
            # Labelling it would bracket every caption in an archive that has no
            # variants in it and say nothing by doing so.
            label_backs = has_front_side and has_back_side
            label_photos = multiple_fronts or label_backs

            # An unversioned scan is variant A: that is precisely why the second
            # scan of a print is lettered 'b' and not 'a'. Printing it as
            # "[Photo A]" is what makes the letters in the block the letters on
            # disk. Only beside a lettered sibling, though -- with none there is
            # nothing to disambiguate and the A would be invented -- and never
            # when the group holds a real 'a', which would be two files claiming
            # one label.
            explicit_versions = {
                (it["version"] or "").strip().casefold()
                for it in group
                if (it["version"] or "").strip()
            }
            implied_first_variant = bool(explicit_versions) and "a" not in explicit_versions

            def _display_version(ver: str | None) -> str:
                letter = (ver or "").strip()
                if not letter:
                    return "A" if implied_first_variant else ""
                # A single letter is the filename grammar's own token and reads
                # as an identifier, so it is capitalized to match; anything
                # longer came from a manifest ``version`` and is the caller's
                # own wording to leave alone.
                return letter.upper() if len(letter) == 1 else letter

            def _label_for(is_back: bool, ver: str | None, page_num: int | None = None) -> str:
                """Return the label a file's own caption is filed under, or ""."""
                # The letter appears only where it disambiguates, decided per
                # role independently -- which is exactly what multiple_fronts and
                # multiple_backs already answer, so they answer it here. Two
                # photos and one back give "[Photo A]", "[Photo B]" and a bare
                # "[Back]": the lone back needs no letter to be found.
                if is_back:
                    if not label_backs:
                        return ""
                    base, lettered = "Back", multiple_backs
                else:
                    if not label_photos:
                        return ""
                    base, lettered = "Photo", multiple_fronts
                letter = _display_version(ver) if lettered else ""
                if lettered and not letter and page_num is not None:
                    # multiple_fronts is also true of a multi-page document,
                    # where nothing distinguishes the pages but the page
                    # number itself -- none of them carry a variant letter, so
                    # _display_version has nothing to disambiguate with and
                    # every page would otherwise collapse onto the identical
                    # bare "[Photo]" label. The number is what disk already
                    # calls them (album-page1.jpg, album-page2.jpg), so using
                    # it here keeps the label the letters-on-disk rule already
                    # promises for variants.
                    letter = str(page_num)
                return f"[{base} {letter}]" if letter else f"[{base}]"

            # --- Intake: every file's caption, in one deterministic order -------
            #
            # ``_slot_rank_key`` and not the manifest's order, for the reason
            # every other choice in this loop is made on rank: the block is a
            # property of the object, so a folder listed in a different order has
            # to produce the same one. A file that already holds a whole block
            # contributes all of its sections at once, in the order that block
            # had, which is what keeps a settled group's answer stable.
            own_metadata = [utils.load_item_metadata(it) or {} for it in group]
            group_intake = [
                (
                    (entry_meta.get("caption") or "").strip(),
                    _label_for(entry["is_back"], entry["version"], entry.get("page_num")),
                )
                for entry, entry_meta in sorted(
                    zip(group, own_metadata), key=lambda pair: _slot_rank_key(pair[0])
                )
            ]

            # --- This run's own transcription, merged in last --------------------
            #
            # ``caption`` is this run's verbatim transcription and nothing else.
            # ``ai_caption`` -- the model's interpretation -- is never merged into
            # the caption block; it reaches EXIF:UserComment on its own, via
            # canonical.py's own mapping.
            #
            # One analysis per group either way -- both payload branches append
            # exactly one entry to ``analyses`` -- so this step no longer forks
            # on ``group_payload``. A group payload's transcription already
            # carries its own [Front]/[Back]/[Page N] headers (see the prompt
            # built in ``analyze_group_parts``), so it is split and filed under
            # those exactly as a pre-existing per-file caption would be; a lone
            # scan's plain, unbracketed transcription is filed unlabelled.
            fresh_group_caption = ""
            if analyses:
                fresh_group_caption = _strip_empty_caption_sections(
                    (analyses[0][0].get("caption") or "").strip()
                )

            # Built only for a group that will actually use it. A multipage
            # group takes the per-file path below and, when a file has no
            # transcription of its own, assembles its own fallback from a
            # narrower intake -- so assembling the group-wide block here too
            # would be work thrown away, and not cheap work: the intake it
            # folds is every file's stored caption, compared pairwise, on
            # exactly the long documents this change exists to make scale.
            caption_block = (
                None
                if multipage_present
                else _assemble_caption_block(group_intake, fresh_group_caption)
            )

            # === Each page of a document carries its own caption ===============
            #
            # The block above is what a group of views of one object gets, on
            # every one of its files, exactly as it always has. A multipage
            # group does not build it at all: its files each assemble their
            # own below, and the one that falls back assembles a narrower
            # block from the files that fell back with it. A 63-page letter
            # wrote the whole book into all 63 files' Description: 63x
            # redundant, and wrong for the reader, who opens page 37 and is
            # shown page 1. Each file gets its OWN part's transcription instead,
            # which is also what the .md sidecar has been writing all along --
            # until now the sidecar and Description disagreed about the same
            # document.
            #
            # The trigger is document-ness, not size. ``multipage_present``
            # already means "an ordered sequence of pages rather than views of
            # one object", which is the distinction the reasoning depends on; a
            # byte threshold would make it unpredictable ("why does page 20 have
            # the whole book and page 21 not?"). A print, its back and a rescan
            # keep the shared block, because they ARE one object.
            #
            # Uniform within the group: a ``Back`` in a multipage group gets its
            # own ``Back`` transcription, not the book. Attribution follows
            # part-ness or it does not, and the back of page 3 is no more the
            # whole book than page 3 is. Variants of one page need no rule of
            # their own -- page2.jpg and page2b.jpg both resolve to ``Page 2``
            # and both find that one transcription.
            #
            # Two things here are load-bearing and neither is obvious:
            #
            # - the intake is THIS file's own stored caption and nothing else.
            #   Sweeping the group, as the block above does, would hand every
            #   page every other page's stored text on the first ``-rw``; from
            #   the pass after that, that text is the file's own stored caption
            #   and the change has undone itself while still passing any test
            #   that only looks at a single run.
            # - the fresh text carries NO label. The file holds exactly one
            #   part's text, so there is nothing to tell it apart from -- the
            #   same rule a lone scan already follows. A label would also have
            #   to be one ``_CAPTION_LABEL_RE`` recognizes or it would be
            #   re-attributed on the next read, and teaching that regex
            #   "[Page N]" re-introduces a bug this repo shipped once already:
            #   a letterhead repeated across two pages becomes a cross-section
            #   duplicate and the section-scoped line dedup deletes it. Measured
            #   against both labelled candidates, unlabelled is also the only
            #   one that is a fixed point from run 1 rather than run 2.
            #
            # Nothing here looks at what an earlier release stored. An archive
            # processed under the group-wide rule keeps the block it holds, on
            # every file, permanently; that block is a stable fixed point under
            # this rule and reconciling it is the user's own act.
            # ``isinstance`` twice over, and not for tidiness: this map is
            # model-written, and while the parse normalizes it, ``canonical``
            # is whatever reached the emit loop. A reply of
            # ``"transcriptions": ["Page 1"]``, or a page whose value came back
            # as a list of lines, is valid JSON that would raise here -- inside
            # the per-group try, after the analysis is already paid for, taking
            # down a whole group over a caption it could simply have declined
            # to attribute.
            group_transcriptions = canonical.get("transcriptions")
            if not isinstance(group_transcriptions, dict):
                group_transcriptions = {}
            # One (caption, scope) per entry of ``group``, positionally, so the
            # emit loop can zip it beside ``own_metadata`` rather than key it by
            # a path a manifest may list twice.
            captions_for_files: list[tuple[str | None, str | None]] = [
                (caption_block, None)
            ] * len(group)
            if multipage_present:
                captions_for_files = []
                kept_group_block: list[str] = []
                # Decided for every file before any block is built, because the
                # fallback block depends on WHICH files fell back.
                #
                # Resolving a label is not the same as having travelled under
                # it. A file that lost its slot -- an untagged scan unseated by
                # a real ``-front``, a TIFF displaced by its JPEG -- still
                # resolves to a label, and if some OTHER file rode that label
                # the lookup below finds a transcription belonging to a
                # different piece of paper. Writing that into this file's
                # Description under ``caption_scope: "part"`` would state
                # affirmatively that the attribution was made on purpose.
                # ``analyzed_paths`` is the set the payload actually carried,
                # so it is the question worth asking.
                attributed: list[str] = []
                for entry in group:
                    # Attribution follows the SLOT this file contends for, not
                    # whether this particular path was the one sent. The two
                    # differ for every file that yields its slot to a better
                    # claimant and is still recorded:
                    #
                    # - a crop yields to the uncropped original and a displaced
                    #   TIFF yields to its JPEG, but each is a view of the same
                    #   physical page as the file that won, so that page's
                    #   transcription is theirs too. Asking whether the path
                    #   itself travelled would hand a crop of page 1 the whole
                    #   document instead of page 1.
                    # - an untagged scan unseated by a real ``-front`` is NOT a
                    #   view of the front; it merely resolves to the same label.
                    #   Its slot is popped from ``variant_parts`` when it is
                    #   unseated, so the lookup below finds no winner and it
                    #   falls back, which is the whole point of the check.
                    slot_key = _manifest_part_key(entry)
                    if slot_key == "none" and entry["version"] in relabelled_versions_frozen:
                        slot_key = "page:1"
                    # ``carried_as`` first, because a manifest may list one path
                    # twice under contradicting roles -- once as its filename's
                    # page, once flagged a back. Only one of those addresses
                    # survives arbitration, and the losing ENTRY still reaches
                    # this loop; resolving it through its own discarded slot
                    # finds no winner, falls back, and then overwrites the
                    # correctly attributed record for that same path, because
                    # both entries emit under one key. The path travelled under
                    # exactly one label, so that is the one to attribute it by.
                    slot_version, slot_key = carried_as.get(
                        entry["path"], (entry["version"], slot_key)
                    )
                    # The label comes from the RETAINED address too, not from
                    # the entry: taking the address's winner while keeping the
                    # losing entry's label would look up a transcription
                    # belonging to the address that was discarded. So would
                    # keeping the entry's own version, which is a second way for
                    # one manifest to name the same path twice.
                    part_label = part_label_for_slot(slot_key)
                    part_value = group_transcriptions.get(part_label)
                    part_text = part_value.strip() if isinstance(part_value, str) else ""
                    slot_winner = variant_parts.get(slot_version, {}).get(slot_key)
                    attributed.append(
                        part_text if slot_winner in analyzed_paths else ""
                    )

                # The block a file with no transcription of its own falls back
                # to, built from the stored captions of the files that ALSO
                # fell back -- never from the ones that got their own page.
                #
                # When nothing was attributed this is the group intake entire,
                # so a group whose reply carried no map at all keeps exactly
                # today's block on every file, which is what the fallback
                # promises. When some pages were attributed it matters: their
                # stored captions are, from the second ``-rw`` pass onward, the
                # thin per-page captions this rule just wrote, and sweeping
                # them back in would hand the unanswered file every answered
                # page's text a second time, each under a ``[Photo N]`` label
                # attributing one page's words to another. Measured on a
                # six-page group with two pages unanswered: 56 characters on
                # pass 1, 127 on pass 2, then stable -- growth of one step,
                # which is still a file whose caption changed on a run that
                # found nothing new.
                fallback_intake = [
                    (
                        (entry_meta.get("caption") or "").strip(),
                        _label_for(entry["is_back"], entry["version"], entry.get("page_num")),
                    )
                    for entry, entry_meta, part_text in sorted(
                        zip(group, own_metadata, attributed),
                        key=lambda triple: _slot_rank_key(triple[0]),
                    )
                    if not part_text
                ]
                # Only when something actually falls back. With every file
                # attributed -- the ordinary case for a document the model
                # answered in full -- this would otherwise fold the whole
                # transcript into a block no branch reads, once per group, on
                # top of the per-file blocks already built.
                fallback_block = (
                    _assemble_caption_block(fallback_intake, fresh_group_caption)
                    if any(not part_text for part_text in attributed)
                    else None
                )

                for entry, entry_meta, part_text in zip(group, own_metadata, attributed):
                    if not part_text:
                        # No map at all, a partial one, or a file that was
                        # never sent. Inventing an attribution nothing supports
                        # is the one thing this codebase consistently refuses to
                        # do, so the file takes the group's transcription
                        # instead -- and says which of the two regimes it is in,
                        # so an embedder reading a folder that ended up mixed
                        # can tell rather than guessing from length.
                        kept_group_block.append(os.path.basename(entry["path"]))
                        captions_for_files.append((fallback_block, "group"))
                        continue
                    own_caption = (entry_meta.get("caption") or "").strip()
                    captions_for_files.append(
                        (_assemble_caption_block([(own_caption, "")], part_text), "part")
                    )
                if kept_group_block:
                    logger.info(
                        "Group '%s': %d of %d file(s) have no transcription of their own "
                        "and keep the group's caption block: %s",
                        stem,
                        len(kept_group_block),
                        len(group),
                        ", ".join(kept_group_block),
                    )

            def _tok(u: dict | None, key: str) -> int:
                return int(u.get(key)) if (u and isinstance(u.get(key), int)) else 0
            tot_prompt = sum(_tok(rec.get("_usage"), "prompt_tokens") for rec, _, _ in analyses)
            tot_completion = sum(_tok(rec.get("_usage"), "completion_tokens") for rec, _, _ in analyses)
            # analyses has one entry per API call made for this group (usually
            # exactly one); take the resolved model string from it -- this
            # dict otherwise replaces the per-analysis _usage entirely, so
            # omitting "model" here silently drops it downstream (cost
            # estimation, provenance) even though the underlying API call did
            # return one.
            usage_model = (analyses[0][0].get("_usage") or {}).get("model") if analyses else None
            canonical["_usage"] = {
                "prompt_tokens": tot_prompt or None,
                "completion_tokens": tot_completion or None,
                "input_tokens": tot_prompt or None,
                "output_tokens": tot_completion or None,
                "total_tokens": (tot_prompt + tot_completion) or None,
                "model": usage_model,
            }

            canonical["keywords"] = shared_keywords

            pages_map: dict[str, list[str]] = {}
            if multipage_present:
                for num in page_nums_all:
                    key = str(num)
                    part_paths: list[str] = []
                    for ver in variant_list_sorted:
                        pth = variant_parts.get(ver, {}).get(f"page:{num}")
                        if pth and pth not in part_paths:
                            part_paths.append(pth)
                    if part_paths:
                        pages_map[key] = part_paths

            canonical["all_variant_files"] = {
                "front": [it["path"] for it in group if not it["is_back"]],
                "back": [it["path"] for it in group if it["is_back"]],
                "variants": [
                    {
                        "path": it["path"],
                        "version": it.get("version"),
                        "is_back": it["is_back"],
                        "preferred": bool(it.get("preferred")),
                    }
                    for it in group
                ],
                "all": [it["path"] for it in group],
            }
            if pages_map:
                canonical["all_variant_files"]["pages"] = pages_map
            if crops:
                # Additive, and only for groups that actually hold crops: the
                # plug-in fans metadata out locally, so it still needs to know
                # which files are crops and which slot each one sat in. Every
                # crop is listed, including an orphan that was analyzed for want
                # of an original. Rank-ordered, unlike the arrival-ordered lists
                # above, since nothing existing depends on this key's order.
                crops_map: dict[str, list[str]] = {}
                for it in crops:
                    part_key = _manifest_part_key(it)
                    if part_key == "none" and it["version"] in relabelled_versions:
                        # The untagged slot of this variant became page 1 above,
                        # so a crop of it is filed under the label it ended up
                        # with rather than the one it was parsed into.
                        part_key = "page:1"
                    slot = f"{it['version'] or ''}:{part_key}"
                    crops_map.setdefault(slot, []).append(it["path"])
                canonical["all_variant_files"]["crops"] = crops_map
            if all_negatives:
                canonical["all_variant_files"]["negatives"] = all_negatives
            if displaced_slots:
                # Additive, and only for groups that lost a file this way: the
                # ``front`` list above is every front-side file in the group, so
                # on its own it reads as though each of them reached the model.
                # This names the ones that could not.
                canonical["all_variant_files"]["displaced"] = displaced_slots

            canonical_analysis_notes = next(
                (rec.get("analysis_notes") for rec, _, _ in analyses if rec.get("analysis_notes")),
                canonical.get("analysis_notes"),
            )
            if canonical_analysis_notes:
                # Rule 3: keep analysis notes in sync across all variants.
                canonical["analysis_notes"] = canonical_analysis_notes

            # Rule 1 (continued): the markers this group asserts. Nothing else
            # spelled like a marker can have leaked from a sibling, so the strip
            # below narrows this set per file rather than applying it whole.
            applied_markers = frozenset(
                marker for marker in map(_item_part_marker, group) if marker
            )

            # Document-mode sidecar placement facts (contract section 8),
            # computed once per group rather than once per file below: both are
            # static across every file the loop emits, and skipped outright
            # when the flag is off so an ordinary run pays nothing for a
            # feature it never asked for.
            sidecar_group_files: tuple[str, ...] = ()
            sidecar_page_count: int | None = None
            # Which file owns each sidecar destination, decided by rank rather
            # than by the order the group happens to be listed in. Two members
            # whose names differ only by extension -- a TIFF master beside its
            # JPEG derivative, which the slot-collision rule above calls the
            # commonest shape there is -- resolve to one ``<stem>.md``, and one
            # of them has to yield. Rank is the same order that already chose
            # which of the two the model saw, so the sidecar ends up belonging
            # to the file that was actually analyzed, and a folder listed in a
            # different order still produces the same result.
            sidecar_owner: dict[str, str] = {}
            if cfg.sidecar_md != utils.SIDECAR_MD_OFF:
                ranked_group = sorted(group, key=_slot_rank_key)
                for ranked_entry in ranked_group:
                    sidecar_owner.setdefault(
                        doc_sidecar.sidecar_path_for(ranked_entry["path"]),
                        ranked_entry["path"],
                    )
                sidecar_group_files = tuple(
                    os.path.basename(g["path"]) for g in ranked_group
                )
                sidecar_page_count = len(page_nums_all) if multipage_present else None

            # emit per-file (merged with per-file metadata)
            for it, own_meta, (file_caption, caption_scope) in zip(
                group, own_metadata, captions_for_files
            ):
                # Read before the merge below, which is the last moment the two
                # are distinguishable: a marker this file already carried is the
                # caller's own keyword however many siblings share the part it
                # names, so it is not one of the leaks the strip may undo.
                own_markers = utils.part_markers_in(
                    own_meta.get("keywords") or own_meta.get("tags")
                )
                per_meta = utils.merge_original_sources(own_meta, combined_meta)

                # The one keyword that is a property of this file rather than of
                # the object.
                part_marker = _item_part_marker(it)
                record_for_item = deepcopy(canonical)
                keywords_for_item = utils.union_keywords(shared_keywords, group_pc_codes)
                if part_marker:
                    keywords_for_item = utils.union_keywords(keywords_for_item, [part_marker])
                record_for_item["keywords"] = keywords_for_item

                # Rule 2, for a group of views of one object: the group's one
                # block, byte-identical on every file. This file's own caption
                # is already in it, under this file's label, put there by the
                # intake sweep above -- joining it again here is what would give
                # each file a personal preamble and make the blocks diverge. For
                # a multipage document it is instead this file's own part, built
                # above, and ``caption_scope`` records which of the two this
                # file got. The key is written only inside a multipage group,
                # where the two regimes can differ file to file; everywhere else
                # the caption is group-scoped by design and saying so on every
                # record would be noise.
                if file_caption:
                    record_for_item["caption"] = file_caption
                # Cleared before it is conditionally set, because the record
                # this was deep-copied from is the MODEL's, and nothing filters
                # a reply down to the keys the schema names. A reply that
                # happened to carry a "caption_scope" of its own would ride
                # through to an ordinary photo's record, where the documented
                # contract says the key does not appear at all -- and an
                # embedder is told to read exactly this key to tell the two
                # caption regimes apart. Only a value derived here is true.
                record_for_item.pop("caption_scope", None)
                if caption_scope:
                    record_for_item["caption_scope"] = caption_scope

                merged, report = merge_metadata(
                    record_for_item,
                    per_meta,
                    cfg,
                    original_title_from_file=(
                        titles_may_be_from_files
                        if title_from_file_ids is None
                        else id(own_meta) in title_from_file_ids
                    ),
                )
                # ``combined_meta`` is the whole group's metadata keywords,
                # un-stripped, and it has just been merged into every file, so a
                # marker belonging to one file has to come back off the others.
                utils.apply_part_keyword(
                    merged, part_marker, applied_markers - own_markers
                )
                merged["all_variant_files"] = canonical["all_variant_files"]

                # Document-mode sidecar (contract sections 7-8): the gate is
                # exactly the contract's, and a crop is excluded either way
                # (D9) -- it is a supporting view of its parent and its
                # sidecar would duplicate the parent's byte for byte.
                # ``isinstance`` before the membership test, and not for tidiness:
                # ``category`` is model-written, and a reply of ``["Document"]``
                # is valid JSON that reaches here as an unhashable list, where
                # ``in`` on a frozenset raises TypeError. This gate runs after
                # the analysis is paid for and inside the per-group try, so that
                # would fail the entire group over a sidecar it could simply
                # have declined to write.
                merged_category = merged.get("category")
                if not it["is_crop"] and (
                    cfg.sidecar_md == utils.SIDECAR_MD_ALL
                    or (
                        cfg.sidecar_md == utils.SIDECAR_MD_AUTO
                        and isinstance(merged_category, str)
                        and merged_category in utils.SIDECAR_AUTO_CATEGORIES
                    )
                ):
                    # Invariant, borrowed from the payload rules above: a file
                    # is never overwritten in silence. The owner was settled by
                    # rank before this loop began; anyone else pointing at the
                    # same destination says so and writes nothing.
                    destination = doc_sidecar.sidecar_path_for(it["path"])
                    # Two guards, because a collision has two shapes. Within a
                    # group, rank settled the owner before this loop began.
                    # Across groups there is no rank to appeal to -- under
                    # ``--group-by none`` each file is its own group -- so the
                    # first writer of a destination in this run keeps it.
                    owner = sidecar_owner.get(destination, it["path"])
                    contender = sidecar_written.get(destination)
                    if owner != it["path"] or contender is not None:
                        logger.warning(
                            "Group '%s': %s and %s share one sidecar destination (%s); "
                            "writing %s's and skipping %s's.",
                            stem,
                            os.path.basename(contender or owner),
                            os.path.basename(it["path"]),
                            os.path.basename(destination),
                            os.path.basename(contender or owner),
                            os.path.basename(it["path"]),
                        )
                    else:
                        sidecar_written[destination] = it["path"]
                        sidecar_context = doc_sidecar.SidecarContext(
                            group_id=stem,
                            part_label=resolve_part_label(
                                it,
                                multipage_present=multipage_present,
                                relabelled_versions=relabelled_versions_frozen,
                            ),
                            group_files=sidecar_group_files,
                            page_count=sidecar_page_count,
                            page_number=it["page_num"] if it["part_kind"] == "page" else None,
                        )
                        sidecar_path = doc_sidecar.write_markdown_sidecar(
                            merged, it, sidecar_context, cfg
                        )
                        if sidecar_path:
                            logger.info(
                                "Markdown sidecar written for %s.", os.path.basename(it["path"])
                            )

                merged["_merge"] = report
                patch, patch_meta = build_canonical_patch(merged, cfg)

                if changeset_writer and run_id:
                    # ``own_meta`` and not ``per_meta``: the changeset is
                    # per-file write instructions, so each file diffs against
                    # what it itself held. Diffing against the group-combined
                    # metadata treated a value found only on a sibling as
                    # already present on this file, and the write that should
                    # have brought this file up to the group's shared answer
                    # was silently dropped.
                    before_snapshot = canonical_values_from_metadata(own_meta, cfg)
                    after_snapshot = canonical_values_from_patch(patch)
                    if it.get(utils.HYDRATION_FAILED_KEY):
                        # -r asked for this file's metadata and could not read
                        # it, so the diff below would compare against emptiness
                        # and propose overwriting values the file may really
                        # hold. Unread is not empty: propose nothing.
                        logger.warning(
                            "%s: -r could not read this file, so no writes are proposed for it.",
                            os.path.basename(it["path"]),
                        )
                        proposed_changes = {"set": {}, "keywords_add": [], "keywords_remove": []}
                    else:
                        proposed_changes = diff_canonical_metadata(before_snapshot, after_snapshot)
                    emit_changeset_record(
                        changeset_writer,
                        run_id=run_id,
                        group_id=stem,
                        group_key=stem,
                        path=it["path"],
                        sent_to_model=sent_to_model_snapshot,
                        file_metadata=before_snapshot,
                        proposed_changes=proposed_changes,
                    )

                results[it["path"]] = merged
                _emit(it["path"], "ok", {"result": merged, "patch": patch, "patch_meta": patch_meta, "usage": {"prompt_tokens": (merged.get("_usage") or {}).get("prompt_tokens"), "completion_tokens": (merged.get("_usage") or {}).get("completion_tokens"), "total_tokens": (merged.get("_usage") or {}).get("total_tokens"), "model": (merged.get("_usage") or {}).get("model")}})
                emitted_ok.add(it["path"])

        # Exception (not BaseException) so KeyboardInterrupt/SystemExit still abort.
        except Exception as e:
            if (
                strict_run_failures
                and isinstance(e, ProviderApiError)
                and _is_run_fatal(e)
            ):
                # A missing key or SDK is a property of the run, not of one
                # photo: isolating it would repeat the same error per group.
                # Raised before any record is emitted, so a run that cannot work
                # at all reports one failure rather than a full set of them.
                raise
            error_payload = _normalized_error_payload(e)
            if error_payload.get("type") not in SELF_EXPLANATORY_ERROR_TYPES:
                error_payload["traceback"] = traceback.format_exception(e.__class__, e, e.__traceback__)
            failed_groups += 1
            if first_error is None:
                first_error = e
            logger.error(
                "Group '%s' failed on %s: %s: %s",
                stem,
                os.path.basename(subject),
                error_payload["type"],
                error_payload["message"],
                exc_info=error_payload["type"] not in SELF_EXPLANATORY_ERROR_TYPES,
            )
            err_payload = {"error": error_payload}
            for it in group:
                if it["path"] not in emitted_ok:
                    _emit(it["path"], "error", err_payload)

    if strict_run_failures and first_error is not None and not results:
        raise first_error
    # A run that lost something reports its total at WARNING: the per-group
    # messages it summarizes are already at that level, so an INFO-only summary
    # would vanish at exactly the threshold where the count matters most.
    #
    # "recorded without being sent" rather than "displaced or dropped": every
    # group now travels whole, so the only files this can count are ones that
    # yielded a slot to a sibling -- a crop to its parent, a TIFF master to its
    # JPEG derivative, an untagged file to an explicit front. Each keeps its
    # record, taken from the analysis of the file that won the slot, so "dropped"
    # named a loss that does not occur while the number itself has to stay
    # visible. Saying what happened settles both.
    summarize_at_warning = bool(failed_groups or unsent_paths)
    logger.log(
        logging.WARNING if summarize_at_warning else logging.INFO,
        "Batch completed for %d group(s); %d file(s) recorded, %d group(s) failed, "
        "%d file(s) recorded without being sent to the model.",
        len(group_keys),
        len(results),
        failed_groups,
        len(unsent_paths),
    )
    return {
        "results": results,
        "errors": errors,
        "groups_failed": failed_groups,
        "files_unsent": len(unsent_paths),
        "cancelled": False,
    }
