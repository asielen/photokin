"""Tests for :mod:`photokin.doc_sidecar`, the markdown sidecar writer.

Golden-file style where it pins the layout: a front/back pair and one page
of a multipage group get their whole rendered file asserted, so the body and
frontmatter shape is pinned rather than merely described. See
``docs/document-mode-contract.md`` section 4 for the frozen rules this tests
against.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from photokin import utils
from photokin.doc_sidecar import SidecarContext, write_markdown_sidecar


def _config(**overrides: Any) -> utils.Config:
    """Build a minimal ``Config`` for a test, provider defaulted to Claude."""
    base = utils.Config(provider="anthropic")
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _item(path: str) -> dict[str, Any]:
    """Build the minimal manifest grouping entry ``write_markdown_sidecar`` reads."""
    return {"path": path}


def _read(path: str | None) -> str:
    """Read back a written sidecar, asserting a path was actually returned."""
    assert path is not None
    return Path(path).read_text(encoding="utf-8")


# --- Golden files ------------------------------------------------------


def test_front_back_pair_front_file(tmp_path: Path) -> None:
    """A front/back pair's front file: single-part transcription, full frontmatter."""
    image_path = str(tmp_path / "IMG_0001.jpg")
    merged: dict[str, Any] = {
        "title": "Letter to Mother",
        "category": "Document",
        "keywords": ["Document", "1944", "Claude claude-sonnet-4-6 Analyzed"],
        "ai_caption": "[AI Analysis on 2026-08-27]: A handwritten letter on lined paper.",
        "caption": "Dear Mother,\n\nWe arrived safely.",
        "transcriptions": {
            "Front": "Dear Mother,\n\nWe arrived safely.",
            "Back": "Written on the reverse in pencil.",
        },
        "date_guess": {"iso": "1944-06-01", "pattern": "Y!M!D!", "confidence": 0.85},
        "location_guess": {
            "country": "France",
            "state": None,
            "city": "Le Mans",
            "sublocation": None,
            "confidence": 0.9,
        },
        "_usage": {"model": "claude-sonnet-4-6"},
    }
    group_info = SidecarContext(
        group_id="IMG_0001",
        part_label="Front",
        group_files=("IMG_0001.jpg", "IMG_0002.jpg"),
        page_count=None,
        page_number=None,
    )

    result = write_markdown_sidecar(merged, _item(image_path), group_info, _config())

    assert result == str(tmp_path / "IMG_0001.md")
    content = _read(result)
    expected = (
        "---\n"
        'source_file: "IMG_0001.jpg"\n'
        'group: "IMG_0001"\n'
        'part: "Front"\n'
        'group_files: ["IMG_0001.jpg", "IMG_0002.jpg"]\n'
        'title: "Letter to Mother"\n'
        'category: "Document"\n'
        'keywords: ["Document", "1944", "Claude claude-sonnet-4-6 Analyzed"]\n'
        'date: "1944-06-01"\n'
        'date_pattern: "Y!M!D!"\n'
        "date_confidence: 0.85\n"
        'location: {country: "France", city: "Le Mans", confidence: 0.9}\n'
        'analyzed_by: "Claude claude-sonnet-4-6 (2026-08-27)"\n'
        "---\n"
        "\n"
        "# Letter to Mother\n"
        "\n"
        "[AI Analysis on 2026-08-27]: A handwritten letter on lined paper.\n"
        "\n"
        "## Transcription — Front\n"
        "\n"
        "Dear Mother,\n"
        "\n"
        "We arrived safely.\n"
    )
    assert content == expected


def test_front_back_pair_back_file(tmp_path: Path) -> None:
    """The same group's back file: same frontmatter shape, its own part/transcription."""
    image_path = str(tmp_path / "IMG_0002.jpg")
    merged: dict[str, Any] = {
        "title": "Letter to Mother",
        "category": "Document",
        "keywords": ["Document"],
        "ai_caption": None,
        "caption": "Dear Mother,\n\nWe arrived safely.\n\nWritten on the reverse in pencil.",
        "transcriptions": {
            "Front": "Dear Mother,\n\nWe arrived safely.",
            "Back": "Written on the reverse in pencil.",
        },
        "date_guess": {},
        "location_guess": {},
        "_usage": {},
    }
    group_info = SidecarContext(
        group_id="IMG_0001",
        part_label="Back",
        group_files=("IMG_0001.jpg", "IMG_0002.jpg"),
        page_count=None,
        page_number=None,
    )

    result = write_markdown_sidecar(
        merged, _item(image_path), group_info, _config(model="gpt-4o-mini")
    )

    content = _read(result)
    # ai_caption is None above (no dated prefix to parse), so analyzed_by falls
    # back to date.today() per the contract -- assert against the real clock
    # rather than a literal so the test stays green across a day rollover.
    from datetime import date as real_date

    expected = (
        "---\n"
        'source_file: "IMG_0002.jpg"\n'
        'group: "IMG_0001"\n'
        'part: "Back"\n'
        'group_files: ["IMG_0001.jpg", "IMG_0002.jpg"]\n'
        'title: "Letter to Mother"\n'
        'category: "Document"\n'
        'keywords: ["Document"]\n'
        f'analyzed_by: "Claude claude-sonnet-4-6 ({real_date.today().isoformat()})"\n'  # noqa: DTZ011
        "---\n"
        "\n"
        "# Letter to Mother\n"
        "\n"
        "## Transcription — Back\n"
        "\n"
        "Written on the reverse in pencil.\n"
    )
    assert content == expected
    # _usage carried no model, so analyzed_by fell back to the provider's
    # resolved model, not the (unrelated, OpenAI-shaped) config.model above.
    assert 'analyzed_by: "Claude claude-sonnet-4-6' in content


def test_multipage_page_with_correction(tmp_path: Path) -> None:
    """One page of a multipage group: page_order correction, flags, page_count."""
    image_path = str(tmp_path / "letter_p3.jpg")
    merged: dict[str, Any] = {
        "title": None,
        "category": "Document",
        "keywords": [],
        "ai_caption": "",
        "caption": "irrelevant on the attributed path",
        "transcriptions": {
            "Page 1": "First page text.",
            "Page 2": "Second page text.",
            "Page 3": "Third page text, filed out of order.",
        },
        "page_order": {
            "Page 3": {"page": 2, "flags": ["out_of_order"]},
        },
        "date_guess": {"iso": None, "pattern": None, "confidence": None},
        "location_guess": None,
        "_usage": {"model": "claude-haiku-4-5"},
    }
    group_info = SidecarContext(
        group_id="letter",
        part_label="Page 3",
        group_files=("letter_p1.jpg", "letter_p2.jpg", "letter_p3.jpg"),
        page_count=3,
        page_number=3,
    )

    result = write_markdown_sidecar(merged, _item(image_path), group_info, _config())

    content = _read(result)
    # ai_caption is "" above (no dated prefix to parse), so analyzed_by falls
    # back to date.today() per the contract -- assert against the real clock
    # rather than a literal so the test stays green across a day rollover.
    from datetime import date as real_date

    expected = (
        "---\n"
        'source_file: "letter_p3.jpg"\n'
        'group: "letter"\n'
        'part: "Page 3"\n'
        "page: 2\n"
        "page_from_filename: 3\n"
        'page_order_flags: ["out_of_order"]\n'
        "page_count: 3\n"
        'group_files: ["letter_p1.jpg", "letter_p2.jpg", "letter_p3.jpg"]\n'
        'category: "Document"\n'
        f'analyzed_by: "Claude claude-haiku-4-5 ({real_date.today().isoformat()})"\n'  # noqa: DTZ011
        "---\n"
        "\n"
        "# letter\n"
        "\n"
        "## Transcription — Page 3\n"
        "\n"
        "Third page text, filed out of order.\n"
    )
    assert content == expected


# --- Fallback path -------------------------------------------------------


def test_fallback_when_no_transcriptions_key(tmp_path: Path) -> None:
    """No ``transcriptions`` on the record: whole caption block, bare heading, scope key."""
    image_path = str(tmp_path / "IMG_0003.jpg")
    merged: dict[str, Any] = {
        "title": "Old Photo",
        "caption": "A caption written before transcriptions existed.",
        "ai_caption": "[AI Analysis on 2025-01-01]: An older analysis.",
        "_usage": {"model": "gpt-4o"},
    }
    group_info = SidecarContext(
        group_id="IMG_0003",
        part_label="Front",
        group_files=("IMG_0003.jpg",),
        page_count=None,
        page_number=None,
    )

    result = write_markdown_sidecar(merged, _item(image_path), group_info, _config())

    content = _read(result)
    assert 'transcription_scope: "group"\n' in content
    assert "\n## Transcription\n\n" in content
    assert "## Transcription —" not in content
    assert content.endswith("A caption written before transcriptions existed.\n")


def test_fallback_when_part_label_not_in_map(tmp_path: Path) -> None:
    """``transcriptions`` present but missing this file's resolved label: same fallback."""
    image_path = str(tmp_path / "IMG_0004.jpg")
    merged: dict[str, Any] = {
        "title": "Displaced Scan",
        "caption": "Group caption text.",
        "ai_caption": None,
        "transcriptions": {"Front": "Only the front was ever transcribed."},
        "_usage": {"model": "gpt-4o"},
    }
    # This file's resolved part label ("Back") never made it into the payload
    # under that label -- e.g. a displaced/unseated variant (contract sec 2).
    group_info = SidecarContext(
        group_id="IMG_0004",
        part_label="Back",
        group_files=("IMG_0004.jpg",),
        page_count=None,
        page_number=None,
    )

    result = write_markdown_sidecar(merged, _item(image_path), group_info, _config())

    content = _read(result)
    assert 'transcription_scope: "group"\n' in content
    assert "\n## Transcription\n\nGroup caption text.\n" in content


# --- Omission of every optional key ---------------------------------------


def test_omits_every_optional_key_when_absent(tmp_path: Path) -> None:
    """A record with none of the optional fields emits none of their keys."""
    image_path = str(tmp_path / "bare.jpg")
    merged: dict[str, Any] = {
        "title": None,
        "category": None,
        "keywords": [],
        "ai_caption": None,
        "caption": "",
        # A transcriptions map that DOES cover this part label, so the
        # attributed path is taken and transcription_scope stays absent too
        # -- this test is about the optional frontmatter keys, not the
        # fallback path (covered separately below).
        "transcriptions": {"Front": "Bare transcription text."},
        "date_guess": {},
        "location_guess": {},
        "_usage": {},
    }
    group_info = SidecarContext(
        group_id="bare",
        part_label="Front",
        group_files=(),
        page_count=None,
        page_number=None,
    )

    result = write_markdown_sidecar(merged, _item(image_path), group_info, _config())

    content = _read(result)
    for absent_key in (
        "page:",
        "page_from_filename:",
        "page_order_flags:",
        "page_count:",
        "group_files:",
        "title:",
        "category:",
        "keywords:",
        "date:",
        "date_pattern:",
        "date_confidence:",
        "location:",
        "transcription_scope:",
    ):
        assert absent_key not in content, f"unexpected key present: {absent_key}"
    # The always-present keys are still there.
    assert 'source_file: "bare.jpg"' in content
    assert 'group: "bare"' in content
    assert 'part: "Front"' in content
    assert "analyzed_by:" in content
    # Null title falls back to the group id in the heading.
    assert content.startswith("---\n") and "\n# bare\n" in content


def test_location_map_disappears_when_all_members_null(tmp_path: Path) -> None:
    """A location_guess whose every member is null omits the whole ``location`` key."""
    image_path = str(tmp_path / "no_loc.jpg")
    merged: dict[str, Any] = {
        "title": "Photo",
        "location_guess": {
            "country": None,
            "state": None,
            "city": None,
            "sublocation": None,
            "confidence": None,
        },
        "_usage": {"model": "m"},
    }
    group_info = SidecarContext(
        group_id="no_loc", part_label="Front", group_files=(), page_count=None, page_number=None
    )

    content = _read(
        write_markdown_sidecar(merged, _item(image_path), group_info, _config())
    )

    assert "location:" not in content


# --- analyzed_by ------------------------------------------------------


def test_analyzed_by_date_from_ai_caption_not_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The analysis date comes from ai_caption's dated prefix, not date.today()."""
    import photokin.doc_sidecar as doc_sidecar_module

    class _FrozenToday:
        @staticmethod
        def isoformat() -> str:
            raise AssertionError("date.today() must not be consulted when ai_caption has a date")

    class _FrozenDate:
        @staticmethod
        def today() -> _FrozenToday:
            return _FrozenToday()

    monkeypatch.setattr(doc_sidecar_module, "date", _FrozenDate)

    image_path = str(tmp_path / "dated.jpg")
    merged: dict[str, Any] = {
        "title": "Dated",
        "ai_caption": "[AI Analysis on 1999-12-31]: Old analysis text.",
        "_usage": {"model": "claude-sonnet-4-6"},
    }
    group_info = SidecarContext(
        group_id="dated", part_label="Front", group_files=(), page_count=None, page_number=None
    )

    content = _read(
        write_markdown_sidecar(merged, _item(image_path), group_info, _config())
    )

    assert 'analyzed_by: "Claude claude-sonnet-4-6 (1999-12-31)"' in content


def test_analyzed_by_falls_back_to_today_without_dated_prefix(tmp_path: Path) -> None:
    """No dated prefix in ai_caption: falls back to today's date."""
    image_path = str(tmp_path / "undated.jpg")
    merged: dict[str, Any] = {
        "title": "Undated",
        "ai_caption": "[AI Analysis]: no date on this one.",
        "_usage": {"model": "m"},
    }
    group_info = SidecarContext(
        group_id="undated", part_label="Front", group_files=(), page_count=None, page_number=None
    )

    content = _read(
        write_markdown_sidecar(merged, _item(image_path), group_info, _config())
    )

    from datetime import date as real_date

    assert f"({real_date.today().isoformat()})" in content  # noqa: DTZ011


def test_analyzed_by_falls_back_to_resolved_model_when_usage_absent(tmp_path: Path) -> None:
    """No ``_usage.model`` on the record: falls back to resolve_model_for_provider."""
    image_path = str(tmp_path / "nousage.jpg")
    merged: dict[str, Any] = {"title": "No usage"}
    group_info = SidecarContext(
        group_id="nousage", part_label="Front", group_files=(), page_count=None, page_number=None
    )

    content = _read(
        write_markdown_sidecar(
            merged, _item(image_path), group_info, _config(claude_model_name="haiku")
        )
    )

    assert 'analyzed_by: "Claude claude-haiku-4-5-20251001' in content


# --- Failure contract ---------------------------------------------------


def test_unwritable_destination_returns_none_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An ``OSError`` from ``open`` is caught, logged at WARNING, never raised."""
    import photokin.doc_sidecar as doc_sidecar_module

    def _raise_os_error(*args: Any, **kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(doc_sidecar_module, "open", _raise_os_error, raising=False)

    image_path = str(tmp_path / "unwritable.jpg")
    merged: dict[str, Any] = {"title": "Unwritable"}
    group_info = SidecarContext(
        group_id="unwritable", part_label="Front", group_files=(), page_count=None, page_number=None
    )

    with caplog.at_level(logging.WARNING, logger="photokin.doc_sidecar"):
        result = write_markdown_sidecar(merged, _item(image_path), group_info, _config())

    assert result is None
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "unwritable.jpg" in caplog.text


def test_unicode_encode_error_from_a_lone_surrogate_returns_none_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A lone surrogate in a transcription is caught, not left to fail the group.

    A model-written transcription can carry a lone UTF-16 surrogate (from a
    provider's own text handling upstream), and ``open(...).write()`` raises
    ``UnicodeEncodeError`` -- a ``ValueError``, not an ``OSError`` -- when it
    hits one. An ``OSError``-only guard let that sail straight past this
    function into the batch loop's per-group exception handler, discarding
    the paid-for analysis and writing an error payload for every file of the
    group, not just the one whose transcription happened to carry the
    surrogate.
    """
    image_path = str(tmp_path / "surrogate.jpg")
    merged: dict[str, Any] = {
        "title": "Surrogate",
        "transcriptions": {"Front": "before \ud800 after"},
        "_usage": {"model": "m"},
    }
    group_info = SidecarContext(
        group_id="surrogate", part_label="Front", group_files=(), page_count=None, page_number=None
    )

    with caplog.at_level(logging.WARNING, logger="photokin.doc_sidecar"):
        result = write_markdown_sidecar(merged, _item(image_path), group_info, _config())

    assert result is None
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "surrogate.jpg" in caplog.text


# --- YAML escaping --------------------------------------------------------


def test_hostile_yaml_string_escapes_correctly(tmp_path: Path) -> None:
    """A hostile title with quotes, backslash, hash, dash, newline, and non-ASCII."""
    hostile = (
        'a "quote": with \\ backslash, #hash, - dash, a\nnewline, '
        "an em—dash and a right single quotation mark’"
    )
    image_path = str(tmp_path / "hostile.jpg")
    merged: dict[str, Any] = {"title": hostile, "_usage": {"model": "m"}}
    group_info = SidecarContext(
        group_id="hostile", part_label="Front", group_files=(), page_count=None, page_number=None
    )

    content = _read(
        write_markdown_sidecar(merged, _item(image_path), group_info, _config())
    )

    expected_escaped = (
        'a \\"quote\\": with \\\\ backslash, #hash, - dash, a\\nnewline, '
        "an em—dash and a right single quotation mark’"
    )
    assert f'title: "{expected_escaped}"' in content

    pytest.importorskip("yaml")
    import yaml  # type: ignore[import-untyped]

    frontmatter_text = content.split("---\n", 2)[1]
    parsed = yaml.safe_load(frontmatter_text)
    assert parsed["title"] == hostile


def test_control_character_escaped_as_unicode_and_round_trips() -> None:
    """A raw C0 control character (not \\n/\\t/\\r) round-trips through \\uXXXX."""
    from photokin.doc_sidecar import _yaml_string

    value = "before\x07after"  # BEL, 0x07
    rendered = _yaml_string(value)

    assert rendered == '"before\\u0007after"'

    pytest.importorskip("yaml")
    import yaml

    assert yaml.safe_load(rendered) == value


def test_backslash_immediately_before_escapable_char_is_not_conflated() -> None:
    """A literal backslash followed by a literal 'n' is not read as a real newline escape."""
    from photokin.doc_sidecar import _yaml_string

    value = "literal backslash-n: \\n (not a newline)"
    rendered = _yaml_string(value)

    # The backslash is escaped on its own; the following "n" is untouched.
    assert rendered == '"literal backslash-n: \\\\n (not a newline)"'

    pytest.importorskip("yaml")
    import yaml

    assert yaml.safe_load(rendered) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "plain",
        'quote " inside',
        "tab\there",
        "carriage\rreturn",
        "back\\slash",
        "#leading hash and - leading dash",
        "emoji-free em—dash and curly ’ quote",
        "multi\nline\nvalue",
    ],
)
def test_yaml_string_round_trips_hostile_values(value: str) -> None:
    """Every hostile scalar this module might emit parses back to itself."""
    from photokin.doc_sidecar import _yaml_string

    pytest.importorskip("yaml")
    import yaml

    assert yaml.safe_load(_yaml_string(value)) == value
