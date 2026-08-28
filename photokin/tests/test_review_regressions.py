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
the one case -- the caption-block dedup scoping -- that needs its own
end-to-end, repeated-run harness and has no existing home for it (the closest
fit, ``test_read_flag_hazards.py``, is not one of this change's own files).

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
    """A letterhead repeated across two pages of one document is not erased.

    The caption block's line-level dedup used to compare every line against
    every other line in the WHOLE block, with no notion of which absorbed
    section a line came from. A multipage transcription is absorbed as one
    unlabelled section (``[Page N]`` is deliberately not a recognized caption
    label -- contract section 3), so a letterhead printed on page 1 and again
    on page 2, or a repeated "Dear Mother," salutation, sat inside that SAME
    section and its second occurrence was silently dropped -- losing real
    transcription, not an echo.

    Worse, the loss did not stay put: the STORED block (missing the second
    occurrence) no longer matched what the next ``-rw`` pass synthesized
    FRESH from the model's per-part transcriptions (which still repeats the
    line, because ``_synthesize_caption`` never deduplicates). The two no
    longer read as the same content, so the near-identical section gate could
    not recognize them as a restatement, both got kept, and the block gained
    a stray fragment on every subsequent pass -- an unbounded, self-inflicted
    growth loop caused by the tool's own dedup fighting its own synthesis.

    Scoping the key to the section it was seen in fixes both: a repeat
    WITHIN one section (two pages of the same transcription) is real content
    and is kept; a repeat ACROSS two different sections (an actual echo, e.g.
    a stored block and a freshly synthesized near-twin of it) is still
    caught.
    """

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.work: str = scratch.name

    #: The model's transcription of a 3-page letter whose stationery header
    #: is printed at the top of both page 1 and page 2 -- the exact shape the
    #: bug description names ("a letterhead... repeated across pages").
    _TRANSCRIPTIONS: typing.ClassVar[dict[str, str]] = {
        "Page 1": "Smith Family Stationery\nDear Mother, we arrived safely.",
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

    def test_the_repeat_survives_and_the_block_settles_rather_than_growing(self) -> None:
        page1, page2, page3 = (
            _touch(self.work, f"doc-page{n}.jpg") for n in (1, 2, 3)
        )
        paths = [page1, page2, page3]

        # Pass 1: nothing on disk yet, so the block is built entirely from
        # this run's own fresh transcription -- the single-section case the
        # bug lived in. The repeated letterhead line must appear TWICE, not
        # once.
        items = [{"path": p} for p in paths]
        first_out = self._run_once(items)
        first_captions = {p: first_out["results"][p]["caption"] for p in paths}
        self.assertEqual(
            len(set(first_captions.values())), 1,
            "the group's block was not identical across every file of the group",
        )
        first_block = first_captions[page1]
        self.assertEqual(
            first_block.count("Smith Family Stationery"), 2,
            "the letterhead printed on page 1 and again on page 2 was "
            f"deduplicated down to one occurrence, losing real text: {first_block!r}",
        )

        # -rw in a loop: each pass reads back exactly what the pass before it
        # wrote, the way the real CLI's -rw does. From the second pass
        # onward every file already holds a labelled copy of the block, so
        # this is the steady state a settled archive is actually in -- and
        # the case most likely to grow without bound if the dedup and the
        # synthesis ever disagree about what counts as a repeat.
        held = dict(first_captions)
        settled: list[dict[str, str]] = []
        for _pass in range(3):
            reread: list[dict[str, typing.Any]] = [
                {"path": p, "metadata": {"caption": held[p]}} for p in paths
            ]
            out = self._run_once(reread)
            held = {p: out["results"][p]["caption"] for p in paths}
            settled.append(dict(held))

        second, third, fourth = settled
        self.assertEqual(
            second, third,
            "re-reading the block this tool itself wrote changed it on the "
            "very next pass -- under -rw that block IS the next pass's "
            "input, so anything that is not a fixed point grows without "
            f"bound: {second!r} != {third!r}",
        )
        self.assertEqual(third, fourth)
        settled_block = second[page1]
        self.assertEqual(
            settled_block.count("Smith Family Stationery"), 2,
            "the repeated letterhead line did not survive into the settled, "
            f"repeatedly-read block: {settled_block!r}",
        )
        # The specific structural-tail growth the bug produced: an old
        # release's stored block gaining an extra bracketed fragment (here,
        # a stray trailing "[Page 1]") on every re-run because the stored
        # and freshly-synthesized copies of the same content no longer
        # compared as near-identical.
        self.assertEqual(
            settled_block.count("[Page 1]"), 1,
            f"the block accumulated an extra structural fragment: {settled_block!r}",
        )


if __name__ == "__main__":
    unittest.main()
