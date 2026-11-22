"""A document page's own fields never reach another page (issue #26)."""

from __future__ import annotations

import json
import os

from photokin import core, utils
from photokin.tests.test_manifest_grouping import _recording

_PAGE_ONE_META = {
    "title": "Fire Chief and Inspector Dodson",
    "keywords": ["Ruth"],
    "dateTimeOriginal": "1916:08:17 00:00:00",
    "city": "San Diego",
}


def _results(items: list[dict]) -> dict[str, dict]:
    """Run *items* through the stream and return ``{basename: result}``."""
    lines: list[str] = []
    with _recording(["Letter"]):
        core.process_manifest_stream(
            manifest={"items": items}, cfg=utils.Config(dry_run=True), ndjson_writer=lines.append
        )
    records = (json.loads(line) for line in lines)
    return {os.path.basename(r["path"]): r["result"] for r in records}


def test_a_pages_own_fields_stay_on_that_page() -> None:
    # Arrange
    items = [
        {"path": "letter-page1.tif", "metadata": _PAGE_ONE_META},
        {"path": "letter-page2.tif", "metadata": {}},
    ]

    # Act
    results = _results(items)

    # Assert
    page_one, page_two = results["letter-page1.tif"], results["letter-page2.tif"]
    assert "Ruth" in page_one["keywords"]
    assert "Ruth" not in page_two["keywords"]
    assert "Letter" in page_two["keywords"]
    assert page_one.get("title") == _PAGE_ONE_META["title"]
    assert page_two.get("title") != _PAGE_ONE_META["title"]
    assert "San Diego" not in json.dumps(page_two)
    assert "1916" not in json.dumps(page_two)


def test_a_back_still_takes_its_fronts_fields() -> None:
    # Arrange
    items = [
        {"path": "box3_017.tif", "metadata": _PAGE_ONE_META},
        {"path": "box3_017-back.tif", "metadata": {}},
    ]

    # Act
    results = _results(items)

    # Assert
    back = results["box3_017-back.tif"]
    assert "Ruth" in back["keywords"]
    assert back.get("title") == _PAGE_ONE_META["title"]
