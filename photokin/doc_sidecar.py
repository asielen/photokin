"""Write the per-image markdown sidecar for document-mode analysis.

The sidecar is a human-readable companion to the JSON analysis payload:
YAML frontmatter carrying the same merged fields a researcher would want to
filter or sort on, followed by a body whose transcription section is the
verbatim text the model read off the page. It is meant to be read by a
person, not re-parsed by photokin, so the frontmatter emitter below trades
completeness for simplicity -- it only needs to round-trip through a
person's eyes and, incidentally, through any real YAML parser.

There is no YAML dependency: PyYAML is not a requirement of this project,
and the emitted subset (quoted scalars, flow lists, one flat flow map) does
not need one. See ``docs/document-mode-contract.md`` section 4, which this
module implements field-for-field.

Code map:
- SidecarContext        the per-file placement facts the emit loop supplies
- write_markdown_sidecar   PUBLIC: write "<stem>.md" beside an image
- _build_body              title/ai_caption/transcription body, plus the
                           fallback flag frontmatter needs
- _build_frontmatter_lines the ordered, omit-if-empty frontmatter key list
- _resolve_page_fields     page / page_from_filename / page_order_flags
- _flat_location           non-null members of location_guess, in field order
- _analyzed_by             "<provider> <model> (<date>)" provenance string
- _analysis_date_from_caption  pull the dated "[AI Analysis on ...]:" prefix
- _yaml_value              serialize one Python value per the emitter subset
- _yaml_string             quote and escape one string scalar
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from . import utils

logger = logging.getLogger(__name__)

#: Field order the flat ``location`` map is emitted in, matching
#: ``merge._standardize_location_guess``. Fixed rather than dict-iteration
#: order so the sidecar is stable regardless of how the record was built.
_LOCATION_FIELD_ORDER: tuple[str, ...] = ("country", "state", "city", "sublocation", "confidence")

#: The dated analysis-date prefix ``inject_analysis_date`` (core.py) writes,
#: e.g. "[AI Analysis on 2026-08-27]:". Matched at the start of the caption,
#: allowing the same leading whitespace ``inject_analysis_date`` tolerates.
_ANALYSIS_DATE_RE = re.compile(r"^\s*\[AI Analysis on (\d{4}-\d{2}-\d{2})\]:")

#: One-character YAML escapes, checked per source character below -- never as
#: a sequence of string-wide ``.replace()`` calls, which would double-escape
#: a backslash that happens to precede a character this table also escapes.
_YAML_CHAR_ESCAPES: dict[str, str] = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\t": "\\t",
    "\r": "\\r",
}


@dataclass(frozen=True)
class SidecarContext:
    """Per-file placement facts the emit loop already knows, for one sidecar.

    Everything here is positional/structural -- where this file sits in its
    group -- as opposed to ``merged``, which carries the model's and merge's
    judgments about its content. Frozen because it is built once per file and
    only ever read.

    Attributes:
        group_id: The bucket stem the manifest grouped this file under.
        part_label: The payload part label this file travelled under, from
            ``resolve_part_label`` -- not guaranteed to be a key of
            ``merged["transcriptions"]`` (see ``write_markdown_sidecar``).
        group_files: Basenames of every file in the group, in group rank
            order.
        page_count: Count of ``Page N`` parts in the group, or ``None`` when
            the group is not multipage.
        page_number: The page number this file's own filename implies, or
            ``None`` when it is not a page.
    """

    group_id: str
    part_label: str
    group_files: tuple[str, ...]
    page_count: int | None
    page_number: int | None


def sidecar_path_for(image_path: str) -> str:
    """Return the markdown sidecar destination for one image.

    ``<stem>.md`` beside the image, the same derivation ``<stem>.json`` already
    uses (D8), so there is nothing new to learn about where these land.

    That derivation drops the extension, which means two files of one group can
    share a destination: a TIFF master kept beside its JPEG derivative is the
    commonest shape in a scanning archive, and this codebase's own grouping
    calls it out by name. The JSON sidecar never had to care -- it is written
    once per group, from the primary -- but the markdown one is written per
    file, so the collision is real and the caller has to resolve it rather than
    let the second write erase the first.

    Args:
        image_path: The image the sidecar belongs to.

    Returns:
        The absolute path of that image's ``.md`` sidecar.
    """
    img_dir = os.path.dirname(os.path.abspath(image_path))
    img_base = os.path.splitext(os.path.basename(image_path))[0]
    return os.path.join(img_dir, f"{img_base}.md")


def write_markdown_sidecar(
    merged: dict[str, Any],
    item: dict[str, Any],
    group_info: SidecarContext,
    config: utils.Config,
) -> str | None:
    """Write the markdown sidecar for one file's analysis, beside its image.

    The destination is :func:`sidecar_path_for`'s, which two files of one group
    can share; the caller is responsible for not asking for the same
    destination twice (see the emit loop's collision guard).

    Mirrors ``core._write_sidecar_document``'s failure contract exactly: the
    analysis this sidecar describes is already paid for by the time this
    runs, so a destination that cannot be written (a read-only file left by a
    previous run, a lock held by a sync client, a full disk) must not take
    that analysis down with it. This function never raises.

    Args:
        merged: The post-``merge_metadata`` record for this file.
        item: The manifest grouping entry for this file; only ``item["path"]``
            is read, to name the destination and the sidecar's ``source_file``.
        group_info: Structural placement of this file within its group.
        config: Run configuration, read for the ``analyzed_by`` provenance
            string (provider, and the model fallback when the record carries
            none).

    Returns:
        The path written, or ``None`` when it could not be written, which has
        already been logged at WARNING.
    """
    image_path = item["path"]
    md_path = sidecar_path_for(image_path)

    body, used_fallback = _build_body(merged, group_info.part_label, group_info.group_id)
    frontmatter_lines = _build_frontmatter_lines(merged, item, group_info, config, used_fallback)
    content = "---\n" + "\n".join(frontmatter_lines) + "\n---\n\n" + body

    try:
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(content)
    except (OSError, UnicodeError) as exc:
        # ``UnicodeError`` alongside ``OSError`` because the content here is
        # model-written transcription: a lone surrogate in it raises
        # UnicodeEncodeError on the write, which is a ValueError and would sail
        # straight past an OSError-only guard into the batch loop's per-group
        # handler -- discarding the paid-for analysis and writing an error
        # payload for every file of the group. That is precisely the outcome
        # this function exists to prevent, so the guard has to cover the way
        # this particular writer actually fails.
        logger.warning(
            "Markdown sidecar not written for %s (%s): the analysis is kept in the results.",
            os.path.basename(image_path),
            exc,
        )
        return None
    return md_path


def _build_body(
    merged: dict[str, Any],
    part_label: str,
    group_id: str,
) -> tuple[str, bool]:
    """Build the sidecar body and report whether the fallback path was used.

    Args:
        merged: The post-merge record.
        part_label: This file's resolved part label.
        group_id: The group's bucket stem, used as the title when the record
            has none.

    Returns:
        A tuple of the rendered body text (trailing newline included) and
        whether the fallback (group-caption) transcription path was taken --
        the caller needs that to decide whether to emit
        ``transcription_scope: group``.
    """
    title = merged.get("title") or group_id
    lines = [f"# {title}"]

    ai_caption = merged.get("ai_caption")
    if isinstance(ai_caption, str) and ai_caption.strip():
        lines.append("")
        lines.append(ai_caption)

    transcriptions = merged.get("transcriptions")
    if isinstance(transcriptions, dict) and part_label in transcriptions:
        heading = f"## Transcription — {part_label}"
        transcription_text = transcriptions[part_label]
        used_fallback = False
    else:
        # No attributable transcription for this file -- either the record
        # never carried the map (an older release, or a response with no
        # written text) or this file's part was displaced/unseated and never
        # rode the payload under any label (contract section 2). Either way,
        # the caption block is the only text this sidecar can honestly claim.
        heading = "## Transcription"
        transcription_text = merged.get("caption") or ""
        used_fallback = True

    lines.append("")
    lines.append(heading)
    lines.append("")
    lines.append(transcription_text)

    return "\n".join(lines) + "\n", used_fallback


def _resolve_page_fields(
    merged: dict[str, Any],
    group_info: SidecarContext,
) -> tuple[int | None, int | None, list[str] | None]:
    """Resolve the ``page``/``page_from_filename``/``page_order_flags`` triple.

    ``merged["page_order"]``, when present, is the consolidation pass's
    corrected page number and flags, keyed by part label (contract section
    6). It takes precedence over the filename-derived number carried on
    ``group_info``, but the filename number is still surfaced under
    ``page_from_filename`` when the correction actually changed it -- so a
    reader can see both what the filename said and what the model corrected
    it to, without that becoming noise on the (common) case where they agree.

    Args:
        merged: The post-merge record, read for ``page_order``.
        group_info: Supplies the filename-derived fallback page number.

    Returns:
        ``(page, page_from_filename, page_order_flags)``, each ``None``
        (``page_order_flags`` ``None`` rather than empty) when it has nothing
        to contribute to the frontmatter.
    """
    page_order = merged.get("page_order")
    entry = page_order.get(group_info.part_label) if isinstance(page_order, dict) else None
    corrected = entry.get("page") if isinstance(entry, dict) else None
    if not isinstance(corrected, int):
        return group_info.page_number, None, None

    flags = entry.get("flags") if isinstance(entry, dict) else None
    flags_list = list(flags) if isinstance(flags, list) and flags else None

    if group_info.page_number is not None and corrected != group_info.page_number:
        return corrected, group_info.page_number, flags_list
    return corrected, None, flags_list


def _flat_location(location_guess: Any) -> dict[str, Any]:
    """Return the non-null members of ``location_guess``, in field order.

    Args:
        location_guess: ``merged["location_guess"]``, expected to be a dict
            shaped by ``merge._standardize_location_guess`` but not assumed
            to be -- a non-dict or missing value yields an empty map, which
            the caller omits.

    Returns:
        A dict holding only the members of ``_LOCATION_FIELD_ORDER`` whose
        value is not ``None``, in that fixed order.
    """
    if not isinstance(location_guess, dict):
        return {}
    return {
        field: location_guess[field]
        for field in _LOCATION_FIELD_ORDER
        if location_guess.get(field) is not None
    }


def _analysis_date_from_caption(ai_caption: Any) -> str:
    """Parse the ISO date out of an ``ai_caption``'s dated analysis prefix.

    Args:
        ai_caption: ``merged["ai_caption"]``, of whatever type the record
            happens to hold.

    Returns:
        The ``YYYY-MM-DD`` string from a leading ``[AI Analysis on ...]:``
        prefix when present, else today's date. Parsing the caption rather
        than always using today's clock is what keeps the sidecar
        deterministic under a stubbed or replayed model response.
    """
    if isinstance(ai_caption, str):
        match = _ANALYSIS_DATE_RE.match(ai_caption)
        if match:
            return match.group(1)
    # A calendar date, not an instant -- there is no timezone to attach, and
    # this mirrors core.py's own ``date.today()`` fallback (inject_analysis_date).
    return date.today().isoformat()  # noqa: DTZ011


def _analyzed_by(merged: dict[str, Any], config: utils.Config) -> str:
    """Build the ``analyzed_by`` provenance string.

    Args:
        merged: The post-merge record, read for ``_usage["model"]`` and
            ``ai_caption``.
        config: Run configuration, read for the provider and, when the
            record carries no usage model, the model fallback.

    Returns:
        ``"<provider display name> <model> (<analysis date>)"``, e.g.
        ``"Claude claude-sonnet-4-6 (2026-08-27)"``.
    """
    provider_name = utils.provider_display_name(config.provider)

    usage = merged.get("_usage")
    model = usage.get("model") if isinstance(usage, dict) else None
    if not model:
        model = utils.resolve_model_for_provider(config)

    analysis_date = _analysis_date_from_caption(merged.get("ai_caption"))
    return f"{provider_name} {model} ({analysis_date})"


def _build_frontmatter_lines(
    merged: dict[str, Any],
    item: dict[str, Any],
    group_info: SidecarContext,
    config: utils.Config,
    used_fallback: bool,
) -> list[str]:
    """Build the frontmatter as rendered ``key: value`` lines, contract order.

    Args:
        merged: The post-merge record.
        item: The manifest grouping entry, read for ``path``.
        group_info: Structural placement of this file within its group.
        config: Run configuration, passed through to ``_analyzed_by``.
        used_fallback: Whether ``_build_body`` fell back to the group caption;
            when it did, ``transcription_scope: group`` is appended.

    Returns:
        One rendered line per surviving key, in contract table order. A key
        whose value is ``None`` or empty is omitted entirely -- there is no
        key with a null or empty value in the output.
    """
    entries: list[tuple[str, Any]] = [
        ("source_file", os.path.basename(item["path"])),
        ("group", group_info.group_id),
        ("part", group_info.part_label),
    ]

    page, page_from_filename, page_order_flags = _resolve_page_fields(merged, group_info)
    if page is not None:
        entries.append(("page", page))
    if page_from_filename is not None:
        entries.append(("page_from_filename", page_from_filename))
    if page_order_flags:
        entries.append(("page_order_flags", page_order_flags))

    if group_info.page_count is not None:
        entries.append(("page_count", group_info.page_count))
    if group_info.group_files:
        entries.append(("group_files", list(group_info.group_files)))

    if merged.get("title"):
        entries.append(("title", merged["title"]))
    if merged.get("category"):
        entries.append(("category", merged["category"]))

    keywords = merged.get("keywords")
    if isinstance(keywords, list) and keywords:
        entries.append(("keywords", keywords))

    date_guess = merged.get("date_guess")
    if isinstance(date_guess, dict):
        if date_guess.get("iso"):
            entries.append(("date", date_guess["iso"]))
        if date_guess.get("pattern"):
            entries.append(("date_pattern", date_guess["pattern"]))
        if date_guess.get("confidence") is not None:
            entries.append(("date_confidence", date_guess["confidence"]))

    location = _flat_location(merged.get("location_guess"))
    if location:
        entries.append(("location", location))

    entries.append(("analyzed_by", _analyzed_by(merged, config)))

    if used_fallback:
        entries.append(("transcription_scope", "group"))

    return [f"{key}: {_yaml_value(value)}" for key, value in entries]


def _yaml_string(value: str) -> str:
    """Quote and escape one string scalar per the contract's emitter rules.

    Escaping is done one source character at a time rather than with
    sequential ``str.replace()`` calls, so a literal backslash the input
    already contains can never be mistaken for the start of an escape this
    function is about to introduce.

    Args:
        value: The raw string to embed as a double-quoted YAML scalar.

    Returns:
        The value wrapped in double quotes, with ``\\`` and ``"`` escaped,
        ``\\n``/``\\t``/``\\r`` used for newline/tab/carriage-return, and any
        other C0 control character encoded as ``\\uXXXX``.
    """
    escaped_chars: list[str] = []
    for char in value:
        replacement = _YAML_CHAR_ESCAPES.get(char)
        if replacement is not None:
            escaped_chars.append(replacement)
        elif ord(char) < 0x20:
            escaped_chars.append(f"\\u{ord(char):04x}")
        else:
            escaped_chars.append(char)
    return '"' + "".join(escaped_chars) + '"'


def _yaml_value(value: Any) -> str:
    """Serialize one Python value per the contract's YAML emitter subset.

    Covers exactly the shapes this module ever needs to emit: quoted
    strings, bare bools/ints/floats, flow lists, and the one flat flow map
    (``location``). ``bool`` is checked ahead of ``int``/``float`` because
    ``bool`` is a subclass of ``int`` in Python.

    Args:
        value: The value to serialize -- a ``str``, ``bool``, ``int``,
            ``float``, ``list``/``tuple``, or flat ``dict`` of those.

    Returns:
        The rendered scalar, flow list, or flow map.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _yaml_string(value)
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_yaml_value(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_yaml_value(v) for v in value) + "]"
    # Nothing in the contract's key list produces any other type; fall back
    # to a quoted string rather than raising, in keeping with this module's
    # never-raise failure contract.
    return _yaml_string(str(value))
