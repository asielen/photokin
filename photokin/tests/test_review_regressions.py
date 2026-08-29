"""Regression tests for defects an adversarial review of document mode found.

Each test here exists to fail the moment its fix is silently reverted; the
docstring on each names the consequence the defect had, not just the
mechanism, so a future reader knows what is actually at stake. See
``docs/document-mode-contract.md`` (section 3's caption-synthesis rule was
amended as part of these fixes) and ``docs/document-mode.md`` for the shapes
these guard.

Companion cases that fit naturally beside an existing module's own harness
live there instead (``test_chunked_groups.py``, ``test_doc_sidecar.py``,
``test_sidecar_md.py``, ``test_transcriptions_contract.py``); this file holds
the cases that need their own end-to-end, repeated-run harness: the caption
dedup scoping, and -- added with per-page captions -- the accepted behavior of
an archive processed before them, which is a decision rather than a bug and is
pinned here so it does not get "fixed" later.

Only the provider boundary is stubbed, so the real prompt assembly, grouping,
merge and caption-block machinery all run and are observable.
"""
import json
import os
import tempfile
import typing
import unittest
from collections.abc import Iterator
from contextlib import contextmanager

from photokin import core, utils


@contextmanager
def _provider_stubbed(reply: dict) -> Iterator[None]:
    """Run the real analyzers against one canned parsed reply, every call.

    Args:
        reply: The parsed model reply every call in the block should behave
            as though it got.

    Yields:
        Nothing; the patches are active for the duration of the block.
    """
    from unittest.mock import patch

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


def _touch(directory: str, name: str) -> str:
    """Create an empty placeholder file named *name* and return its path."""
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8"):
        pass
    return path


class TestCaptionLineDedupIsScopedPerSection(unittest.TestCase):
    """A letterhead repeated inside one page's transcription is not erased.

    The caption block's line-level dedup used to compare every line against
    every other line in the WHOLE block, with no notion of which absorbed
    section a line came from. A document's transcription is absorbed as one
    unlabelled section (``[Page N]`` is deliberately not a recognized caption
    label -- contract section 3), so a letterhead printed twice, or a repeated
    "Dear Mother," salutation, sat inside that SAME section and its second
    occurrence was silently dropped -- losing real transcription, not an echo.

    Worse, the loss did not stay put: the STORED text (missing the second
    occurrence) no longer matched what the next ``-rw`` pass synthesized
    FRESH from the model's per-part transcriptions (which still repeats the
    line, because synthesis never deduplicates). The two no longer read as the
    same content, so the near-identical section gate could not recognize them
    as a restatement, both got kept, and the caption gained a stray fragment
    on every subsequent pass -- an unbounded, self-inflicted growth loop
    caused by the tool's own dedup fighting its own synthesis.

    Scoping the key to the section it was seen in fixes both: a repeat
    WITHIN one section (one sheet's own printed stationery, twice) is real
    content and is kept; a repeat ACROSS two different sections (an actual
    echo, e.g. a stored caption and a freshly synthesized near-twin of it) is
    still caught.

    Per-page captions narrowed the blast radius rather than removing it. The
    cross-page half of the original report -- a header on page 1 and again on
    page 2 -- is no longer one section's problem, because the two pages no
    longer share a caption at all; the within-a-page half is untouched and is
    what this case now feeds. Nothing here licenses teaching
    ``_CAPTION_LABEL_RE`` to match "[Page N]": doing that would section a
    document's stored caption by page and hand the cross-page repeat straight
    back to the dedup.
    """

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.work: str = scratch.name

    #: The model's transcription of a 3-page letter on printed stationery.
    #: The header appears at the top of pages 1 and 2 -- the cross-page repeat
    #: the bug description names -- and page 1 also carries it TWICE on its own
    #: sheet, as a header and again on a continuation slip. Both repeats matter
    #: now and for different reasons: the within-a-file one is what the
    #: section-scoped key protects directly, and the across-files one is what
    #: stopped being one block's problem when each page took its own caption.
    _TRANSCRIPTIONS: typing.ClassVar[dict[str, str]] = {
        "Page 1": (
            "Smith Family Stationery\n"
            "Dear Mother, we arrived safely.\n"
            "Smith Family Stationery\n"
            "(continued on the enclosed slip)"
        ),
        "Page 2": "Smith Family Stationery\nThe weather here is fine.",
        "Page 3": "Love, Sam",
    }

    def _run_once(self, items: list[dict]) -> dict:
        """Analyze *items* with the constant transcription reply above."""
        reply = {"result": {"k": {"transcriptions": self._TRANSCRIPTIONS, "keywords": ["Document"]}}}
        with _provider_stubbed(reply):
            return core.process_manifest_stream(
                manifest={"items": items},
                cfg=utils.Config(dry_run=True, max_edge=None, no_update_vocab=True),
            )

    def _pages(self) -> list[str]:
        """Create the three page files and return their paths in page order."""
        return [_touch(self.work, f"doc-page{n}.jpg") for n in (1, 2, 3)]

    def _rerun(self, paths: list[str], held: dict[str, str], passes: int) -> list[dict[str, str]]:
        """Run ``-rw`` in a loop, feeding each pass what the one before it wrote.

        Args:
            paths: The group's files.
            held: The caption each file already holds going into pass 1.
            passes: How many passes to run.

        Returns:
            One ``{path: caption}`` per pass, in order.
        """
        produced: list[dict[str, str]] = []
        for _pass in range(passes):
            items: list[dict[str, typing.Any]] = [
                {"path": p, "metadata": {"caption": held[p]}} if held.get(p) else {"path": p}
                for p in paths
            ]
            out = self._run_once(items)
            held = {p: out["results"][p]["caption"] for p in paths}
            produced.append(dict(held))
        return produced

    def test_the_repeat_survives_and_the_caption_settles_rather_than_growing(self) -> None:
        page1, page2, page3 = self._pages()
        paths = [page1, page2, page3]

        # Pass 1: nothing on disk yet, so each caption is built entirely from
        # this run's own fresh transcription -- the single-section case the
        # bug lived in. Page 1's letterhead is printed twice on page 1, and
        # both occurrences are real text, not an echo.
        first = self._rerun(paths, {}, 1)[0]
        self.assertEqual(
            first[page1].count("Smith Family Stationery"), 2,
            "the letterhead printed twice on page 1 was deduplicated down to "
            f"one occurrence, losing real text: {first[page1]!r}",
        )
        # Each page holds its own sheet and nobody else's. Before per-page
        # captions this was one block on all three files, which is what made
        # the cross-page repeat a same-section repeat in the first place.
        self.assertEqual(
            [first[p] for p in paths],
            [self._TRANSCRIPTIONS[label] for label in ("Page 1", "Page 2", "Page 3")],
        )

        # -rw in a loop: each pass reads back exactly what the pass before it
        # wrote, the way the real CLI's -rw does. From pass two onward every
        # file already holds its own settled caption, which is the steady
        # state an archive is actually in and the case most likely to grow
        # without bound if the dedup and the synthesis ever disagree about
        # what counts as a repeat.
        second, third, fourth = self._rerun(paths, first, 3)
        self.assertEqual(
            second, first,
            "re-reading the caption this tool itself wrote changed it on the "
            "very next pass -- under -rw that caption IS the next pass's "
            f"input, so anything that is not a fixed point grows: {second!r}",
        )
        self.assertEqual([third, fourth], [first, first])
        self.assertEqual(
            second[page1].count("Smith Family Stationery"), 2,
            "the repeated letterhead line did not survive into the settled, "
            f"repeatedly-read caption: {second[page1]!r}",
        )
        # The structural-tail growth the original bug produced: a stray
        # bracketed fragment appearing because a stored copy and a freshly
        # synthesized copy of the same content stopped comparing as
        # near-identical. A per-page caption carries no label at all, so any
        # "[Page 1]" here is a fragment nothing wrote on purpose.
        self.assertEqual(second[page1].count("[Page 1]"), 0, second[page1])


class TestAnArchiveWrittenBeforePerPageCaptionsIsLeftAlone(unittest.TestCase):
    """The 0.4.0 group block already in a document's files is a fixed point.

    There is no migration, by decision: an archive processed under the
    group-wide rule keeps the whole document's transcription in every page's
    Description, permanently, and photokin does not go looking for it. The
    consequence is a folder that ends up mixed -- documents analyzed from here
    on carry their own page, ones analyzed earlier carry the book -- and the
    thing that must NOT also be true is that re-running grows the old ones.

    It very nearly was. A stored group block re-reads as one unlabelled
    section, and this file's own fresh page text then re-reads as a second
    section whose every word is already in the first -- so the line dedup drops
    all of its content and, before this was fixed, kept its layout: the blank
    lines and the "---" footnote rule the conventions produce. Those were
    written back, read again next pass, and appended again. Measured at five
    characters of growth per run, forever, on exactly the archives the
    no-migration decision leaves in place.
    """

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.work: str = scratch.name

    #: Two pages of a letter, each ending in the "---" footnote rule the
    #: transcription conventions produce. The rule line is the whole hazard:
    #: it carries no word, so it is layout and is never treated as a repeat.
    _TRANSCRIPTIONS: typing.ClassVar[dict[str, str]] = {
        "Page 1": "Dear Mother, we arrived safely.\n---\n* the harbour, she means",
        "Page 2": "The weather here is fine.\n---\n* written in pencil",
    }

    def test_the_stored_block_neither_changes_nor_grows(self) -> None:
        pages = [_touch(self.work, f"old-page{n}.jpg") for n in (1, 2)]
        # What 0.4.0 wrote: the whole document, byte-identical on every file.
        legacy = "\n".join(
            f"[{label}]\n{text}" for label, text in self._TRANSCRIPTIONS.items()
        )
        reply = {
            "result": {"k": {"transcriptions": self._TRANSCRIPTIONS, "keywords": ["Document"]}}
        }

        held = {path: legacy for path in pages}
        for run_number in range(1, 6):
            items = [{"path": p, "metadata": {"caption": held[p]}} for p in pages]
            with _provider_stubbed(reply):
                out = core.process_manifest_stream(
                    manifest={"items": items},
                    cfg=utils.Config(dry_run=True, max_edge=None, no_update_vocab=True),
                )
            held = {p: out["results"][p]["caption"] for p in pages}
            with self.subTest(run=run_number):
                self.assertEqual(
                    held,
                    {path: legacy for path in pages},
                    "an archive processed before per-page captions was rewritten "
                    "by a re-run. It is meant to be left exactly as it is -- and "
                    "the way this fails is by accumulating the layout lines of a "
                    "section whose every word was already in the stored block",
                )


if __name__ == "__main__":
    unittest.main()
