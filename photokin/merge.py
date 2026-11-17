"""Reconcile an AI-generated metadata record with a photo's existing metadata.

This module owns the "should we trust this?" policy that sits between the model's
proposals and what actually gets written to a file. The model reasons about *what
it sees* (a date guess, a location guess, transcribed text); this module decides
*what to keep*, giving human-entered/original metadata priority and applying
conservative, config-gated heuristics before overriding anything.

The single public entry point is :func:`merge_record_with_original`. Everything
else is a small, pure helper it composes. The function returns both the merged
record and a report of what it overrode/unioned, so callers can log decisions.

Key domain rules encoded here:
- Keywords union (original first), case-insensitive de-dup, spelling preserved.
- Original caption/location win when present (a structured ``locationShown``,
  else the flat ``country``/``stateProvince``/``city``/``location`` keys); an
  original title wins only where
  the model returned none, since a title the model returns was read off the
  print while a file's own title is as likely to be scanner boilerplate.
- The original date is evidence, not truth: it drives the correction heuristic
  and fills ``dateTimeOriginal`` when that heuristic declines, but it no longer
  overwrites the model's ``date_guess`` at confidence 1.0 -- on a scan it is the
  scan date, not the capture date.
- One rule decides whether the model's date is written (:func:`_date_to_write`),
  and the ``DATE: <pattern>`` keyword follows the write: it is added when a
  date is written and never taken from the model. A date written only because
  of the ``date_min_confidence`` floor also gets ``AI Inferred Date``.
- A reviewed ``DATE:`` keyword (one without ``AI Inferred Date``) is a human
  "hands off the date" signal: no date is written over it.

Code map:
- _norm_str_set                       order-preserving, case-insensitive de-dup
- _extract_year                       pull a plausible 4-digit year from free text
- _valid_pattern                      sanity-check a model date pattern (Y!M!D!)
- _date_to_write                      the date rule: which model date is written
- _date_keywords                      the keywords that follow a written date
- _normalize_location_component       trim/empty-to-None a single location field
- _structured_location_from_original  read location parts from original metadata
- _render_location_string             join location parts into a display string
- _standardize_location_guess         normalize the merged location_guess shape
- merge_record_with_original          PUBLIC: merge record + original -> (merged, report)
"""

from typing import Dict, Any, Tuple, List
import re

from .utils import (
    AI_INFERRED_DATE_KEYWORD,
    Config,
    has_reviewed_date_keyword,
    is_date_marker,
)


def _norm_str_set(seq) -> List[str]:
    """
    Normalize a sequence of strings into a de-duplicated, order-preserving list.

    - Strips whitespace
    - Drops empty strings
    - Case-insensitive for de-duplication, but preserves original spelling/casing.
    """
    seen = set()
    out: List[str] = []
    for x in (seq or []):
        if isinstance(x, str):
            k = x.strip().lower()
            if k and k not in seen:
                seen.add(k)
                out.append(x.strip())
    return out


def _extract_year(value: str) -> int | None:
    """
    Best-effort year extractor for flexible date strings.

    Accepts ISO-like dates (YYYY, YYYY-MM, YYYY-MM-DD, YYYY-MM-DDTHH:MM:SS),
    EXIF-like values, or arbitrary text with a 4-digit year embedded.

    Returns:
        int year (between 1800 and 2100), or None if nothing plausible is found.
    """
    if not isinstance(value, str):
        return None
    m = re.search(r"(\d{4})", value)
    if not m:
        return None
    year = int(m.group(1))
    if 1800 <= year <= 2100:
        return year
    return None


def _valid_pattern(pattern: str) -> bool:
    """
    Basic sanity check for date_guess.pattern coming from the model.

    Expected forms, e.g.:
      "Y~"      (decade best guess -- the most common real output)
      "Y!"
      "Y!M~"
      "Y!M!D!"
      "Y?M!D!"  (for odd cases where year is unknown but month/day known)

    Markers per the prompt spec: ! = confident, ~ = best guess, ? = unknown.
    "@" is also tolerated (an older marker some runs produced). This is
    intentionally permissive but rejects obviously bogus strings.
    """
    if not isinstance(pattern, str):
        return False
    pattern = pattern.strip().upper()
    return bool(re.fullmatch(r"Y[!?~@](M[!?~@])?(D[!?~@])?", pattern))


def _normalize_location_component(val: Any) -> str | None:
    if not isinstance(val, str):
        return None
    cleaned = val.strip()
    return cleaned or None


def _structured_location_from_original(original: Dict[str, Any]) -> Dict[str, str | None]:
    loc_obj = None
    if isinstance(original.get("locationShown"), dict):
        loc_obj = original.get("locationShown")
    elif isinstance(original.get("location_shown"), dict):
        loc_obj = original.get("location_shown")

    def _from_obj(obj: dict) -> Dict[str, str | None]:
        return {
            "country": _normalize_location_component(obj.get("country")),
            "state": _normalize_location_component(obj.get("stateProvince") or obj.get("state")),
            "city": _normalize_location_component(obj.get("city")),
            "sublocation": _normalize_location_component(obj.get("subLocation") or obj.get("location")),
        }

    # Without a structured object, the flat keys -- what -r hydrates from the
    # file's XMP/IPTC location, and what a manifest may carry -- are read the
    # same way, a bare "location" string standing for the sublocation.
    return _from_obj(loc_obj or original)


def _render_location_string(parts: Dict[str, str | None]) -> str | None:
    ordered = [parts.get("country"), parts.get("state"), parts.get("city"), parts.get("sublocation")]
    display = [p for p in ordered if _normalize_location_component(p)]
    return "\n".join(display) if display else None


def _standardize_location_guess(loc_guess: Dict[str, Any] | None) -> Dict[str, Any]:
    loc_guess = loc_guess or {}
    return {
        "country": _normalize_location_component(loc_guess.get("country")),
        "state": _normalize_location_component(loc_guess.get("state")),
        "city": _normalize_location_component(loc_guess.get("city")),
        "sublocation": _normalize_location_component(loc_guess.get("sublocation")),
        "confidence": loc_guess.get("confidence"),
    }


_IMPORT_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PRECISE_PATTERNS = frozenset({"Y!M!D!", "Y!M!"})


def _date_to_write(
    date_guess: dict[str, Any], existing: str, config: Config
) -> tuple[str, bool] | None:
    """Decide whether the model's date is written over the file's date.

    ===================================  ==========================================
    The file's date                      The model's date is written when
    ===================================  ==========================================
    none                                 confidence >= ``date_min_confidence``
    more than ``date_override_year_gap`` confidence >= ``date_min_confidence``
    years away
    closer                               a ``Y!M!D!``/``Y!M!`` pattern, confidence
                                         >= ``date_override_precise_confidence_
                                         threshold`` and a gap over
                                         ``date_override_precise_year_gap``
    ===================================  ==========================================

    A large gap is evidence the file's date is a scan or import date, so it
    needs no confident guess to be replaced. A reviewed ``DATE:`` keyword is
    checked by the caller.

    Args:
        date_guess: The model's ``date_guess``.
        existing: The file's own date, or ``""``.
        config: Supplies the thresholds.

    Returns:
        ``(value, inferred)``: the date to write, keeping the existing date's
        time-of-day suffix, and whether it is written only because of the
        floor (its confidence is under the normal gate for its case). ``None``
        when nothing is written.
    """
    confidence = date_guess.get("confidence")
    import_date = str(date_guess.get("import_date") or "").strip()
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or confidence < config.date_min_confidence
        or not _IMPORT_DATE_RE.fullmatch(import_date)
    ):
        return None
    if not existing:
        return import_date, confidence < config.date_confidence_threshold
    existing_year = _extract_year(existing)
    if existing_year is None:
        return None
    gap = abs(int(import_date[:4]) - existing_year)
    if gap > config.date_override_year_gap:
        inferred = confidence < config.date_override_confidence_threshold
    elif (
        str(date_guess.get("pattern") or "").strip() in _PRECISE_PATTERNS
        and confidence >= config.date_override_precise_confidence_threshold
        and gap > config.date_override_precise_year_gap
    ):
        inferred = False
    else:
        return None
    time_suffix = existing[existing.index("T") :] if "T" in existing else ""
    return import_date + time_suffix, inferred


def _date_keywords(keywords: list[str], pattern: str, inferred: bool) -> list[str]:
    """Replace the date keywords in ``keywords`` with the ones for a written date.

    Called only when a date is written, so any date keyword already present is
    an earlier run's unreviewed one: a reviewed date is never written over.

    Args:
        keywords: The merged keywords.
        pattern: The model's ``date_guess.pattern``; an implausible one adds
            no ``DATE:`` keyword.
        inferred: Whether the date is written only because of the floor.

    Returns:
        The keywords with ``DATE: <pattern>`` and, for an inferred date,
        ``AI Inferred Date`` in place of any earlier date keywords.
    """
    added = ([f"DATE: {pattern}"] if _valid_pattern(pattern) else []) + (
        [AI_INFERRED_DATE_KEYWORD] if inferred else []
    )
    return _norm_str_set([kw for kw in keywords if not is_date_marker(kw)] + added)


def merge_record_with_original(
    record: Dict[str, Any],
    original: Dict[str, Any],
    config: Config,
    *,
    original_title_from_file: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Merge an AI-generated metadata record with the original metadata.

    Args:
        record: The model's record for this file.
        original: The file's original metadata, already merged from its own
            source and its group's.
        config: Run configuration supplying the date-override thresholds.
        original_title_from_file: Whether ``original["title"]`` may have been
            read out of the file's own tags rather than supplied by the caller.
            It is the whole of the difference in the title rule below; see that
            rule for why. Nothing here can work it out, so it arrives as a claim
            the caller made: ``-r`` sets it and no other CLI flag does, and an
            embedder driving ``process_manifest_stream`` sets it only if its own
            hydrator read a file. Merely running a hydrator is not the claim.

    Returns:
        (merged_record, merge_report)

    High-level behavior:
    - Keywords: union original + AI (original first, de-duplicated).
    - Caption: keep original caption as "caption_original" if present.
    - Title: the original wins, except that a title which may have come out of
      the file itself does not outrank one the model transcribed off the object.
    - Location: an explicit original "location" overrides AI.
    - Date: an explicit original "date" is recorded as "date_original" and fills
      "dateTimeOriginal" when nothing else has, but it never overwrites the
      model's "date_guess". It is the file's EXIF:DateTimeOriginal, which on a
      scan is the day the print was scanned; treating that as the capture date
      at confidence 1.0 destroyed the inference the model was paid for. What
      reaches the file is decided by :func:`_date_to_write`.
    - Written date: unless the keywords carry a reviewed ``DATE:`` marker,
      ``_date_to_write`` may set "dateTimeOriginal" to the model's
      ``import_date``; the date keywords are then replaced to match
      (:func:`_date_keywords`). With no date written, the model's own
      ``DATE:`` keyword is dropped, so the keyword never names a date the
      file does not hold.

    This keeps the "should we trust this?" logic here, while leaving the
    nuanced "what do we know about the date?" reasoning to the model.
    """
    merged = {**record}
    report: Dict[str, Any] = {"overrides": [], "unions": []}

    # --- Keywords: original first, then AI ---------------------------------
    orig_k = original.get("keywords") or original.get("tags") or []
    if isinstance(orig_k, str):
        orig_k = [orig_k]
    ai_k = merged.get("keywords") or []

    # The model's date keywords are dropped: whether the file gets a date, and
    # so a DATE: keyword, is decided below.
    combo = _norm_str_set(list(orig_k) + [kw for kw in ai_k if not is_date_marker(kw)])
    if combo != ai_k:
        merged["keywords"] = combo
        report["unions"].append("keywords")

    # --- Date: one rule, and the date keywords follow the write -----------
    ai_date = record.get("date_guess")
    date_guess = ai_date if isinstance(ai_date, dict) else {}
    written = (
        None
        if has_reviewed_date_keyword(combo)
        else _date_to_write(date_guess, (original.get("date") or "").strip(), config)
    )
    if written is not None:
        merged["dateTimeOriginal"], inferred = written
        report["overrides"].append("dateTimeOriginal")
        merged["keywords"] = _date_keywords(
            combo, str(date_guess.get("pattern") or "").strip(), inferred
        )

    # --- Title: the original wins, unless it may be the file's own ---------
    #
    # The model returns a title only when one is legibly printed on the object
    # ("Include a title only if clearly indicated in the text on the image",
    # image_rules.txt), so a title it returns was read off the print. A title
    # -r read out of XMP:Title is as likely to be scanner boilerplate --
    # "Scanned Image", the bare filename -- as a human's words, and boilerplate
    # that outranked a transcription would make reading the file strictly worse
    # than not reading it. So under -r the original yields to a model title.
    #
    # A title the *caller* supplied is different evidence and keeps its old
    # precedence outright: a manifest title is one a human typed into Lightroom,
    # a title an embedder's own hydrator pulled out of a genealogy database is
    # the same thing by a longer route, and this branch is the only thing
    # standing between either of them and the model's transcription overwriting
    # it in the file. Narrowing the rule for those too would lose human data to
    # fix a problem it does not have, which is why the two are separated rather
    # than decided once.
    t = (original.get("title") or "").strip()
    if t and not (original_title_from_file and (merged.get("title") or "").strip()):
        if t != (merged.get("title") or "").strip():
            merged["title"] = t
            report["overrides"].append("title")

    # --- Caption: keep original alongside AI versions ----------------------
    c = (original.get("caption") or "").strip()
    if c:
        # Preserve the original human caption separately. The AI-generated
        # caption (transcribed text) lives in merged["caption"], and the
        # Lightroom caption can be inspected here.
        merged["caption_original"] = c

    # --- Date: the original is evidence, not truth -------------------------
    #
    # An original "date" is the file's EXIF:DateTimeOriginal, and on a flatbed
    # scan that is the day the print was scanned, not the day the photograph
    # was taken. Stamping it over date_guess at confidence 1.0 asserted the
    # scan date as the capture date and discarded "circa 1952, confidence 0.7"
    # with it. The model's inference now survives in the record; the original
    # keeps every job it had in deciding what reaches the FILE. It has already
    # driven the gap heuristic above, and it fills dateTimeOriginal here when
    # that heuristic declined -- which is what stops an unendorsed inference
    # from being proposed against a date the file already holds.
    d = (original.get("date") or "").strip()
    if d:
        merged["date_original"] = d
        if not str(merged.get("dateTimeOriginal") or "").strip():
            merged["dateTimeOriginal"] = d

    # --- Location: prefer original when present ---------------------------
    loc_parts = _structured_location_from_original(original)
    has_original_loc = any(_normalize_location_component(v) for v in loc_parts.values())
    if has_original_loc:
        merged["location_guess"] = {**loc_parts, "confidence": 1.0}
        report["overrides"].append("location_guess")

    # --- Location display string ------------------------------------------
    loc_guess = _standardize_location_guess(merged.get("location_guess") if isinstance(merged.get("location_guess"), dict) else {})
    if not any(loc_guess.get(k) for k in ("country", "state", "city", "sublocation")):
        fallback_sub = _normalize_location_component(loc_guess.get("name"))
        if fallback_sub:
            loc_guess = {**loc_guess, "sublocation": fallback_sub}
            merged["location_guess"] = loc_guess
    loc_display = _render_location_string({
        "country": loc_guess.get("country"),
        "state": loc_guess.get("state"),
        "city": loc_guess.get("city"),
        "sublocation": loc_guess.get("sublocation"),
    })
    if loc_display:
        merged["location"] = loc_display

    merged["location_guess"] = _standardize_location_guess(merged.get("location_guess"))

    return merged, report
