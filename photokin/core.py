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
- _build_provider_client        construct the SDK client for the active provider
- _ensure_provenance_keyword    guarantee one provider/model provenance keyword
- _should_run_archival_upload   gate Files-API upload by provider
- _normalized_error_payload     build a provider-normalized error record
- analyze_photo                 PUBLIC: full pipeline for one front(+back) photo
- analyze_group_parts           PUBLIC: analyze ordered parts (front/back/pages)
- analyze_group_front_back      PUBLIC: convenience wrapper over analyze_group_parts
- analyze_folder                PUBLIC: batch a whole folder
- analyze_manifest              PUBLIC: aggregate wrapper over the stream
- process_manifest_stream       PUBLIC: streaming NDJSON batch (the plugin path)
"""

import json
import os
import re
import sys
import traceback
from pathlib import Path
from datetime import date
from typing import Callable, Dict, Any, List
from copy import deepcopy

from . import utils
from .api import call_model, extract_output_text, get_response_model
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

UPDATE_MASTER_EXACT = "master_exact"
UPDATE_MERGE_PER_VARIANT = "merge_per_variant"
_EMPTY_CAPTION_MARKERS = (
    "no text visible",
    "none",
    "blank",
    "empty",
    "n/a",
)


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
            print(f"Wrote LLM request dump: {dump_path}", file=sys.stderr)
        except OSError as exc:
            print(f"[WARN] Could not write LLM request dump {dump_path}: {exc}", file=sys.stderr)

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




def _build_provider_client(config: utils.Config):
    """Build provider SDK client using the selected provider and API key env var."""
    provider = utils.normalize_provider(config.provider)
    if provider == "anthropic":
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError("Anthropic provider selected but anthropic package is not installed.") from exc
        api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
        return anthropic.Anthropic(api_key=api_key or None)
    if provider == "gemini":
        try:
            from google import genai
            from google.genai import types as genai_types
        except ImportError as exc:
            raise RuntimeError("Gemini provider selected but google-genai package is not installed.") from exc
        api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
        # Unlike the Anthropic/OpenAI SDKs used here, google-genai has no
        # default request timeout -- observed in practice as a single
        # generate_content() call hanging indefinitely (over an hour, no
        # error, no response) with no way to detect or recover from it
        # short of killing the whole process. 3 minutes comfortably covers
        # every real per-photo response time seen in this pipeline (even
        # multi-image groups), while still failing well before a silent
        # hang can block an entire batch run.
        return genai.Client(api_key=api_key or None, http_options=genai_types.HttpOptions(timeout=180_000))
    if provider == "openrouter":
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("OpenRouter provider selected but openai package is not installed.") from exc
        api_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
        if not api_key:
            # Do NOT pass api_key=None here: the OpenAI SDK would fall back to
            # OPENAI_API_KEY from the environment while still targeting the
            # OpenRouter base_url, leaking the wrong provider's secret to
            # OpenRouter. Require the OpenRouter key explicitly instead.
            raise RuntimeError(
                "OpenRouter provider selected but OPENROUTER_API_KEY is not set."
            )
        base_url = (os.getenv("OPENROUTER_BASE_URL") or "").strip() or "https://openrouter.ai/api/v1"
        return OpenAI(api_key=api_key, base_url=base_url)
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("OpenAI provider selected but openai package is not installed.") from exc
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    return OpenAI(api_key=api_key or None)


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

    from .errors import ProviderApiError

    if isinstance(exc, ProviderApiError):
        error_type = exc.error_type
        status_code = exc.status_code

    payload: Dict[str, Any] = {"type": error_type, "message": str(exc)}
    if status_code is not None:
        payload["status_code"] = int(status_code)
    return payload

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
            print(f"[Upload] Uploaded {label} image (file_id={fid})")
    else:
        print(f"[Upload] Skipping archival upload for provider {provider} (Files API unsupported).")

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
        print(f"[Convert] Payload bytes for {labels[i]} image sent to model: {sz} bytes ({wh}, {dims.get('mime')})")

    # Prompts (include forwarded metadata if present and allowed by metadata_forward_path)
    forward_fields = None
    try:
        if config.metadata_forward_path and os.path.isfile(config.metadata_forward_path):
            with open(config.metadata_forward_path, "r", encoding="utf-8") as fh:
                mp = json.load(fh)
            forward_fields = mp.get("forward_fields")
    except (OSError, json.JSONDecodeError) as exc:
        if os.getenv("MEL_VERBOSE") or os.getenv("MEL_DEBUG"):
            print(f"[WARN] Failed to load forwarded metadata: {exc}", file=sys.stderr)

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
            print("[WARN]", w, file=sys.stderr)
        if config.fail_on_forbidden:
            raise SystemExit(2)

    # New keywords → TOML (reuse the already-loaded vocab data)
    new_kws = [k for k in (record.get("keywords") or []) if k not in known_keywords]
    proposed_raw = record.get("proposed_new_keywords")
    if not isinstance(proposed_raw, list):
        print(
            '[WARN] "proposed_new_keywords" missing or invalid; skipping vocab updates.',
            file=sys.stderr,
        )
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
                    print(
                        f'[WARN] Skipping keyword "{k}" because proposed_new_keywords is missing.',
                        file=sys.stderr,
                    )
                    continue
                if not isinstance(k, str):
                    print('[WARN] Skipping non-string keyword in new keyword list.', file=sys.stderr)
                    continue
                if k.upper().startswith("PC-"):
                    print(
                        f'[WARN] Skipping keyword "{k}" (PC- prefix not allowed).',
                        file=sys.stderr,
                    )
                    continue

                p = proposed_map.get(k)
                if not p:
                    print(
                        f'[WARN] New keyword "{k}" missing from "proposed_new_keywords"; skipping.',
                        file=sys.stderr,
                    )
                    continue

                section = (p.get("section") or "").strip()
                note = (p.get("note") or "").strip()
                if not section:
                    print(
                        f'[WARN] Skipping keyword "{k}" (missing section in proposed_new_keywords).',
                        file=sys.stderr,
                    )
                    continue
                if utils.note_looks_placeholder(note):
                    print(
                        f'[WARN] Skipping keyword "{k}" (note is missing or placeholder).',
                        file=sys.stderr,
                    )
                    continue

                if utils.insert_keyword_into_vocab_file(config.vocab_path, section, k, note):
                    inserted_count += 1

            if inserted_count:
                print(
                    f"[Upload] Vocabulary updated ({inserted_count} new keyword(s) inserted into {config.vocab_path})"
                )
        except Exception as e:
            msg = (
                "Vocabulary update failed TOML validation. "
                f"Review {config.vocab_path} and restore {config.vocab_path}.bak if needed."
            )
            print(f"[ERROR] {msg} ({e})", file=sys.stderr)
            raise RuntimeError(msg) from e

    # Optional per-photo sidecar
    img_dir = os.path.dirname(os.path.abspath(front))
    img_base = os.path.splitext(os.path.basename(front))[0]
    if write_sidecar:
        json_path = os.path.join(img_dir, f"{img_base}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2 if config.pretty_json else None, ensure_ascii=False)
        print(f"[Done] Analysis completed. JSON saved as {json_path}")
    else:
        print(f"[Done] Analysis completed for {os.path.basename(front)} (sidecar disabled)")

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
            print(f"[Upload] Uploaded {role} variant image (file_id={fid})")
    else:
        print(f"[Upload] Skipping archival upload for provider {provider} (Files API unsupported).")

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
        print(
            f"[Convert] Payload bytes for {role} variant {idx + 1} "
            f"sent to model: {sz} bytes ({wh}, {dims.get('mime')})"
        )

    forward_fields = None
    try:
        if config.metadata_forward_path and os.path.isfile(config.metadata_forward_path):
            with open(config.metadata_forward_path, "r", encoding="utf-8") as fh:
                mp = json.load(fh)
            forward_fields = mp.get("forward_fields")
    except (OSError, json.JSONDecodeError) as exc:
        if os.getenv("MEL_VERBOSE") or os.getenv("MEL_DEBUG"):
            print(f"[WARN] Failed to load forwarded metadata: {exc}", file=sys.stderr)

    prompt_items = utils.build_prompt_bundle(
            model_name,
            today,
            provider_name = provider_name,
            forwarded_meta = original_meta,
            forward_fields = forward_fields,
            cfg = config,
    )

    extra_lines: list[str] = [
        "GROUP VARIANTS NOTE:",
        "You are seeing multiple scans or variants of the same physical photograph or document.",
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
            print("[WARN]", w, file=sys.stderr)
        if config.fail_on_forbidden:
            raise SystemExit(2)

    new_kws = [k for k in kws if isinstance(k, str) and k not in known_keywords]
    proposed_raw = record.get("proposed_new_keywords")
    if not isinstance(proposed_raw, list):
        print(
            '[WARN] "proposed_new_keywords" missing or invalid; skipping vocab updates.',
            file=sys.stderr,
        )
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
                    print(
                        f'[WARN] Skipping keyword "{k}" because proposed_new_keywords is missing.',
                        file=sys.stderr,
                    )
                    continue
                if not isinstance(k, str):
                    print('[WARN] Skipping non-string keyword in new keyword list.', file=sys.stderr)
                    continue
                if k.upper().startswith("PC-"):
                    print(
                        f'[WARN] Skipping keyword "{k}" (PC- prefix not allowed).',
                        file=sys.stderr,
                    )
                    continue

                p = proposed_map.get(k)
                if not p:
                    print(
                        f'[WARN] New keyword "{k}" missing from "proposed_new_keywords"; skipping.',
                        file=sys.stderr,
                    )
                    continue

                section = (p.get("section") or "").strip()
                note = (p.get("note") or "").strip()
                if not section:
                    print(
                        f'[WARN] Skipping keyword "{k}" (missing section in proposed_new_keywords).',
                        file=sys.stderr,
                    )
                    continue
                if utils.note_looks_placeholder(note):
                    print(
                        f'[WARN] Skipping keyword "{k}" (note is missing or placeholder).',
                        file=sys.stderr,
                    )
                    continue

                if utils.insert_keyword_into_vocab_file(config.vocab_path, section, k, note):
                    inserted_count += 1

            if inserted_count:
                print(
                    f"[Upload] Vocabulary updated ({inserted_count} new keyword(s) inserted into {config.vocab_path})"
                )
        except Exception as e:
            print(f"[ERROR] Failed to insert new keywords: {e}", file=sys.stderr)

    img_dir = os.path.dirname(os.path.abspath(main_key))
    img_base = os.path.splitext(os.path.basename(main_key))[0]
    if write_sidecar:
        json_path = os.path.join(img_dir, f"{img_base}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                indent=2 if config.pretty_json else None,
                ensure_ascii=False,
            )
        print(f"[Done] Group analysis completed. JSON saved as {json_path}")
    else:
        print(
            f"[Done] Group analysis completed for {os.path.basename(main_key)} "
            "(sidecar disabled)"
        )

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


def analyze_folder(
    folder_path: str,
    config: utils.Config = utils.Config(),
    *,
    write_sidecars: bool = False
) -> Dict[str, Any]:
    """
    Batch mode for entire folders.

    The helper purposely only analyzes the primary front/back images because the
    OpenAI calls are the expensive part; instead of re-processing every variant,
    we capture the file lists under ``all_variant_files`` so Lightroom can fan
    out metadata locally.  That "why" often gets lost, so it lives here now.
    """
    folder = utils.normalize_path(folder_path) or ""
    if not os.path.isdir(folder):
        raise NotADirectoryError(folder)

    grouped = utils.group_folder_images(folder)
    if not grouped:
        print("[WARN] No image files found in folder.", file=sys.stderr)
        return {"results": {}}

    aggregated: Dict[str, Any] = {"results": {}}
    for stem, entry in grouped.items():
        primary_front = entry["primary"]["front"]
        primary_back = entry["primary"]["back"]
        if not primary_front:
            continue
        data = analyze_photo(primary_front, primary_back, config, original_meta=None, write_sidecar=write_sidecars)
        rec = data["result"][primary_front]
        # augment with variants
        all_fronts = [entry["primary"]["front"]] + [v["front"] for v in entry["variants"]]
        all_backs = [entry["primary"]["back"]] + [v["back"] for v in entry["variants"]]
        rec["all_variant_files"] = {"front": [p for p in all_fronts if p], "back": [p for p in all_backs if p]}

        img_dir = os.path.dirname(os.path.abspath(primary_front))
        img_base = os.path.splitext(os.path.basename(primary_front))[0]
        if write_sidecars:
            json_path = os.path.join(img_dir, f"{img_base}.json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2 if config.pretty_json else None, ensure_ascii=False)
        aggregated["results"][primary_front] = rec
    print(f"[Done] Batch completed for {len(aggregated['results'])} primary set(s).")
    return aggregated


def analyze_manifest(
    manifest: dict | str,
    config: utils.Config = utils.Config(),
    *,
    update_policy: str = UPDATE_MERGE_PER_VARIANT,
    write_sidecars: bool = False,
    ndjson_writer=None,
    changeset_writer=None,
    changeset_run_id: str | None = None,
    metadata_hydrator: Callable[[List[dict]], None] | None = None,
) -> dict:
    """
    Convenience wrapper around :func:`process_manifest_stream` that preserves the
    historically non-streaming signature.

    The CLI and Lightroom plug-in both rely on this to share the manifest logic
    without duplicating batching behavior, hence the one-stop wrapper.
    """
    return process_manifest_stream(
        manifest=manifest,
        cfg=config,
        update_policy=update_policy,
        write_sidecars=write_sidecars,
        ndjson_writer=ndjson_writer,
        changeset_writer=changeset_writer,
        changeset_run_id=changeset_run_id,
        metadata_hydrator=metadata_hydrator,
    )


def process_manifest_stream(
    manifest: dict | str,
    cfg: utils.Config,
    *,
    update_policy: str = UPDATE_MERGE_PER_VARIANT,
    write_sidecars: bool = False,
    ndjson_writer=None,
    changeset_writer=None,
    changeset_run_id: str | None = None,
    metadata_hydrator: Callable[[List[dict]], None] | None = None,
) -> dict:
    """Stream manifest processing results while still returning a full snapshot.

    Lightroom drives large batches and needs partial feedback to stay responsive,
    so we stream NDJSON records as soon as each group finishes *and* build the
    aggregate result that older callers expect.  This dual behavior is the core
    design constraint worth documenting.
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
            print(f"[WARN] Failed to load forwarded metadata: {exc}", file=sys.stderr)
        forward_fields = None

    items = man.get("items", [])
    if metadata_hydrator is not None:
        metadata_hydrator(items)
    buckets: dict[str, list[dict]] = {}
    for raw in items:
        p = utils.normalize_path(raw.get("path") or "")
        if not p:
            continue
        parsed = utils.parse_media_filename(p)
        stem, is_back, version = parsed.base_id, parsed.part_kind == "back", parsed.variant_id
        entry = {
            "path": p,
            "is_back": is_back,
            "version": version,
            "part_kind": parsed.part_kind,
            "page_num": parsed.page_num,
            "preferred": bool(raw.get("preferred")),
            "metadata": raw.get("metadata"),
            "metadata_path": raw.get("metadata_path"),
        }
        buckets.setdefault(stem, []).append(entry)

    results: dict[str, dict] = {}
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

    group_keys = ordered_group_keys(buckets)
    for stem in group_keys:
        group = buckets[stem]
        try:
            # choose primary (fallback-safe even if only backs are present)
            primary_idx = utils.pick_master_index(group, update_policy=update_policy)
            primary_item = group[primary_idx]
            primary_front = primary_item["path"] if not primary_item["is_back"] else None
            primary_version = primary_item.get("version")
            primary_back = None

            # Prefer a back with the same version as the primary, then any unversioned back
            primary_back = next(
                (it["path"] for it in group if it["is_back"] and it.get("version") == primary_version),
                None,
            )
            if primary_back is None:
                primary_back = next((it["path"] for it in group if it["is_back"] and it.get("version") is None), None)
            if primary_back is None:
                primary_back = next((it["path"] for it in group if it["is_back"]), None)

            # Last resort: if no front exists, use any item so we still return a result instead of KeyError
            if primary_front is None:
                primary_front = next((it["path"] for it in group if not it["is_back"]), None)
                primary_version = primary_version or next((it.get("version") for it in group if not it["is_back"]), None)
            if primary_front is None:
                primary_front = primary_item["path"]
                primary_version = primary_version or primary_item.get("version")

            combined_meta = utils.combine_group_metadata(group)
            sent_to_model_snapshot = select_forwarded_metadata(combined_meta, forward_fields)

            variant_parts: dict[str | None, dict[str, str]] = {}
            variant_order: list[str | None] = []
            multipage_present = any((it.get("part_kind") == "page") for it in group)

            for it in group:
                ver = it.get("version")
                if ver not in variant_order:
                    variant_order.append(ver)
                part_kind = it.get("part_kind")
                part_key = "none"
                if part_kind == "front":
                    part_key = "front"
                elif part_kind == "back":
                    part_key = "back"
                elif part_kind == "page":
                    part_key = f"page:{it.get('page_num')}"
                variant_parts.setdefault(ver, {})
                variant_parts[ver].setdefault(part_key, it["path"])

            if multipage_present:
                # Guardrail: only treat an untagged file as Page 1 when the overall
                # base_id has explicit -pageN entries, so single unrelated photos
                # don't accidentally become page docs.
                for ver, parts in variant_parts.items():
                    if "page:1" not in parts and "none" in parts:
                        parts["page:1"] = parts.pop("none")

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

            # At this point we have:
            #   - ``group``: all manifest entries for this logical photo (same stem)
            #   - ``primary_front`` / ``primary_back``: the chosen canonical pair
            #   - ``variant_pairs``: {version -> {"front": ..., "back": ...}}
            #
            # We now call into the model. When ``cfg.process_all_variants`` is True we
            # send *all* front/back variants together so the model can write a single
            # natural caption that applies across the set. When it is False we fall
            # back to the older behavior of analyzing only the primary pair.

            analyses: list[tuple[dict, str, str | None]] = []

            if cfg.process_all_variants:
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

                    if not parts_for_analysis:
                        raise ValueError(f"No parts collected for multipage group {stem}")

                    data_group = analyze_group_parts(
                        parts=parts_for_analysis,
                        config=cfg,
                        original_meta=combined_meta,
                        write_sidecar=write_sidecars,
                    )
                else:
                    all_fronts: list[str] = []
                    all_backs: list[str] = []
                    for ver in variant_list_sorted:
                        pair = variant_pairs.get(ver, {})
                        f = pair.get("front")
                        if f and f not in all_fronts:
                            all_fronts.append(f)
                        b = pair.get("back")
                        if b and b not in all_backs:
                            all_backs.append(b)

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
                # --- Legacy path: analyze a single front/back pair -------------------
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
            # - When analyzing all variants, combine keywords from every photo but keep "back"
            #   on backs only and keep PC* codes tied to their variant.
            # - Preserve existing captions, then append generated captions labeled by
            #   front/back and variant letter when multiples exist.
            # - Share AI analysis notes across the set.
            # - Pick the highest-confidence location/date guess across analyses.
            # - When only the master is analyzed, apply its merged metadata to every file,
            #   still keeping PC* and "back" scoped per variant/back.

            def _split_keywords_for_merge(keywords: list[str] | None) -> tuple[list[str], list[str]]:
                base: list[str] = []
                pc_only: list[str] = []
                for raw in keywords or []:
                    if not isinstance(raw, str):
                        continue
                    kw = raw.strip()
                    if not kw:
                        continue
                    upper_kw = kw.upper()
                    if upper_kw == "BACK":
                        continue
                    if upper_kw.startswith("PC"):
                        pc_only.append(kw)
                        continue
                    base.append(kw)
                return base, pc_only

            keyword_bases: list[list[str]] = []
            pc_by_version: dict[str | None, list[str]] = {}
            combined_base, _ = _split_keywords_for_merge(combined_meta.get("keywords"))
            if combined_base:
                keyword_bases.append(combined_base)
            for rec, _, ver in analyses:
                base_kw, pc_kw = _split_keywords_for_merge(rec.get("keywords"))
                if base_kw:
                    keyword_bases.append(base_kw)
                if pc_kw:
                    pc_by_version.setdefault(ver, []).extend(pc_kw)
            # Rule 1: union all shared keywords, but PC* stays per-variant and "back"
            # only applies when we later emit a back record.
            shared_keywords = utils.union_keywords(*keyword_bases)
            pc_by_version = {ver: utils.union_keywords(kws) for ver, kws in pc_by_version.items()}

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

            def _label(kind: str, ver: str | None, include_ver: bool) -> str:
                base = "Back" if kind == "back" else "Front"
                if include_ver and ver:
                    return f"[{base} {ver}]"
                return f"[{base}]"

            def _strip_existing_label(cap: str, kind: str) -> str:
                """Remove an existing front/back prefix from ``cap`` for ``kind``.

                The model occasionally returns captions prefixed with "Front:",
                "[Back]", etc. When we prepend our own label (which encodes
                variant information), strip any matching prefix first so we
                don't produce duplicated labels like "[Back] Back:".
                """

                def _remove(prefix_pattern: str, text: str) -> str | None:
                    m = re.match(prefix_pattern, text, re.IGNORECASE)
                    if m and m.group(1).lower().startswith(kind):
                        trimmed = text[m.end():].lstrip()
                        return trimmed if trimmed else None
                    return None

                start = cap.lstrip()
                # Bracketed form, optionally with variant letter: [Back b]:
                stripped = _remove(r"^\[(front|back)(\s+[a-z])?\]\s*:?", start)
                if stripped is not None:
                    return stripped
                # Simple form: Back:
                stripped = _remove(r"^(front|back)\s*:?", start)
                return stripped if stripped is not None else cap.strip()

                # Rule 2/2a/2b: keep per-photo captions, then append AI captions.
                #
                # When ``cfg.process_all_variants`` is True, we treat the analysis as
                # group-level: the single caption from ``analyses[0]`` is assumed to be
                # a natural description of the logical photo as a whole, so we build at
                # most two entries:
                #
                #   [Front] <group caption>   (if any fronts exist)
                #   [Back]  <group caption>   (if any backs exist)
                #
                # When ``cfg.process_all_variants`` is False, we fall back to the
                # original behavior of building per-variant entries like:
                #
                #   [Front a] ...
                #   [Back b]  ...
                #

            # Build the block we’re going to append to any existing Lightroom caption.
            caption_entries: list[str] = []

            if cfg.process_all_variants:
                # Group-aware mode:
                # The model has already produced a complete transcription in the
                # caption field (with any [Front]/[Back] headers it decided to add).
                # We reuse that block as-is for every item in the group.
                if analyses:
                    rec0, _path0, _ver0 = analyses[0]
                    cap0 = _strip_empty_caption_sections((rec0.get("caption") or "").strip())
                    if cap0:
                        caption_entries.append(cap0)
            else:
                # Legacy per-variant behavior: preserve separate entries with variant
                # letters when needed so you can see each version's caption.
                seen_caps: set[str] = set()
                for rec, _, ver in analyses:
                    cap = _strip_empty_caption_sections((rec.get("caption") or rec.get("ai_caption") or "").strip())
                    if not cap:
                        continue
                    pair = variant_pairs.get(ver) or {}
                    has_back = bool(pair.get("back"))

                    def _add_caption(kind: str, include_ver: bool):
                        label = _label(kind, ver, include_ver)
                        body = _strip_existing_label(cap, kind)
                        line = f"{label} {body}" if label else body
                        key = " ".join(line.split()).lower()
                        if line and key not in seen_caps:
                            seen_caps.add(key)
                            caption_entries.append(line)

                    # If a back exists for this variant, prefer labeling that caption
                    # with the back role only to avoid duplicating front/back copies
                    # of the same text. When there's no back, keep the front entry
                    # (with variant label when needed).
                    if has_back:
                        _add_caption("back", multiple_backs)
                    else:
                        _add_caption("front", multiple_fronts)

            def _join_captions(existing: str | None, generated: list[str]) -> str | None:
                lines: list[str] = []
                seen_local: set[str] = set()
                for part in [existing] + generated:
                    if not part:
                        continue
                    key = " ".join(part.split()).lower()
                    if key in seen_local:
                        continue
                    seen_local.add(key)
                    lines.append(part)
                return "\n".join(lines) if lines else None

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

            canonical_analysis_notes = next(
                (rec.get("analysis_notes") for rec, _, _ in analyses if rec.get("analysis_notes")),
                canonical.get("analysis_notes"),
            )
            if canonical_analysis_notes:
                # Rule 3: keep analysis notes in sync across all variants.
                canonical["analysis_notes"] = canonical_analysis_notes

            # emit per-file (merged with per-file metadata)
            for it in group:
                per_meta = utils.load_item_metadata(it) or {}
                if update_policy == UPDATE_MERGE_PER_VARIANT:
                    per_meta = utils.merge_original_sources(per_meta, combined_meta)

                record_for_item = deepcopy(canonical)
                per_version_pc = pc_by_version.get(it.get("version")) or []
                keywords_for_item = utils.union_keywords(shared_keywords, per_version_pc)
                if it["is_back"]:
                    # Rule 1 (continued): append "back" only on back items.
                    keywords_for_item = utils.union_keywords(keywords_for_item, ["back"])
                record_for_item["keywords"] = keywords_for_item

                # Rule 2: preserve each photo's caption, then append labeled AI captions.
                combined_caption = _join_captions((per_meta.get("caption") or "").strip() or None, caption_entries)
                if combined_caption:
                    record_for_item["caption"] = combined_caption

                merged, report = merge_metadata(record_for_item, per_meta, cfg)
                if it["is_back"]:
                    utils.ensure_keyword_back(merged)
                else:
                    merged["keywords"] = [
                        kw for kw in merged.get("keywords") or [] if not (isinstance(kw, str) and kw.strip().lower() == "back")
                    ]
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

        except Exception as e:
            error_payload = _normalized_error_payload(e)
            if error_payload.get("type") not in {"rate_limit", "overloaded", "invalid_input", "invalid_request", "api_status", "length"}:
                error_payload["traceback"] = traceback.format_exception(e.__class__, e, e.__traceback__)
            err_payload = {"error": error_payload}
            for it in group:
                _emit(it["path"], "error", err_payload)

    return {"results": results}
