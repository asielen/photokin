"""Canonical tag mapping: turn a loose metadata dict into authoritative tag keys.

The rest of the pipeline speaks in fuzzy, model-friendly field names (``caption``,
``keywords``, ``location_guess``, ``date_guess``). Writers (ExifTool, the
changeset emitter) need stable industry-standard tag keys (XMP/IPTC/EXIF). This
module is the single place that maps the former onto the latter, applying the
confidence thresholds in :class:`~photokin.utils.Config` so low-confidence
guesses are surfaced as *suggestions* rather than written.

Two output shapes share that mapping logic:
- a flat ``{tag: value}`` dict (``canonical_values_*``), and
- a changeset ``{tag: {"op": "set", "value": ...}}`` patch plus a side-channel
  ``patch_meta`` of below-threshold suggestions (``build_canonical_patch``).

Code map:
- _clean_str / _clean_str_list           trim/de-dup scalars and string lists
- _location_components_from_metadata     read location parts straight from metadata
- _location_components_from_guess        read location parts + confidence from a guess
- _date_from_metadata                    pick a date (explicit wins over guess)
- canonical_values_from_patch            PUBLIC: extract set-values from a patch
- canonical_values_from_metadata         PUBLIC: metadata -> flat {tag: value}
- build_canonical_patch                  PUBLIC: metadata -> (patch, suggestions)
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

from .utils import Config

# Spelled ``XMP-dc:`` (hyphen), not ``XMP:dc:`` (second colon) and not bare
# ``XMP:``. The three spellings are not interchangeable to ExifTool:
#
# - ``XMP:dc:Subject``  is rejected outright -- "Sorry, XMP:dc:Subject doesn't
#   exist or isn't writable", exit 1, nothing written. ExifTool's tag syntax is
#   ``FAMILY:TAG`` with at most one separator; the second colon is not a
#   namespace qualifier it understands. This is what these three constants said
#   until the writability test below was added, which is why ``-w`` could not
#   put a keyword, title or caption into a file at all.
# - ``XMP:Description``  works, but names the family-0 group and leaves the
#   namespace implicit; it would resolve to a different tag if a non-dc
#   namespace ever defined the same leaf name.
# - ``XMP-dc:Description``  works and is unambiguous. It is also the spelling
#   ExifTool itself prints back under ``-G1``, so the constant here matches what
#   a user sees when they inspect the file they just wrote.
#
# The read path (``exiftool/manifest.py``'s DEFAULT_EXIFTOOL_FIELDS) asks for
# the bare ``XMP:Description`` form deliberately -- as a *read* target it is the
# tolerant spelling -- and the two address the same underlying tag, so a ``-r``
# run sees what a ``-w`` run wrote. There is a test that holds this line:
# ``test_canonical_tags_are_writable.py`` drives the real ExifTool binary
# against a real image for every tag defined in this module.
CANONICAL_KEYWORDS_TAG = "XMP-dc:Subject"
CANONICAL_TITLE_TAG = "XMP-dc:Title"
CANONICAL_DESCRIPTION_TAG = "XMP-dc:Description"
CANONICAL_USER_COMMENT_TAG = "EXIF:UserComment"
CANONICAL_DATE_TAG = "EXIF:DateTimeOriginal"

CANONICAL_LOCATION_TAGS = {
    "country": "IPTC:Country-PrimaryLocationName",
    "state": "IPTC:Province-State",
    "city": "IPTC:City",
    "sublocation": "IPTC:Sub-location",
}


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _clean_str_list(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    seen: set[str] = set()
    for item in values or []:
        if not isinstance(item, str):
            continue
        trimmed = item.strip()
        lowered = trimmed.lower()
        if trimmed and lowered not in seen:
            seen.add(lowered)
            out.append(trimmed)
    return out


def _location_components_from_metadata(meta: Dict[str, Any]) -> Dict[str, str | None]:
    loc_obj = None
    if isinstance(meta.get("locationShown"), dict):
        loc_obj = meta.get("locationShown")
    elif isinstance(meta.get("location_shown"), dict):
        loc_obj = meta.get("location_shown")

    if loc_obj:
        return {
            "country": _clean_str(loc_obj.get("country")),
            "state": _clean_str(loc_obj.get("stateProvince") or loc_obj.get("state") or loc_obj.get("province")),
            "city": _clean_str(loc_obj.get("city")),
            "sublocation": _clean_str(loc_obj.get("subLocation") or loc_obj.get("location") or loc_obj.get("sublocation")),
        }

    return {
        "country": _clean_str(meta.get("country")),
        "state": _clean_str(meta.get("stateProvince") or meta.get("state") or meta.get("province")),
        "city": _clean_str(meta.get("city")),
        "sublocation": _clean_str(meta.get("subLocation") or meta.get("location") or meta.get("sublocation")),
    }


def _location_components_from_guess(meta: Dict[str, Any]) -> Tuple[Dict[str, str | None], float | None, str | None]:
    guess = meta.get("location_guess")
    if not isinstance(guess, dict):
        return _location_components_from_metadata(meta), 1.0, None

    components = {
        "country": _clean_str(guess.get("country")),
        "state": _clean_str(guess.get("state") or guess.get("province")),
        "city": _clean_str(guess.get("city")),
        "sublocation": _clean_str(guess.get("sublocation")),
    }
    confidence = guess.get("confidence")
    reason = guess.get("reason") if isinstance(guess.get("reason"), str) else None
    return components, confidence, reason


def _date_from_metadata(meta: Dict[str, Any]) -> Tuple[str | None, float | None, str | None, str | None, str]:
    date_value = _clean_str(meta.get("dateTimeOriginal")) or _clean_str(meta.get("date"))
    if date_value:
        return date_value, 1.0, None, None, "metadata"

    date_guess = meta.get("date_guess") if isinstance(meta.get("date_guess"), dict) else None
    suggested = None
    confidence = None
    reason = None
    if date_guess:
        suggested = _clean_str(date_guess.get("import_date")) or _clean_str(date_guess.get("iso"))
        confidence = date_guess.get("confidence")
        reason = date_guess.get("reason") if isinstance(date_guess.get("reason"), str) else None

    return suggested, confidence, suggested, reason, "guess"


def canonical_values_from_patch(patch: Dict[str, Any] | None) -> Dict[str, Any]:
    """Flatten a changeset patch into ``{tag: value}`` for its applied edits.

    Only ``op == "set"`` entries are real writes; everything else (suggestions,
    no-ops) is ignored. Used to recover "what would actually be written" from a
    patch without re-deriving it from metadata.
    """
    patch = patch or {}
    values: Dict[str, Any] = {}
    for key, payload in patch.items():
        if not isinstance(payload, dict):
            continue
        if payload.get("op") != "set":
            continue
        values[key] = payload.get("value")
    return values


def canonical_values_from_metadata(meta: Dict[str, Any] | None, config: Config) -> Dict[str, Any]:
    """Map a metadata dict to a flat ``{canonical_tag: value}`` dict.

    Applies the same precedence and confidence gating as
    :func:`build_canonical_patch` (explicit dates/locations always win;
    guesses are only included when at or above the configured threshold), but
    returns plain values rather than changeset ops. Below-threshold guesses are
    simply omitted here — use ``build_canonical_patch`` when you also need the
    rejected suggestions.
    """
    meta = meta or {}
    values: Dict[str, Any] = {}

    keywords = _clean_str_list(meta.get("keywords") or meta.get("tags"))
    if keywords:
        values[CANONICAL_KEYWORDS_TAG] = keywords

    title = _clean_str(meta.get("title"))
    if title:
        values[CANONICAL_TITLE_TAG] = title

    description = _clean_str(meta.get("description") or meta.get("caption"))
    if description:
        values[CANONICAL_DESCRIPTION_TAG] = description

    user_comment = (
        _clean_str(meta.get("analysis_notes"))
        or _clean_str(meta.get("ai_caption"))
        or _clean_str(meta.get("user_comment"))
        or _clean_str(meta.get("userComment"))
    )
    if user_comment:
        values[CANONICAL_USER_COMMENT_TAG] = user_comment

    components, confidence, _reason = _location_components_from_guess(meta)
    if components and (confidence is None or confidence >= config.location_confidence_threshold):
        for component, tag in CANONICAL_LOCATION_TAGS.items():
            value = components.get(component)
            if value:
                values[tag] = value

    date_value, confidence, _suggested, _reason, source = _date_from_metadata(meta)
    if date_value:
        if source == "metadata":
            values[CANONICAL_DATE_TAG] = date_value
        elif confidence is None or confidence >= config.date_confidence_threshold:
            values[CANONICAL_DATE_TAG] = date_value

    return values


def build_canonical_patch(meta: Dict[str, Any] | None, config: Config) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build a canonical changeset patch and a side-channel of suggestions.

    Returns ``(patch, patch_meta)`` where:
    - ``patch`` maps canonical tags to ``{"op": "set", "value": ...}`` for edits
      confident enough to write (explicit metadata, or guesses at/above the
      relevant ``Config`` threshold).
    - ``patch_meta`` holds below-threshold location/date guesses as
      ``{"suggested", "confidence", "reason"}`` so the UI can offer them without
      the writer applying them.

    Splitting confident writes from suggestions is what lets the plugin show
    "we think this is X" without silently mutating the file.
    """
    meta = meta or {}
    patch: Dict[str, Any] = {}
    patch_meta: Dict[str, Any] = {}

    keywords = _clean_str_list(meta.get("keywords") or meta.get("tags"))
    if keywords:
        patch[CANONICAL_KEYWORDS_TAG] = {"op": "set", "value": keywords}

    title = _clean_str(meta.get("title"))
    if title:
        patch[CANONICAL_TITLE_TAG] = {"op": "set", "value": title}

    description = _clean_str(meta.get("description") or meta.get("caption"))
    if description:
        patch[CANONICAL_DESCRIPTION_TAG] = {"op": "set", "value": description}

    user_comment = (
        _clean_str(meta.get("analysis_notes"))
        or _clean_str(meta.get("ai_caption"))
        or _clean_str(meta.get("user_comment"))
        or _clean_str(meta.get("userComment"))
    )
    if user_comment:
        patch[CANONICAL_USER_COMMENT_TAG] = {"op": "set", "value": user_comment}

    components, confidence, reason = _location_components_from_guess(meta)
    if components and (confidence is None or confidence >= config.location_confidence_threshold):
        for component, tag in CANONICAL_LOCATION_TAGS.items():
            value = components.get(component)
            if value:
                patch[tag] = {"op": "set", "value": value}
    elif components and any(components.values()):
        for component, tag in CANONICAL_LOCATION_TAGS.items():
            value = components.get(component)
            if value:
                patch_meta[tag] = {
                    "suggested": value,
                    "confidence": confidence,
                    "reason": reason,
                }

    date_value, confidence, suggested, reason, source = _date_from_metadata(meta)
    if date_value:
        if source == "metadata":
            patch[CANONICAL_DATE_TAG] = {"op": "set", "value": date_value}
        elif confidence is None or confidence >= config.date_confidence_threshold:
            patch[CANONICAL_DATE_TAG] = {"op": "set", "value": date_value}
        else:
            patch_meta[CANONICAL_DATE_TAG] = {
                "suggested": suggested or date_value,
                "confidence": confidence,
                "reason": reason,
            }

    return patch, patch_meta
