"""A document with too many pages is refused before any model call (issue #26)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from photokin import core, utils
from photokin.core import build_manifest_buckets, oversized_page_sets
from photokin.tests.test_cli_input_surface import _CliTestCase, _StreamSpy


def _pages(stem: str, count: int) -> list[str]:
    return [f"{stem}-page{n}.jpg" for n in range(1, count + 1)]


def _buckets(*names: str) -> dict[str, list[dict]]:
    return build_manifest_buckets([{"path": n} for n in names], log_overrides=False)


def test_counts_distinct_pages_largest_first() -> None:
    # Arrange
    names = [*_pages("memoir", 4), "memoir-page4-crop.jpg", *_pages("letter", 3), "box3_025.jpg"]

    # Act
    oversized = oversized_page_sets(_buckets(*names), 2)

    # Assert
    assert oversized == [("memoir", 4), ("letter", 3)]


def test_zero_lifts_the_cap() -> None:
    # Act / Assert
    assert oversized_page_sets(_buckets(*_pages("memoir", 60)), 0) == []


def test_album_pages_do_not_count() -> None:
    # Act / Assert
    assert oversized_page_sets(_buckets(*_pages("photo-album", 60)), 50) == []


def test_the_stream_refuses_before_any_model_call() -> None:
    # Arrange
    manifest = {"items": [{"path": n} for n in _pages("memoir", 3)]}

    # Act / Assert
    with (
        patch("photokin.core.analyze_group_parts") as parts,
        pytest.raises(ValueError, match="'memoir' has 3 pages"),
    ):
        core.process_manifest_stream(manifest=manifest, cfg=utils.Config(max_group_pages=2))
    parts.assert_not_called()


class TestTheCliRefusesALongDocument(_CliTestCase):
    """The CLI says why and how to proceed, and never starts the batch."""

    def test_over_the_cap_is_refused_with_the_remedy(self) -> None:
        # Arrange
        folder = self.make_folder(*_pages("memoir", 3))
        stream = _StreamSpy()

        # Act
        with patch("photokin.cli.process_manifest_stream", stream):
            code, _stdout, stderr = self.run_cli([folder, "--max-group-pages", "2"])

        # Assert
        self.assertEqual(code, 2)
        self.assertFalse(stream.called)
        problem, remedy = self.usage_error(stderr)
        self.assertEqual(problem, "[ERROR] `memoir` has 3 pages, over the 2-page cap.")
        self.assertIn("--max-group-pages 0", remedy)

    def test_zero_lets_it_run(self) -> None:
        # Arrange
        folder = self.make_folder(*_pages("memoir", 3))
        stream = _StreamSpy()

        # Act
        with patch("photokin.cli.process_manifest_stream", stream):
            code, _stdout, _stderr = self.run_cli([folder, "--max-group-pages", "0"])

        # Assert
        self.assertIsNone(code)
        self.assertTrue(stream.called)
