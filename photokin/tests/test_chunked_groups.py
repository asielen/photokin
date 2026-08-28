"""What a group too large for one payload actually sends, and what it banks.

Phase 3 of document mode splits an oversized group into several bounded calls
and reconciles them with one text-only consolidation call. Two properties carry
the whole feature and both are asserted here rather than assumed:

- a group that fits in one payload takes a path byte-identical to the one it
  took before chunking existed -- same prompt items, same order, one call, no
  chunk note; and
- the consolidation pass may be as wrong as it likes without costing the group
  anything: a missing page order, a garbled page order, an unparseable reply
  and a reply with no result block each fall back and none of them raises.

Only the provider boundary is stubbed, so the real prompt assembly, the real
partitioner, the real tolerant parsers and the real caption synthesis all run
and are observable.
"""
import json
import logging
import os
import shutil
import tempfile
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from photokin import core, utils

_CHUNK_NOTE_MARKER = "CHUNK NOTE:"
_CONSOLIDATION_MARKER = "CONSOLIDATION PASS"
_CONSOLIDATION_INPUT_MARKER = "CONSOLIDATION INPUT (JSON)"


class _Call:
    """One captured model call: what images it carried and what it said."""

    def __init__(self, images: list[str], texts: list[str]) -> None:
        self.images: list[str] = images
        self.texts: list[str] = texts

    @property
    def prompt(self) -> str:
        """The call's prompt items joined, for substring assertions."""
        return "\n".join(self.texts)

    @property
    def is_consolidation(self) -> bool:
        """Whether this is the text-only consolidation call."""
        return not self.images


class _StubResponse:
    """A provider response carrying only what the analyzer reads off one."""

    def __init__(self, index: int) -> None:
        self.index: int = index
        self.usage: dict[str, int] = {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        }
        self.model: str = "test-model"


@contextmanager
def _provider_stubbed(
    reply_for: Callable[[int, _Call], Any],
) -> Iterator[list[_Call]]:
    """Run the real analyzer against canned replies, capturing every call.

    Args:
        reply_for: Called with the zero-based call index and the call just
            captured; returns that call's reply as a dict (serialized to JSON)
            or as a raw string (sent verbatim, which is how a malformed reply
            is staged).

    Yields:
        The captured calls, in the order they were made.
    """
    calls: list[_Call] = []

    def _call_model(
        client: object,
        model: str,
        prompt_items: list[dict],
        urls: list[str],
        provider: str | None = None,
        dump_request: Any = None,
    ) -> _StubResponse:
        calls.append(
            _Call(
                images=[url for url in urls if url],
                texts=[
                    item.get("text", "") for item in prompt_items if isinstance(item, dict)
                ],
            )
        )
        return _StubResponse(len(calls) - 1)

    def _extract_output_text(resp: _StubResponse, provider: str | None = None) -> str:
        reply = reply_for(resp.index, calls[resp.index])
        return reply if isinstance(reply, str) else json.dumps(reply)

    def _build_data_url(path: str, quality: int, max_edge: int | None) -> tuple[str, int, dict]:
        return (
            f"data:{os.path.basename(path)}",
            4,
            {"mime": "image/jpeg", "width": 10, "height": 10, "resized": False},
        )

    with (
        patch("photokin.core._build_provider_client", return_value=object()),
        patch("photokin.core._should_run_archival_upload", return_value=False),
        patch("photokin.core.call_model", _call_model),
        patch("photokin.core.extract_output_text", _extract_output_text),
        patch("photokin.core.get_response_model", return_value="test-model"),
        patch("photokin.utils.build_data_url_and_size", _build_data_url),
    ):
        yield calls


def _chunk_reply(labels: list[str]) -> dict[str, Any]:
    """Build one chunk call's reply, transcribing exactly the parts it saw."""
    return {
        "result": {
            "k": {
                "keywords": ["Document", f"Block {labels[0]}"],
                "title": f"Block starting at {labels[0]}",
                "category": "Document",
                "ai_caption": "[AI Analysis]: A block of one document.",
                "transcriptions": {label: f"text of {label}" for label in labels},
                "date_guess": {"iso": "1944", "confidence": 0.4, "pattern": "Y!"},
                "location_guess": {"country": "France", "confidence": 0.3},
                "proposed_new_keywords": [],
            }
        }
    }


def _consolidation_reply(
    page_order: dict[str, Any] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """Build a well-formed consolidation reply, optionally with a page verdict."""
    reply: dict[str, Any] = {
        "result": {
            "k": {
                "keywords": ["Document", "Letter"],
                "title": "The whole document",
                "category": "Document",
                "ai_caption": "[AI Analysis]: The whole document.",
                "date_guess": {"iso": "1944-11", "confidence": 0.9, "pattern": "Y!M!"},
                "location_guess": {"country": "France", "confidence": 0.8},
                "proposed_new_keywords": [],
            }
        }
    }
    if page_order is not None:
        reply["page_order"] = page_order
    if notes is not None:
        reply["page_order_notes"] = notes
    return reply


class _ChunkedGroupTestCase(unittest.TestCase):
    """Base giving each test scratch page files and a stubbed analyzer run."""

    maxDiff = None

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.work: str = scratch.name

    def page_parts(self, count: int) -> list[tuple[str, list[str]]]:
        """Create *count* one-image ``Page N`` parts, page 1 first."""
        parts: list[tuple[str, list[str]]] = []
        for num in range(1, count + 1):
            parts.append((f"Page {num}", [self.make_file(f"doc-page{num}.jpg")]))
        return parts

    def make_file(self, name: str) -> str:
        """Create an empty placeholder file named *name* and return its path."""
        path = os.path.join(self.work, name)
        with open(path, "w", encoding="utf-8"):
            pass
        return path

    def config(self, budget: int) -> utils.Config:
        """A run configuration that writes nothing and chunks at *budget*."""
        return utils.Config(
            max_edge=None,
            no_update_vocab=True,
            max_images_per_call=budget,
        )

    def labels_of(self, call: _Call) -> list[str]:
        """Return the page labels a captured call carried, in payload order."""
        return [
            f"Page {os.path.basename(url).removeprefix('data:doc-page').removesuffix('.jpg')}"
            for url in call.images
        ]

    def run_group(
        self,
        parts: list[tuple[str, list[str]]],
        budget: int,
        reply_for: Callable[[int, _Call], Any],
    ) -> tuple[dict[str, Any], list[_Call]]:
        """Analyze *parts* against a stub and return (record, calls)."""
        with _provider_stubbed(reply_for) as calls:
            data = core.analyze_group_parts(parts=parts, config=self.config(budget))
        record = next(iter(data["result"].values()))
        return record, calls


class TestTheCallsAChunkedGroupMakes(_ChunkedGroupTestCase):
    """How an oversized group is broken up, and what the last call carries."""

    def test_twenty_pages_at_eight_is_three_chunks_and_one_consolidation(self) -> None:
        parts = self.page_parts(20)

        def _reply(index: int, call: _Call) -> Any:
            if call.is_consolidation:
                return _consolidation_reply()
            return _chunk_reply(self.labels_of(call))

        _record, calls = self.run_group(parts, 8, _reply)

        self.assertEqual(len(calls), 4)
        self.assertEqual(
            [self.labels_of(call) for call in calls[:3]],
            [
                [f"Page {num}" for num in range(1, 9)],
                [f"Page {num}" for num in range(9, 17)],
                [f"Page {num}" for num in range(17, 21)],
            ],
        )
        for call in calls[:3]:
            self.assertIn(_CHUNK_NOTE_MARKER, call.prompt)

        consolidation = calls[3]
        self.assertEqual(consolidation.images, [])
        self.assertIn(_CONSOLIDATION_MARKER, consolidation.prompt)
        self.assertIn(_CONSOLIDATION_INPUT_MARKER, consolidation.prompt)

    def test_the_consolidation_input_carries_the_evidence_not_the_pixels(self) -> None:
        parts = self.page_parts(6)

        def _reply(index: int, call: _Call) -> Any:
            if call.is_consolidation:
                return _consolidation_reply()
            return _chunk_reply(self.labels_of(call))

        _record, calls = self.run_group(parts, 4, _reply)

        payload_text = next(
            text for text in calls[-1].texts if text.startswith(_CONSOLIDATION_INPUT_MARKER)
        )
        payload = json.loads(payload_text.split("\n", 1)[1])
        self.assertEqual(
            [entry["label"] for entry in payload["parts"]],
            [f"Page {num}" for num in range(1, 7)],
        )
        self.assertEqual(payload["parts"][2]["files"], ["doc-page3.jpg"])
        self.assertEqual(payload["parts"][2]["page_from_filename"], 3)
        self.assertEqual(payload["parts"][2]["transcription"], "text of Page 3")
        self.assertEqual(len(payload["provisional_metadata_by_block"]), 2)
        self.assertEqual(
            payload["provisional_metadata_by_block"][0]["parts_seen"],
            [f"Page {num}" for num in range(1, 5)],
        )

    def test_a_front_and_its_back_stay_in_one_chunk_at_a_boundary(self) -> None:
        parts = self.page_parts(10)
        parts.append(("Front", [self.make_file("doc-front.jpg")]))
        parts.append(("Back", [self.make_file("doc-back.jpg")]))

        def _reply(index: int, call: _Call) -> Any:
            if call.is_consolidation:
                return _consolidation_reply()
            return _chunk_reply(["Front"])

        _record, calls = self.run_group(parts, 8, _reply)

        self.assertEqual(len(calls), 3)
        first_chunk = [os.path.basename(url) for url in calls[0].images]
        self.assertIn("data:doc-front.jpg", first_chunk)
        self.assertIn("data:doc-back.jpg", first_chunk)
        second_chunk = [os.path.basename(url) for url in calls[1].images]
        self.assertNotIn("data:doc-front.jpg", second_chunk)
        self.assertNotIn("data:doc-back.jpg", second_chunk)

    def test_a_chunk_over_the_budget_is_never_silent(self) -> None:
        # A part is atomic, so three variant scans of one page cannot be split
        # to fit a budget of two. The call is still made; the run says so.
        parts = [
            ("Page 1", [self.make_file(f"doc-page1{letter}.jpg") for letter in "abc"]),
            ("Page 2", [self.make_file("doc-page2.jpg")]),
        ]

        def _reply(index: int, call: _Call) -> Any:
            if call.is_consolidation:
                return _consolidation_reply()
            return _chunk_reply(["Page 1"])

        with self.assertLogs("photokin.core", level="WARNING") as logged:
            _record, calls = self.run_group(parts, 2, _reply)

        self.assertEqual([len(call.images) for call in calls], [3, 1, 0])
        self.assertTrue(
            any("over the --max-images-per-call budget of 2" in line for line in logged.output),
            logged.output,
        )


class TestTheUnchunkedPathIsUntouched(_ChunkedGroupTestCase):
    """A group that fits in one payload must send exactly what it always did."""

    _EXPECTED_NOTE = "\n".join(
        [
            "GROUP VARIANTS NOTE:",
            "You are seeing multiple scans or variants of the same physical photograph or document.",
        ]
        + [
            f"{'The first' if num == 1 else 'The next'} 1 image(s) are Page {num} variants of the item."
            for num in range(1, 9)
        ]
        + [
            "Analyze all provided images together as one unified item, preserving the part order given.",
            (
                "When filling the caption field, transcribe all visible text from each part "
                "across all variants, merging duplicates and applying the LINE BREAKS rules "
                "above: paragraph and deliberate breaks are kept, and the wrapped lines of "
                "running prose are joined."
            ),
            "Do NOT describe the scene in the caption field; only transcribed text (with [ ] for guesses and semi-illegible text).",
            "Describe the visual scene and give 3–6 sentences of cautious but comprehensive historical analysis ONLY in the ai_caption field, starting with '[AI Analysis]:'.",
        ]
    )

    def _single_call(self, page_count: int, budget: int) -> _Call:
        """Analyze a *page_count*-page group at *budget* and return its one call."""
        parts = self.page_parts(page_count)

        def _reply(index: int, call: _Call) -> Any:
            return _chunk_reply(self.labels_of(call))

        _record, calls = self.run_group(parts, budget, _reply)
        self.assertEqual(len(calls), 1)
        return calls[0]

    def test_a_group_at_the_budget_makes_one_unannotated_call(self) -> None:
        call = self._single_call(8, 8)

        self.assertEqual(len(call.images), 8)
        self.assertNotIn(_CHUNK_NOTE_MARKER, call.prompt)
        self.assertNotIn(_CONSOLIDATION_MARKER, call.prompt)
        self.assertEqual(call.texts[-1], self._EXPECTED_NOTE)

    def test_disabling_chunking_restores_one_call_at_any_size(self) -> None:
        call = self._single_call(20, 0)

        self.assertEqual(len(call.images), 20)
        self.assertNotIn(_CHUNK_NOTE_MARKER, call.prompt)

    def test_the_prompt_does_not_depend_on_the_budget_when_one_call_is_made(self) -> None:
        at_budget = self._single_call(8, 8)
        disabled = self._single_call(8, 0)

        self.assertEqual(at_budget.texts, disabled.texts)


class TestWhatAChunkedGroupBanks(_ChunkedGroupTestCase):
    """The record a chunked group returns: transcriptions, caption, usage."""

    def _run_ten_pages(self) -> tuple[dict[str, Any], list[_Call]]:
        """Analyze ten pages at a budget of four (three chunks + consolidation)."""
        parts = self.page_parts(10)

        def _reply(index: int, call: _Call) -> Any:
            if call.is_consolidation:
                return _consolidation_reply()
            return _chunk_reply(self.labels_of(call))

        return self.run_group(parts, 4, _reply)

    def test_transcriptions_from_every_chunk_are_unioned_in_part_order(self) -> None:
        record, calls = self._run_ten_pages()

        self.assertEqual(len(calls), 4)
        self.assertEqual(
            list(record["transcriptions"].items()),
            [(f"Page {num}", f"text of Page {num}") for num in range(1, 11)],
        )

    def test_the_caption_is_the_synthesized_union_not_a_hand_built_string(self) -> None:
        record, _calls = self._run_ten_pages()

        self.assertEqual(
            record["caption"],
            core._synthesize_caption(
                record["transcriptions"], [f"Page {num}" for num in range(1, 11)]
            ),
        )
        self.assertTrue(record["caption"].startswith("[Page 1]\ntext of Page 1\n[Page 2]"))

    def test_usage_is_summed_across_every_chunk_call_and_the_consolidation(self) -> None:
        record, calls = self._run_ten_pages()

        self.assertEqual(len(calls), 4)
        self.assertEqual(record["_usage"]["prompt_tokens"], 40)
        self.assertEqual(record["_usage"]["completion_tokens"], 20)
        self.assertEqual(record["_usage"]["total_tokens"], 60)
        self.assertEqual(record["_usage"]["model"], "test-model")

    def test_the_consolidated_metadata_replaces_the_chunk_guesses(self) -> None:
        record, _calls = self._run_ten_pages()

        self.assertEqual(record["title"], "The whole document")
        self.assertEqual(record["date_guess"]["iso"], "1944-11")
        self.assertIn("Letter", record["keywords"])


class TestThePageOrderVerdictIsDataNeverAction(_ChunkedGroupTestCase):
    """The corrected order reaches the record and the log, and nothing else."""

    def _run_misnamed(
        self,
        page_order: dict[str, Any] | None,
        notes: list[str] | None = None,
    ) -> tuple[dict[str, Any], list[_Call]]:
        """Analyze four deliberately misnamed pages, returning *page_order*."""
        parts = self.page_parts(4)

        def _reply(index: int, call: _Call) -> Any:
            if call.is_consolidation:
                return _consolidation_reply(page_order, notes)
            return _chunk_reply(self.labels_of(call))

        return self.run_group(parts, 2, _reply)

    def test_a_corrected_page_reaches_the_record_and_warns(self) -> None:
        verdict = {
            "Page 3": {"page": 4, "flags": ["out_of_order"]},
            "Page 4": {"page": 3},
        }

        with self.assertLogs("photokin.core", level="WARNING") as logged:
            record, calls = self._run_misnamed(verdict, ["Page 4 completes Page 2."])

        self.assertEqual(len(calls), 3)
        self.assertEqual(record["page_order"], verdict)
        self.assertEqual(record["page_order_notes"], ["Page 4 completes Page 2."])
        self.assertTrue(
            any(
                "do not read in filename order" in line and "Page 3 reads as page 4" in line
                for line in logged.output
            ),
            logged.output,
        )

    def test_an_agreeing_verdict_lands_without_a_warning(self) -> None:
        verdict = {f"Page {num}": {"page": num} for num in range(1, 5)}

        with self.assertNoLogs("photokin.core", level="WARNING"):
            record, _calls = self._run_misnamed(verdict)

        self.assertEqual(record["page_order"], verdict)

    def test_a_label_the_payload_never_sent_is_dropped(self) -> None:
        record, _calls = self._run_misnamed(
            {"Page 2": {"page": 2}, "Page 99": {"page": 99}, "Cover": {"page": 1}}
        )

        self.assertEqual(record["page_order"], {"Page 2": {"page": 2}})


class TestTheConsolidationPassMayFailWithoutCost(_ChunkedGroupTestCase):
    """Every way the last call can disappoint, and the group surviving it."""

    def _run_with_consolidation(self, consolidation_reply: Any) -> dict[str, Any]:
        """Analyze six pages at a budget of four with a given final reply."""
        parts = self.page_parts(6)

        def _reply(index: int, call: _Call) -> Any:
            if call.is_consolidation:
                return consolidation_reply
            return _chunk_reply(self.labels_of(call))

        record, _calls = self.run_group(parts, 4, _reply)
        return record

    def test_a_reply_with_no_page_order_leaves_the_filename_order_standing(self) -> None:
        record = self._run_with_consolidation(_consolidation_reply())

        self.assertNotIn("page_order", record)
        self.assertNotIn("page_order_notes", record)
        self.assertEqual(record["title"], "The whole document")

    def test_a_malformed_page_order_is_dropped_rather_than_raised(self) -> None:
        reply = _consolidation_reply()
        reply["page_order"] = ["Page 1", "Page 2"]

        record = self._run_with_consolidation(reply)

        self.assertNotIn("page_order", record)
        self.assertEqual(record["title"], "The whole document")

    def test_an_unparseable_reply_still_yields_a_usable_record(self) -> None:
        record = self._run_with_consolidation("this is not JSON at all")

        self.assertNotIn("page_order", record)
        self.assertEqual(len(record["transcriptions"]), 6)
        self.assertTrue(record["caption"].startswith("[Page 1]\ntext of Page 1"))
        self.assertIn("Document", record["keywords"])

    def test_a_reply_with_no_result_block_folds_the_chunk_answers(self) -> None:
        record = self._run_with_consolidation({"page_order": {"Page 1": {"page": 1}}})

        # Chunk 1's answer is the base, widened by the highest-confidence
        # guesses and the union of the keywords across every chunk.
        self.assertEqual(record["title"], "Block starting at Page 1")
        self.assertIn("Block Page 5", record["keywords"])
        self.assertEqual(record["page_order"], {"Page 1": {"page": 1}})


class TestACaptionOnlyChunksTextIsNeverDiscarded(_ChunkedGroupTestCase):
    """A chunk that answers with ``caption`` instead of ``transcriptions`` keeps its pages.

    Compliance with the new field is per CALL, not per run (the plan's own
    fallback promise is that a model ignoring it degrades to current
    behavior) -- so one chunked group can genuinely hold both a chunk that
    filled ``transcriptions`` and a chunk that fell back to the old
    ``caption`` field. Reading only the map whenever ANY chunk filled it
    dropped every non-complying chunk's pages out of the record, the caption
    block and the sidecars without a word: a silent loss of the
    transcription the group was billed for, for exactly the pages that chunk
    covered.
    """

    def test_the_caption_only_chunks_text_reaches_the_record_and_warns(self) -> None:
        parts = self.page_parts(8)

        def _reply(index: int, call: _Call) -> Any:
            if call.is_consolidation:
                return _consolidation_reply()
            labels = self.labels_of(call)
            if labels and labels[0] == "Page 5":
                # This chunk ignores the new field entirely -- the plan's own
                # tolerant fallback, exercised per call rather than per run.
                return {
                    "result": {
                        "k": {
                            "keywords": ["Document"],
                            "title": "Second block",
                            "category": "Document",
                            "ai_caption": "[AI Analysis]: A second block.",
                            "caption": "Page 5 through 8 text, merged by the model.",
                            "date_guess": {"iso": "1944", "confidence": 0.4, "pattern": "Y!"},
                            "location_guess": {"country": "France", "confidence": 0.3},
                            "proposed_new_keywords": [],
                        }
                    }
                }
            return _chunk_reply(labels)

        with self.assertLogs("photokin.core", level="WARNING") as logged:
            record, calls = self.run_group(parts, 4, _reply)

        self.assertEqual(len(calls), 3)  # two page chunks + consolidation
        self.assertIn(
            "Page 5 through 8 text, merged by the model.", record["caption"],
            f"the non-complying chunk's pages vanished entirely: {record['caption']!r}",
        )
        self.assertTrue(
            any(
                "answered with 'caption' instead of 'transcriptions'" in line
                for line in logged.output
            ),
            logged.output,
        )


class TestConsolidationCannotOverwriteFoldedProposedKeywords(_ChunkedGroupTestCase):
    """The consolidation pass's mandated empty array cannot erase a chunk's real proposal.

    ``proposed_new_keywords`` is not one of the fields the consolidation pass
    is allowed to replace on the record (``_CONSOLIDATED_FIELDS``), and
    ``consolidation.txt`` no longer even asks the model for it. Before, the
    field's mandated empty array overwrote the proposals
    ``_fold_chunk_records`` had just folded from the chunks -- and the
    vocabulary-insert block downstream then rejected every new keyword for
    want of a proposal, so a chunked group could never teach the vocabulary
    anything, though an unchunked group always could.
    """

    def setUp(self) -> None:
        super().setUp()
        self.vocab_path = os.path.join(self.work, "vocab_keywords_examples.toml")
        shutil.copy(utils.Config().vocab_path, self.vocab_path)

    def test_a_chunked_groups_fully_described_keyword_reaches_the_vocabulary(self) -> None:
        with open(self.vocab_path, encoding="utf-8") as handle:
            before = handle.read().splitlines()

        keyword = "Harborview"
        parts = self.page_parts(4)

        def _chunk_reply_proposing(labels: list[str]) -> dict[str, Any]:
            reply = _chunk_reply(labels)
            reply["result"]["k"]["keywords"] = ["Document", keyword]
            reply["result"]["k"]["proposed_new_keywords"] = [
                {
                    "keyword": keyword,
                    "section": "photo_format",
                    "note": "A fully described reason this keyword earns a place.",
                }
            ]
            return reply

        def _reply(index: int, call: _Call) -> Any:
            if call.is_consolidation:
                reply = _consolidation_reply()
                reply["result"]["k"]["keywords"] = ["Document", keyword]
                # consolidation.txt tells the model never to send this field;
                # sent anyway here (a non-complying reply) to prove the empty
                # array cannot erase the chunks' real proposal even when it
                # arrives, not merely that the prompt asks it not to.
                reply["result"]["k"]["proposed_new_keywords"] = []
                return reply
            return _chunk_reply_proposing(self.labels_of(call))

        cfg = utils.Config(max_edge=None, max_images_per_call=2, vocab_path=self.vocab_path)
        with _provider_stubbed(_reply):
            core.analyze_group_parts(parts=parts, config=cfg)

        with open(self.vocab_path, encoding="utf-8") as handle:
            after = handle.read().splitlines()
        added = [line for line in after if line not in before]

        self.assertTrue(
            any(f'keyword = "{keyword}"' in line for line in added),
            f"the chunked group's proposed keyword never reached the vocabulary: {added!r}",
        )


class TestGroupVariantsNoteDoesNotContradictLineBreakRules(unittest.TestCase):
    """The note read last, right before the images, cannot outrank the rules read first.

    This note is the LAST thing the model reads before the images, so an
    unqualified "preserving line breaks" here outranked the LINE BREAKS
    rules stated tens of thousands of characters earlier in the prompt and
    undid the whole flowed-prose convention for every multi-part group --
    the one place document mode's paragraph-flow behavior mattered most. It
    now points at those rules instead of contradicting them.
    """

    def test_the_note_defers_to_the_line_break_rules_rather_than_repeating_them(self) -> None:
        note = core._build_group_variants_note(
            [("Page 1", ["a.jpg"]), ("Page 2", ["b.jpg"])], image_count=2
        )
        self.assertNotIn("preserving line breaks", note)
        self.assertIn("LINE BREAKS rules", note)

    def test_the_full_note_text_pinned_elsewhere_in_this_module_agrees(self) -> None:
        # TestTheUnchunkedPathIsUntouched pins the complete unchunked-path
        # note byte for byte; this checks that pinned text is itself
        # consistent with the rule above rather than restating it here too.
        pinned = TestTheUnchunkedPathIsUntouched._EXPECTED_NOTE
        self.assertNotIn("preserving line breaks", pinned)
        self.assertIn("LINE BREAKS rules", pinned)


class TestAStrayChunkTranscriptionLabelIsCarriedNotLost(_ChunkedGroupTestCase):
    """A chunk's mis-spelled label still reaches the caption, and the run says so.

    The merge into ``merged_transcriptions`` is an exact-key lookup, so a
    chunk that spelled a label even slightly differently ("page 9" for
    "Page 9") contributes nothing under the correct key. On the unchunked
    path a stray label merely lands in the caption under its own bogus
    heading, which is visible; on the chunked path it was invisible -- the
    text was simply gone, with no warning naming what went missing or why.
    """

    def test_the_stray_labels_text_survives_and_the_run_warns(self) -> None:
        parts = self.page_parts(9)

        def _reply(index: int, call: _Call) -> Any:
            if call.is_consolidation:
                return _consolidation_reply()
            labels = self.labels_of(call)
            if labels == ["Page 9"]:
                reply = _chunk_reply(labels)
                # Correctly-cased "Page 9" never appears in this chunk's
                # reply at all -- only the mis-cased spelling does.
                reply["result"]["k"]["transcriptions"] = {"page 9": "text of page 9"}
                return reply
            return _chunk_reply(labels)

        with self.assertLogs("photokin.core", level="WARNING") as logged:
            record, calls = self.run_group(parts, 8, _reply)

        self.assertEqual(len(calls), 3)  # two page chunks + consolidation
        self.assertNotIn("Page 9", record["transcriptions"])
        self.assertIn("page 9", record["transcriptions"])
        self.assertIn("text of page 9", record["caption"])
        self.assertTrue(
            any(
                "this payload never named" in line and "'page 9'" in line
                for line in logged.output
            ),
            logged.output,
        )


class TestCaptionOnlyChunksAreJoinedInPayloadOrderNotCallOrder(_ChunkedGroupTestCase):
    """A chunked group's caption sections read in the object's own order.

    The partitioner appends every non-page part to chunk 1 after its pages, so
    a front and its back never split at a chunk boundary (its rules 4 and 5).
    That is right for the calls and wrong for the caption: chunk 1 comes back
    describing pages 1-6 AND both sides, chunk 2 describes pages 7-10, and
    simply concatenating them reads "Page 1..6, Front, Back, Page 7..10" --
    an order the object does not have and an unchunked call would never write.

    The sections have to move, not the chunks: chunk 1's caption is a single
    string that already holds the out-of-order sections inside it, so no
    amount of sorting the chunks can fix this. Sorting the chunks was in fact
    tried, and did nothing at all.
    """

    _CHUNK_ONE_CAPTION = (
        "[Page 1]\nfirst page\n[Page 6]\nsixth page\n[Front]\nfront text\n[Back]\nback text"
    )
    _CHUNK_TWO_CAPTION = "[Page 7]\nseventh page\n[Page 10]\ntenth page"

    def test_the_sides_follow_every_page_rather_than_splitting_the_run(self) -> None:
        parts = self.page_parts(10)
        parts.append(("Front", [self.make_file("doc-front.jpg")]))
        parts.append(("Back", [self.make_file("doc-back.jpg")]))

        def _reply(index: int, call: _Call) -> Any:
            if call.is_consolidation:
                return _consolidation_reply()
            first_chunk = any("doc-page1.jpg" in url for url in call.images)
            return {
                "result": {
                    "k": {
                        "keywords": ["Document"],
                        "title": "t",
                        "category": "Document",
                        "ai_caption": "[AI Analysis]: a",
                        # No "transcriptions": this is the documented fallback a
                        # provider that ignores the new field takes, and the
                        # plan's promise is that it degrades to what a single
                        # call would have produced.
                        "caption": (
                            self._CHUNK_ONE_CAPTION if first_chunk else self._CHUNK_TWO_CAPTION
                        ),
                        "date_guess": {"iso": "1944", "confidence": 0.4, "pattern": "Y!"},
                        "location_guess": {"country": "France", "confidence": 0.3},
                        "proposed_new_keywords": [],
                    }
                }
            }

        record, calls = self.run_group(parts, 8, _reply)

        self.assertEqual(len(calls), 3)  # two page chunks + consolidation
        self.assertEqual(
            record["caption"],
            "[Page 1]\nfirst page\n"
            "[Page 6]\nsixth page\n"
            "[Page 7]\nseventh page\n"
            "[Page 10]\ntenth page\n"
            "[Front]\nfront text\n"
            "[Back]\nback text",
        )

    def test_a_sub_label_the_model_chose_stays_inside_its_own_part(self) -> None:
        # Only labels this payload named are moved. A model's own [Address] or
        # [Postmark] sub-label is part of that section's text and must travel
        # with it, not be hoisted out and sorted as though it were a part.
        parts = self.page_parts(10)
        parts.append(("Back", [self.make_file("doc-back.jpg")]))

        def _reply(index: int, call: _Call) -> Any:
            if call.is_consolidation:
                return _consolidation_reply()
            first_chunk = any("doc-page1.jpg" in url for url in call.images)
            caption = (
                "[Page 1]\nfirst page\n[Back]\n[Address]\nMom and Dad\n[Postmark]\nLE MANS"
                if first_chunk
                else "[Page 9]\nninth page"
            )
            return {
                "result": {
                    "k": {
                        "keywords": ["Document"],
                        "title": "t",
                        "category": "Document",
                        "ai_caption": "[AI Analysis]: a",
                        "caption": caption,
                        "date_guess": {"iso": "1944", "confidence": 0.4, "pattern": "Y!"},
                        "location_guess": {"country": "France", "confidence": 0.3},
                        "proposed_new_keywords": [],
                    }
                }
            }

        record, _calls = self.run_group(parts, 8, _reply)

        self.assertEqual(
            record["caption"],
            "[Page 1]\nfirst page\n"
            "[Page 9]\nninth page\n"
            "[Back]\n[Address]\nMom and Dad\n[Postmark]\nLE MANS",
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
