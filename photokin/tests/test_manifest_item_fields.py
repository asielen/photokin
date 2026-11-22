"""Per-item id, read, write, sidecar_path and source_path (issue #26, part 3)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from photokin import core, utils
from photokin.exiftool import ExiftoolConfig, apply_changeset
from photokin.exiftool.hydrate import hydrate_item_metadata
from photokin.tests.test_manifest_grouping import _recording


def _run(items: list[dict], cfg: utils.Config | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """Stream *items*; return the item records, changeset records and progress events."""
    lines: list[str] = []
    changeset: list[str] = []
    events: list[dict] = []
    with _recording(["Letter"], real_patch_builder=True):
        core.process_manifest_stream(
            manifest={"items": items},
            cfg=cfg or utils.Config(dry_run=True),
            ndjson_writer=lines.append,
            changeset_writer=changeset.append,
            changeset_run_id="run",
            run_event_writer=events.append,
        )
    return [json.loads(x) for x in lines], [json.loads(x) for x in changeset], events


def test_id_and_source_path_come_back_on_every_record() -> None:
    # Arrange
    items: list[dict] = [
        {"path": "crop-a.jpg", "id": "vc-1", "source_path": "album-page-006.tif", "group": "a"},
        {"path": "crop-b.jpg", "id": 2, "group": "b"},
    ]

    # Act
    records, changeset, events = _run(items)

    # Assert
    assert [(r["id"], r.get("source_path")) for r in records] == [
        ("vc-1", "album-page-006.tif"),
        (2, None),
    ]
    assert [(c["id"], c.get("source_path")) for c in changeset] == [
        ("vc-1", "album-page-006.tif"),
        (2, None),
    ]
    assert [e["ids"] for e in events] == [["vc-1"], [2]]


def test_an_ordinary_item_gains_no_new_keys() -> None:
    # Act
    records, changeset, events = _run([{"path": "box3_025.jpg"}])

    # Assert
    assert not {"id", "source_path", "write"} & (records[0].keys() | changeset[0].keys())
    assert "ids" not in events[0]


def test_write_false_is_reported_with_its_patch() -> None:
    # Act
    records, changeset, _events = _run([{"path": "crop.jpg", "write": "false"}])

    # Assert
    assert records[0]["write"] is False
    assert records[0]["patch"]
    assert changeset[0]["write"] is False
    assert changeset[0]["proposed_changes"]["keywords_add"]


def test_the_applier_leaves_a_write_false_record_alone(tmp_path: Path) -> None:
    # Arrange
    changeset = tmp_path / "changeset.ndjson"
    record = {"path": "/x/crop.jpg", "write": False, "proposed_changes": {"set": {"XMP-dc:Title": "T"}}}
    changeset.write_text(json.dumps(record) + "\n", encoding="utf-8")

    # Act
    with (
        patch("photokin.exiftool.apply.resolve_exiftool_path", return_value="/fake/exiftool"),
        patch("photokin.exiftool.apply.subprocess.run") as run,
    ):
        summary = apply_changeset(str(changeset), ExiftoolConfig(enabled=True))

    # Assert
    run.assert_not_called()
    assert (summary["files_written"], summary["files_with_proposals"]) == (0, 0)
    assert summary["files_left_to_caller"] == 1


def test_write_false_gets_no_sidecar_unless_it_names_one(tmp_path: Path) -> None:
    # Arrange
    named = tmp_path / "transcripts" / "page6.md"
    named.parent.mkdir()
    items = [
        {"path": str(tmp_path / "temp-a.jpg"), "write": False, "group": "a"},
        {"path": str(tmp_path / "temp-b.jpg"), "write": False, "group": "b", "sidecar_path": str(named)},
    ]

    # Act
    _run(items, utils.Config(dry_run=True, sidecar_md=utils.SIDECAR_MD_ALL))

    # Assert
    assert not (tmp_path / "temp-a.md").exists()
    assert not (tmp_path / "temp-b.md").exists()
    assert named.read_text(encoding="utf-8").startswith("---\n")


def test_read_false_keeps_the_hydrator_off_the_file() -> None:
    # Arrange
    items: list[dict] = [
        {"path": os.path.abspath("export.jpg"), "read": False, "metadata": {"title": "From catalog"}},
        {"path": os.path.abspath("scan.tif")},
    ]
    read = MagicMock(return_value=[])

    # Act
    with (
        patch("photokin.exiftool.hydrate.resolve_exiftool_path", return_value="/fake/exiftool"),
        patch("photokin.exiftool.manifest.run_exiftool_json", read),
    ):
        hydrate_item_metadata(items, ExiftoolConfig())

    # Assert
    assert read.call_args.kwargs["files"] == [os.path.abspath("scan.tif")]
    assert items[0]["metadata"] == {"title": "From catalog"}
    assert utils.HYDRATION_FAILED_KEY not in items[0]


def test_one_group_per_crop_keeps_crops_apart() -> None:
    # Arrange
    items = [{"path": f"page-006-copy{n}.jpg", "group": f"vc-{n}"} for n in (1, 2, 3)]

    # Act
    buckets = core.build_manifest_buckets(items, log_overrides=False)

    # Assert
    assert {key: len(entries) for key, entries in buckets.items()} == {
        "vc-1": 1,
        "vc-2": 1,
        "vc-3": 1,
    }

