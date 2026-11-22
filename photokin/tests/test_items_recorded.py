"""The stream counts manifest items apart from files (issue #26, part 4)."""

from __future__ import annotations

from photokin import core, utils
from photokin.tests.test_manifest_grouping import _recording


def test_items_sharing_a_path_count_once_as_files_and_each_as_items() -> None:
    # Arrange
    manifest = {"items": [{"path": "page-6.tif"}, {"path": "page-6.tif"}, {"path": "page-7.tif"}]}
    lines: list[str] = []

    # Act
    with _recording(None, real_patch_builder=False):
        data = core.process_manifest_stream(
            manifest=manifest, cfg=utils.Config(dry_run=True), ndjson_writer=lines.append
        )

    # Assert
    assert len(data["results"]) == 2
    assert data["items_recorded"] == len(lines) == 3
