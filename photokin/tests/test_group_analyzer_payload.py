"""What the group analyzer may claim about its payload, and what it may bank.

Both cases here became reachable on the default path in Phase C1. Retiring the
primary bound the callee to the group's contents rather than to a flag, so a
group holding a negative or a page now takes ``analyze_group_parts`` even when
that group is a single file -- which is the commonest shape a negative has in an
archive. The prompt it prepends and the vocabulary it may write were both
written for the multi-image case.

Only the provider boundary is stubbed, so the real prompt assembly and the real
vocabulary-insert block run and are observable.
"""
import json
import os
import shutil
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from photokin import core, utils

_MULTIPLE_SCANS_CLAIM = "You are seeing multiple scans"
_SINGLE_SCAN_CLAIM = "You are seeing a single scan"


class _CapturedCall:
    """The one model call a stubbed run made."""

    def __init__(self) -> None:
        self.image_count: int = 0
        self.prompt_text: str = ""


@contextmanager
def _provider_stubbed(reply: dict) -> Iterator[_CapturedCall]:
    """Run the real analyzer against a canned reply, capturing what it sent.

    Args:
        reply: The parsed model reply the run should behave as though it got.

    Yields:
        The captured image count and joined prompt text.
    """
    captured = _CapturedCall()

    def _call_model(
        client: object,
        model: str,
        prompt_items: list[dict],
        urls: list[str],
        provider: str | None = None,
        dump_request: Any = None,
    ) -> dict:
        captured.image_count = len([url for url in urls if url])
        captured.prompt_text = "\n".join(
            item.get("text", "") for item in prompt_items if isinstance(item, dict)
        )
        return {}

    with (
        patch("photokin.core._build_provider_client", return_value=object()),
        patch("photokin.core._should_run_archival_upload", return_value=False),
        patch("photokin.core.call_model", _call_model),
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
        yield captured


class _AnalyzerTestCase(unittest.TestCase):
    """Base giving each test scratch files the analyzer will accept."""

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


class TestTheNoteMatchesThePayload(_AnalyzerTestCase):
    """The group note may not assert a payload the model can count for itself.

    ``analyze_group_parts`` is reached for its part labels as much as for its
    size: a lone negative and a lone album page are each one image that still
    has to be named as the part it is. Telling the model it is "seeing multiple
    scans or variants" of one object when it has been handed exactly one is
    contradicted by the request it arrived in.
    """

    def _prompt_for(self, parts: list[tuple[str, list[str]]]) -> _CapturedCall:
        """Analyze *parts* against a stub and return what was sent."""
        reply = {"result": {"k": {"caption": "c", "keywords": []}}}
        with _provider_stubbed(reply) as captured:
            core.analyze_group_parts(
                parts=parts,
                config=utils.Config(max_edge=None, no_update_vocab=True),
            )
        return captured

    def test_a_one_image_group_is_not_told_it_is_seeing_several(self) -> None:
        for label, names in (("a lone negative", ("box3_026-negative.jpg",)),
                             ("a stray album page", ("alb-page2.jpg",))):
            with self.subTest(label):
                (path,) = self.make_files(*names)
                captured = self._prompt_for([("Negative", [path])])

                self.assertEqual(captured.image_count, 1)
                self.assertNotIn(
                    _MULTIPLE_SCANS_CLAIM,
                    captured.prompt_text,
                    "the model was handed one image and told it was seeing several",
                )
                self.assertIn(_SINGLE_SCAN_CLAIM, captured.prompt_text)

    def test_the_part_label_still_travels_with_the_single_image(self) -> None:
        # The whole reason a one-image group takes this analyzer: the label is
        # what tells the model the image is a negative rather than the print.
        (path,) = self.make_files("box3_026-negative.jpg")

        captured = self._prompt_for([("Negative", [path])])

        self.assertIn("are Negative variants of the item", captured.prompt_text)

    def test_a_real_multi_image_group_still_says_multiple(self) -> None:
        front, back = self.make_files("box3_025.jpg", "box3_025-back.jpg")

        captured = self._prompt_for([("Front", [front]), ("Back", [back])])

        self.assertEqual(captured.image_count, 2)
        self.assertIn(_MULTIPLE_SCANS_CLAIM, captured.prompt_text)
        self.assertNotIn(_SINGLE_SCAN_CLAIM, captured.prompt_text)


class TestPartMarkersNeverEnterTheVocabulary(_AnalyzerTestCase):
    """A marker approved into the vocabulary is a token the pipeline then strips.

    Once the model is told a ``Negative`` part is present it emits "Negative" as
    a keyword, and the vocabulary-insert block runs on the raw model keywords --
    before the fan-out removes the marker from every file it does not describe.
    Banking one would put it in every subsequent prompt as an approved keyword,
    teaching the model to propose a token this same code defines as not being
    one. The vocabulary file ships with neither marker in it.
    """

    def _run_with_proposed(self, keywords: list[str]) -> list[str]:
        """Analyze one image whose reply proposes *keywords*, returning vocab additions.

        Args:
            keywords: Keywords the model returns, each also fully described in
                ``proposed_new_keywords`` so nothing but the guard under test
                can stop it being written.

        Returns:
            The lines the run added to its copy of the vocabulary file.
        """
        (path,) = self.make_files("box3_026-negative.jpg")
        vocab = os.path.join(self.work, "vocab_keywords_examples.toml")
        shutil.copy(utils.Config().vocab_path, vocab)
        with open(vocab, encoding="utf-8") as handle:
            before = handle.read().splitlines()

        reply = {
            "result": {
                "k": {
                    "caption": "c",
                    "keywords": list(keywords),
                    "proposed_new_keywords": [
                        {
                            "keyword": keyword,
                            "section": "photo_format",
                            "note": f"A described reason for {keyword}.",
                        }
                        for keyword in keywords
                    ],
                }
            }
        }
        with _provider_stubbed(reply):
            core.analyze_group_parts(
                parts=[("Negative", [path])],
                config=utils.Config(max_edge=None, vocab_path=vocab),
            )

        with open(vocab, encoding="utf-8") as handle:
            after = handle.read().splitlines()
        return [line for line in after if line not in before]

    def test_a_marker_is_refused_while_an_ordinary_keyword_beside_it_is_written(self) -> None:
        for marker in sorted(utils.PART_MARKER_KEYWORDS):
            with self.subTest(marker=marker):
                added = self._run_with_proposed([marker.title(), "seaside"])

                self.assertTrue(
                    any('keyword = "seaside"' in line for line in added),
                    "the fixture proves nothing if no keyword was written at all",
                )
                self.assertEqual(
                    [line for line in added if marker.lower() in line.lower()],
                    [],
                    f"{marker!r} was banked as an approved vocabulary keyword, and "
                    "the fan-out strips it from every file it does not describe",
                )


if __name__ == "__main__":
    unittest.main()
