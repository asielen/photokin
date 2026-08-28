"""The per-part ``transcriptions`` response contract, frozen for document mode.

Covers the producer half of the seam ``docs/document-mode-contract.md`` pins
down: normalization of the model-written map, deterministic caption synthesis
from it, the tolerant fallback when the map is absent or unusable, the
file-to-label resolution consumers import, and a styling-mark round trip
proving the synthesized caption reaches the per-file caption block
byte-identically.

Only the provider boundary is stubbed, so real prompt assembly, real parsing
and the real caption-block machinery run and are observable.
"""
import json
import os
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

from photokin import core, utils


@contextmanager
def _provider_stubbed(reply: dict) -> Iterator[None]:
    """Run the real analyzers against a canned parsed reply.

    Args:
        reply: The parsed model reply the run should behave as though it got.

    Yields:
        Nothing; the patches are active for the duration of the block.
    """
    with (
        patch("photokin.core._build_provider_client", return_value=object()),
        patch("photokin.core._should_run_archival_upload", return_value=False),
        patch("photokin.core.call_model", return_value={}),
        patch("photokin.core.extract_output_text", return_value=json.dumps(reply)),
        patch("photokin.core.get_response_model", return_value="test-model"),
        patch(
            "photokin.utils.build_data_url_and_size",
            return_value=(
                "data:image/jpeg;base64,AA==",
                4,
                {"mime": "image/jpeg", "width": 10, "height": 10, "resized": False},
            ),
        ),
    ):
        yield


class _ScratchFilesTestCase(unittest.TestCase):
    """Base giving each test scratch files the analyzers will accept."""

    maxDiff = None

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.work: str = scratch.name

    def make_files(self, *names: str) -> list[str]:
        """Create empty placeholder files named *names* and return their paths."""
        paths = []
        for name in names:
            path = os.path.join(self.work, name)
            with open(path, "w", encoding="utf-8"):
                pass
            paths.append(path)
        return paths


class TestCaptionSynthesis(unittest.TestCase):
    """What ``_synthesize_caption`` may emit, per contract section 3."""

    def test_one_part_is_the_bare_text_with_no_label(self) -> None:
        # The lone-scan-carries-no-label rule: a single transcription must not
        # bracket every caption in an archive that has no variants in it.
        self.assertEqual(core._synthesize_caption({"Front": "Dear Mother"}, ["Front"]),
                         "Dear Mother")

    def test_two_parts_are_labelled_sections_in_payload_order(self) -> None:
        # Map insertion order is deliberately Back-first: payload part order,
        # not the model's key order, decides the section order.
        caption = core._synthesize_caption(
            {"Back": "b text", "Front": "f text"}, ["Front", "Back"]
        )
        self.assertEqual(caption, "[Front]\nf text\n[Back]\nb text")

    def test_missing_and_empty_parts_contribute_no_section(self) -> None:
        # Back is absent from the map and Negative is blank, so only Front
        # contributes a section -- but the payload named three parts, so the
        # section still carries its label. Bare text is the lone-scan rule, and
        # this is not a lone scan.
        caption = core._synthesize_caption(
            {"Front": "f text", "Negative": "   "}, ["Front", "Back", "Negative"]
        )
        self.assertEqual(caption, "[Front]\nf text")

    def test_a_lone_answering_part_keeps_its_label_in_a_multi_part_payload(self) -> None:
        # The commonest inscribed photo there is: a front/back pair whose
        # writing is all on the back. Losing the [Back] label here does not
        # stay cosmetic -- unlabelled text is prose nobody attributed, so the
        # next -rw run attributes it to the file it was read off and the back's
        # writing becomes "[Photo] ..." on the front, permanently.
        caption = core._synthesize_caption({"Back": "To Grandma, 1950"}, ["Front", "Back"])
        self.assertEqual(caption, "[Back]\nTo Grandma, 1950")

    def test_a_lone_part_payload_is_still_bare(self) -> None:
        # The lone-scan-carries-no-label rule itself, unchanged: one part sent,
        # one part answered, no bracket invented for an archive that has no
        # variants in it.
        self.assertEqual(core._synthesize_caption({"Front": "porch"}, ["Front"]), "porch")

    def test_nothing_surviving_is_the_empty_string(self) -> None:
        for label, transcriptions in (("empty map", {}), ("all blank", {"Front": " \n "})):
            with self.subTest(label):
                self.assertEqual(core._synthesize_caption(transcriptions, ["Front"]), "")

    def test_an_unexpected_label_is_appended_rather_than_dropped(self) -> None:
        # The model answered about a part the payload never named; the answer
        # rides at the end instead of being silently lost.
        caption = core._synthesize_caption(
            {"Marginalia": "m text", "Front": "f text"}, ["Front", "Back"]
        )
        self.assertEqual(caption, "[Front]\nf text\n[Marginalia]\nm text")


class TestTranscriptionNormalization(unittest.TestCase):
    """What ``_normalize_transcriptions`` keeps, per contract section 1."""

    def test_non_dict_values_normalize_to_none(self) -> None:
        for label, raw in (("absent", None), ("string", "Front"), ("list", ["Front"])):
            with self.subTest(label):
                self.assertIsNone(core._normalize_transcriptions(raw))

    def test_only_str_to_str_entries_survive_and_values_are_stripped(self) -> None:
        cleaned = core._normalize_transcriptions(
            {"Front": "  padded  ", 1: "int key", "Back": None, "Negative": ["list"]}
        )
        self.assertEqual(cleaned, {"Front": "padded"})

    def test_whitespace_only_values_are_dropped(self) -> None:
        cleaned = core._normalize_transcriptions({"Front": "text", "Back": " \n\t "})
        self.assertEqual(cleaned, {"Front": "text"})

    def test_nothing_surviving_is_none_never_an_empty_dict(self) -> None:
        for label, raw in (("empty map", {}), ("all dropped", {"Front": "  ", 2: "x"})):
            with self.subTest(label):
                self.assertIsNone(core._normalize_transcriptions(raw))


class TestAnalyzerContract(_ScratchFilesTestCase):
    """How the analyzers apply the map, and what the fallback leaves alone."""

    def _config(self) -> utils.Config:
        return utils.Config(max_edge=None, no_update_vocab=True)

    def test_a_reply_without_transcriptions_is_todays_record_verbatim(self) -> None:
        # The tolerant fallback the whole extension hangs on: a model that
        # ignores the new field must produce exactly the record it produces
        # today, with no retry spent demanding the map.
        front, back = self.make_files("box3_025.jpg", "box3_025-back.jpg")
        reply = {"result": {"k": {"caption": "[Front]\nHello\n[Back]\nMarch 1944",
                                  "keywords": []}}}

        with _provider_stubbed(reply):
            data = core.analyze_group_parts(
                parts=[("Front", [front]), ("Back", [back])], config=self._config()
            )

        record = data["result"][front]
        self.assertEqual(record["caption"], "[Front]\nHello\n[Back]\nMarch 1944")
        self.assertNotIn("transcriptions", record)

    def test_transcriptions_win_and_the_caption_is_replaced(self) -> None:
        front, back = self.make_files("box3_025.jpg", "box3_025-back.jpg")
        reply = {
            "result": {
                "k": {
                    "caption": "the model's own merged caption",
                    "transcriptions": {"Back": "b text", "Front": "f text"},
                    "keywords": [],
                }
            }
        }

        with _provider_stubbed(reply):
            data = core.analyze_group_parts(
                parts=[("Front", [front]), ("Back", [back])], config=self._config()
            )

        record = data["result"][front]
        self.assertEqual(record["caption"], "[Front]\nf text\n[Back]\nb text")
        self.assertEqual(record["transcriptions"], {"Back": "b text", "Front": "f text"})

    def test_a_map_nothing_survives_from_falls_back_and_leaves_no_key(self) -> None:
        # A malformed map is not an error and costs nothing: the model's own
        # caption stands, and the record never carries an empty dict.
        (front,) = self.make_files("box3_025.jpg")
        reply = {"result": {"k": {"caption": "c", "keywords": [],
                                  "transcriptions": {"Front": "   "}}}}

        with _provider_stubbed(reply):
            data = core.analyze_group_parts(parts=[("Front", [front])], config=self._config())

        record = data["result"][front]
        self.assertEqual(record["caption"], "c")
        self.assertNotIn("transcriptions", record)

    def test_a_single_part_group_synthesizes_the_bare_text(self) -> None:
        (page,) = self.make_files("alb-page2.jpg")
        reply = {"result": {"k": {"transcriptions": {"Page 2": "Album notes"},
                                  "keywords": []}}}

        with _provider_stubbed(reply):
            data = core.analyze_group_parts(parts=[("Page 2", [page])], config=self._config())

        self.assertEqual(data["result"][page]["caption"], "Album notes")

    def test_analyze_photo_orders_front_then_back(self) -> None:
        front, back = self.make_files("box3_025.jpg", "box3_025-back.jpg")
        reply = {
            "result": {
                "k": {
                    "caption": "ignored",
                    "transcriptions": {"Back": "b text", "Front": "f text"},
                    "keywords": [],
                }
            }
        }

        with _provider_stubbed(reply):
            data = core.analyze_photo(front, back, self._config())

        record = data["result"][front]
        self.assertEqual(record["caption"], "[Front]\nf text\n[Back]\nb text")
        self.assertEqual(record["transcriptions"], {"Back": "b text", "Front": "f text"})

    def test_analyze_photo_without_a_back_appends_an_unrequested_back(self) -> None:
        # No back image was sent, so "Back" is not in the part order -- but the
        # model still said something about one, and the answer is kept.
        (front,) = self.make_files("box3_025.jpg")
        reply = {"result": {"k": {"transcriptions": {"Front": "f text", "Back": "b text"},
                                  "keywords": []}}}

        with _provider_stubbed(reply):
            data = core.analyze_photo(front, None, self._config())

        self.assertEqual(data["result"][front]["caption"], "[Front]\nf text\n[Back]\nb text")


class TestResolvePartLabel(unittest.TestCase):
    """The one file-to-label function, over every part kind."""

    @staticmethod
    def _entry(part_kind: str, page_num: int | None = None, version: str | None = None) -> dict:
        return {"part_kind": part_kind, "page_num": page_num, "version": version}

    def test_every_part_kind_maps_to_its_payload_label(self) -> None:
        cases: list[tuple[dict, str]] = [
            (self._entry("front"), "Front"),
            (self._entry("back"), "Back"),
            (self._entry("negative"), "Negative"),
            (self._entry("page", page_num=3), "Page 3"),
            # '-pageN' accepts any run of digits, so page 0 is a slot of its own.
            (self._entry("page", page_num=0), "Page 0"),
            # A page entry with no parsed number competes for the page 1 slot.
            (self._entry("page", page_num=None), "Page 1"),
            # An untagged file travels as the front side of its variant.
            (self._entry("none"), "Front"),
        ]
        for entry, expected in cases:
            with self.subTest(expected=expected):
                label = core.resolve_part_label(
                    entry, multipage_present=False, relabelled_versions=frozenset()
                )
                self.assertEqual(label, expected)

    def test_the_untagged_slot_that_became_page_one_resolves_to_page_one(self) -> None:
        label = core.resolve_part_label(
            self._entry("none", version="b"),
            multipage_present=True,
            relabelled_versions=frozenset({"b"}),
        )
        self.assertEqual(label, "Page 1")

    def test_the_relabel_needs_both_the_pages_and_the_version(self) -> None:
        # The relabel is per-variant: an untagged file whose own variant kept
        # its untagged slot -- or a group with no pages at all -- stays a Front.
        for label, multipage, relabelled in (
            ("only another version was relabelled", True, frozenset({"b"})),
            ("no multipage parts at all", False, frozenset({None})),
        ):
            with self.subTest(label):
                resolved = core.resolve_part_label(
                    self._entry("none", version=None),
                    multipage_present=multipage,
                    relabelled_versions=relabelled,
                )
                self.assertEqual(resolved, "Front")

    def test_the_unversioned_relabel_uses_the_none_version(self) -> None:
        label = core.resolve_part_label(
            self._entry("none", version=None),
            multipage_present=True,
            relabelled_versions=frozenset({None}),
        )
        self.assertEqual(label, "Page 1")


class TestStylingMarksRoundTrip(_ScratchFilesTestCase):
    """Styling marks survive synthesis and the caption block byte-identically.

    The transcription conventions put crossed-out text in ``~~..~~``,
    underlines in ``_.._``, margin notes in ``> [margin note]`` blockquotes and
    footnotes after a ``---`` rule -- lines that read like noise to machinery
    written for prose captions. Both pages here end in a ``---`` rule on
    purpose: a repeated wordless rule line is layout, and the line-level dedup
    must keep the second one rather than gluing page two's footnote onto its
    prose.
    """

    PAGE_ONE = (
        "Dear Mother, we ~~left~~ arrived on the _fourth_ of June.\n"
        "> [margin note] x\n"
        "---\n"
        "* the harbour, she means"
    )
    PAGE_TWO = (
        "The crossing was calm and the food [illegible ~2 words].\n"
        "> [margin note] y\n"
        "---\n"
        "* written sideways in pencil"
    )

    def test_the_synthesized_caption_reaches_every_file_unchanged(self) -> None:
        page1, page2 = self.make_files("doc-page1.jpg", "doc-page2.jpg")
        expected = f"[Page 1]\n{self.PAGE_ONE}\n[Page 2]\n{self.PAGE_TWO}"
        reply = {
            "result": {
                "k": {
                    "caption": "the model's own merged caption",
                    "transcriptions": {"Page 1": self.PAGE_ONE, "Page 2": self.PAGE_TWO},
                    "keywords": ["Document"],
                }
            }
        }

        with _provider_stubbed(reply):
            out = core.process_manifest_stream(
                manifest={"items": [{"path": page1}, {"path": page2}]},
                cfg=utils.Config(dry_run=True, max_edge=None, no_update_vocab=True),
            )

        self.assertEqual(sorted(out["results"]), sorted([page1, page2]))
        for path, merged in out["results"].items():
            with self.subTest(path=os.path.basename(path)):
                # Byte-identical: through _absorb_caption, the near-identical
                # dedup and the line-level dedup, nothing was resectioned,
                # dropped or reflowed.
                self.assertEqual(merged["caption"], expected)
                # The map rides the fan-out whole: every file of the group
                # holds every part's transcription.
                self.assertEqual(
                    merged["transcriptions"],
                    {"Page 1": self.PAGE_ONE, "Page 2": self.PAGE_TWO},
                )


class TestBackOnlyTranscriptionStaysAttributedAcrossRepeatedRuns(_ScratchFilesTestCase):
    """The end-to-end companion to ``TestCaptionSynthesis``'s unit-level cases.

    ``_synthesize_caption`` deciding the bare-text rule from how many parts
    the payload SENT, rather than how many came back with text, is unit-tested
    above; what is not covered there is what actually happens to a front/back
    PAIR carried all the way through ``-rw`` in a loop, which is where the
    regression this amendment fixed was actually discovered. Deciding the rule
    from the survivor count dropped the ``[Back]`` label for exactly this
    shape -- a front/back pair whose writing is all on the back -- and an
    unlabelled caption is prose nobody attributed, so the very next ``-rw``
    pass attributed it to the file it read it off, permanently turning
    "[Back] ..." into "[Photo] ..." on the FRONT print. Once mislabelled it
    never recovers, because the mislabel itself is now the file's own stored
    caption the run after reads back.
    """

    def test_the_back_label_never_migrates_to_the_front(self) -> None:
        front, back = self.make_files("box3_025.jpg", "box3_025-back.jpg")
        reply = {
            "result": {
                "k": {
                    "transcriptions": {"Back": "To Grandma, love Ruth, 1950."},
                    "keywords": ["Document"],
                }
            }
        }

        def _run_once(items: list[dict]) -> dict[str, str]:
            with _provider_stubbed(reply):
                out = core.process_manifest_stream(
                    manifest={"items": items},
                    cfg=utils.Config(dry_run=True, max_edge=None, no_update_vocab=True),
                )
            return {path: out["results"][path]["caption"] for path in (front, back)}

        items: list[dict] = [{"path": front}, {"path": back}]
        passes: list[dict[str, str]] = []
        for _run in range(4):
            captions = _run_once(items)
            passes.append(captions)
            items = [{"path": path, "metadata": {"caption": captions[path]}} for path in (front, back)]

        for index, captions in enumerate(passes, start=1):
            with self.subTest(pass_number=index):
                self.assertEqual(
                    captions[front], "[Back]\nTo Grandma, love Ruth, 1950.",
                    "the back's writing was not attributed to [Back] on the "
                    f"front file: {captions[front]!r}",
                )
                self.assertEqual(captions[front], captions[back])
                self.assertNotIn(
                    "[Photo]", captions[front],
                    "the back's writing was re-attributed to the front as "
                    f"[Photo] ...: {captions[front]!r}",
                )
        # Identical on the very first pass and every pass after: with
        # transcriptions=={"Back": ...} against a two-part payload, the
        # correct label is available immediately -- there is no settling
        # period the way an unlabelled block needs one (contrast the
        # page-repeat case in test_review_regressions.py, whose first pass
        # has nothing on disk to read a label back from).
        self.assertTrue(all(p == passes[0] for p in passes[1:]))


if __name__ == "__main__":
    unittest.main()
