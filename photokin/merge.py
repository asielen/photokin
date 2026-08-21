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
- Original caption/location win when present; an original title wins only where
  the model returned none, since a title the model returns was read off the
  print while a file's own title is as likely to be scanner boilerplate.
- The original date is evidence, not truth: it drives the correction heuristic
  and fills ``dateTimeOriginal`` when that heuristic declines, but it no longer
  overwrites the model's ``date_guess`` at confidence 1.0 -- on a scan it is the
  scan date, not the capture date.
- A reviewed ``DATE:`` keyword is a human "hands off the date" signal that
  suppresses the date-correction heuristic.

Code map:
- _norm_str_set                       order-preserving, case-insensitive de-dup
- _extract_year                       pull a plausible 4-digit year from free text
- _has_date_keyword                   detect a human-reviewed ``DATE:`` marker
- _valid_pattern                      sanity-check a model date pattern (Y!M!D!)
- _normalize_location_component       trim/empty-to-None a single location field
- _structured_location_from_original  read location parts from original metadata
- _render_location_string             join location parts into a display string
- _standardize_location_guess         normalize the merged location_guess shape
- merge_record_with_original          PUBLIC: merge record + original -> (merged, report)
"""

from typing import Dict, Any, Tuple, List
import logging
import os
import re

from .utils import Config

logger = logging.getLogger(__name__)


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


def _has_date_keyword(keywords: List[str]) -> bool:
    """
    Return True if any keyword looks like a reviewed DATE: ... marker.

    This is your "human has already looked at this date" signal. When present,
    we avoid auto-correcting dateTimeOriginal and avoid injecting new DATE:
    keywords from AI heuristics.
    """
    for kw in keywords or []:
        if not isinstance(kw, str):
            continue
        if kw.strip().upper().startswith("DATE:"):
            return True
    return False


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

    if loc_obj:
        return _from_obj(loc_obj)

    loc_text = _normalize_location_component(original.get("location"))
    if loc_text:
        return {
            "country": None,
            "state": None,
            "city": None,
            "sublocation": loc_text,
        }

    return {
        "country": None,
        "state": None,
        "city": None,
        "sublocation": None,
    }


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
      reaches the file is unchanged: the fix-up below still decides that.
    - DateTimeOriginal fix-up (config-driven):
        * If there is NO existing DATE: keyword,
        * and AI has a high-confidence date_guess,
        * and AI year vs original year differ by the configured year gap,
        * and date_guess.import_date is a valid YYYY-MM-DD,
          then we:
            - update merged["dateTimeOriginal"] to that import_date
              (preserving any time-of-day suffix), and
            - add a DATE: <pattern> keyword, using date_guess.pattern
              if it looks valid.

        If the model gives a precise Y!M!D! or Y!M! pattern with higher confidence,
        we allow a smaller year gap. This helps newer photos where metadata is
        usually accurate within a decade, while still allowing large gaps for
        older scanned photos.


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

    combo = _norm_str_set(list(orig_k) + list(ai_k))
    if combo != ai_k:
        merged["keywords"] = combo
        report["unions"].append("keywords")

    has_reviewed_date_kw = _has_date_keyword(combo)

    # --- High-confidence date sanity check vs. dateTimeOriginal ------------
    #
    # We look at:
    #   - record["date_guess"] (iso, import_date, confidence, pattern)
    #   - original["dateTimeOriginal"]
    #
    # If:
    #   * There is no DATE: keyword yet (no human-reviewed date),
    #   * confidence meets the configured threshold (or the precise threshold
    #     for Y!M!D!/Y!M! patterns),
    #   * AI year vs EXIF year differ by the configured year gap,
    #   * import_date is a valid YYYY-MM-DD,
    # then we:
    #   * update dateTimeOriginal to import_date (+ time-of-day suffix), and
    #   * add a DATE: <pattern> keyword if we don't already have one and the
    #     pattern looks sane.
    #
    try:
        ai_date = record.get("date_guess") or {}
        if not isinstance(ai_date, dict):
            ai_date = {}

        ai_iso = (ai_date.get("iso") or "").strip()
        ai_import = (ai_date.get("import_date") or "").strip()
        ai_conf = ai_date.get("confidence")
        ai_pattern = (ai_date.get("pattern") or "").strip()

        dt_orig_raw = (original.get("date") or "").strip()

        # Prefer a year derived from import_date, fall back to iso.
        ai_year = _extract_year(ai_import) or _extract_year(ai_iso)
        dt_year = _extract_year(dt_orig_raw)

        # import_date should be a strict YYYY-MM-DD when present.
        is_valid_import_date = bool(
            re.fullmatch(r"\d{4}-\d{2}-\d{2}", ai_import or "")
        )

        min_year_gap = config.date_override_year_gap
        if (
            ai_pattern in {"Y!M!D!", "Y!M!"}
            and isinstance(ai_conf, (int, float))
            and ai_conf >= config.date_override_precise_confidence_threshold
        ):
            min_year_gap = config.date_override_precise_year_gap
        # Diagnostic for the date-override decision. Kept behind a debug flag and
        # routed to the logger so it never pollutes the NDJSON result stream.
        if os.getenv("MEL_VERBOSE") or os.getenv("MEL_DEBUG"):
            logger.debug(
                "[DATE-OVERRIDE-CHECK] path=%s dt_orig_raw=%r dt_year=%s ai_year=%s "
                "ai_conf=%s import_date=%r min_gap=%s abs_gap=%s has_DATE_keyword=%s",
                original.get("path"),
                dt_orig_raw,
                dt_year,
                ai_year,
                ai_conf,
                ai_import,
                min_year_gap,
                abs(ai_year - dt_year) if ai_year and dt_year else None,
                "DATE:" in " ".join(original.get("keywords", [])),
            )
        if (
            not has_reviewed_date_kw
            and isinstance(ai_conf, (int, float))
            and ai_conf >= config.date_override_confidence_threshold
            and ai_year is not None
            and dt_year is not None
            and abs(ai_year - dt_year) > min_year_gap
            and is_valid_import_date
        ):
            # Use import_date as the canonical replacement date. If the original
            # had a time-of-day suffix (e.g., "T12:34:56Z"), preserve it.
            time_suffix = ""
            if "T" in dt_orig_raw:
                time_suffix = dt_orig_raw[dt_orig_raw.index("T") :]
            new_dt = ai_import + time_suffix
            merged["dateTimeOriginal"] = new_dt
            report["overrides"].append("dateTimeOriginal")

            # Add DATE: keyword when:
            #   - we still don't have any DATE: keyword, and
            #   - the model gave us a plausible pattern (Y~/Y!/Y!M~/Y!M!D!/etc).
            if not _has_date_keyword(merged.get("keywords") or []):
                if _valid_pattern(ai_pattern):
                    new_keywords = _norm_str_set(
                        (merged.get("keywords") or []) + [f"DATE: {ai_pattern}"]
                    )
                    merged["keywords"] = new_keywords
                    report["unions"].append("keywords")
    except (KeyError, TypeError, ValueError) as exc:
        # Never allow heuristics to break the merge step.
        # If anything goes sideways, we skip the date fix-up.
        if os.getenv("MEL_VERBOSE") or os.getenv("MEL_DEBUG"):
            logger.warning("Skipping date override heuristics: %s", exc)

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
