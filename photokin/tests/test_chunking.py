"""Tests for :mod:`photokin.chunking`, the pure chunk partitioner (Phase 3).

Property-style where the property is the thing that actually matters:
``partition_parts`` must never drop, duplicate, or reorder a file, so most
cases here assert the flattened concatenation of the returned chunks equals
the input alongside whatever chunk *shape* the case is really about. See
``docs/document-mode-contract.md`` section 5 for the frozen rules this tests
against.
"""

from __future__ import annotations

import copy
import re

import pytest

from photokin.chunking import partition_parts

Part = tuple[str, list[str]]


def _pages(n: int, per_page: int = 1) -> list[Part]:
    """Build ``n`` page parts, each with ``per_page`` variant scan paths.

    Args:
        n: Number of page parts to build, 1-indexed in the returned labels.
        per_page: Number of file paths (variant scans) per page part.

    Returns:
        Ordered ``("Page N", [paths])`` parts, ``Page 1`` first.
    """
    return [
        (f"Page {i}", [f"page{i}v{v}.jpg" for v in range(per_page)])
        for i in range(1, n + 1)
    ]


def _flatten(chunks: list[list[Part]]) -> list[Part]:
    """Concatenate every chunk's parts back into one ordered list."""
    flat: list[Part] = []
    for chunk in chunks:
        flat.extend(chunk)
    return flat


def _all_paths(parts: list[Part]) -> list[str]:
    """Flatten every part's paths, in part order, into one list of filenames."""
    paths: list[str] = []
    for _, part_paths in parts:
        paths.extend(part_paths)
    return paths


def _as_multiset(parts: list[Part]) -> list[tuple[str, tuple[str, ...]]]:
    """Canonicalize parts into an order-independent, sortable representation.

    Used only to check that partitioning drops and duplicates nothing. It is
    deliberately order-insensitive: rule 4/5's chunk-1 budget reduction can
    (and, whenever a group both needs multiple page chunks and carries a
    non-page part, must -- see the module docstring) place ``others`` ahead
    of later page chunks in the flattened output, so flattened order is not
    guaranteed to match ``parts`` order once both conditions hold. What *is*
    guaranteed, and what this checks, is that every part shows up exactly
    once.
    """
    return sorted((label, tuple(paths)) for label, paths in parts)


# === The core invariant: nothing lost, nothing duplicated, nothing reordered ===

CONCATENATION_CASES: list[tuple[str, list[Part], int]] = [
    ("empty", [], 8),
    ("single small group", [("Front", ["f.jpg"]), ("Back", ["b.jpg"])], 8),
    ("20-page group", _pages(20), 8),
    ("63-page memoir", _pages(63), 8),
    (
        "pages plus front/back/negative",
        _pages(10) + [("Front", ["f.jpg"]), ("Back", ["b.jpg"]), ("Negative", ["n.jpg"])],
        8,
    ),
    ("oversized single part", _pages(5) + [("Page 6", [f"p6v{i}.jpg" for i in range(12)])], 8),
    ("chunk_size zero", _pages(20), 0),
    ("chunk_size negative", _pages(20), -3),
    ("chunk_size one", _pages(5), 1),
    ("only front/back, over budget", [("Front", ["f.jpg"] * 5), ("Back", ["b.jpg"] * 5)], 8),
]


@pytest.mark.parametrize("label, parts, chunk_size", CONCATENATION_CASES, ids=[c[0] for c in CONCATENATION_CASES])
def test_concatenation_equals_input(label: str, parts: list[Part], chunk_size: int) -> None:
    """Flattening the returned chunks always reproduces the input's parts.

    This is the invariant the whole feature rests on: whatever partitioning
    strategy is used, no file is ever dropped or duplicated by it. Compared
    as a multiset (see ``_as_multiset``) rather than a strict sequence,
    because rule 4/5's chunk-1 budget reduction legitimately moves a
    non-page part ahead of a later page chunk in the flattened output.
    """
    result = partition_parts(parts, chunk_size)
    assert _as_multiset(_flatten(result)) == _as_multiset(parts)


# === Specific shapes the contract calls out by name ===


def test_ordinary_group_is_one_chunk() -> None:
    """chunk_size <= 0, total <= chunk_size, or no page parts -> one chunk.

    This is what keeps an ordinary front/back group's call sequence
    byte-identical to today's behavior (D3).
    """
    parts: list[Part] = [("Front", ["f.jpg"]), ("Back", ["b.jpg"])]
    assert partition_parts(parts, 8) == [parts]


def test_zero_chunk_size_disables_chunking_at_any_size() -> None:
    """--max-images-per-call 0 restores single-call behavior at any size."""
    parts = _pages(63)
    assert partition_parts(parts, 0) == [parts]


def test_group_with_no_pages_is_never_chunked() -> None:
    """A part-only-of-variants group is never split: there is no safe boundary."""
    parts: list[Part] = [
        ("Front", [f"f{i}.jpg" for i in range(6)]),
        ("Back", [f"b{i}.jpg" for i in range(6)]),
    ]
    assert partition_parts(parts, 8) == [parts]


def test_20_page_group_splits_into_3_contiguous_chunks() -> None:
    """20 pages at chunk_size 8 -> 8, 8, 4, each block contiguous."""
    parts = _pages(20)
    result = partition_parts(parts, 8)
    assert len(result) == 3
    assert [label for label, _ in result[0]] == [f"Page {i}" for i in range(1, 9)]
    assert [label for label, _ in result[1]] == [f"Page {i}" for i in range(9, 17)]
    assert [label for label, _ in result[2]] == [f"Page {i}" for i in range(17, 21)]
    assert [len(_all_paths(chunk)) for chunk in result] == [8, 8, 4]


def test_63_page_memoir_shape() -> None:
    """The case the whole feature exists for: 63 pages at chunk_size 8 -> 8 chunks."""
    parts = _pages(63)
    result = partition_parts(parts, 8)
    assert len(result) == 8
    for chunk in result[:-1]:
        assert len(_all_paths(chunk)) == 8
    assert len(_all_paths(result[-1])) == 7
    # Every page appears exactly once, in order, across the whole run.
    assert [label for chunk in result for label, _ in chunk] == [f"Page {i}" for i in range(1, 64)]


def test_front_back_pair_at_boundary_stays_in_one_chunk() -> None:
    """A front/back pair rides chunk 1 whole, however the pages are packed."""
    parts = _pages(16) + [("Front", ["f.jpg"]), ("Back", ["b.jpg"])]
    result = partition_parts(parts, 8)
    labels_in_chunk_1 = [label for label, _ in result[0]]
    assert "Front" in labels_in_chunk_1
    assert "Back" in labels_in_chunk_1
    assert all("Front" not in [lbl for lbl, _ in chunk] for chunk in result[1:])
    assert all("Back" not in [lbl for lbl, _ in chunk] for chunk in result[1:])


def test_oversized_part_becomes_its_own_chunk() -> None:
    """A part with 12 variants at chunk_size 8 becomes its own chunk, unsplit."""
    big_part: Part = ("Page 6", [f"p6v{i}.jpg" for i in range(12)])
    parts = _pages(5) + [big_part]
    result = partition_parts(parts, 8)
    oversized_chunks = [chunk for chunk in result if any(label == "Page 6" for label, _ in chunk)]
    assert len(oversized_chunks) == 1
    assert oversized_chunks[0] == [big_part]


def test_oversized_part_mid_sequence_does_not_leak_into_neighbors() -> None:
    """An oversized part in the middle of the page run still isolates cleanly."""
    big_part: Part = ("Page 3", [f"p3v{i}.jpg" for i in range(12)])
    parts: list[Part] = [
        ("Page 1", ["p1.jpg"]),
        ("Page 2", ["p2.jpg"]),
        big_part,
        ("Page 4", ["p4.jpg"]),
        ("Page 5", ["p5.jpg"]),
    ]
    result = partition_parts(parts, 8)
    assert _flatten(result) == parts
    chunk_with_big = next(chunk for chunk in result if big_part in chunk)
    assert chunk_with_big == [big_part]


def test_chunk_size_0_negative_and_1() -> None:
    """chunk_size 0 and negative both disable chunking; 1 forces one page per chunk."""
    parts = _pages(5)
    assert partition_parts(parts, 0) == [parts]
    assert partition_parts(parts, -1) == [parts]
    result = partition_parts(parts, 1)
    assert len(result) == 5
    assert all(len(chunk) == 1 for chunk in result)


def test_pages_plus_all_non_page_parts_ride_chunk_1_in_source_order() -> None:
    """Front, Back, and Negative are all in chunk 1, in source order, with >=1 page."""
    parts = _pages(10) + [
        ("Front", ["f.jpg"]),
        ("Back", ["b.jpg"]),
        ("Negative", ["n.jpg"]),
    ]
    result = partition_parts(parts, 8)
    chunk_1_labels = [label for label, _ in result[0]]
    assert chunk_1_labels.index("Front") < chunk_1_labels.index("Back") < chunk_1_labels.index("Negative")
    page_label = re.compile(r"^Page\s+\d+$", re.IGNORECASE)
    assert any(page_label.match(label) for label in chunk_1_labels)


def test_only_front_back_variants_over_budget_is_one_chunk_not_a_bug() -> None:
    """A no-page group over budget is documented as one chunk, not chunked.

    There is no safe boundary in a group with no page parts -- splitting
    Front/Back/Negative apart would separate views of the same physical
    object -- so rule 1 opts the whole group out of chunking regardless of
    how many variant images it holds.
    """
    parts: list[Part] = [
        ("Front", [f"f{i}.jpg" for i in range(6)]),
        ("Back", [f"b{i}.jpg" for i in range(6)]),
    ]
    result = partition_parts(parts, 8)
    assert result == [parts]
    assert len(result) == 1


def test_empty_parts_list() -> None:
    """An empty group partitions to a single empty chunk."""
    assert partition_parts([], 8) == [[]]


# === Determinism and non-mutation ===


def test_determinism_same_input_same_output() -> None:
    """Calling twice on equal input produces equal output."""
    parts = _pages(20) + [("Front", ["f.jpg"]), ("Back", ["b.jpg"])]
    first = partition_parts(parts, 8)
    second = partition_parts(copy.deepcopy(parts), 8)
    assert first == second


def test_input_is_not_mutated() -> None:
    """The caller's parts list, and its nested path lists, are left untouched."""
    parts: list[Part] = _pages(20) + [("Front", ["f.jpg"]), ("Back", ["b.jpg"])]
    original = copy.deepcopy(parts)
    partition_parts(parts, 8)
    assert parts == original
