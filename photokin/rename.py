"""
photokin.rename
========================

The rename-mode planner: a pure function from an in-memory folder listing to
a rename plan. See ``docs/rename-mode.md`` section 4 for the specification
this module implements to the letter -- section numbers in comments below
refer to it.

Nothing here touches disk. ``plan_rename`` takes the folder's listing (every
file, image or not) and each image's already-known metadata/overrides as
plain data, and returns the plan as a JSON-shaped dict (section 6.2). The
caller -- ``cli.py`` in a later phase -- does the ``os.listdir``, the
ExifTool date hydration, and the actual renaming (``rename_apply.py``); this
module never does any of that, so the whole planner can be exercised with
nothing but constructed paths and dicts.

Code map:
- RenameItem                   PUBLIC: one planner input (path + overrides)
- plan_rename                  PUBLIC: the planner
- DEFAULT_COMPANION_EXTENSIONS default non-image extensions carried along
- SCHEMA_VERSION               the plan dict's own schema version (6.2)
- _PhotoDate                   a possibly-partial calendar date
- _parse_photo_date            read one EXIF:DateTimeOriginal-shaped string
- _render_date_format          render a _PhotoDate through the FORMAT grammar (4.4)
- _tokenize_prefix_template    split a prefix template into literal/token pieces
- _validate_literal_text       Windows-illegal-character/reserved-name check on literals
- _render_template              render one group's prefix from the tokenized template
- _variant_was_dashed           did the parser's dashed or digit-adjacent alternative match?
- _Member                      one parsed, override-applied planner item
- _GroupPlan                   one group's members plus its rendered prefix
- _build_unplannable_entry     an entry for a group that could not be rendered
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from . import utils
from .changeset import make_photo_id, make_run_id
from .exiftool.apply import _TAG_FILENAME_UNSAFE_RE, EXIF_DATE_FORMAT

#: Bumped whenever the plan dict's shape changes in a way a consumer (the
#: executor, a catalog wrapper reading the manifest contract) could care
#: about, the same rule ``changeset.SCHEMA_VERSION`` follows.
SCHEMA_VERSION = 1

#: Non-image extensions carried along with a renamed image by default (4.6's
#: "companion files"). ``--companions`` extends this set at the CLI layer;
#: the planner takes the already-resolved set as ``companion_extensions``.
DEFAULT_COMPANION_EXTENSIONS: frozenset[str] = frozenset({".md", ".json", ".xmp", ".txt"})

_DEFAULT_DATE_FORMAT = "yyyy-mm-dd"


@dataclass
class RenameItem:
    """One planner input: an image file plus whatever the caller already
    knows about it.

    Mirrors the manifest item shape section 4.1 describes, but as a typed
    value the planner can consume without a JSON layer in between.
    ``order``, when given on every item passed to :func:`plan_rename` in one
    call, replaces the alphabetical sort (4.3). ``is_back`` and ``version``
    are honored exactly as ``core._resolve_manifest_entry`` honors the same
    two manifest fields: ``None`` means "no override, read the filename"; an
    explicit value always wins and materializes into the rendered name.
    ``preferred`` is accepted for parity with that override set, but no rule
    in section 4 reads it -- the plan's own overrides table (4.1) lists it
    without describing an effect on a rendered name, so it is carried
    through and otherwise ignored here.

    Args:
        path: Absolute path of an image file, as it exists on disk today.
        metadata: The item's own metadata dict, if any -- read for
            ``metadata["EXIF:DateTimeOriginal"]``. ``None`` and ``{}`` both
            mean "nothing known".
        order: Explicit position, from a manifest's grid order. ``None``
            means "use this file's alphabetical (or ``--order natural``)
            position instead".
        is_back: ``True``/``False`` overrides the filename's own back/front
            reading; ``None`` leaves it alone.
        version: Overrides the parsed variant letter. ``None`` leaves it
            alone; an empty (or all-whitespace) string explicitly clears an
            existing variant letter, mirroring how the manifest override
            already behaves in ``core.py``.
        preferred: Accepted, unused -- see the class docstring.
        size: The file's size in bytes, carried into the plan's preflight
            record (section 5.1 is what actually reads it).
        mtime: The file's modification time, carried the same way.
    """

    path: str
    metadata: dict[str, Any] | None = None
    order: int | None = None
    is_back: bool | None = None
    version: str | None = None
    preferred: bool = False
    size: int | None = None
    mtime: float | None = None


# === Dates ===


@dataclass(frozen=True)
class _PhotoDate:
    """A calendar date that may be missing its month and/or day.

    ``EXIF:DateTimeOriginal`` is sometimes written down with only the
    information actually known -- a bare year, or a year and month -- and
    4.4 requires the template to render ``"00"`` for the parts such a date
    lacks rather than guessing at them. ``partial`` is true whenever either
    is missing, which is what earns a group's "partial date" note.
    """

    year: int
    month: int | None
    day: int | None
    partial: bool


_PARTIAL_DATE_RE = re.compile(r"^(?P<year>\d{4})(?::(?P<month>\d{2}))?$")

_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

#: A partial date's year has to land somewhere plausible for a photograph,
#: the same range ``merge._extract_year`` already treats as sane for a
#: freeform date string -- reused here rather than re-litigated, since both
#: are "is this actually a year" checks on the same kind of input.
_PLAUSIBLE_YEAR_RANGE = (1800, 2100)


def _parse_photo_date(raw: str) -> _PhotoDate | None:
    """Parse one ``EXIF:DateTimeOriginal``-shaped string into a :class:`_PhotoDate`.

    Tries, in order: the two EXIF-style formats section 4.1 names
    (``%Y:%m:%d %H:%M:%S`` and ``%Y:%m:%d``); the partial forms section
    4.4's "renders 00 for the parts it lacks" rule exists to cover
    (``"1952"``, ``"1952:06"``); then ISO 8601 via
    :meth:`datetime.fromisoformat`, which on Python 3.11+ also accepts a
    trailing ``Z``.

    Args:
        raw: The metadata value, as read from
            ``metadata["EXIF:DateTimeOriginal"]``.

    Returns:
        The parsed date, or ``None`` if *raw* matches none of the accepted
        forms.
    """
    text = raw.strip()
    if not text:
        return None
    for fmt in (EXIF_DATE_FORMAT, "%Y:%m:%d"):
        try:
            parsed = datetime.strptime(text, fmt)  # noqa: DTZ007 (date-only value, no zone to carry)
        except ValueError:
            continue
        return _PhotoDate(parsed.year, parsed.month, parsed.day, partial=False)
    partial_match = _PARTIAL_DATE_RE.match(text)
    if partial_match:
        year = int(partial_match.group("year"))
        if not (_PLAUSIBLE_YEAR_RANGE[0] <= year <= _PLAUSIBLE_YEAR_RANGE[1]):
            return None
        month_text = partial_match.group("month")
        if month_text is not None:
            month = int(month_text)
            # The regex only constrains this to two digits -- "1952:13" and
            # "1952:00" both match it. Rejecting an out-of-range month here,
            # rather than downstream in the renderer, is what sends the
            # group to the documented missing-date error (or --undated)
            # instead of into _render_date_token's IndexError on
            # _MONTH_NAMES[13 - 1].
            if not (1 <= month <= 12):
                return None
        else:
            month = None
        return _PhotoDate(year, month, None, partial=True)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _PhotoDate(parsed.year, parsed.month, parsed.day, partial=False)


_FORMAT_TOKENS = ("yyyy", "mmmm", "mmm", "mm", "yy", "dd")


def _render_date_token(token: str, photo_date: _PhotoDate) -> str:
    """Render one FORMAT token off *photo_date* directly (see :func:`_render_date_format`)."""
    if token == "yyyy":
        return f"{photo_date.year:04d}"
    if token == "yy":
        return f"{photo_date.year % 100:02d}"
    if token == "mmmm":
        return _MONTH_NAMES[photo_date.month - 1] if photo_date.month else "00"
    if token == "mmm":
        return _MONTH_NAMES[photo_date.month - 1][:3] if photo_date.month else "00"
    if token == "mm":
        return f"{photo_date.month:02d}" if photo_date.month else "00"
    return f"{photo_date.day:02d}" if photo_date.day else "00"  # "dd"


def _render_date_format(fmt: str, photo_date: _PhotoDate) -> str:
    """Render *photo_date* through the small FORMAT grammar section 4.4 defines.

    A FORMAT containing ``%`` is handed to :meth:`datetime.strftime`
    unchanged, for anyone who wants a code the small grammar does not have
    (``%j``, ``%U``). Otherwise this walks *fmt* left to right, at each
    position matching the longest known token case-insensitively
    (``yyyy`` before ``yy``; ``mmmm`` before ``mmm`` before ``mm``) and
    reading that part straight off *photo_date* -- never through
    :meth:`datetime.strftime` -- which is why ``mmm``/``mmmm`` render the
    same on Windows as anywhere else: ``%b``/``%B`` are locale-dependent,
    and this grammar promises they are not.

    Args:
        fmt: The FORMAT string, e.g. ``"yymmdd"`` or the default
            ``"yyyy-mm-dd"``.
        photo_date: The date to render; a missing month or day renders
            ``"00"`` for every token that would have shown it.

    Returns:
        The rendered string.
    """
    if "%" in fmt:
        month = photo_date.month or 1
        day = photo_date.day or 1
        return datetime(photo_date.year, month, day).strftime(fmt)  # noqa: DTZ001 (date only, no zone)

    out: list[str] = []
    lowered = fmt.lower()
    i = 0
    n = len(fmt)
    while i < n:
        for token in _FORMAT_TOKENS:
            if lowered.startswith(token, i):
                out.append(_render_date_token(token, photo_date))
                i += len(token)
                break
        else:
            out.append(fmt[i])
            i += 1
    return "".join(out)


# === Prefix template ===

_VALID_TOKEN_NAMES = frozenset({"date", "today", "folder", "orig"})
_TOKENS_WITH_FORMAT = frozenset({"date", "today"})


def _tokenize_prefix_template(template: str) -> list[str | tuple[str, str | None]]:
    """Split *template* into literal and token pieces, in read order.

    ``{{`` is a literal brace, never a token opener; a bare ``}`` (nothing
    was opened) is just a literal character, since it cannot be confused
    with anything else in this grammar. Every ``{...}`` span otherwise must
    name one of the four tokens (4.4) and, for ``folder``/``orig``, must not
    carry a ``:FORMAT``.

    Args:
        template: The raw ``--rename PREFIX`` template string.

    Returns:
        A list mixing literal ``str`` pieces and ``(name, format)`` token
        pieces, where ``format`` is ``None`` when no ``:FORMAT`` was given.

    Raises:
        ValueError: For an unterminated ``{``, an unknown token name, or a
            ``:FORMAT`` on a token that does not take one -- all three are
            "illegal ... in the template" per 4.6's error list.
    """
    pieces: list[str | tuple[str, str | None]] = []
    literal_buf: list[str] = []
    i = 0
    n = len(template)
    while i < n:
        ch = template[i]
        if ch == "{":
            if template[i : i + 2] == "{{":
                literal_buf.append("{")
                i += 2
                continue
            end = template.find("}", i + 1)
            if end == -1:
                raise ValueError(f"unterminated '{{' in prefix template {template!r}")
            body = template[i + 1 : end]
            name, sep, fmt_part = body.partition(":")
            name_lower = name.strip().lower()
            if name_lower not in _VALID_TOKEN_NAMES:
                raise ValueError(f"unknown token '{{{body}}}' in prefix template {template!r}")
            if sep and name_lower not in _TOKENS_WITH_FORMAT:
                raise ValueError(
                    f"token '{{{name_lower}}}' does not take a FORMAT "
                    f"in prefix template {template!r}"
                )
            fmt: str | None = fmt_part if sep else None
            if literal_buf:
                pieces.append("".join(literal_buf))
                literal_buf = []
            pieces.append((name_lower, fmt))
            i = end + 1
            continue
        literal_buf.append(ch)
        i += 1
    if literal_buf:
        pieces.append("".join(literal_buf))
    return pieces


_WINDOWS_ILLEGAL_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{d}" for d in range(1, 10)}
    | {f"LPT{d}" for d in range(1, 10)}
)


def _validate_literal_text(
    pieces: Sequence[str | tuple[str, str | None]], template: str
) -> str | None:
    """Validate the literal (non-token) text of a tokenized template.

    Token *output* is sanitized instead of validated (see
    ``_TAG_FILENAME_UNSAFE_RE`` in :func:`_render_template`); literal text
    the person actually typed is validated and left alone, per 4.4's "a
    path separator, a character illegal on Windows, or a Windows reserved
    device name is an error."

    Args:
        pieces: The tokenized template, from :func:`_tokenize_prefix_template`.
        template: The original template string, for the error message.

    Returns:
        An error message, or ``None`` if the literal text is clean.
    """
    literal_chunks = [p for p in pieces if isinstance(p, str)]
    for chunk in literal_chunks:
        match = _WINDOWS_ILLEGAL_CHARS_RE.search(chunk)
        if match:
            return (
                f"prefix template {template!r} contains a character illegal "
                f"on Windows: {match.group()!r}"
            )
    concatenated = "".join(literal_chunks).strip()
    if concatenated.upper() in _WINDOWS_RESERVED_NAMES:
        return f"prefix template {template!r} is a Windows reserved name"
    return None


class _MissingDate(Exception):
    """Internal signal: a group needs ``{date}`` but has neither a date nor
    an ``--undated`` literal to stand in for it."""


def _render_template(
    pieces: Sequence[str | tuple[str, str | None]],
    *,
    photo_date: _PhotoDate | None,
    today: _PhotoDate,
    folder_name: str,
    orig: str,
    undated_literal: str | None,
) -> tuple[str, bool]:
    """Render one group's prefix from the tokenized template.

    Every token's rendered value is sanitized through
    ``exiftool.apply._TAG_FILENAME_UNSAFE_RE`` (4.4's "Token values are
    sanitized with the existing ... policy") before being spliced in;
    literal text was already validated once, up front, by
    :func:`_validate_literal_text`.

    Args:
        pieces: The tokenized template.
        photo_date: The group's representative date, or ``None`` if it has
            none.
        today: The run's date (``--today`` override or the real today),
            always complete.
        folder_name: Value for ``{folder}``.
        orig: Value for ``{orig}``, already stripped of its trailing digit
            run (4.4).
        undated_literal: The ``--undated`` literal, or ``None``.

    Returns:
        The rendered prefix (before the trailing-``-`` trim the caller does)
        and whether a partial date was used to render it.

    Raises:
        _MissingDate: The template uses ``{date}``, *photo_date* is
            ``None``, and *undated_literal* is also ``None``.
    """
    out: list[str] = []
    used_partial_date = False
    for piece in pieces:
        if isinstance(piece, str):
            out.append(piece)
            continue
        name, fmt = piece
        if name == "date":
            if photo_date is not None:
                value = _render_date_format(fmt or _DEFAULT_DATE_FORMAT, photo_date)
                if photo_date.partial:
                    used_partial_date = True
            elif undated_literal is not None:
                value = undated_literal
            else:
                raise _MissingDate()
        elif name == "today":
            value = _render_date_format(fmt or _DEFAULT_DATE_FORMAT, today)
        elif name == "folder":
            value = folder_name
        else:  # "orig"
            value = orig
        out.append(_TAG_FILENAME_UNSAFE_RE.sub("_", value))
    return "".join(out), used_partial_date


_ORIG_STRIP_RE = re.compile(r"-?\d+$")

# The same variant regex parse_media_filename matches against, re-run here on
# the already crop/part-stripped stem so the planner can tell which
# alternative matched -- see the module docstring and the phase 1a interface
# note this satisfies. Widening parse_media_filename's own return value to
# expose this was out of scope for that phase (utils.py changes were
# restricted to functions added beside the parser, not the parser itself).
_VARIANT_RE = re.compile(
    r"^(?P<stem>.+?)(?:-(?P<variant>[a-z])|(?<=\d)(?P<variant2>[a-z]))?$",
    re.IGNORECASE,
)

# The exact page regex parse_media_filename matches against, re-run here so
# _variant_was_dashed can strip a page suffix by its REAL text length rather
# than reconstructing one from the parsed page number: parse_media_filename
# reads that number with int(), which drops leading zeros ("007" -> 7), so a
# length computed from the integer is wrong for any zero-padded page number.
_PAGE_SUFFIX_RE = re.compile(r"^(.*?)-page(\d+)$", re.IGNORECASE)


def _variant_was_dashed(canonical_stem: str, parsed: utils.ParsedName) -> bool:
    """Return whether *canonical_stem*'s variant letter, if any, was dashed.

    Reconstructs the exact intermediate stem :func:`utils.parse_media_filename`
    would have matched its variant regex against, using the crop/part fields
    it already derived from this same *canonical_stem* -- so this never
    re-detects crop/part itself, only slices known-length suffixes off. The
    dashed alternative (``-b``) is exactly the form
    :func:`utils.render_media_filename` always rewrites to digit-adjacent
    (``b``), so this is the "variant form normalized" note's trigger.

    Args:
        canonical_stem: The stem after :func:`utils.canonicalize_stem`, the
            same string that was fed to :func:`utils.parse_media_filename`.
        parsed: That call's result.

    Returns:
        ``True`` if a variant letter is present and was written after a
        ``-`` rather than directly after a digit.
    """
    tail = canonical_stem
    if parsed.is_crop:
        tail = tail[: -len("-crop")]
    if parsed.part_kind == "back":
        tail = tail[: -len("-back")]
    elif parsed.part_kind == "front":
        tail = tail[: -len("-front")]
    elif parsed.part_kind == "negative":
        tail = tail[: -len("-negative")]
    elif parsed.part_kind == "page" and parsed.page_num is not None:
        # Re-run the same page regex parse_media_filename used and take its
        # own group(1), rather than slicing by a length rebuilt from the
        # parsed (leading-zero-stripped) integer -- see _PAGE_SUFFIX_RE.
        page_match = _PAGE_SUFFIX_RE.match(tail)
        if page_match:
            tail = page_match.group(1)
    match = _VARIANT_RE.match(tail)
    return bool(match and match.group("variant") is not None)


# === Grouping ===


@dataclass
class _Member:
    """One :class:`RenameItem`, parsed and with its overrides applied."""

    item: RenameItem
    position: int
    dirname: str
    ext: str
    parsed: utils.ParsedName
    final_parsed: utils.ParsedName
    notes: list[str]
    photo_date: _PhotoDate | None
    version_error: str | None = None


def _name_key(path: str) -> tuple[str, str]:
    """The ``(name.lower(), name)`` tie-break :func:`utils.list_folder_images` uses."""
    name = os.path.basename(path)
    return (name.lower(), name)


_DIGIT_RUN_RE = re.compile(r"(\d+)")


def _natural_sort_key(name: str) -> tuple[int | str, ...]:
    """``--order natural``'s key: digit runs compared numerically (4.3).

    The natural components alone are not a total order: they discard case
    and a numeric run's own spelling, so ``file1.tif`` and ``file01.tif``
    (or ``File1.tif``) produce the same key and compare equal. Python's
    sort is stable, so two equal keys are then left in whatever order they
    arrived in -- meaning merely permuting the manifest, with nothing about
    the files themselves changing, changes the assigned numbers (C10). The
    ordinary ``(name.lower(), name)`` key breaks that tie deterministically,
    the same way it already does for the plain ``"name"`` order.
    """
    parts = _DIGIT_RUN_RE.split(name.lower())
    natural = tuple(int(part) if part.isdigit() else part for part in parts)
    return natural + (name.lower(), name)


def _validate_version_override(raw: str) -> tuple[str | None, str | None]:
    """Validate a manifest ``version`` override (4.1, C1).

    The grammar supports exactly one variant letter (``parse_media_filename``'s
    ``m_var``, a single ``[a-zA-Z]``); an override that is not that -- a word
    like ``"blue"``, a digit, punctuation -- is not a smaller version of the
    same idea, it is a different kind of value that ``render_media_filename``
    would otherwise concatenate straight into the filename
    (``x-001blue.tif``), which ``parse_media_filename`` then reads back as an
    unversioned base id, silently splitting the renamed file from its
    siblings on the very next run. Rejecting it here, before it ever reaches
    the renderer, is what turns that into a named plan error instead of a
    filename that lies about its own grammar.

    Args:
        raw: The manifest's ``version`` string, exactly as given. An empty
            (or all-whitespace) string is a valid override -- it explicitly
            clears an existing variant letter (see the manifest contract).

    Returns:
        A ``(variant, error)`` pair: ``(letter_or_None, None)`` when *raw*
        is empty (clears the variant) or is exactly one letter, otherwise
        ``(None, message)`` with a message naming what was wrong -- the
        caller must not apply the override in that case.
    """
    stripped = raw.strip()
    if not stripped:
        return None, None
    if len(stripped) == 1 and stripped.isalpha():
        return stripped.lower(), None
    return None, f"version override {raw!r} is not a single letter"


def _build_member(item: RenameItem, position: int) -> _Member:
    """Parse one item and apply its ``is_back``/``version`` overrides (4.1, 4.2)."""
    norm_path = os.path.normpath(os.path.abspath(item.path))
    dirname = os.path.dirname(norm_path)
    base, ext = os.path.splitext(os.path.basename(norm_path))
    canonical_stem, notes = utils.canonicalize_stem(base)
    notes = list(notes)
    parsed = utils.parse_media_filename(canonical_stem + ext)

    if (
        item.version is None
        and parsed.variant_id is not None
        and _variant_was_dashed(canonical_stem, parsed)
    ):
        notes.append("variant form normalized")

    final_part_kind = parsed.part_kind
    final_page_num = parsed.page_num
    final_variant = parsed.variant_id
    if item.is_back is True and final_part_kind != "back":
        final_part_kind, final_page_num = "back", None
    elif item.is_back is False and final_part_kind == "back":
        final_part_kind = "front"
    version_error = None
    if item.version is not None:
        validated_variant, version_error = _validate_version_override(item.version)
        if version_error is None:
            final_variant = validated_variant
        # else: leave final_variant as the filename's own reading -- the
        # invalid override is reported (below, via version_error) rather
        # than coerced into the name or dropped without a trace.

    final_parsed = utils.ParsedName(
        base_id=parsed.base_id,
        variant_id=final_variant,
        part_kind=final_part_kind,
        page_num=final_page_num,
        is_crop=parsed.is_crop,
    )

    photo_date = None
    if item.metadata:
        raw_date = item.metadata.get("EXIF:DateTimeOriginal")
        if isinstance(raw_date, str):
            photo_date = _parse_photo_date(raw_date)

    return _Member(
        item=item,
        position=position,
        dirname=dirname,
        ext=ext,
        parsed=parsed,
        final_parsed=final_parsed,
        notes=notes,
        photo_date=photo_date,
        version_error=version_error,
    )


@dataclass
class _GroupPlan:
    """One group: its members, in position order, plus its rendered prefix.

    ``prefix`` is ``None`` when the group could not be rendered at all
    (missing date, empty prefix) -- its members still get an entry each
    (see :func:`_build_unplannable_entry`), just with no target.
    """

    key: str
    display: str
    members: list[_Member]
    prefix: str | None
    used_partial_date: bool


def _build_unplannable_entry(member: _Member, group_display: str) -> dict[str, Any]:
    """An ``entries[]`` record for a group :func:`_GroupPlan` could not render."""
    return {
        "path": member.item.path,
        "photo_id": make_photo_id(member.item.path),
        "size": member.item.size,
        "mtime": member.item.mtime,
        "target": None,
        "target_stem": None,
        "group": group_display,
        "prefix": None,
        "number": None,
        "variant": member.final_parsed.variant_id,
        "part": None if member.final_parsed.part_kind == "none" else member.final_parsed.part_kind,
        "page": member.final_parsed.page_num,
        "crop": member.final_parsed.is_crop,
        "changed": False,
        "notes": list(member.notes),
        "companions": [],
    }


def plan_rename(
    *,
    folder: str,
    disk_files: Sequence[str],
    items: Sequence[RenameItem],
    prefix_template: str,
    digits: int = 3,
    order_mode: str = "name",
    undated_literal: str | None = None,
    today: date | None = None,
    companion_extensions: frozenset[str] | None = None,
    managed_by: dict[str, Any] | None = None,
    photokin_version: str = "",
    run_id: str | None = None,
) -> dict[str, Any]:
    """Plan a rename of *items*, a pure function over the injected listing.

    Implements ``docs/rename-mode.md`` sections 4.1 through 4.6: grouping by
    ``(folder, canonical base_id.lower())``, the ``{date}``/``{today}``/
    ``{folder}``/``{orig}`` template, numbering that restarts per rendered
    prefix, and every error/warning the plan lists. No IO happens here --
    *disk_files* is the caller's own folder listing, and *items* carries
    whatever metadata/overrides the caller already resolved (hydrated EXIF,
    a manifest's per-item fields).

    Args:
        folder: The folder this run targets. Used for the ``{folder}``
            token, the plan's own ``folder`` field, and to scope
            *disk_files* and detect a member sitting outside it.
        disk_files: Every file's path directly inside *folder* -- images and
            non-images alike, including *items*' own paths. Non-image files
            are matched against renamed images by exact stem to become
            companions or ``left_behind`` entries (4.6); image files not in
            *items* become bystanders, whose current names are reserved.
        items: The images being renamed.
        prefix_template: The ``--rename PREFIX`` template (4.4).
        digits: Zero-padded number width (default 3, section 2).
        order_mode: ``"name"`` (default) or ``"natural"`` -- the fallback
            order used when *items* do not all carry an explicit ``order``
            (4.3). ``"manifest"`` is not a valid value here; it is what the
            plan's own ``order`` field reports when every item does.
        undated_literal: ``--undated LITERAL``; stands in for ``{date}`` in
            a group with no date, per 4.4.
        today: ``--today`` override for ``{today}``; ``date.today()`` when
            ``None``.
        companion_extensions: The already-``--companions``-extended set;
            :data:`DEFAULT_COMPANION_EXTENSIONS` when ``None``.
        managed_by: Copied verbatim into the plan (6.1); this function does
            not enforce the ``-w`` usage error it implies -- that is a CLI
            concern.
        photokin_version: Copied verbatim into the plan's
            ``photokin_version`` field.
        run_id: Overrides ``changeset.make_run_id()`` for a reproducible
            plan (tests; the CLI leaves this unset).

    Returns:
        The plan dict, shaped exactly as section 6.2 specifies
        (``schema_version`` 1). Errors do not raise -- they land in the
        plan's own ``errors`` list, so the caller can still show every group
        that *did* plan successfully; refusing to apply a plan that carries
        errors is the executor's job, not this function's.

    Raises:
        ValueError: *digits* is less than 1, or *order_mode* is neither
            ``"name"`` nor ``"natural"`` -- both are caller-contract
            violations, not data problems, so they raise rather than
            landing in ``errors``.
    """
    if digits < 1:
        raise ValueError(f"digits must be at least 1, got {digits}")
    if order_mode not in ("name", "natural"):
        raise ValueError(f"unknown order_mode {order_mode!r}")

    errors: list[str] = []
    warnings: list[str] = []
    left_behind: list[dict[str, Any]] = []

    # Absolute before anything derives from it (C9): a relative folder
    # (".", "scans") must not leak into plan["folder"] -- a later
    # --rename-finish run from a different working directory would resolve
    # it somewhere else entirely -- nor into {folder}, which needs the
    # folder's real name, not "." or "..".
    normalized_folder = os.path.normpath(os.path.abspath(folder))
    resolved_today = today if today is not None else date.today()  # noqa: DTZ011 (local run date)
    resolved_run_id = run_id if run_id is not None else make_run_id()
    effective_companions = (
        companion_extensions if companion_extensions is not None else DEFAULT_COMPANION_EXTENSIONS
    )

    if items and all(it.order is not None for it in items):
        order_label = "manifest"
        ordered_items = sorted(
            items, key=lambda it: (it.order if it.order is not None else 0, _name_key(it.path))
        )
    elif order_mode == "natural":
        order_label = "natural"
        ordered_items = sorted(items, key=lambda it: _natural_sort_key(os.path.basename(it.path)))
    else:
        order_label = "name"
        ordered_items = sorted(items, key=lambda it: _name_key(it.path))

    def _skeleton(plan_errors: list[str]) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": resolved_run_id,
            "photokin_version": photokin_version,
            "folder": normalized_folder,
            "prefix_template": prefix_template,
            "digits": digits,
            "order": order_label,
            "managed_by": managed_by,
            "entries": [],
            "left_behind": [],
            "warnings": [],
            "errors": plan_errors,
        }

    try:
        pieces = _tokenize_prefix_template(prefix_template)
    except ValueError as exc:
        return _skeleton([str(exc)])

    literal_error = _validate_literal_text(pieces, prefix_template)
    if literal_error:
        return _skeleton([literal_error])

    members = [_build_member(item, position) for position, item in enumerate(ordered_items)]

    for member in members:
        if member.version_error is not None:
            errors.append(f"{os.path.basename(member.item.path)}: {member.version_error}")

    groups: dict[str, list[_Member]] = {}
    for member in members:
        groups.setdefault(member.parsed.base_id.lower(), []).append(member)

    group_order = sorted(groups.keys(), key=lambda gk: min(m.position for m in groups[gk]))

    today_date = _PhotoDate(resolved_today.year, resolved_today.month, resolved_today.day, partial=False)
    group_plans: list[_GroupPlan] = []

    for group_key in group_order:
        group_members = sorted(groups[group_key], key=lambda m: m.position)
        display = group_members[0].parsed.base_id

        if not display:
            # A filename that is only a part suffix ("_back.tif" -> base_id
            # "") groups under the same empty key as any other such file.
            # Their targets do not collide (the part suffix differs), so
            # nothing is lost, but two unrelated photos silently becoming
            # one numbered object is worth flagging -- grouping itself is
            # unchanged, this only names what happened.
            member_names = ", ".join(sorted(os.path.basename(m.item.path) for m in group_members))
            warnings.append(f"group with an empty base id: {member_names}")

        bad_dirs = sorted({m.dirname for m in group_members if m.dirname != normalized_folder})
        if bad_dirs:
            errors.append(
                f"group '{display}': members sit in different folders "
                f"(expected '{normalized_folder}', also saw {', '.join(bad_dirs)})"
            )

        representative = min(
            group_members,
            key=lambda m: (
                utils.PART_RANK.get(m.final_parsed.part_kind, utils.UNRANKED_PART),
                m.position,
            ),
        )
        rep_date = representative.photo_date
        if rep_date is not None:
            disagreeing = [
                m
                for m in group_members
                if m is not representative
                and m.photo_date is not None
                and (m.photo_date.year, m.photo_date.month, m.photo_date.day)
                != (rep_date.year, rep_date.month, rep_date.day)
            ]
            if disagreeing:
                other_names = ", ".join(os.path.basename(m.item.path) for m in disagreeing)
                warnings.append(
                    f"group '{display}': date disagreement, using the date from "
                    f"'{os.path.basename(representative.item.path)}' (also saw a different "
                    f"date from {other_names})"
                )

        orig_value = _ORIG_STRIP_RE.sub("", display)
        try:
            prefix_before_trim, used_partial_date = _render_template(
                pieces,
                photo_date=rep_date,
                today=today_date,
                folder_name=os.path.basename(normalized_folder),
                orig=orig_value,
                undated_literal=undated_literal,
            )
        except _MissingDate:
            errors.append(
                f"group '{display}': no date available "
                "(pass --undated LITERAL to give it one)"
            )
            group_plans.append(_GroupPlan(group_key, display, group_members, None, False))
            continue

        # A leading '-' is trimmed the same way a trailing one already is:
        # {folder} at a drive root (os.path.basename of "C:\\" is "") renders
        # an empty {folder} value, so a template like "{folder}-bag" would
        # otherwise render the hostile prefix "-bag" -- a name starting with
        # "-" is section 4.4's "not a name" the same way an empty one is, and
        # is actively hostile to command-line tools that read a leading '-'
        # as a flag. A prefix that was nothing but dashes now strips to
        # empty and is caught by the empty check below, same as before.
        prefix = prefix_before_trim.strip("-")
        if prefix != prefix_before_trim:
            warnings.append(f"prefix '{prefix_before_trim}' trimmed to '{prefix}'")
        if not prefix:
            errors.append(f"group '{display}': rendered prefix is empty")
            group_plans.append(_GroupPlan(group_key, display, group_members, None, False))
            continue
        if prefix[-1].isdigit():
            warnings.append(f"prefix '{prefix}' ends in a digit")

        group_plans.append(_GroupPlan(group_key, display, group_members, prefix, used_partial_date))

    bucket_totals: dict[str, int] = {}
    for group_plan in group_plans:
        if group_plan.prefix is not None:
            bucket_key = group_plan.prefix.lower()
            bucket_totals[bucket_key] = bucket_totals.get(bucket_key, 0) + 1

    for bucket_key in sorted(bucket_totals):
        total = bucket_totals[bucket_key]
        if total >= 10**digits:
            errors.append(f"bucket '{bucket_key}' needs more than {digits} digits ({total} files)")

    entries: list[dict[str, Any]] = []
    bucket_counters: dict[str, int] = {}
    for group_plan in group_plans:
        if group_plan.prefix is None:
            for member in group_plan.members:
                entries.append(_build_unplannable_entry(member, group_plan.display))
            continue

        bucket_key = group_plan.prefix.lower()
        bucket_counters[bucket_key] = bucket_counters.get(bucket_key, 0) + 1
        number = bucket_counters[bucket_key]

        for member in group_plan.members:
            try:
                target_filename = utils.render_media_filename(
                    group_plan.prefix, number, digits, member.final_parsed, member.ext
                )
            except ValueError as exc:
                errors.append(f"{os.path.basename(member.item.path)}: {exc}")
                entries.append(_build_unplannable_entry(member, group_plan.display))
                continue

            target_stem = os.path.splitext(target_filename)[0]
            entry_notes = list(member.notes)
            if group_plan.used_partial_date:
                entry_notes.append("partial date")
            changed = os.path.basename(member.item.path) != target_filename

            entries.append(
                {
                    "path": member.item.path,
                    "photo_id": make_photo_id(member.item.path),
                    "size": member.item.size,
                    "mtime": member.item.mtime,
                    "target": target_filename,
                    "target_stem": target_stem,
                    "group": group_plan.display,
                    "prefix": group_plan.prefix,
                    "number": number,
                    "variant": member.final_parsed.variant_id,
                    "part": (
                        None
                        if member.final_parsed.part_kind == "none"
                        else member.final_parsed.part_kind
                    ),
                    "page": member.final_parsed.page_num,
                    "crop": member.final_parsed.is_crop,
                    "changed": changed,
                    "notes": entry_notes,
                    "companions": [],
                }
            )

    for entry in entries:
        for note in entry["notes"]:
            if note in ("part separator normalized", "variant form normalized"):
                warnings.append(f"{os.path.basename(entry['path'])}: {note}")

    bystander_names: set[str] = set()
    _attach_companions_and_bystanders(
        entries=entries,
        disk_files=sorted(disk_files, key=_name_key),
        normalized_folder=normalized_folder,
        known_image_paths=[os.path.normpath(os.path.abspath(m.item.path)) for m in members],
        companion_extensions=effective_companions,
        left_behind=left_behind,
        warnings=warnings,
        bystander_names=bystander_names,
    )

    _validate_targets(
        entries=entries, bystander_names=bystander_names, errors=errors
    )

    for entry in entries:
        if entry["target"] and len(entry["target"].encode("utf-8")) > 255:
            errors.append(f"{entry['target']}: name exceeds 255 bytes")
        # A companion's own target (target_stem + its own extension) is not
        # the same string as the image's target -- a longer companion
        # extension (".json" vs ".tif") can push it past the limit on its
        # own even when the image's target is within it, so each companion
        # target needs its own check rather than inheriting the image's.
        for companion in entry["companions"]:
            if len(companion["target"].encode("utf-8")) > 255:
                errors.append(f"{companion['target']}: name exceeds 255 bytes")

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": resolved_run_id,
        "photokin_version": photokin_version,
        "folder": normalized_folder,
        "prefix_template": prefix_template,
        "digits": digits,
        "order": order_label,
        "managed_by": managed_by,
        "entries": entries,
        "left_behind": left_behind,
        "warnings": warnings,
        "errors": errors,
    }


def _attach_companions_and_bystanders(
    *,
    entries: list[dict[str, Any]],
    disk_files: Sequence[str],
    normalized_folder: str,
    known_image_paths: list[str],
    companion_extensions: frozenset[str],
    left_behind: list[dict[str, Any]],
    warnings: list[str],
    bystander_names: set[str],
) -> None:
    """Match *disk_files* against *entries* by exact stem (4.3, 4.6).

    A non-image file whose stem exactly matches a renamed image's stem
    becomes that entry's companion when its extension is in
    *companion_extensions*, or a ``left_behind`` record naming the reason
    otherwise. When a companion's stem matches more than one entry -- the
    ordinary case being a same-stem image pair sharing one slot (4.3), e.g.
    ``photo.tif``/``photo.jpg``/``photo.md`` -- it needs exactly ONE owner:
    attaching it to every matching entry makes the same rendered companion
    target appear twice, which :func:`_validate_targets` then reports as a
    duplicate and refuses the whole plan (C6's regression is this exact
    folder). The owner is the matching entry whose extension sorts first
    (``.jpg`` before ``.tif``), tie-broken by path -- deterministic and
    independent of *entries*' or the filesystem's own ordering.

    An image file in *disk_files* that is not one of *entries*' own source
    paths is a bystander: its current name, case-folded, is added to
    *bystander_names*, so :func:`_validate_targets` can reject any rendered
    target that collides with it (4.6's "a target matching a bystander's
    current name"). Whether a disk path is one of *known_image_paths* is
    answered with :func:`utils.paths_are_same_file` (question 2: is this
    the same file, not merely the same spelling) -- a manifest's own
    spelling of a path and the spelling ``os.scandir`` returns for the
    identical file can differ only in case on Windows, and without that
    real-identity check the difference reads as two different files,
    making a file its own bystander and breaking idempotency. The exact
    (already-normalized) spelling is tried first, in a set, so this stays
    O(n) for the overwhelming common case where every path already agrees
    on spelling; the fallback only runs the pairwise check for whichever
    handful of paths do not match exactly.

    Mutates *entries* (each matched one's ``companions`` list),
    *left_behind*, *warnings* and *bystander_names* in place.
    """
    entry_indices_by_stem: dict[str, list[int]] = {}
    for idx, entry in enumerate(entries):
        stem = utils.casefold_filename(os.path.splitext(os.path.basename(entry["path"]))[0])
        entry_indices_by_stem.setdefault(stem, []).append(idx)

    known_exact = set(known_image_paths)

    for disk_path in disk_files:
        # norm_disk_path is what gets stored on companion/left_behind
        # records -- unchanged from before C9, so those still carry
        # whatever spelling the caller's listing used. abs_disk_path is
        # solely for comparing against normalized_folder and
        # known_image_paths, which are themselves absolute now (C9); a
        # third path spelling here would just make the comparison itself
        # wrong again.
        norm_disk_path = os.path.normpath(disk_path)
        abs_disk_path = os.path.normpath(os.path.abspath(disk_path))
        if os.path.dirname(abs_disk_path) != normalized_folder:
            continue
        name = os.path.basename(norm_disk_path)
        stem, ext = os.path.splitext(name)
        if ext.lower() in utils.VALID_EXTS:
            is_known = abs_disk_path in known_exact or any(
                utils.paths_are_same_file(abs_disk_path, known) for known in known_image_paths
            )
            if not is_known:
                bystander_names.add(utils.casefold_filename(name))
            continue
        matches = entry_indices_by_stem.get(utils.casefold_filename(stem), [])
        if not matches:
            continue
        matched_groups = {entries[i]["group"] for i in matches}
        if len(matched_groups) > 1:
            warnings.append(
                f"{name}: companion stem matches more than one group "
                f"({', '.join(sorted(matched_groups))})"
            )
        if ext.lower() in companion_extensions:
            plannable = [i for i in matches if entries[i]["target_stem"] is not None]
            if plannable:
                owner = min(
                    plannable,
                    key=lambda i: (os.path.splitext(entries[i]["path"])[1].lower(), entries[i]["path"]),
                )
                target_stem = entries[owner]["target_stem"]
                entries[owner]["companions"].append(
                    {"path": norm_disk_path, "target": target_stem + ext}
                )
        else:
            left_behind.append({"path": norm_disk_path, "reason": "extension outside companion set"})
            warnings.append(f"{name}: left behind (extension outside companion set)")


def _validate_targets(
    *,
    entries: list[dict[str, Any]],
    bystander_names: set[str],
    errors: list[str],
) -> None:
    """Duplicate-target and bystander-collision checks (4.6).

    Question (1), "do two target names collide" -- always
    ``utils.casefold_filename``, per the case-folding policy, so the plan is
    safe wherever it is later applied regardless of which OS planned it.
    """
    target_sources: dict[str, list[str]] = {}
    for entry in entries:
        if entry["target"] is not None:
            target_sources.setdefault(utils.casefold_filename(entry["target"]), []).append(
                entry["path"]
            )
        for companion in entry["companions"]:
            target_sources.setdefault(
                utils.casefold_filename(companion["target"]), []
            ).append(companion["path"])

    for target_key, sources in sorted(target_sources.items()):
        if len(sources) > 1:
            errors.append(f"duplicate target '{target_key}', from: {', '.join(sorted(sources))}")
        if target_key in bystander_names:
            errors.append(
                f"target '{target_key}' matches a bystander's current name, from: "
                f"{', '.join(sorted(sources))}"
            )
