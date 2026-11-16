"""Scans of things attached to a photo group with the photo.

A studio folder's cover, a card mount, the envelope a print was kept in, a
clipped note: each is scanned on its own, and each is evidence about the photo
(a studio imprint dates and places a portrait). They group with the photo by a
closed list of trailing words, so an arbitrary trailing word still stays part
of the name. ``-bw`` (a black-and-white copy) and ``-page-N`` join the grammar
so a file groups the same way here as in the archive's ``fha photoindex``.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from photokin import core, utils


class TestTheGrammar(unittest.TestCase):
    """What the filename says, word by word."""

    def test_each_attached_word_groups_with_the_photo(self) -> None:
        for word in ("cover", "folder", "mount", "envelope", "sleeve", "note", "note-2"):
            with self.subTest(word=word):
                parsed = utils.parse_media_filename(f"210409-box-florence-044-{word}.jpg")

                self.assertEqual(parsed.base_id, "210409-box-florence-044")
                self.assertEqual(parsed.part_kind, "attached")
                self.assertEqual(parsed.attachment, word)

    def test_an_attached_objects_back_is_its_own_role_not_the_photos_back(self) -> None:
        parsed = utils.parse_media_filename("woodbury-harry-034b-Envelope-Back-crop.tif")

        self.assertEqual(
            (parsed.base_id, parsed.variant_id, parsed.part_kind, parsed.attachment),
            ("woodbury-harry-034", "b", "attached", "envelope-back"),
        )
        self.assertTrue(parsed.is_crop)

    def test_any_other_trailing_word_stays_in_the_name(self) -> None:
        for name in ("smith-farm-01400.jpg", "smith-farm-01400-back.jpg", "grandma-letter.jpg"):
            with self.subTest(name=name):
                parsed = utils.parse_media_filename(name)

                self.assertNotEqual(parsed.part_kind, "attached")
                self.assertIsNone(parsed.attachment)

    def test_bw_and_dashed_pages(self) -> None:
        bw = utils.parse_media_filename("box3_025-bw.jpg")
        bw_back = utils.parse_media_filename("box3_025-bw-back.jpg")
        page = utils.parse_media_filename("album-page-2.jpg")

        self.assertEqual((bw.base_id, bw.part_kind, bw.attachment), ("box3_025", "attached", "bw"))
        # The copy's back stays with the copy, in the photo's group.
        self.assertEqual(
            (bw_back.base_id, bw_back.part_kind, bw_back.attachment),
            ("box3_025", "attached", "bw-back"),
        )
        self.assertEqual((page.base_id, page.part_kind, page.page_num), ("album", "page", 2))

    def test_a_prefix_ending_in_a_part_word_is_refused_not_misread(self) -> None:
        plain = utils.parse_media_filename("x.jpg")
        for prefix in ("scrapbook-page", "aunt-may-note"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                utils.render_media_filename(prefix, 1, 3, plain, ".jpg")

    def test_attached_objects_rank_after_the_back(self) -> None:
        self.assertGreater(utils.PART_RANK["attached"], utils.PART_RANK["back"])
        self.assertGreater(utils.PART_RANK["attached"], utils.PART_RANK["negative"])

    def test_a_rename_keeps_the_suffix(self) -> None:
        for name in ("x-044b-envelope-back-crop.tif", "x-044-note-2.jpg", "x-044-bw.jpg"):
            with self.subTest(name=name):
                parsed = utils.parse_media_filename(name)
                ext = os.path.splitext(name)[1]

                rendered = utils.render_media_filename("new", 7, 3, parsed, ext)

                again = utils.parse_media_filename(rendered)
                self.assertEqual(again.base_id, "new-007")
                self.assertEqual(
                    (again.variant_id, again.part_kind, again.attachment, again.is_crop),
                    (parsed.variant_id, parsed.part_kind, parsed.attachment, parsed.is_crop),
                )


class _Recorder:
    """Stand-in for the three analyzers, recording each call's parts."""

    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.calls: list[tuple[str, list[tuple[str, list[str]]]]] = []

    def _answer(self, callee: str, parts: list[tuple[str, list[str]]]) -> dict:
        self.calls.append(
            (callee, [(label, [os.path.basename(p) for p in paths]) for label, paths in parts])
        )
        first = parts[0][1][0]
        return {"result": {first: json.loads(json.dumps(self.reply))}}

    def photo(self, front: str, back: str | None = None, *_a: Any, **_k: Any) -> dict:
        """Record an ``analyze_photo`` call."""
        return self._answer("photo", [("Front", [front])] + ([("Back", [back])] if back else []))

    def front_back(self, fronts: list[str], backs: list[str], *_a: Any, **_k: Any) -> dict:
        """Record an ``analyze_group_front_back`` call."""
        parts = [(label, paths) for label, paths in (("Front", fronts), ("Back", backs)) if paths]
        return self._answer("front_back", parts)

    def parts(self, parts: list[tuple[str, list[str]]], *_a: Any, **_k: Any) -> dict:
        """Record an ``analyze_group_parts`` call."""
        return self._answer("parts", list(parts))


@contextmanager
def _recording(reply: dict) -> Iterator[_Recorder]:
    """Replace the analyzers with a recorder for the block."""
    recorder = _Recorder(reply)
    with (
        patch("photokin.core.analyze_photo", recorder.photo),
        patch("photokin.core.analyze_group_front_back", recorder.front_back),
        patch("photokin.core.analyze_group_parts", recorder.parts),
    ):
        yield recorder


class TestTheGroupSeesItsAttachedObjects(unittest.TestCase):
    """One call carries the photo and everything attached to it."""

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.work = scratch.name

    def run_stream(self, files: dict[str, str | None], reply: dict) -> tuple[_Recorder, dict]:
        """Run ``files`` ({name: its own caption}) through the stream."""
        items = []
        for name, caption in files.items():
            path = os.path.join(self.work, name)
            with open(path, "w", encoding="utf-8"):
                pass
            items.append({"path": path, **({"metadata": {"caption": caption}} if caption else {})})
        with _recording(reply) as recorder:
            out = core.process_manifest_stream(manifest={"items": items}, cfg=utils.Config())
        return recorder, out

    def test_cover_back_and_photo_share_one_call(self) -> None:
        recorder, out = self.run_stream(
            {
                "044.jpg": None,
                "044-back.jpg": None,
                "044-cover.jpg": None,
                "044-envelope-back.jpg": None,
            },
            {"caption": "", "keywords": ["family"]},
        )

        self.assertEqual(
            recorder.calls,
            [
                (
                    "parts",
                    [
                        ("Front", ["044.jpg"]),
                        ("Back", ["044-back.jpg"]),
                        ("Cover", ["044-cover.jpg"]),
                        ("Envelope Back", ["044-envelope-back.jpg"]),
                    ],
                )
            ],
        )
        self.assertEqual(len(out["results"]), 4)
        self.assertEqual(out["errors"], {})

    def test_a_black_and_white_copy_rides_with_the_photo(self) -> None:
        recorder, _out = self.run_stream(
            {"044.jpg": None, "044-bw.jpg": None}, {"caption": "", "keywords": []}
        )

        self.assertEqual(
            recorder.calls,
            [("parts", [("Front", ["044.jpg"]), ("Black and White", ["044-bw.jpg"])])],
        )

    def test_the_cover_caption_is_labelled_and_stable_across_runs(self) -> None:
        reply = {"caption": "", "keywords": []}
        _rec, first = self.run_stream(
            {"044.jpg": "Ruth, 1948", "044-cover.jpg": "Bachrach, Boston"}, reply
        )
        block = first["results"][os.path.join(self.work, "044.jpg")]["caption"]

        _rec, second = self.run_stream({"044.jpg": block, "044-cover.jpg": block}, reply)

        self.assertIn("[Cover] Bachrach, Boston", block)
        self.assertNotIn("[Photo]", block.split("[Cover]")[1])
        self.assertEqual(second["results"][os.path.join(self.work, "044.jpg")]["caption"], block)


class TestTheBlackAndWhiteCopyNeverLeads(unittest.TestCase):
    """The plain scan stays the photo; its B&W copy rides along, labelled apart.

    ``047-bw.jpg`` sorts before ``047.jpg`` by path (``-`` < ``.``), so any
    tie in rank hands the copy the lead.
    """

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.work = scratch.name
        self.stream = TestTheGroupSeesItsAttachedObjects.run_stream.__get__(self)

    def test_the_copys_back_travels_with_the_group(self) -> None:
        recorder, _out = self.stream(
            {"047.jpg": None, "047-bw.jpg": None, "047-bw-back.jpg": None},
            {"caption": "", "keywords": []},
        )

        self.assertEqual(
            recorder.calls,
            [
                (
                    "parts",
                    [
                        ("Front", ["047.jpg"]),
                        ("Black and White", ["047-bw.jpg"]),
                        ("Black and White Back", ["047-bw-back.jpg"]),
                    ],
                )
            ],
        )

    def test_the_photos_caption_stays_the_photos_across_runs(self) -> None:
        reply = {"caption": "", "keywords": []}
        front = os.path.join(self.work, "047.jpg")
        _rec, first = self.stream(
            {"047.jpg": "Aunt May at Coney Island", "047-bw.jpg": None}, reply
        )
        block = first["results"][front]["caption"]

        _rec, second = self.stream({"047.jpg": block, "047-bw.jpg": block}, reply)

        self.assertEqual(block, "Aunt May at Coney Island")
        self.assertEqual(second["results"][front]["caption"], block)

    def test_the_photos_own_title_is_what_the_model_is_told(self) -> None:
        titles = {"047.jpg": "Aunt May", "047-bw.jpg": "bw copy scan"}
        entries = [
            {
                "path": os.path.join(self.work, name),
                "part_kind": utils.parse_media_filename(name).part_kind,
                "metadata": {"title": title},
            }
            for name, title in titles.items()
        ]

        self.assertEqual(utils.combine_group_metadata(entries)["title"], "Aunt May")


if __name__ == "__main__":
    unittest.main()
