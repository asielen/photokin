"""
photokin.utils
====================

Shared utilities and data structures.

Includes:
- Config dataclass and environment defaults.
- Path normalization and existence checks.
- TOML (vocab) helpers and guardrails.
- Image I/O: TIFF→JPEG conversion, data URL creation, archival upload.
- Prompt assembly for the model.
- JSON cleanup + retry parser.

This is the shared foundation imported across the package. It is large, so it is
organized into the sections below (matching the ``# === ... ===`` dividers); jump
to a section rather than reading top-to-bottom.

Code map (by section):
- Config              the Config dataclass + environment-variable defaults
- Path helpers        normalize_path (the canonical path key) + existence checks
- Vocabulary helpers  load/flatten the keyword TOML, guardrails, vocab inserts
- Prompt helpers      load static prompt fragment files
- JSON helpers        thin JSON load wrappers
- Image helpers       TIFF→JPEG conversion, data-URL encoding, archival upload
- Prompt assembly     build_prompt_bundle + photo-context resolution
- JSON parsing        model-output cleanup and the retry-parser (_ParseLogger)
- Filename parsing    parse_media_filename: base id / part kind / variant / page
- Folder grouping     group files into front/back/variant/page sets
- Manifest helpers    load_manifest (shape validation), master selection
- Metadata merge      small helpers shared by the merge step
- Response helpers    normalize/inspect provider responses
"""

from __future__ import annotations
# Public API used by other modules:
# Config, normalize_path, ensure_paths_exist, load_vocab_sections,
# flatten_known_keywords, warn_forbiddenish_keywords, note_looks_placeholder,
# insert_keyword_into_vocab_file, safe_backup, resolve_default_paths,
# build_data_url_and_size, archival_upload, build_prompt_bundle, parse_with_retry,
# parse_media_filename, group_folder_images, pick_master_index, load_manifest,
# load_item_metadata, combine_group_metadata, ensure_keyword_back, union_keywords,
# merge_original_sources, dedupe_captions_with_source, extract_usage.
import base64
import io
import json
import logging
import mimetypes
import os
import re
import textwrap
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, timezone
from json import JSONDecodeError
from typing import Any, Dict, List, Set, Tuple, Optional

from photokin.face_utils import face_tags_to_llm_block

logger = logging.getLogger(__name__)

# === Config ===

# Resolve defaults relative to this file (NOT the current working directory).
_THIS_DIR = Path(__file__).resolve().parent
_DEFAULT_PROMPTS_DIR = _THIS_DIR / "prompts_photo_ai"

@dataclass
class Config:
    """Runtime configuration for an analysis run, with environment-variable defaults.

    One mutable settings object threaded through the whole pipeline: provider/model
    selection, prompt/vocab paths, image-sizing limits, the confidence thresholds
    that gate date/location writes, and run flags (dry-run, sidecars, etc.).
    Defaults read from environment variables so the Lightroom plugin can configure
    a run purely via the env it sets on the subprocess.
    """

    model: str = os.getenv("OPENAI_MODEL", "gpt-4o")
    provider_name: str = os.getenv("LLM_PROVIDER_NAME", "ChatGPT")
    provider: str = os.getenv("LLM_PROVIDER", "openai")
    claude_model_name: str = os.getenv("CLAUDE_MODEL", "sonnet")
    gemini_model_name: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    openrouter_model_name: str = os.getenv("OPENROUTER_MODEL", "moonshotai/kimi-k3")
    prompts_dir: str = str(_DEFAULT_PROMPTS_DIR)
    vocab_path: str = str(_DEFAULT_PROMPTS_DIR / "vocab_keywords_examples.toml")
    forbidden_path: str = str(_DEFAULT_PROMPTS_DIR / "forbidden_inferences.txt")
    metadata_forward_path: Optional[str] = None # path to a JSON with {"forward_fields": [...]}
    jpeg_quality: int = 80
    pretty_json: bool = False
    no_update_vocab: bool = False
    fail_on_forbidden: bool = False
    max_edge: int | None = 1024
    process_all_variants: bool = False
    date_confidence_threshold: float = 0.7
    location_confidence_threshold: float = 0.7
    # date_guess override policy (merge.py)
    date_override_confidence_threshold: float = 0.6
    date_override_precise_confidence_threshold: float = 0.8
    date_override_year_gap: int = 20
    date_override_precise_year_gap: int = 5
    photo_context_text: str | None = None
    photo_context_file: str | None = None
    debug_dump_llm_request: bool = False
    debug_dump_dir: str | None = None
    run_batch_id: str | None = None
    dry_run: bool = False


CLAUDE_MODELS: Dict[str, str] = {
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

DEFAULT_CLAUDE_MODEL = "sonnet"


def normalize_provider(provider: str | None) -> str:
    """Normalize provider values to supported runtime identifiers."""
    raw = (provider or "").strip().lower()
    if raw in {"claude", "anthropic"}:
        return "anthropic"
    if raw in {"gemini", "google"}:
        return "gemini"
    if raw == "openrouter":
        return "openrouter"
    return "openai"


def provider_display_name(provider: str | None) -> str:
    """Return the user-facing provider name used in prompts/keywords."""
    normalized = normalize_provider(provider)
    if normalized == "anthropic":
        return "Claude"
    if normalized == "gemini":
        return "Gemini"
    if normalized == "openrouter":
        return "OpenRouter"
    return "ChatGPT"


def resolve_claude_model(model_or_alias: str | None, default_alias: str | None = None) -> str:
    """Resolve a Claude alias (e.g. sonnet) to a concrete Anthropic model string."""
    model_value = (model_or_alias or "").strip()
    if model_value in CLAUDE_MODELS:
        return CLAUDE_MODELS[model_value]
    if model_value.startswith("claude-"):
        return model_value
    chosen_default = (default_alias or DEFAULT_CLAUDE_MODEL).strip() or DEFAULT_CLAUDE_MODEL
    return CLAUDE_MODELS.get(chosen_default, CLAUDE_MODELS[DEFAULT_CLAUDE_MODEL])


def resolve_model_for_provider(config: Config) -> str:
    """Resolve the effective model name sent to the selected provider API."""
    provider = normalize_provider(config.provider)
    if provider == "anthropic":
        preferred = config.model if config.model.startswith("claude-") else config.claude_model_name
        return resolve_claude_model(preferred)
    if provider == "gemini":
        return config.gemini_model_name or "gemini-2.5-flash"
    if provider == "openrouter":
        return config.openrouter_model_name or "moonshotai/kimi-k3"
    return config.model.strip()


MAX_PHOTO_CONTEXT_BYTES = 200 * 1024

# Manifest fields forwarded to the model by default.
DEFAULT_METADATA_FORWARD_FIELDS: tuple[str, ...] = (
    "keywords",
    "title",
    "caption",
    "userComment",
    "dateTimeOriginal",
    "location",
    "city",
    "state",
    "stateProvince",
    "country",
    "locationShown",
    "gps",
    "faceTags",
)


# === Path helpers ===

def normalize_path(p: str | None) -> str | None:
    """Normalize a path: strip surrounding quotes/space, expand ``~``, normpath.

    This is the canonical path key used throughout the pipeline (manifest items,
    result keys, exiftool inputs). Note that ``os.path.normpath`` is
    platform-dependent — it yields backslashes on Windows — so anything compared
    against these keys must be normalized the *same* way (see the ExifTool
    ``SourceFile`` handling in ``photokin.exiftool.hydrate``). Returns ``None``
    only for ``None`` input.
    """
    if p is None:
        return None
    p = p.strip()
    if len(p) >= 2 and p[0] == p[-1] and p[0] in ("'", '"'):
        p = p[1:-1]
    return os.path.normpath(os.path.expanduser(p))


def ensure_paths_exist(paths: list[str]) -> None:
    """Raise ``FileNotFoundError`` for the first path that isn't an existing file.

    The common cause is a user pasting a quoted path, so the error hint calls
    that out explicitly. Empty entries are skipped.
    """
    for p in paths:
        if p and not os.path.isfile(p):
            logger.error(
                "File not found after normalization: %s\n"
                "        Tip: Don’t include surrounding quotes in the path.",
                p,
            )
            raise FileNotFoundError(p)


# === Vocabulary helpers ===

try:
    import tomllib  # Python 3.11+
    _HAS_TOMLLIB = True
except Exception:
    _HAS_TOMLLIB = False

try:
    import toml  # pip install toml
    _HAS_TOML = True
except Exception:
    _HAS_TOML = False

SECTION_IDS = [
    "people_subjects","clothing_fashion","objects_artifacts","animals_pets",
    "setting_environment","architecture_built","events_occasions","photo_format",
    "written_elements_identifiers","activities_actions","emblems_symbols_context","landscape_nature",
    "documents_records","date_reference",
]

# Old canonical spellings still found in user-customized vocab files.
_LEGACY_SECTION_HEADERS = {
    "date_reference": "date_refernce",
}

FORBIDDEN_KEYWORD_SUBSTRINGS = {
    "family","parents","siblings","mother","father","son","daughter",
    "friends","boyfriend","girlfriend","husband","wife",
    "happy","sad","angry","excited","smiling","serious"
}

# The model frequently proposes shorthand section names (e.g. "people",
# "animals") instead of the canonical compound SECTION_IDS used as TOML
# headers (e.g. "people_subjects", "animals_pets"). Map the common ones so
# those proposals aren't silently dropped.
SECTION_ALIASES = {
    "people": "people_subjects",
    "subjects": "people_subjects",
    "clothing": "clothing_fashion",
    "fashion": "clothing_fashion",
    "objects": "objects_artifacts",
    "artifacts": "objects_artifacts",
    "animals": "animals_pets",
    "pets": "animals_pets",
    "setting": "setting_environment",
    "environment": "setting_environment",
    "locations": "setting_environment",
    "location": "setting_environment",
    "architecture": "architecture_built",
    "events": "events_occasions",
    "occasions": "events_occasions",
    "format": "photo_format",
    "written": "written_elements_identifiers",
    "identifiers": "written_elements_identifiers",
    "activities": "activities_actions",
    "actions": "activities_actions",
    "emblems": "emblems_symbols_context",
    "symbols": "emblems_symbols_context",
    "landscape": "landscape_nature",
    "nature": "landscape_nature",
    "documents": "documents_records",
    "records": "documents_records",
    "date": "date_reference",
    "dates": "date_reference",
    "date_refernce": "date_reference",
}


def normalize_section_id(section: str) -> str:
    """Map a model-proposed section name to a canonical SECTION_IDS entry, if known."""
    key = (section or "").strip().lower()
    if key in SECTION_IDS:
        return key
    return SECTION_ALIASES.get(key, key)


_LOG_PARSE_INPUTS = (os.getenv("MEL_LOG_PARSE_WITH_RETRY") or "").strip().lower()
_LOG_PARSE_INPUTS_ENABLED = _LOG_PARSE_INPUTS not in {"", "0", "false", "no"}


def _load_toml(path: str) -> Dict[str, Any]:
    with open(path, "rb") as f:
        if _HAS_TOMLLIB:  # type: ignore
            return tomllib.load(f)  # type: ignore
        if _HAS_TOML:
            return toml.load(f)     # type: ignore
        raise RuntimeError("TOML support required. Use Python 3.11+ or `pip install toml`.")


def _validate_toml_file(path: str) -> None:
    try:
        _load_toml(path)
    except Exception as e:
        raise ValueError(
            f"TOML validation failed for {path}. The file may be malformed: {e}"
        ) from e


def load_vocab_sections(vocab_path: str) -> Tuple[Dict[str, List[Any]], List[Dict[str, Any]]]:
    """Load the keyword vocabulary TOML into ``(sections, new_keywords)``.

    ``sections`` maps each known ``SECTION_IDS`` id to its keyword list (empty if
    absent), and ``new_keywords`` is the running list of model-proposed additions.
    Missing/malformed sections degrade to empty lists rather than raising, so a
    partially hand-edited vocab file still loads.
    """
    data = _load_toml(vocab_path)
    sections: Dict[str, List[Any]] = {}
    for sid in SECTION_IDS:
        header = sid
        if header not in data and sid in _LEGACY_SECTION_HEADERS and _LEGACY_SECTION_HEADERS[sid] in data:
            header = _LEGACY_SECTION_HEADERS[sid]
        if header in data and isinstance(data[header], dict) and "keywords" in data[header]:
            sections[sid] = data[header]["keywords"] or []
        else:
            sections[sid] = []
    new_keywords_list = data.get("new_keywords", [])
    if not isinstance(new_keywords_list, list):
        new_keywords_list = []
    return sections, new_keywords_list


def flatten_known_keywords(sections: Dict[str, List[Any]], new_keywords_list: List[Dict[str, Any]]) -> Set[str]:
    """Collapse vocab sections + pending additions into one set of known keywords.

    Handles both shapes a section list may contain (bare strings and
    ``{"keyword": ...}`` dicts). Used to decide which model-suggested keywords are
    genuinely new and worth appending to the vocabulary.
    """
    known: Set[str] = set()
    for arr in sections.values():
        for item in arr:
            if isinstance(item, str):
                known.add(item)
            elif isinstance(item, dict) and "keyword" in item:
                known.add(item["keyword"])
    for entry in new_keywords_list:
        k = entry.get("keyword")
        if isinstance(k, str):
            known.add(k)
    return known


def warn_forbiddenish_keywords(keywords: List[str]) -> List[str]:
    """Return warning strings for keywords containing forbidden/subjective terms.

    The model is told to avoid subjective or relationship-implying keywords; this
    is a word-boundary regex guard that flags (does not remove) any that slip
    through, so a reviewer can decide. One warning per offending keyword.
    """
    warnings = []
    for k in keywords:
        if k.strip().endswith(" Analyzed"):
            # Provenance tag (e.g. "Claude claude-sonnet-4-6 Analyzed") auto-added by
            # this tool; not user/model content, so it's exempt from this check.
            continue
        low = k.strip().lower()
        for bad in FORBIDDEN_KEYWORD_SUBSTRINGS:
            if re.search(rf"\b{re.escape(bad)}\b", low):
                warnings.append(f'Keyword "{k}" appears to contain forbidden/subjective/relationship term: "{bad}".')
                break
    return warnings


_PLACEHOLDER_NOTE_FRAGMENTS = {
    "auto-added",
    "auto added",
    "provide a reason",
    "n/a",
    "todo",
    "tbd",
    "placeholder",
}


def note_looks_placeholder(note: str) -> bool:
    """Return True if a reason/note is empty or obvious filler (n/a, todo, etc.).

    The model is required to justify new keywords; this rejects non-answers so
    placeholder reasons don't get treated as real provenance. Empty notes count
    as placeholders.
    """
    note_clean = (note or "").strip().lower()
    if not note_clean:
        return True
    return any(fragment in note_clean for fragment in _PLACEHOLDER_NOTE_FRAGMENTS)


def insert_keyword_into_vocab_file(
    vocab_path: str,
    section: str,
    keyword: str,
    note: str,
) -> bool:
    """
    Insert a single keyword object into the target section's keywords list.

    This is intentionally minimal: we do not parse TOML fully. We find:
      1) The section header line: [section]
      2) The first "keywords = [" line after that header
      3) The closing "]" line of that keywords array
    Then we insert a new object line just before the closing bracket.

    Returns True if insertion occurred; False if we skip (unknown section, etc.).
    """
    kw_clean = (keyword or "").strip()
    note_clean = (note or "").strip()
    section_clean = normalize_section_id(section)

    if not kw_clean:
        logger.warning("Skipping empty keyword insert request.")
        return False
    if kw_clean.upper().startswith("PC-"):
        logger.warning('Skipping keyword "%s" (PC- prefix not allowed).', kw_clean)
        return False
    if not section_clean:
        logger.warning('Skipping keyword "%s" (missing section).', kw_clean)
        return False
    if not note_clean:
        logger.warning('Skipping keyword "%s" (missing note).', kw_clean)
        return False

    path = Path(vocab_path)
    original_text = path.read_text(encoding="utf-8")
    lines = original_text.splitlines()

    header = f"[{section_clean}]"
    header_idx = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if header_idx is None:
        logger.warning(
            'Skipping keyword "%s" (section "%s" not found).', kw_clean, section_clean
        )
        return False

    # Find the first "keywords = [" line after the header, but stop if a new section starts.
    keywords_idx = None
    for i in range(header_idx + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            break
        if stripped.startswith("keywords") and "[" in stripped:
            keywords_idx = i
            break
    if keywords_idx is None:
        logger.warning(
            'Skipping keyword "%s" (section "%s" missing keywords list).', kw_clean, section_clean
        )
        return False

    # Find the closing "]" for the keywords array. We keep it simple: look for a line that
    # contains only "]" after the keywords line.
    closing_idx = None
    for i in range(keywords_idx + 1, len(lines)):
        if lines[i].strip() == "]":
            closing_idx = i
            break
    if closing_idx is None:
        logger.warning(
            'Skipping keyword "%s" (section "%s" keywords list not closed).',
            kw_clean,
            section_clean,
        )
        return False

    # Find the last non-empty, non-comment line before the closing bracket.
    last_idx = None
    for i in range(closing_idx - 1, keywords_idx, -1):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        last_idx = i
        break

    # Determine indentation: prefer existing entry indentation if present.
    if last_idx is not None and last_idx >= 0:
        indent_match = re.match(r"\s*", lines[last_idx])
        indent = indent_match.group(0) if indent_match else "  "
    else:
        indent = "  "

    # Ensure the previous last entry ends with a comma, so we don't break TOML arrays.
    if last_idx is not None and last_idx != keywords_idx:
        last_line = lines[last_idx].rstrip()
        if not last_line.endswith(","):
            lines[last_idx] = last_line + ","

    # Escape keyword/note safely using JSON encoding, which handles quotes and backslashes.
    kw_json = json.dumps(kw_clean)
    note_json = json.dumps(note_clean)
    new_line = f"{indent}{{ keyword = {kw_json}, note = {note_json} }},"
    lines.insert(closing_idx, new_line)

    updated_text = "\n".join(lines)
    if original_text.endswith("\n"):
        updated_text += "\n"

    path.write_text(updated_text, encoding="utf-8")

    # Validate that TOML still parses; restore original content if it doesn't.
    try:
        _load_toml(vocab_path)
    except Exception as e:
        path.write_text(original_text, encoding="utf-8")
        raise ValueError(
            f"TOML validation failed after inserting keyword into {vocab_path}: {e}"
        ) from e

    logger.info('Added keyword "%s" to section "%s".', kw_clean, section_clean)
    return True


def safe_backup(path: str) -> None:
    """Write a one-time ``<path>.bak`` copy before a file is mutated.

    Best-effort and idempotent: it never overwrites an existing ``.bak`` (so the
    first backup wins) and only warns on failure rather than raising, so backup
    trouble never blocks the primary operation.
    """
    backup_path = path + ".bak"
    try:
        if not os.path.exists(backup_path):
            with open(path, "rb") as src, open(backup_path, "wb") as dst:
                dst.write(src.read())
    except Exception as e:
        logger.warning("Could not create backup for %s: %s", path, e)


# === Prompt helpers ===
import importlib.resources as _res

def _prompts_path(cfg: Config | None = None) -> str:
    # Photo archiver ships prompts internally; callers can override via Config.prompts_dir.
    prompts_dir = cfg.prompts_dir if cfg else Config.prompts_dir
    return str(_res.files(__package__) / prompts_dir)

def _resolve_prompt_file(name: str, cfg: Config | None = None) -> str:
    prompts_dir = cfg.prompts_dir if cfg else Config.prompts_dir
    return str(_res.files(__package__) / prompts_dir / name)

def resolve_default_paths(cfg: Config) -> None:
    """Fill in default prompt-related paths if not explicitly set in code."""
    if cfg.vocab_path is None:
        cfg.vocab_path = _resolve_prompt_file("vocab_keywords_examples.toml", cfg)
    if cfg.metadata_forward_path is None:
        mfp = Path(_resolve_prompt_file("metadata_forward.toml", cfg))
        cfg.metadata_forward_path = str(mfp) if mfp.exists() else None
    if cfg.forbidden_path is None:
        cfg.forbidden_path = _resolve_prompt_file("forbidden_inferences.txt", cfg)

# === JSON helpers ===

def _load_json(p: str) -> Any:
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

# === Image helpers ===

try:
    from PIL import Image
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False

def _is_tiff(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in (".tif", ".tiff")

def _ensure_pillow():
    if not _HAS_PIL:
        raise RuntimeError("Pillow required for resizing and TIFF conversion. Install: pip install Pillow")

def _open_image(path: str) -> Image.Image:
    _ensure_pillow()
    im = Image.open(path)
    # Auto-orient if EXIF says so
    try:
        from PIL import ImageOps
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass
    return im

def _to_rgb(im: "Image.Image") -> "Image.Image":
    if im.mode not in ("RGB", "L"):
        return im.convert("RGB")
    if im.mode == "L":
        return im.convert("RGB")
    return im

def _resize_if_needed(im: "Image.Image", max_edge: int | None) -> tuple["Image.Image", bool]:
    if not max_edge or max_edge <= 0:
        return im, False
    w, h = im.size
    longest = max(w, h)
    if longest <= max_edge:
        return im, False
    scale = max_edge / float(longest)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    im2 = im.resize((new_w, new_h), resample=Image.LANCZOS)
    return im2, True

def _encode_jpeg_bytes(im: "Image.Image", quality: int) -> bytes:
    buf = io.BytesIO()
    _to_rgb(im).save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()

def build_data_url_and_size(path: str, jpeg_quality: int, max_edge: int | None = None) -> tuple[str, int, dict]:
    """
    Return (data_url, raw_bytes_len, meta_dict) for the bytes sent to the model.
    Policy:
      - If TIFF OR max_edge specified -> open via Pillow, optional resize, encode JPEG.
      - Else (JPEG/PNG and no resize) -> pass original bytes/mime.
    meta: {"mime": "...", "width": W, "height": H, "resized": bool}
    """
    if _is_tiff(path) or (max_edge is not None and max_edge > 0):
        im = _open_image(path)
        orig_w, orig_h = im.size
        im2, resized = _resize_if_needed(im, max_edge)
        raw = _encode_jpeg_bytes(im2, jpeg_quality)
        if resized:
            logger.info(
                "Downscaled %s %dx%d -> %dx%d (max_edge=%s)",
                os.path.basename(path),
                orig_w,
                orig_h,
                im2.width,
                im2.height,
                max_edge,
            )
        b64 = base64.b64encode(raw).decode("ascii")
        meta = {"mime": "image/jpeg", "width": im2.width, "height": im2.height, "resized": resized}
        return f"data:image/jpeg;base64,{b64}", len(raw), meta

    # No resize and not TIFF: keep original format
    mime, _ = mimetypes.guess_type(path)
    if mime is None:
        mime = "image/jpeg"
    with open(path, "rb") as fh:
        raw = fh.read()
    # Dimensions (best effort)
    width = height = None
    try:
        im = _open_image(path)
        width, height = im.size
    except Exception:
        pass
    b64 = base64.b64encode(raw).decode("ascii")
    meta = {"mime": mime, "width": width, "height": height, "resized": False}
    return f"data:{mime};base64,{b64}", len(raw), meta

def archival_upload(client, path: str, jpeg_quality: int, purpose: str = "user_data") -> str:
    """Upload to Files API for archival. Returns file id. Not used in analysis request."""
    if _is_tiff(path):
        _ensure_pillow()
        im = _open_image(path)
        raw = _encode_jpeg_bytes(im, jpeg_quality)
        logger.info("TIFF -> JPEG in-memory (%s, quality=%d)", os.path.basename(path), jpeg_quality)
        bio = io.BytesIO(raw)
        bio.name = os.path.splitext(os.path.basename(path))[0] + "_upload.jpg"  # type: ignore[attr-defined]
        uploaded = client.files.create(file=bio, purpose=purpose)
        return uploaded.id
    with open(path, "rb") as fh:
        uploaded = client.files.create(file=fh, purpose=purpose)
        return uploaded.id


# === Prompt assembly ===

def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def build_prompt_bundle(
    model_name: str,
    today: str,
    *,
    provider_name: str | None = None,
    forwarded_meta: Optional[dict] = None,
    forward_fields: Optional[list] = None,
    cfg: Config | None = None,
) -> List[Dict[str, str]]:
    """Assemble the ordered system/instruction prompt pieces sent to the model.

    Stitches the static prompt fragments (system header, rules, vocab examples,
    output format) together with run-specific context — provider/model name,
    today's date, and any forwarded original metadata — into the list of
    role/content blocks the API layer expects. Centralizing assembly here keeps
    prompt ordering identical across single, group, and batch flows.
    """
    pieces: List[Dict[str, str]] = []

    provider = (provider_name or (cfg.provider_name if cfg else None) or "ChatGPT").strip() or "ChatGPT"
    vars_common = {"MODEL_NAME": model_name, "PROVIDER_NAME": provider}

    def add_text(text: str, vars: Dict[str, str] | None = None):
        text = text.strip()
        if vars:
            for k, v in vars.items():
                text = text.replace(f"{{{{{k}}}}}", v)
        pieces.append({"type": "input_text", "text": text})

    def add_txt(name: str, vars: Dict[str, str] | None = None):
        add_text(_read_text(_resolve_prompt_file(name, cfg)), vars)

    add_txt("system_header.txt", vars_common)
    add_txt("instructions_front_back.txt", {"TODAY": today})
    add_txt("image_rules.txt", vars_common)
    add_txt("categories.txt")

    # The rule files above refer to "the provided preferred vocabulary" and the
    # forbidden-inference guardrails, so both must actually reach the model.
    # Honor Config overrides for these two paths (they are also used by the
    # post-run validation/vocab-update steps).
    forbidden_p = (cfg.forbidden_path if cfg else None) or _resolve_prompt_file("forbidden_inferences.txt", cfg)
    add_text(_read_text(forbidden_p), vars_common)

    vocab_p = (cfg.vocab_path if cfg else None) or _resolve_prompt_file("vocab_keywords_examples.toml", cfg)
    add_text("PREFERRED VOCABULARY (TOML)\n\n" + _read_text(vocab_p))

    add_txt("output_format.txt", vars_common)

    # Optional forwarded metadata block (filtered)
    if forwarded_meta:
        effective_forward_fields = list(DEFAULT_METADATA_FORWARD_FIELDS)
        if isinstance(forward_fields, list):
            for field in forward_fields:
                if isinstance(field, str) and field not in effective_forward_fields:
                    effective_forward_fields.append(field)

        sel = {
            k: v for k, v in forwarded_meta.items() if k in effective_forward_fields and v is not None
        }
        if "state" not in sel and sel.get("stateProvince"):
            sel["state"] = sel["stateProvince"]
        try:
            meta_json = json.dumps(sel, ensure_ascii=False)
        except Exception:
            meta_json = str(sel)
        pieces.append({"type": "input_text", "text": "Forwarded metadata: " + meta_json})

        face_block = face_tags_to_llm_block(sel.get("faceTags") if isinstance(sel, dict) else None)
        if face_block:
            pieces.append({"type": "input_text", "text": "[FACE TAGS — AUTHORITATIVE]\n" + face_block})

    photo_context_text = (cfg.photo_context_text if cfg else None) if cfg else None
    if photo_context_text and photo_context_text.strip():
        pieces.append(
            {
                "type": "input_text",
                "text": "[PHOTO CONTEXT — AUTHORITATIVE]\n" + photo_context_text,
            }
        )

    # Final guardrail — provider-specific wording for known quirks.
    guardrail = "Return strictly valid JSON per the specified shape. No markdown, no commentary. Do not use triple-quote markers; use \\n."
    if provider.lower() == "gemini":
        guardrail += (
            "\n\nJSON SYNTAX REMINDERS:"
            "\n• Close arrays with ] not }. Every [ must be matched by ]."
            "\n• Separate sibling key-value pairs with commas, not colons."
            "\n• Do NOT place a comma after the last element in an array or object."
            "\n• Validate that every opening { has a matching } and every [ has a matching ]."
        )
    pieces.append({"type": "input_text", "text": guardrail})
    return pieces


def _truncate_utf8_bytes(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes]
    while truncated:
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    return ""


def _sanitize_photo_context_text(text: str | None, source_label: str) -> str | None:
    if not text:
        return None
    if not text.strip():
        return None
    truncated = _truncate_utf8_bytes(text, MAX_PHOTO_CONTEXT_BYTES)
    original_bytes = len(text.encode("utf-8"))
    final_bytes = len(truncated.encode("utf-8"))
    if original_bytes > MAX_PHOTO_CONTEXT_BYTES:
        logger.warning(
            "Photo context from %s exceeded %d bytes and was truncated to %d bytes.",
            source_label,
            MAX_PHOTO_CONTEXT_BYTES,
            final_bytes,
        )
    else:
        logger.info("Photo context loaded from %s (%d bytes).", source_label, final_bytes)
    return truncated


def _load_photo_context_file(path: str | None, source_label: str) -> str | None:
    normalized = normalize_path(path)
    if not normalized:
        return None
    try:
        with open(normalized, "r", encoding="utf-8") as fh:
            data = fh.read()
    except OSError as exc:
        logger.warning("Unable to read photo context file (%s): %s", normalized, exc)
        return None
    return _sanitize_photo_context_text(data, source_label)


def resolve_photo_context(
    *,
    cli_text: str | None,
    cli_file: str | None,
    manifest: dict | None,
) -> str | None:
    """Resolve the authoritative photo-context text from the highest-priority source.

    Precedence: ``--photo-context-text`` > ``--photo-context-file`` > the
    manifest's ``photo_context_text`` / context file. Returns the sanitized text,
    or ``None`` if no source supplies any. Having one resolver keeps the priority
    order consistent between CLI and manifest-driven (plugin) runs.
    """
    if cli_text and cli_text.strip():
        return _sanitize_photo_context_text(cli_text, "CLI --photo-context-text")
    if cli_file and str(cli_file).strip():
        return _load_photo_context_file(cli_file, "CLI --photo-context-file")

    if not manifest:
        return None

    manifest_text = manifest.get("photo_context_text")
    if isinstance(manifest_text, str) and manifest_text.strip():
        return _sanitize_photo_context_text(manifest_text, "manifest photo_context_text")

    manifest_path = manifest.get("photo_context_path")
    if isinstance(manifest_path, str) and manifest_path.strip():
        return _load_photo_context_file(manifest_path, "manifest photo_context_path")

    return None


# === JSON parsing ===

def _strip_diff_or_markdown_prefixes(raw: str) -> str:
    """Remove markdown list-item or unified-diff prefixes from Gemini output.

    Gemini sometimes formats JSON as markdown bullet lists (``- "key": ...``)
    or as a unified diff (lines prefixed with ``- `` or ``+ ``).  This function
    detects both patterns and strips the prefixes.

    For diff-style output (both ``-`` and ``+`` lines present), only the ``+``
    lines are kept (the "new" side of the diff) to avoid duplicated content.
    """
    if "- " not in raw and "+ " not in raw:
        return raw

    lines = raw.split("\n")
    minus_count = sum(1 for ln in lines if re.match(r"^\s*- ", ln))
    plus_count = sum(1 for ln in lines if re.match(r"^\s*\+ ", ln))
    json_start_chars = frozenset('"[{}]0123456789-tfn')

    # Diff-style: both + and - lines present — keep only + lines
    if minus_count >= 3 and plus_count >= 3:
        cleaned: list[str] = []
        for ln in lines:
            m_plus = re.match(r"^(\s*)\+ (.*)$", ln)
            if m_plus:
                rest = m_plus.group(2).lstrip()
                if rest and rest[0] in json_start_chars:
                    cleaned.append(m_plus.group(1) + m_plus.group(2))
                    continue
            m_minus = re.match(r"^(\s*)- (.*)$", ln)
            if m_minus:
                # Drop minus lines entirely (old side of diff)
                continue
            cleaned.append(ln)
        return "\n".join(cleaned)

    # Markdown list-style: only - lines
    if minus_count < 3:
        return raw

    cleaned = []
    for ln in lines:
        m = re.match(r"^(\s*)- (.*)$", ln)
        if m:
            rest = m.group(2).lstrip()
            if rest and rest[0] in json_start_chars:
                cleaned.append(m.group(1) + m.group(2))
                continue
        cleaned.append(ln)
    return "\n".join(cleaned)


def _collapse_illegible_runs(raw: str) -> str:
    """Collapse long runs of ``[?]`` or ``[ ? ]`` markers into ``[illegible]``.

    Gemini sometimes emits hundreds or thousands of ``[?]`` markers when it
    encounters bleed-through text on the reverse side of paper.  This bloats
    the JSON string (potentially causing output truncation) and is useless
    for archival purposes.  Collapse 5+ consecutive markers into a single
    ``[illegible]`` token.
    """
    # Match 5+ consecutive [?] or [ ? ] separated by optional whitespace/newlines
    raw = re.sub(
        r'(?:\[[ ]?\?[ ]?\][\s,]*){5,}',
        '[illegible] ',
        raw,
    )
    return raw


def _cleanup_model_json(raw: str) -> str:
    """Fix common model JSON generation quirks via a single-pass scan.

    Handles:
    - Markdown list-item prefixes (``- ``) injected by Gemini.
    - Literal newlines/carriage returns inside string values → \\n / \\r
    - Mismatched closing delimiter: model outputs ``}`` to close an array
      that was opened with ``[`` (a recurring Gemini output quirk).
    - ``]:`` after an array close → ``,`` (model emits `]:` instead of `],``).
    - Trailing commas before ``]`` or ``}`` (common model quirk).
    - Control characters inside strings (tab is allowed, others escaped).
    """
    if not raw:
        return raw

    raw = _strip_diff_or_markdown_prefixes(raw)
    raw = _collapse_illegible_runs(raw)

    result: list[str] = []
    in_string = False
    escape_next = False
    stack: list[str] = []  # '{' or '[' for each open container
    n = len(raw)
    i = 0

    while i < n:
        ch = raw[i]

        if escape_next:
            result.append(ch)
            escape_next = False
            i += 1
            continue

        if in_string:
            if ch == "\\":
                result.append(ch)
                escape_next = True
            elif ch == '"':
                result.append(ch)
                in_string = False
            elif ch == "\n":
                result.append("\\n")
            elif ch == "\r":
                result.append("\\r")
            elif ord(ch) < 0x20 and ch != "\t":
                # Escape other control characters that are invalid in JSON strings
                result.append(f"\\u{ord(ch):04x}")
            else:
                result.append(ch)
        else:
            if ch == '"':
                result.append(ch)
                in_string = True
            elif ch in ("{", "["):
                result.append(ch)
                stack.append(ch)
            elif ch == "}":
                # Remove trailing comma before closing delimiter
                _strip_trailing_comma(result)
                closing_an_array = stack and stack[-1] == "["
                if closing_an_array:
                    result.append("]")
                else:
                    result.append(ch)
                if stack:
                    stack.pop()
                # If we just closed an array (via mismatched }), apply the
                # same ]: -> , look-ahead as for ].
                if closing_an_array:
                    j = i + 1
                    while j < n and raw[j] in (" ", "\t"):
                        j += 1
                    if j < n and raw[j] == ":":
                        result.append(",")
                        i = j + 1
                        continue
            elif ch == "]":
                # Remove trailing comma before closing delimiter
                _strip_trailing_comma(result)
                if stack and stack[-1] == "{":
                    result.append("}")
                else:
                    result.append(ch)
                if stack:
                    stack.pop()
                # Look ahead past whitespace: if next non-space char is ':'
                # replace it with ',' (Gemini quirk: emits ]: instead of ],)
                j = i + 1
                while j < n and raw[j] in (" ", "\t"):
                    j += 1
                if j < n and raw[j] == ":":
                    result.append(",")
                    i = j + 1
                    continue
            else:
                result.append(ch)

        i += 1

    return "".join(result)


def _strip_trailing_comma(result: list[str]) -> None:
    """Remove a trailing comma (and surrounding whitespace) from the result buffer.

    Trailing commas before ``]`` or ``}`` are invalid JSON but commonly
    produced by LLMs.  Walk backwards through *result*, skipping whitespace
    characters, and pop the comma if found.
    """
    # Walk backwards skipping whitespace
    idx = len(result) - 1
    while idx >= 0 and result[idx] in (" ", "\t", "\n", "\r", "\\n", "\\r"):
        idx -= 1
    if idx >= 0 and result[idx] == ",":
        result.pop(idx)


def _extract_json_payload(raw: str) -> str:
    """
    Extract the JSON payload from model output without rewriting its contents.

    - Prefer fenced ```json blocks, otherwise a generic fenced block.
    - If unfenced and junk surrounds the object, slice from the first "{" to
      the last "}" (only when both exist and form a valid range).
    - Always return a stripped string; never alter characters inside.
    """
    s = (raw or "").strip()

    m = re.search(r"```json\s*(.*?)\s*```", s, flags=re.DOTALL | re.IGNORECASE)
    if m:
        payload = m.group(1)
    else:
        m_generic = re.search(r"```\s*(.*?)\s*```", s, flags=re.DOTALL)
        if m_generic:
            payload = m_generic.group(1)
        else:
            first = s.find("{")
            last = s.rfind("}")
            if first != -1 and last != -1 and last > first:
                payload = s[first:last + 1]
            else:
                payload = s

    return payload.strip()


class _ParseLogger:
    """Per-photo JSON parse logger that writes to the batch debug folder.

    Each photo gets its own log file in the debug directory (the same one used
    by ``_build_llm_dump_writer`` in core.py).  Entries are appended with
    timestamps so every stage of ``parse_with_retry`` is captured.
    """

    def __init__(self, config: Config | None, source_path: str | None) -> None:
        self._enabled = _LOG_PARSE_INPUTS_ENABLED
        self._path: Path | None = None
        if not self._enabled:
            return
        debug_dir = Path(
            (config.debug_dump_dir if config else None)
            or os.path.join(os.getcwd(), "debug")
        )
        batch_id = ((config.run_batch_id if config else None) or "batch").strip() or "batch"
        photo_stem = Path(source_path).stem if source_path else "unknown"
        base_name = f"{batch_id}_parse_log_{photo_stem}"
        try:
            debug_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._enabled = False
            return
        dest = debug_dir / f"{base_name}.txt"
        suffix = 1
        while dest.exists():
            dest = debug_dir / f"{base_name}_{suffix}.txt"
            suffix += 1
        self._path = dest

    def log(self, raw_text: str | None, label: str) -> None:
        if not self._enabled or self._path is None:
            return
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(f"[{stamp}] {label}\n")
                fh.write(raw_text or "")
                fh.write("\n\n---\n\n")
        except Exception:
            pass



def parse_with_retry(
    raw: str,
    caller_retry_fn,
    *,
    config: Config | None = None,
    source_path: str | None = None,
):
    """
    Parse model JSON with minimal cleanup and helpful error messages.

    Behavior:
    - If the initial raw string is empty/whitespace, call caller_retry_fn()
      once to get a replacement string (usually a re-try from the model).
    - Extract the JSON payload from fences or surrounding text.
    - First attempt: json.loads(payload, strict=True)
    - On failure: run _cleanup_model_json (fixes } vs ] mismatches, escapes
      literal newlines in strings, trailing commas), then
      json.loads(cleaned, strict=False).
    - On failure again: retry with the model once more, then re-attempt
      extraction + cleanup + parse.
    - On final failure: raise JSONDecodeError with augmented context.
    """
    logger = _ParseLogger(config, source_path)

    if not raw or not raw.strip():
        raw = caller_retry_fn()
    logger.log(raw, "parse_with_retry raw")

    payload = _extract_json_payload(raw)

    logger.log(payload, "parse_with_retry payload")

    try:
        return json.loads(payload, strict=True), payload
    except JSONDecodeError:
        pass

    # Second attempt: fix common model JSON quirks and retry.
    cleaned = _cleanup_model_json(payload)
    logger.log(cleaned, "parse_with_retry cleaned")
    try:
        return json.loads(cleaned, strict=False), cleaned
    except JSONDecodeError:
        pass

    # Third attempt: ask the model to regenerate and re-parse.
    logger.log("Retrying with model after cleanup failure", "parse_with_retry retry")
    try:
        raw2 = caller_retry_fn()
    except Exception:
        raw2 = ""
    if raw2 and raw2.strip():
        payload2 = _extract_json_payload(raw2)
        logger.log(payload2, "parse_with_retry retry_payload")
        try:
            return json.loads(payload2, strict=True), payload2
        except JSONDecodeError:
            pass
        cleaned2 = _cleanup_model_json(payload2)
        logger.log(cleaned2, "parse_with_retry retry_cleaned")
        try:
            return json.loads(cleaned2, strict=False), cleaned2
        except JSONDecodeError as e:
            # Use the retry error for the final message since it's the
            # most recent attempt.
            doc = e.doc or cleaned2
            pos = e.pos or 0
            start = max(0, pos - 80)
            end = min(len(doc), pos + 80)
            context = doc[start:end].replace("\n", "\\n")
            snippet = textwrap.shorten(context, width=200)
            augmented_msg = (
                f"{e.msg} at pos {e.pos} (line {e.lineno}, col {e.colno}). "
                f"Context around error: {snippet}"
            )
            raise JSONDecodeError(augmented_msg, e.doc, e.pos) from None

    # Retry produced empty output – report the original cleanup error.
    # Re-run cleanup to get the error details.
    try:
        json.loads(cleaned, strict=False)
    except JSONDecodeError as e:
        doc = e.doc or payload or raw or ""
        pos = e.pos or 0
        start = max(0, pos - 80)
        end = min(len(doc), pos + 80)
        context = doc[start:end].replace("\n", "\\n")
        snippet = textwrap.shorten(context, width=200)
        augmented_msg = (
            f"{e.msg} at pos {e.pos} (line {e.lineno}, col {e.colno}). "
            f"Context around error: {snippet}"
        )
        raise JSONDecodeError(augmented_msg, e.doc, e.pos) from None
    # Should not reach here, but just in case cleanup is now valid:
    return json.loads(cleaned, strict=False), cleaned

# === Filename parsing ===
# Accept common image extensions (case-insensitive)
VALID_EXTS = {".tif", ".tiff", ".jpg", ".jpeg", ".png"}

@dataclass
class ParsedName:
    """Structured result of parsing a media filename into its grouping parts.

    ``base_id`` is the shared id that groups variants/sides of the same photo;
    ``variant_id`` distinguishes versions (e.g. a trailing letter); ``part_kind``
    says whether the file is a front/back/page/negative/none; ``page_num`` is the
    page index for multipage docs; ``is_crop`` flags derived crop files. Folder
    grouping keys off these fields.
    """

    base_id: str
    variant_id: str | None
    part_kind: str  # "front"|"back"|"page"|"negative"|"none"
    page_num: int | None
    is_crop: bool = False

def parse_media_filename(path: str) -> ParsedName:
    """
    Parse media filenames that may include variant letters and part suffixes.

    Rules:
    1) Strip extension.
    2) Strip -crop suffix first; it stacks on top of any other tag.
    3) Detect part suffix in priority order: -negative, -back / -front, -pageN.
    4) After removing the part suffix, detect a trailing single-letter variant ID.
       Variant letters appear before part suffixes (e.g., ...025b-back-crop).
    5) The remaining stem is the base_id.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    part_kind = "none"
    page_num: int | None = None
    is_crop = False

    lowered = stem.lower()

    # Step 2: strip -crop before anything else
    if lowered.endswith("-crop"):
        is_crop = True
        stem = stem[: -len("-crop")]
        lowered = stem.lower()

    # Step 3: detect part kind
    if lowered.endswith("-negative"):
        part_kind = "negative"
        stem = stem[: -len("-negative")]
    elif lowered.endswith("-back"):
        part_kind = "back"
        stem = stem[: -len("-back")]
    elif lowered.endswith("-front"):
        part_kind = "front"
        stem = stem[: -len("-front")]
    else:
        m_page = re.match(r"^(.*?)-page(\d+)$", stem, flags=re.IGNORECASE)
        if m_page:
            part_kind = "page"
            stem = m_page.group(1)
            try:
                page_num = int(m_page.group(2))
            except Exception:
                page_num = None
    # NOTE: Do not infer missing "-page1" here; grouping decides whether an
    # untagged file should be treated as Page 1 only when the group contains
    # other explicit -pageN files.

    m_var = re.match(
        r"^(?P<stem>.+?)(?:-(?P<variant>[a-z])|(?<=\d)(?P<variant2>[a-z]))?$",
        stem,
        flags=re.IGNORECASE,
    )
    if m_var:
        stem = m_var.group("stem")
        var = (m_var.group("variant") or m_var.group("variant2") or "").lower() or None
    else:
        var = None

    return ParsedName(
        base_id=stem,
        variant_id=var,
        part_kind=part_kind,
        page_num=page_num,
        is_crop=is_crop,
    )

# === Folder grouping ===
_VERSION_RE = re.compile(r"""
    ^
    (?P<stem>.+?)                          # non-greedy base name
    (?:
        -(?P<version>[a-z])                # explicit single-letter version: -b, -c, …
        |(?<=\d)(?P<version2>[a-z])        # or letter immediately after a digit: 034b
    )?
    (?P<part>-negative|-back|-front)?      # optional part kind (longest match wins)
    (?P<crop>-crop)?                       # optional crop modifier
    $
""", re.IGNORECASE | re.VERBOSE)

def _split_name_version_back(filename_no_ext: str) -> Optional[dict]:
    """
    Decompose a bare filename (no extension) into its constituent parts.
    Returns dict: {'stem','version','is_front','is_back','is_negative','is_crop'} or None.
    """
    m = _VERSION_RE.match(filename_no_ext)
    if not m:
        return None
    version = (m.group("version") or m.group("version2") or "").lower() or None
    part = (m.group("part") or "").lower()
    return {
        "stem": m.group("stem"),
        "version": version,
        "is_front": part == "-front",
        "is_back": part == "-back",
        "is_negative": part == "-negative",
        "is_crop": bool(m.group("crop")),
    }

def _is_image_file(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VALID_EXTS

def group_folder_images(folder: str) -> dict:
    """
    Scan a folder and group files by stem:
      {
        "<stem>": {
          "primary": {
            "front": <path or None>, "back": <path or None>,
            "front_crop": <path or None>, "back_crop": <path or None>,
          },
          "variants": [
              {"version": "b", "front": <path or None>, "back": <path or None>,
               "front_crop": <path or None>, "back_crop": <path or None>},
          ],
          "pages":      {1: <path>, 2: <path>, ...},   # keyed by page_num int
          "page_crops": {1: <path>, ...},               # crop derivatives per page
          "negative":      <path or None>,
          "negative_crop": <path or None>,
          "all_fronts": [primary_front, v1_front, ...],
          "all_backs":  [primary_back or None, ...]     # aligned index
        },
      }
    For multi-page sets all_fronts is the sorted page list; all_backs is all None.
    Only files with VALID_EXTS are considered.
    """
    folder = os.path.normpath(os.path.expanduser(folder))
    if not os.path.isdir(folder):
        raise NotADirectoryError(folder)

    sets: dict = {}
    for entry in os.listdir(folder):
        full = os.path.join(folder, entry)
        if not os.path.isfile(full):
            continue
        if not _is_image_file(full):
            continue
        parsed = parse_media_filename(full)
        stem = parsed.base_id
        version = parsed.variant_id  # None or 'b','c',...
        is_back = parsed.part_kind == "back"
        is_negative = parsed.part_kind == "negative"
        is_crop = parsed.is_crop

        s = sets.setdefault(stem, {
            "primary": {"front": None, "back": None, "front_crop": None, "back_crop": None},
            "variants": [],
            "pages": {},
            "page_crops": {},
            "negative": None,
            "negative_crop": None,
        })

        if is_negative:
            # Negatives bin to the stem level regardless of any variant letter
            if is_crop:
                s["negative_crop"] = full
            else:
                s["negative"] = full
        elif parsed.part_kind == "page":
            pn = parsed.page_num or 1
            if is_crop:
                s["page_crops"][pn] = full
            else:
                s["pages"][pn] = full
        elif version is None:
            # Primary scan
            if is_back:
                s["primary"]["back_crop" if is_crop else "back"] = full
            else:
                s["primary"]["front_crop" if is_crop else "front"] = full
        else:
            # Variant: find or create slot
            slot = None
            for v in s["variants"]:
                if v["version"] == version:
                    slot = v
                    break
            if slot is None:
                slot = {"version": version, "front": None, "back": None,
                        "front_crop": None, "back_crop": None}
                s["variants"].append(slot)
            if is_back:
                slot["back_crop" if is_crop else "back"] = full
            else:
                slot["front_crop" if is_crop else "front"] = full

    # Order variants; build aligned all_fronts / all_backs
    for s in sets.values():
        s["variants"].sort(key=lambda v: v["version"] or "a")
        if s["pages"]:
            # Multi-page document: all_fronts is the ordered page list
            s["all_fronts"] = [s["pages"][k] for k in sorted(s["pages"])]
            s["all_backs"] = [None] * len(s["all_fronts"])
        else:
            fronts = [s["primary"]["front"]]
            backs = [s["primary"]["back"]]
            for v in s["variants"]:
                fronts.append(v["front"])
                backs.append(v["back"])
            # Nones preserved to keep indices aligned; analysis uses primary only.
            s["all_fronts"] = fronts
            s["all_backs"] = backs

    return sets

def ensure_keyword_back(record: dict) -> dict:
    """
    Guarantee 'back' is present (case-insensitive) in record['keywords'].
    Returns the same dict for convenience.
    """
    kws = record.get("keywords") or []
    if isinstance(kws, str):
        kws = [kws]
    # case-insensitive check
    lower = {k.strip().lower() for k in kws if isinstance(k, str)}
    if "back" not in lower:
        kws = list(kws) + ["back"]
    record["keywords"] = kws
    return record

# === Manifest helpers ===
def load_manifest(path: str) -> dict:
    """Load and shape-validate a manifest JSON file.

    Enforces the minimal contract the rest of the pipeline relies on: an object
    with an ``items`` array. Raises ``ValueError`` on anything else so a
    malformed manifest fails fast with a clear message rather than surfacing as a
    confusing error deep in processing.
    """
    data = _load_json(path)
    if not isinstance(data, dict) or "items" not in data or not isinstance(data["items"], list):
        raise ValueError("Manifest must be an object with an 'items' array of {path, ...}.")
    return data

def pick_master_index(items_in_group: list[dict], *, update_policy: str | None = None) -> int:
    """
    Choose the master within a grouped list.

    Rule order:
      1. Any item with preferred=true.
      2. If ``update_policy == "master_exact"`` pick the non-back item whose
         version is the lowest letter (with ``None``/no letter sorted first).
      3. The first non-back whose basename has no version suffix.
      4. Fallback to index 0.
    """
    for i, it in enumerate(items_in_group):
        if it.get("preferred"):
            return i
    if update_policy == "master_exact":
        candidates: list[tuple[int, str | None]] = []
        for i, it in enumerate(items_in_group):
            if it.get("is_back"):
                continue
            version = it.get("version")
            if version is None:
                name = os.path.splitext(os.path.basename(it["path"]))[0]
                parsed = _split_name_version_back(name)
                version = parsed["version"] if parsed else None
            candidates.append((i, version))
        if candidates:
            candidates.sort(key=lambda t: ((t[1] is not None), t[1] or ""))
            return candidates[0][0]
    # pick the first whose name looks like primary (no version suffix)
    for i, it in enumerate(items_in_group):
        if it.get("is_back"):
            continue
        version = it.get("version")
        if version is None:
            return i
    return 0

def load_item_metadata(it: dict) -> dict | None:
    """Return inline metadata if present, else load from metadata_path if provided."""
    if isinstance(it.get("metadata"), dict):
        return it["metadata"]
    mp = it.get("metadata_path")
    if mp:
        try:
            return _load_json(normalize_path(mp))
        except (OSError, JSONDecodeError) as exc:
            if os.getenv("MEL_VERBOSE") or os.getenv("MEL_DEBUG"):
                logger.warning("Failed to load metadata from %s: %s", mp, exc)
            return None
    return None

def combine_group_metadata(items_in_group: list[dict]) -> dict:
    """
    Combine all per-item metadata for forwarding to the model.
    Rules:
      - keywords: union (case-insensitive)
      - title/caption/date/location: first non-empty wins, but any item with preferred=true takes precedence
    """
    def _norm_str_set(seq) -> List[str]:
        seen = set(); out = []
        for x in (seq or []):
            if isinstance(x, str):
                k = x.strip().lower()
                if k and k not in seen:
                    seen.add(k); out.append(x.strip())
        return out
    keywords = []
    title = caption = date = location = state = city = country = user_comment = None

    # if any preferred has values, pluck them first
    preferred = [it for it in items_in_group if it.get("preferred")]
    scan_order = preferred + [it for it in items_in_group if it not in preferred]

    for it in scan_order:
        meta = load_item_metadata(it) or {}
        ks = meta.get("keywords") or meta.get("tags") or []
        if isinstance(ks, str): ks = [ks]
        keywords.extend(ks)
        if not title:    title    = (meta.get("title") or "").strip() or None
        if not caption:  caption  = (meta.get("caption") or "").strip() or None
        if not date:     date     = (meta.get("dateTimeOriginal") or "").strip() or None
        if not location: location = (meta.get("location") or "").strip() or None
        if not state:       state = (meta.get("stateProvince") or "").strip() or None
        if not city:       city = (meta.get("city") or "").strip() or None
        if not country:       country = (meta.get("country") or "").strip() or None
        if not user_comment:
            user_comment = (meta.get("userComment") or "").strip() or None

    # dedupe keywords
    keywords = _norm_str_set(keywords)
    out = {}
    if keywords: out["keywords"] = keywords
    if title:    out["title"] = title
    if caption:  out["caption"] = caption
    if date:     out["date"] = date
    if location: out["location"] = location
    if state:    out["state"] = state
    if city:     out["city"] = city
    if country:  out["country"] = country
    if user_comment: out["userComment"] = user_comment
    return out

# === Metadata merge helpers ===
def union_keywords(*lists: list[str]) -> list[str]:
    """Case-insensitive, order-preserving union."""
    out, seen = [], set()
    for lst in lists:
        for k in (lst or []):
            if not isinstance(k, str):
                continue
            kk = k.strip()
            lk = kk.lower()
            if kk and lk not in seen:
                seen.add(lk)
                out.append(kk)
    return out

def merge_original_sources(primary: dict | None, fallback: dict | None) -> dict:
    """
    Merge two metadata sources for downstream merging with AI output.

    - keywords/tags: case-insensitive union, preserving order (primary first)
    - title/caption/date/location: prefer ``primary`` when present, else fallback
    """
    primary = primary or {}
    fallback = fallback or {}

    def _norm_list(val):
        if val is None:
            return []
        if isinstance(val, str):
            return [val]
        return list(val) if isinstance(val, list) else []

    pk = _norm_list(primary.get("keywords") or primary.get("tags"))
    fk = _norm_list(fallback.get("keywords") or fallback.get("tags"))
    keywords = union_keywords(pk, fk)

    def _pick(field: str):
        v1 = (primary.get(field) or "").strip()
        if v1:
            return v1
        v2 = (fallback.get(field) or "").strip()
        return v2 or None

    merged: dict[str, str | list[str]] = {}
    if keywords:
        merged["keywords"] = keywords
    for fld in ("title", "caption", "date", "location","state","city","country"): #TODO add notes field
        v = _pick(fld)
        if v:
            merged[fld] = v
    return merged

def dedupe_captions_with_source(items: list[dict]) -> list[dict]:
    """
    Given [{'file': ..., 'caption': '...'}, ...], drop exact-dup captions (case/whitespace-insensitive),
    keep first occurrence order.
    """
    seen, out = set(), []
    for it in items:
        cap = (it.get("caption") or "").strip()
        key = " ".join(cap.split()).lower()
        if cap and key not in seen:
            seen.add(key)
            kept = {"file": it.get("file"), "caption": cap}
            for extra in ("version", "is_back", "source"):
                if extra in it:
                    kept[extra] = it[extra]
            out.append(kept)
    return out

# === Response helpers ===
def extract_usage(resp) -> dict | None:
    """
    Pull normalized token usage from provider response objects.
    Returns None if unavailable.
    """
    try:
        usage_obj = getattr(resp, "usage", None)
        usage_metadata_obj = None
        if not usage_obj:
            usage_metadata_obj = getattr(resp, "usage_metadata", None)
            if not usage_metadata_obj:
                return None

        usage_dict: dict[str, Any] = {}

        def _to_dict(obj: Any) -> dict[str, Any]:
            if isinstance(obj, dict):
                return obj
            if hasattr(obj, "model_dump"):
                return obj.model_dump()
            out: dict[str, Any] = {}
            for key in (
                "input_tokens",
                "output_tokens",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "input_tokens_details",
                "output_tokens_details",
                "prompt_token_count",
                "candidates_token_count",
                "total_token_count",
            ):
                if hasattr(obj, key):
                    out[key] = getattr(obj, key)
            return out

        if usage_obj:
            usage_dict = _to_dict(usage_obj)
        else:
            gemini_dict = _to_dict(usage_metadata_obj)
            usage_dict = {
                "input_tokens": gemini_dict.get("prompt_token_count"),
                "output_tokens": gemini_dict.get("candidates_token_count"),
                "total_tokens": gemini_dict.get("total_token_count"),
                "input_tokens_details": gemini_dict.get("prompt_tokens_details"),
                "output_tokens_details": gemini_dict.get("candidates_tokens_details"),
            }

        def _as_int(val: Any) -> int | None:
            try:
                return int(val)
            except (TypeError, ValueError):
                return None

        model_name = getattr(resp, "model", None)
        # Responses API names these input/output_tokens; Chat Completions
        # (OpenRouter and other compat gateways) names them prompt/completion_tokens.
        prompt_tokens = _as_int(usage_dict.get("input_tokens"))
        if prompt_tokens is None:
            prompt_tokens = _as_int(usage_dict.get("prompt_tokens"))
        completion_tokens = _as_int(usage_dict.get("output_tokens"))
        if completion_tokens is None:
            completion_tokens = _as_int(usage_dict.get("completion_tokens"))
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": _as_int(usage_dict.get("total_tokens")),
            "input_tokens_details": usage_dict.get("input_tokens_details"),
            "output_tokens_details": usage_dict.get("output_tokens_details"),
            "model": model_name,
        }
    except Exception:
        return None
