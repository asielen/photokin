# photo_archiver/merge.py
from typing import Dict, Any, Tuple, List
import os
import sys
import re

from .utils import Config

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
      "Y!"
      "Y@"
      "Y!M@"
      "Y!M!D!"
      "Y?M!D!"  (for odd cases where year is unknown but month/day known)

    This is intentionally permissive but rejects obviously bogus strings.
    """
    if not isinstance(pattern, str):
        return False
    pattern = pattern.strip().upper()
    # Y?, Y!, Y@ optionally followed by M?,M!,M@ and D?,D!,D@
    return bool(re.fullmatch(r"Y[!?@](M[!?@])?(D[!?@])?", pattern))


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
    record: Dict[str, Any], original: Dict[str, Any], config: Config
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Merge an AI-generated metadata record with the original metadata.

    Returns:
        (merged_record, merge_report)

    High-level behavior:
    - Keywords: union original + AI (original first, de-duplicated).
    - Caption: keep original caption as "caption_original" if present.
    - Title: prefer original title when it exists.
    - Date/location:
        * If original has an explicit "date" or "location", they override AI.
        * Otherwise, we consider AI's date_guess/location_guess as proposals.
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
    print(record)
    print(original)
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
        print(
                "[DATE-OVERRIDE-CHECK]",
                f"path={original.get('path')}",
                f"dt_orig_raw={dt_orig_raw!r}",
                f"dt_year={dt_year}",
                f"ai_year={ai_year}",
                f"ai_conf={ai_conf}",
                f"import_date={ai_import!r}",
                f"min_gap={min_year_gap}",
                f"abs_gap={(abs(ai_year - dt_year) if ai_year and dt_year else None)}",
                f"has_DATE_keyword={'DATE:' in ' '.join(original.get('keywords', []))}",
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
            #   - the model gave us a plausible pattern (Y!/Y@/Y!M@/Y!M!D!/etc).
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
            print(f"[WARN] Skipping date override heuristics: {exc}", file=sys.stderr)

    # --- Title: prefer original -------------------------------------------
    t = (original.get("title") or "").strip()
    if t:
        if t != (merged.get("title") or ""):
            merged["title"] = t
            report["overrides"].append("title")

    # --- Caption: keep original alongside AI versions ----------------------
    c = (original.get("caption") or "").strip()
    if c:
        # Preserve the original human caption separately. The AI-generated
        # caption (transcribed text) lives in merged["caption"], and the
        # Lightroom caption can be inspected here.
        merged["caption_original"] = c

    # --- Date: prefer explicit original "date" field if present -----------
    #
    # If the original metadata has an explicit "date" field (e.g. from EXIF
    # Date or a manual tag), we treat that as fully authoritative and
    # overwrite date_guess entirely.
    d = (original.get("date") or "").strip()
    if d:
        merged["date_guess"] = {"iso": d, "confidence": 1.0}
        report["overrides"].append("date_guess")

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
