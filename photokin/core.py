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
- _build_llm_dump_writer        optional debug dumper for raw LLM requests
- inject_analysis_date          stamp a date into the '[AI Analysis]:' caption prefix
- _strip_empty_caption_sections drop empty [Front]/[Back] caption sections
- _normalize_caption_text       reduce a caption to what two copies must share
- _captions_are_near_identical  is this caption a restatement of that one?
- _split_caption_sections       read one file's caption back as labelled sections
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

from . import utils
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


#: The label the caption block puts on this run's analysis. Everything from this
#: marker to the end of a caption is a previous run's output: the block photokin
#: writes is what the next ``-r`` run reads back as the file's own caption, so
#: the analysis has to be findable in it or it would be re-read as human prose,
#: re-labelled, and kept beside the fresh one on every pass.
#:
#: Matched loosely on the way in. ``inject_analysis_date`` rewrites the marker to
#: "[AI Analysis on 1952-06-01]:" in ``ai_caption``, and ``output_format.txt``
#: tells the model to open with this exact string -- which it also does in
#: ``caption`` often enough that both spellings have to be recognized.
_CAPTION_AI_LABEL = "[AI Analysis]:"
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

    if isinstance(exc, ProviderApiError):
        error_type = exc.error_type
        status_code = exc.status_code

    payload: Dict[str, Any] = {"type": error_type, "message": str(exc)}
    if status_code is not None:
        payload["status_code"] = int(status_code)
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
    except OSError as exc:
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

    # New keywords → TOML (reuse the already-loaded vocab data)
    new_kws = [k for k in (record.get("keywords") or []) if k not in known_keywords]
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
                    logger.warning(
                        'New keyword "%s" missing from "proposed_new_keywords"; skipping.', k
                    )
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

    prompt_items = utils.build_prompt_bundle(
            model_name,
            today,
            provider_name = provider_name,
            forwarded_meta = original_meta,
            forward_fields = forward_fields,
            cfg = config,
    )

    # A group reaches this analyzer for its part labels as much as for its size:
    # a lone negative or a lone album page is one image that still has to be
    # named as the part it is. Telling the model it is seeing several would be
    # contradicted by the payload it can count for itself.
    extra_lines: list[str] = [
        "GROUP VARIANTS NOTE:",
        "You are seeing multiple scans or variants of the same physical photograph or document."
        if len(flat_paths) > 1
        else "You are seeing a single scan of one part of a physical photograph or document.",
    ]

    part_counts = [{"label": lbl, "count": len(plist)} for lbl, plist in norm_parts]
    for idx, entry in enumerate(part_counts):
        prefix = "The first" if idx == 0 else "The next"
        extra_lines.append(f"{prefix} {entry['count']} image(s) are {entry['label']} variants of the item.")

    extra_lines.extend(
        [
            "Analyze all provided images together as one unified item, preserving the part order given.",
            "When filling the caption field, transcribe all visible text from each part across all variants, merging duplicates and preserving line breaks.",
            "Do NOT describe the scene in the caption field; only transcribed text (with [ ] for guesses and semi-illegible text).",
            "Describe the visual scene and give 3–6 sentences of cautious but comprehensive historical analysis ONLY in the ai_caption field, starting with '[AI Analysis]:'.",
        ]
    )

    prompt_items = list(prompt_items) + [
        {"type": "input_text", "text": "\n".join(extra_lines)}
    ]

    dump_request_writer = _build_llm_dump_writer(config, main_key, "group")

    def _retry_once_resend_images(extra_instruction: str) -> str:
        prompts2 = list(prompt_items) + [{"type": "input_text", "text": extra_instruction}]
        r2 = call_model(client, model_name, prompts2, image_data_urls, provider=provider, dump_request=dump_request_writer)
        return extract_output_text(r2, provider=provider)

    resp = call_model(client, model_name, list(prompt_items), image_data_urls, provider=provider, dump_request=dump_request_writer)
    usage = utils.extract_usage(resp)
    resolved_model_name = get_response_model(resp, model_name)
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

    data, _raw_used = utils.parse_with_retry(
        raw, _retry_once, config=config, source_path=main_key,
    )

    result_obj = data.get("result", {}) or {}
    if main_key not in result_obj:
        if isinstance(result_obj, dict) and len(result_obj) == 1:
            only_key = next(iter(result_obj.keys()))
            data["result"] = {main_key: result_obj[only_key]}
            result_obj = data["result"]
        else:
            raise ValueError(
                f"Model output did not contain expected main key {main_key!r} "
                f"and could not be normalized."
            )

    if isinstance(result_obj, dict):
        for rec in result_obj.values():
            if isinstance(rec, dict) and "ai_caption" in rec:
                rec["ai_caption"] = inject_analysis_date(rec.get("ai_caption"), date.fromisoformat(today))

    record = result_obj.get(main_key) or {}
    if not isinstance(record, dict):
        raise ValueError("Model output for main result was not an object/dict.")

    _ensure_provenance_keyword(record, provider_name, resolved_model_name)

    sent: Dict[str, Any] = {
        "max_edge": config.max_edge,
        "jpeg_quality": config.jpeg_quality,
        "part_count": len(norm_parts),
        "parts": part_counts,
    }
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
    record["_usage"] = usage

    kws = record.get("keywords", []) or []
    warn_list = utils.warn_forbiddenish_keywords(kws)
    if warn_list:
        for w in warn_list:
            logger.warning("%s", w)
        if config.fail_on_forbidden:
            raise SystemExit(2)

    new_kws = [k for k in kws if isinstance(k, str) and k not in known_keywords]
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
                    logger.warning(
                        'New keyword "%s" missing from "proposed_new_keywords"; skipping.', k
                    )
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

    Returns:
        ``{"results": {path: record}, "errors": {path: payload}}``, one entry per
        file, and the two are disjoint. Every file of a failed group carries
        that group's error payload, bar one already banked when the group raised
        part-way through its per-file loop: that record is complete and its
        ``ok`` line is already on the stream, so it stands.
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

    group_keys = ordered_group_keys(buckets)
    for stem in group_keys:
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
            carried_as: dict[str, str] = {}
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
                    carried_as[path] = part_key
                    continue
                del variant_parts[ver][part_key]
                displaced_slots.setdefault(f"{ver or ''}:{part_key}", []).append(path)
                logger.warning(
                    "Group '%s': %s claims the %s slot as well as the %s slot; "
                    "sending it once, as %s.",
                    stem,
                    path,
                    part_key,
                    carried_as[path],
                    carried_as[path],
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

            # === Rule 2: one caption block, built once, written to every file ===
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
            caption_sections: list[list[str]] = []
            accepted_texts: list[str] = []
            for entry, entry_meta in sorted(
                zip(group, own_metadata), key=lambda pair: _slot_rank_key(pair[0])
            ):
                existing_caption = (entry_meta.get("caption") or "").strip()
                if not existing_caption:
                    continue
                label = _label_for(entry["is_back"], entry["version"], entry.get("page_num"))
                for _key, body in _split_caption_sections(existing_caption, label):
                    # Section by section, never whole-string. Filling in a
                    # missing "[Photo B]" therefore cannot disturb the
                    # "[Photo A]" already written, and a sibling holding the same
                    # caption -- or a trivially reworded copy of it -- adds
                    # nothing rather than adding a near-twin line.
                    text = _caption_section_text(body)
                    if any(
                        _captions_are_near_identical(seen, text) for seen in accepted_texts
                    ):
                        continue
                    accepted_texts.append(text)
                    caption_sections.append(body)

            # --- This run's analysis, appended last -----------------------------
            #
            # One analysis per group either way -- both payload branches append
            # exactly one entry to ``analyses`` -- so the caption block no longer
            # forks on ``group_payload``. That fork is what left the default
            # ``--group-by object`` path unlabelled while pair and none labelled,
            # and it is also what labelled a FRONT file's caption "[Back]"
            # whenever its variant happened to have a back.
            analysis_lines: list[str] = []
            if analyses:
                record0 = analyses[0][0]
                analysis_text = _strip_empty_caption_sections(
                    (record0.get("caption") or record0.get("ai_caption") or "").strip()
                )
                supplied_marker = _CAPTION_AI_MARKER_RE.match(analysis_text)
                if supplied_marker:
                    # The model is told to open with this marker and does so in
                    # the caption field too, so strip its copy rather than
                    # stacking a second one in front of it.
                    analysis_text = analysis_text[supplied_marker.end():].lstrip()
                if analysis_text:
                    body_lines = analysis_text.splitlines()
                    if len(body_lines) == 1:
                        analysis_lines = [f"{_CAPTION_AI_LABEL} {body_lines[0].strip()}"]
                    else:
                        # A group payload comes back carrying its own
                        # [Front]/[Back] headers, so the marker takes a line of
                        # its own rather than being glued onto one of them.
                        analysis_lines = [_CAPTION_AI_LABEL, *body_lines]

            # --- The block ------------------------------------------------------
            #
            # De-duplicated line by line as well as section by section. The
            # section pass settles what each label says; this one is the last
            # net, and it is what stops a model that echoed a caption it was
            # shown from landing that line twice.
            caption_block_lines: list[str] = []
            seen_lines: set[str] = set()
            for line in [ln for body in caption_sections for ln in body] + analysis_lines:
                key = " ".join(line.split()).lower()
                if not key:
                    # A blank line is the author's paragraph break, not a
                    # caption; it is kept as written and never counted as a
                    # duplicate, which is what leaves a multi-paragraph note
                    # byte-identical after a re-read.
                    caption_block_lines.append(line)
                    continue
                if key in seen_lines:
                    continue
                seen_lines.add(key)
                caption_block_lines.append(line)
            caption_block = "\n".join(caption_block_lines).strip("\n") or None

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

            # emit per-file (merged with per-file metadata)
            for it, own_meta in zip(group, own_metadata):
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

                # Rule 2: the group's one block, byte-identical on every file.
                # This file's own caption is already in it, under this file's
                # label, put there by the intake sweep above -- joining it again
                # here is what would give each file a personal preamble and make
                # the blocks diverge.
                if caption_block:
                    record_for_item["caption"] = caption_block

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
                merged["_merge"] = report
                patch, patch_meta = build_canonical_patch(merged, cfg)

                if changeset_writer and run_id:
                    before_snapshot = canonical_values_from_metadata(per_meta, cfg)
                    after_snapshot = canonical_values_from_patch(patch)
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
                and e.error_type in _RUN_FATAL_ERROR_TYPES
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
    return {"results": results, "errors": errors}
