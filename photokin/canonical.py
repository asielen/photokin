"""Canonical tag mapping: turn a loose metadata dict into authoritative tag keys.

The rest of the pipeline speaks in fuzzy, model-friendly field names (``caption``,
``keywords``, ``location_guess``, ``date_guess``). Writers (ExifTool, the
changeset emitter) need stable industry-standard tag keys (XMP/IPTC/EXIF). This
module is the single place that maps the former onto the latter, applying the
location threshold in :class:`~photokin.utils.Config` so a low-confidence
guess is surfaced as a *suggestion* rather than written. A date is decided
upstream, in ``merge``: one it did not write is a suggestion here.

Two output shapes share that mapping logic:
- a flat ``{tag: value}`` dict (``canonical_values_*``), and
- a changeset ``{tag: {"op": "set", "value": ...}}`` patch plus a side-channel
  ``patch_meta`` of below-threshold suggestions (``build_canonical_patch``).

Code map:
- _clean_str / _clean_str_list           trim/de-dup scalars and string lists
- _location_components_from_metadata     read location parts straight from metadata
- _location_components_from_guess        read location parts + confidence from a guess
- _written_date                          the capture date merge decided to write
- _claimed_precision                     how much of a date the model claimed
- _date_created_value                    the date at the precision the model claimed
- split_user_comment                     PUBLIC: a comment's user text, AI block dropped
- merge_user_comment                     PUBLIC: user text + this run's AI block
- _user_comment_value                    the UserComment to propose, or None
- canonical_values_from_patch            PUBLIC: extract set-values from a patch
- canonical_values_from_metadata         PUBLIC: metadata -> flat {tag: value}
- build_canonical_patch                  PUBLIC: metadata -> (patch, suggestions)
"""

from __future__ import annotations

import re
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

# Where the keyword list is mirrored. Lightroom reads lr:HierarchicalSubject
# in preference to dc:Subject once a file has it, and IIM readers read
# IPTC:Keywords, so a list written to CANONICAL_KEYWORDS_TAG alone is read back
# stale. The changeset and the allowlist name only CANONICAL_KEYWORDS_TAG; the
# applier applies the same additions and removals to each of these.
CANONICAL_HIERARCHICAL_KEYWORDS_TAG = "XMP-lr:HierarchicalSubject"
CANONICAL_IPTC_KEYWORDS_TAG = "IPTC:Keywords"
CANONICAL_KEYWORD_MIRROR_TAGS = (CANONICAL_HIERARCHICAL_KEYWORDS_TAG, CANONICAL_IPTC_KEYWORDS_TAG)

#: IPTC:Keywords' limit, in bytes of ExifTool's default IIM encoding.
_IIM_KEYWORD_MAX_BYTES = 64


def iim_holds_keyword(keyword: str) -> bool:
    """Whether ``IPTC:Keywords`` stores ``keyword`` exactly as written.

    ExifTool encodes IIM text as Latin (cp1252) unless the file says
    otherwise, writing ``?`` for any other character, and cuts a keyword at
    64 bytes; either way the stored keyword is no longer the one written, and
    a later removal no longer matches it. Such a keyword is kept in XMP only.

    Args:
        keyword: One flat keyword.

    Returns:
        True when the keyword encodes to cp1252 within 64 bytes.
    """
    try:
        return len(keyword.encode("cp1252")) <= _IIM_KEYWORD_MAX_BYTES
    except UnicodeEncodeError:
        return False

CANONICAL_LOCATION_TAGS = {
    "country": "IPTC:Country-PrimaryLocationName",
    "state": "IPTC:Province-State",
    "city": "IPTC:City",
    "sublocation": "IPTC:Sub-location",
}

# The XMP twin of each IPTC IIM location tag, keyed like CANONICAL_LOCATION_TAGS.
# Lightroom reads location from XMP and trusts the XMP over the IIM block once
# ExifTool has rewritten IIM and updated its digest, so an IIM-only write never
# reaches the catalog. The changeset and the allowlist keep the IIM names; the
# applier writes the twin alongside whenever it writes the IIM tag.
CANONICAL_LOCATION_XMP_TAGS = {
    "country": "XMP-photoshop:Country",
    "state": "XMP-photoshop:State",
    "city": "XMP-photoshop:City",
    "sublocation": "XMP-iptcCore:Location",
}

# Lightroom's "Date Created". Proposed beside CANONICAL_DATE_TAG ("Capture
# Time") from the same date, but at the precision the model claimed: XMP takes
# ``YYYY`` and ``YYYY:MM``, so a year-only inference stays a year here even
# though EXIF needs a full timestamp. The IIM pair takes only a complete date,
# so the applier derives it only when the value has one.
CANONICAL_DATE_CREATED_TAG = "XMP-photoshop:DateCreated"
CANONICAL_IPTC_DATE_TAG = "IPTC:DateCreated"
CANONICAL_IPTC_TIME_TAG = "IPTC:TimeCreated"

#: The line that opens an AI analysis block, dated or not: ``inject_analysis_date``
#: rewrites ``[AI Analysis]:`` to ``[AI Analysis on 1952-06-01]:``. Everything
#: from the first such line to the end of a caption or UserComment is a previous
#: run's analysis; everything before it is a person's.
AI_ANALYSIS_MARKER_RE = re.compile(r"^\s*\[AI Analysis\b[^\]]*\]\s*:?", re.IGNORECASE)

_UNDATED_AI_MARKER = "[AI Analysis]:"

#: A run of line breaks inside a keyword, folded to one space: ExifTool takes
#: arguments one per line once they travel as an argument file.
LINE_BREAKS_RE = re.compile(r"[\r\n]+")

_DATE_PARTS_RE = re.compile(
    r"^(\d{4})(?:[-:](\d{2})(?:[-:](\d{2})(?:[ T](\d{2}:\d{2}(?::\d{2})?))?)?)?(?=$|[ T.Z+-])"
)
_DATE_PATTERN_RE = re.compile(r"Y[!?~@](M([!?~@]))?(D([!?~@]))?")
_ISO_PRECISION_RE = re.compile(r"\d{4}(-\d{2}(-\d{2})?)?")


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
        # A keyword is one line. Folded here, where both sides of the
        # changeset diff are built, so the diff and the write agree.
        trimmed = LINE_BREAKS_RE.sub(" ", item).strip()
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


def _written_date(meta: Dict[str, Any]) -> str | None:
    """The capture date to write: the record's own, never a bare guess.

    ``merge`` decides whether the model's date is written and, when it is,
    puts it in ``dateTimeOriginal``; a ``date_guess`` left beside no date is
    only a suggestion.
    """
    return _clean_str(meta.get("dateTimeOriginal")) or _clean_str(meta.get("date"))


def _claimed_precision(date_guess: dict[str, Any]) -> int:
    """How many of year, month and day the model claimed, from 1 to 3.

    Read from ``pattern`` (``Y~`` -> 1, ``Y!M~`` -> 2, ``Y!M!D!`` -> 3; a ``?``
    component is unclaimed), else from the shape of ``iso``. Anything else --
    a decade, a range, nothing -- claims only the year.
    """
    pattern = str(date_guess.get("pattern") or "").strip().upper()
    match = _DATE_PATTERN_RE.fullmatch(pattern)
    if match:
        has_month = match.group(1) is not None and match.group(2) != "?"
        has_day = has_month and match.group(3) is not None and match.group(4) != "?"
        return 1 + has_month + has_day
    iso_match = _ISO_PRECISION_RE.fullmatch(str(date_guess.get("iso") or "").strip())
    if iso_match:
        return 1 + (iso_match.group(1) is not None) + (iso_match.group(2) is not None)
    return 1


def _date_created_value(date_value: str, meta: dict[str, Any]) -> str | None:
    """Render ``date_value`` for ``XMP-photoshop:DateCreated`` without inventing precision.

    A date the model inferred is cut to the precision it claimed, so a ``Y~``
    guess whose ``import_date`` is a mid-year placeholder is written as the
    year alone. A date the file already held (``date_original``) is kept
    whole. The result uses ExifTool's colon form (``1952``, ``1952:06``,
    ``1952:06:01``, ``1952:06:01 12:00:00``).

    Args:
        date_value: The date chosen for ``EXIF:DateTimeOriginal``.
        meta: The metadata it came from, read for ``date_guess`` and
            ``date_original``.

    Returns:
        The DateCreated value, or ``None`` when ``date_value`` is not a
        year, year-month, date or timestamp (a decade such as ``1910s``).
    """
    match = _DATE_PARTS_RE.match(date_value)
    if not match:
        return None
    parts = [part for part in match.groups()[:3] if part]
    time = match.group(4)
    date_guess = meta.get("date_guess")
    if isinstance(date_guess, dict) and date_value != _clean_str(meta.get("date_original")):
        precision = _claimed_precision(date_guess)
        if precision < len(parts):
            parts, time = parts[:precision], None
    rendered = ":".join(parts)
    return f"{rendered} {time}" if time and len(parts) == 3 else rendered


def split_user_comment(comment: Any) -> str:
    """Return the person's part of a UserComment, dropping any earlier AI block.

    The comment is cut at the first line matching :data:`AI_ANALYSIS_MARKER_RE`.
    Text before it is kept verbatim except for trailing whitespace; a comment
    with no marker is entirely the person's.

    Args:
        comment: The file's existing ``EXIF:UserComment``, or ``None``.

    Returns:
        The user text, or ``""`` when there is none.
    """
    kept: list[str] = []
    for line in str(comment or "").splitlines(keepends=True):
        if AI_ANALYSIS_MARKER_RE.match(line):
            break
        kept.append(line)
    return "".join(kept).rstrip()


def merge_user_comment(existing: Any, ai_block: str) -> str:
    """Join a file's own comment with this run's AI block, replacing any earlier one.

    Example:
        >>> merge_user_comment("Note\n\n[AI Analysis]: old", "[AI Analysis]: new")
        'Note\n\n[AI Analysis]: new'

    Args:
        existing: The file's current ``EXIF:UserComment``, or ``None``.
        ai_block: This run's analysis text, non-empty. A block that does not
            open with the marker gets the undated one, so the next run can
            recognize and replace it instead of keeping it as user text.

    Returns:
        ``<user text>\n\n<AI block>``, or the AI block alone when the file
        holds no user text.
    """
    if not AI_ANALYSIS_MARKER_RE.match(ai_block):
        ai_block = f"{_UNDATED_AI_MARKER} {ai_block}"
    user_text = split_user_comment(existing)
    return f"{user_text}\n\n{ai_block}" if user_text.strip() else ai_block


def _user_comment_value(meta: dict[str, Any]) -> str | None:
    """The UserComment for ``meta``: its own comment merged with its AI block.

    ``meta["userComment"]`` is the file's real ``EXIF:UserComment``. With no AI
    text the comment is returned as it stands, which is what the changeset's
    before-snapshot needs and what keeps a run without analysis from proposing
    a change.
    """
    existing = _clean_str(meta.get("userComment"))
    ai_block = _clean_str(meta.get("analysis_notes")) or _clean_str(meta.get("ai_caption"))
    if ai_block is None:
        return existing
    return merge_user_comment(existing, ai_block)


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

    Applies the same precedence and gating as :func:`build_canonical_patch`
    (explicit dates/locations always win; a location guess is included only at
    or above its threshold, a date only once ``merge`` has written it), but
    returns plain values rather than changeset ops. Guesses that are not
    written are simply omitted here — use ``build_canonical_patch`` when you
    also need the rejected suggestions.
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

    user_comment = _user_comment_value(meta)
    if user_comment:
        values[CANONICAL_USER_COMMENT_TAG] = user_comment

    components, confidence, _reason = _location_components_from_guess(meta)
    if components and (confidence is None or confidence >= config.location_confidence_threshold):
        for component, tag in CANONICAL_LOCATION_TAGS.items():
            value = components.get(component)
            if value:
                values[tag] = value

    date_value = _written_date(meta)
    if date_value:
        values[CANONICAL_DATE_TAG] = date_value
        # A file's own Date Created, when it was read, is what it holds there;
        # only a date with none of its own is rendered from the capture date.
        date_created = _clean_str(meta.get("dateCreated")) or _date_created_value(
            date_value, meta
        )
        if date_created:
            values[CANONICAL_DATE_CREATED_TAG] = date_created

    return values


def build_canonical_patch(meta: Dict[str, Any] | None, config: Config) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build a canonical changeset patch and a side-channel of suggestions.

    Returns ``(patch, patch_meta)`` where:
    - ``patch`` maps canonical tags to ``{"op": "set", "value": ...}`` for edits
      confident enough to write (explicit metadata, a location guess at or
      above its ``Config`` threshold, a date ``merge`` decided to write).
    - ``patch_meta`` holds the location and date guesses not written, as
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

    user_comment = _user_comment_value(meta)
    if user_comment and user_comment != _clean_str(meta.get("userComment")):
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

    date_value = _written_date(meta)
    date_guess = meta.get("date_guess")
    if date_value:
        patch[CANONICAL_DATE_TAG] = {"op": "set", "value": date_value}
        date_created = _date_created_value(date_value, meta)
        if date_created:
            patch[CANONICAL_DATE_CREATED_TAG] = {"op": "set", "value": date_created}
    elif isinstance(date_guess, dict):
        suggested = _clean_str(date_guess.get("import_date")) or _clean_str(date_guess.get("iso"))
        if suggested:
            patch_meta[CANONICAL_DATE_TAG] = {
                "suggested": suggested,
                "confidence": date_guess.get("confidence"),
                "reason": date_guess.get("reason") if isinstance(date_guess.get("reason"), str) else None,
            }

    return patch, patch_meta
