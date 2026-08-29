"""New coverage for the per-page caption rule (docs/per-page-captions.md, A3).

A1 rewrote the pinned tests that the plan's E6-E12 required scoping or
breaking (``test_read_flag_hazards.py::TestADocumentGivesEachPageItsOwnCaption``
and neighbors). This module is additional: it is not a duplicate of that
coverage, it drives the same production code from independent fixtures and
angles the plan called out by name -- content-level proof that two pages
truly differ (not just that they are unequal), the three distinct shapes of
E9's fallback with their INFO disclosure, E10's "whole map rides every file"
promise made explicit, and R2's accepted fixed point pinned deliberately so a
future reader finds a decision here rather than mistaking it for a bug.

The harness is borrowed rather than reinvented, per the plan's instruction:
``_CaptionBlockTestCase`` (record-building, block/scope extraction, the
``-r`` round trip through a real ExifTool stand-in) comes from
``test_read_flag_hazards.py``, which already carries the multipage-aware
convergence shape this module also uses.
"""

import logging
import unittest
from typing import ClassVar
from unittest.mock import patch

from photokin import core
from photokin.tests.test_read_flag_hazards import _CaptionBlockTestCase

_CORE_LOGGER = "photokin.core"


class TestEachPageGetsItsOwnDifferentCaption(_CaptionBlockTestCase):
    """Every file of a document gets a caption, and no two files get the same one.

    ``page_blocks`` already asserts "no two match"; the tests here go further
    and pin WHICH text each file got, so a bug that scrambled the mapping (page
    1 receiving page 3's text, say) would be caught even though it would still
    leave every file different from every other -- the failure mode
    ``page_blocks`` alone cannot see.
    """

    #: Four pages, not three, so a mapping bug that only shows up past the
    #: third entry (an off-by-one in ``page_nums_all`` ordering, say) has
    #: somewhere to hide from a smaller fixture and does not.
    LETTER: ClassVar[dict[str, str]] = {
        "ltr9_001-page1.jpg": "",
        "ltr9_001-page2.jpg": "",
        "ltr9_001-page3.jpg": "",
        "ltr9_001-page4.jpg": "",
    }
    PAGES: ClassVar[dict[str, str]] = {
        "Page 1": "Dearest Nan,",
        "Page 2": "The crossing took eleven days.",
        "Page 3": "Weather has finally turned.",
        "Page 4": "Your loving nephew, Will",
    }

    def test_each_file_holds_exactly_and_only_its_own_part(self) -> None:
        produced = self.page_blocks(self.LETTER, transcriptions=self.PAGES)
        self.assertEqual(
            produced,
            {
                "ltr9_001-page1.jpg": "Dearest Nan,",
                "ltr9_001-page2.jpg": "The crossing took eleven days.",
                "ltr9_001-page3.jpg": "Weather has finally turned.",
                "ltr9_001-page4.jpg": "Your loving nephew, Will",
            },
        )

    def test_no_files_content_leaks_into_a_neighbor(self) -> None:
        """The content-level version of "no two files match".

        Two captions being unequal is consistent with a bug that merely
        garbles one of them (appends a stray word, say); this checks each
        page's text against every OTHER page's transcription by substring,
        which a garbled-but-still-unique caption could still fail.
        """
        produced = self.blocks(self.LETTER, transcriptions=self.PAGES)
        for own_name, own_label in zip(self.LETTER, self.PAGES, strict=True):
            other_texts = [text for label, text in self.PAGES.items() if label != own_label]
            with self.subTest(file=own_name):
                for other_text in other_texts:
                    self.assertNotIn(other_text, produced[own_name])


class TestCaptionBlockAssemblyIsNotWastedOnAMultipageGroup(_CaptionBlockTestCase):
    """C2: a multipage group must never assemble a whole-group block it then
    discards.

    Before this fix, ``caption_block`` was built for every group -- multipage
    included -- even though a multipage group's per-file loop below it never
    reads that value: it assembles its own, narrower, per-file block instead,
    and its own fallback block from a narrower intake when a file has no
    transcription of its own. The discarded assembly folds every file's
    stored caption with pairwise comparisons, on exactly the long documents
    this feature exists to make scale, so the waste is not incidental.

    Pinned by behavior, per the task: ``core._assemble_caption_block`` is
    wrapped with a counting mock, and the call count is asserted directly
    rather than timed. For a multipage group whose files are individually
    attributed, the only calls are one per attributed file (building that
    file's own block) plus exactly one for the group's fallback intake, which
    is computed once regardless of whether any file ends up using it. A group
    block built and thrown away would add exactly one more call than that.
    """

    def _count_assemble_calls(self, captions: dict[str, str], **kwargs) -> int:
        real = core._assemble_caption_block
        with patch(
            "photokin.core._assemble_caption_block", side_effect=real
        ) as mock:
            self.records(captions, **kwargs)
        return mock.call_count

    def test_a_fully_attributed_multipage_group_never_builds_an_unused_group_block(
        self,
    ) -> None:
        group = {
            "doc11_001-page1.jpg": "",
            "doc11_001-page2.jpg": "",
            "doc11_001-page3.jpg": "",
        }
        pages = {
            "Page 1": "One.",
            "Page 2": "Two.",
            "Page 3": "Three.",
        }
        call_count = self._count_assemble_calls(group, transcriptions=pages)
        self.assertEqual(
            call_count,
            len(group) + 1,
            "expected one call per attributed file plus one for the fallback "
            "intake, and no extra call for a group-wide block nothing consumes",
        )

    def test_a_non_multipage_group_still_builds_exactly_one_group_block(self) -> None:
        group = {
            "box11_101.jpg": "First scan of the print",
            "box11_101b.jpg": "Second, cleaner scan",
            "box11_101b-back.jpg": "Return address on the back",
        }
        call_count = self._count_assemble_calls(group)
        self.assertEqual(call_count, 1, "a group of views of one object builds one block")


class TestVariantsOfOnePageShareOneCaption(_CaptionBlockTestCase):
    """The half of the rule that needed no new code: variants combine.

    ``page2.jpg`` and its rescan resolve to the same part label and so read
    the same entry out of ``transcriptions``. Proven here against a document
    that also holds distinct pages, so a bug that accidentally gave every
    file of the group the SAME caption could not pass by accident -- pages 1
    and 3 have to differ from page 2 as well as from each other.
    """

    GROUP: ClassVar[dict[str, str]] = {
        "ltr9_002-page1.jpg": "",
        "ltr9_002-page2.jpg": "",
        "ltr9_002b-page2.jpg": "",
        "ltr9_002-page3.jpg": "",
    }
    PAGES: ClassVar[dict[str, str]] = {
        "Page 1": "Dearest Nan,",
        "Page 2": "The crossing took eleven days.",
        "Page 3": "Your loving nephew, Will",
    }

    def test_the_rescan_matches_its_page_and_nothing_else(self) -> None:
        produced = self.blocks(self.GROUP, transcriptions=self.PAGES)

        self.assertEqual(produced["ltr9_002-page2.jpg"], "The crossing took eleven days.")
        self.assertEqual(produced["ltr9_002b-page2.jpg"], "The crossing took eleven days.")
        # And the pair sharing one caption is not because everything in the
        # group shares one caption -- the surrounding pages still differ.
        self.assertEqual(produced["ltr9_002-page1.jpg"], "Dearest Nan,")
        self.assertEqual(produced["ltr9_002-page3.jpg"], "Your loving nephew, Will")
        self.assertEqual(
            len(set(produced.values())),
            3,
            "four files, three distinct parts, should give three distinct captions",
        )

    def test_the_rescan_still_reads_as_a_resolved_part_not_a_fallback(self) -> None:
        """Both copies of page 2 resolve their part successfully -- ``"part"``
        for the rescan too, not a fallback, since the map answers the LABEL
        and both files travel under the same label.
        """
        scopes = self.scopes(self.GROUP, transcriptions=self.PAGES)
        self.assertEqual(set(scopes.values()), {"part"})


class TestNonMultipageGroupIsUnchanged(_CaptionBlockTestCase):
    """The common case, pinned byte-for-byte against what 0.4.0 wrote.

    A print, its second scan and that scan's back are views of one object, not
    a document, so ``multipage_present`` is false for this group and the
    per-file rule this plan adds never engages. The whole point of E6 (trigger
    on document-ness, not size) is that this shape is untouched; this class is
    the guard that proves it, independent of ``test_read_flag_hazards.py``'s
    own copy of the same guarantee.
    """

    #: A different worked example from the README's (two fronts, one back),
    #: so this pin does not merely re-run the same fixture A1's suite already
    #: covers under a different name.
    GROUP: ClassVar[dict[str, str]] = {
        "box9_101.jpg": "First scan of the print",
        "box9_101b.jpg": "Second, cleaner scan",
        "box9_101b-back.jpg": "Return address on the back",
    }

    def test_the_block_is_byte_identical_to_the_pre_change_shape(self) -> None:
        self.assertEqual(
            self.one_block(self.GROUP),
            "[Photo A] First scan of the print\n"
            "[Photo B] Second, cleaner scan\n"
            "[Back] Return address on the back\n"
            f"{self.ANALYSIS}",
        )

    def test_supplying_transcriptions_does_not_engage_the_per_file_rule(self) -> None:
        """A reply CAN carry ``transcriptions`` for a non-document group (a
        front/back pair the model chose to transcribe); E6's trigger is
        document-ness, not the presence of the key, so this group's block
        must come out exactly as it does with no ``transcriptions`` at all.
        """
        transcriptions = {"Front": "New front text", "Back": "New back text"}

        with_map = self.one_block(self.GROUP, transcriptions=transcriptions)
        without_map = self.one_block(self.GROUP)

        self.assertEqual(with_map, without_map)

    def test_no_file_carries_the_scope_key(self) -> None:
        self.assertEqual(set(self.scopes(self.GROUP).values()), {None})


class TestABackInADocumentGetsItsOwnText(_CaptionBlockTestCase):
    """E7: the rule is uniform within a multipage group, the back included.

    Independent fixture from A1's own back-in-a-document case: three files
    (two pages and a back) all answered, checked individually rather than
    only through ``page_blocks``, and every file's ``caption_scope`` pinned
    to ``"part"`` in the same test so the two claims -- right text, right
    scope -- cannot silently drift apart.
    """

    GROUP: ClassVar[dict[str, str]] = {
        "doc4_007-page1.jpg": "",
        "doc4_007-page2.jpg": "",
        "doc4_007-back.jpg": "",
    }
    TRANSCRIPTIONS: ClassVar[dict[str, str]] = {
        "Page 1": "To whom it may concern,",
        "Page 2": "Signed and witnessed below.",
        "Back": "Filed under estate papers, 1961.",
    }

    def test_the_back_gets_the_backs_own_transcription_not_the_book(self) -> None:
        records = self.records(self.GROUP, transcriptions=self.TRANSCRIPTIONS)

        self.assertEqual(records["doc4_007-page1.jpg"]["caption"], "To whom it may concern,")
        self.assertEqual(records["doc4_007-page2.jpg"]["caption"], "Signed and witnessed below.")
        self.assertEqual(
            records["doc4_007-back.jpg"]["caption"], "Filed under estate papers, 1961."
        )
        for name, record in records.items():
            with self.subTest(file=name):
                self.assertEqual(record["caption_scope"], "part")


class TestConvergenceAcrossRepeatedReadWritePasses(_CaptionBlockTestCase):
    """E8's convergence claim, run four passes deep.

    A per-file caption carries no label (E8), which is what lets it settle on
    the FIRST pass rather than the second: an unlabelled caption is read back
    as one unlabelled section, and this run's fresh text for the same page is
    then recognized as a restatement of what is already there and adds
    nothing. So the fixed point A1 reports is pass 1 itself -- there is no
    "run it once to seed the labels, then it holds" step the way the group
    block historically needed. Passes 2 through 4 below are asserted equal to
    pass 1, not merely to each other, to make exactly that claim.
    """

    LETTER: ClassVar[dict[str, str]] = {
        "ltr9_003-page1.jpg": "",
        "ltr9_003-page2.jpg": "",
        "ltr9_003-page3.jpg": "",
        "ltr9_003-back.jpg": "",
    }
    TRANSCRIPTIONS: ClassVar[dict[str, str]] = {
        "Page 1": "Dear Frank,",
        "Page 2": "The harvest was thin this year.",
        "Page 3": "We hope to see you at Easter.",
        "Back": "Postmarked Cork, March 1931.",
    }

    def test_four_passes_are_byte_identical_to_the_first(self) -> None:
        held = dict(self.LETTER)
        passes: list[dict[str, str]] = []
        for _ in range(4):
            held = self.blocks(held, transcriptions=self.TRANSCRIPTIONS)
            passes.append(dict(held))

        self.assertEqual(
            passes,
            [passes[0]] * 4,
            "a per-page caption is not a fixed point from the first pass",
        )
        self.assertEqual(
            passes[0],
            {
                "ltr9_003-page1.jpg": "Dear Frank,",
                "ltr9_003-page2.jpg": "The harvest was thin this year.",
                "ltr9_003-page3.jpg": "We hope to see you at Easter.",
                "ltr9_003-back.jpg": "Postmarked Cork, March 1931.",
            },
        )

    def test_the_scope_is_also_stable_across_passes(self) -> None:
        held = dict(self.LETTER)
        scope_snapshots: list[dict[str, str | None]] = []
        for _ in range(3):
            scope_snapshots.append(self.scopes(held, transcriptions=self.TRANSCRIPTIONS))
            held = self.blocks(held, transcriptions=self.TRANSCRIPTIONS)

        for snapshot in scope_snapshots:
            self.assertEqual(set(snapshot.values()), {"part"})


class TestTheFallbackKeepsTheGroupBlockThreeWays(_CaptionBlockTestCase):
    """E9, split into its three distinct triggers rather than one.

    Each of these is a different reason a file's resolved part label has no
    entry in ``transcriptions``, and the plan is explicit that all three must
    be handled the same way: keep the group block, disclose
    ``caption_scope: "group"``, and log once at INFO naming the group. A
    test that only ever exercises one of the three (usually "the map is
    missing entirely") would not catch a regression specific to the other
    two, e.g. a fix that special-cased an absent key but forgot a present key
    holding a non-string value, or a resolved label that is simply not one
    the map was ever going to have.
    """

    def test_no_transcriptions_at_all(self) -> None:
        """The reply omits the key entirely -- a provider that does not
        support per-part transcription, or one that declined to answer.
        """
        group = {
            "doc5_010-page1.jpg": "",
            "doc5_010-page2.jpg": "",
        }

        with self.assertLogs(_CORE_LOGGER, level=logging.INFO) as captured:
            produced = self.blocks(group)
            scopes = self.scopes(group)

        self.assertEqual(set(scopes.values()), {"group"})
        self.assertEqual(len(set(produced.values())), 1, "no map: every file keeps one block")

        info_lines = [r.getMessage() for r in captured.records if r.levelno == logging.INFO]
        self.assertTrue(
            any("doc5_010" in line and "keep the group" in line for line in info_lines),
            f"no INFO line named the group and explained the fallback: {info_lines!r}",
        )

    def test_a_partial_map_missing_this_files_page(self) -> None:
        """The map exists and answers most of the document, but this file's
        own page key is simply not one of the entries -- the shape a long
        document that failed part way through actually produces.
        """
        group = {
            "doc5_011-page1.jpg": "",
            "doc5_011-page2.jpg": "",
            "doc5_011-page3.jpg": "",
        }
        # Page 2 is the hole: present neither as a key nor a value.
        partial = {"Page 1": "Received your letter today.", "Page 3": "All well here."}

        with self.assertLogs(_CORE_LOGGER, level=logging.INFO) as captured:
            produced = self.blocks(group, transcriptions=partial)
            scopes = self.scopes(group, transcriptions=partial)

        self.assertEqual(produced["doc5_011-page1.jpg"], "Received your letter today.")
        self.assertEqual(produced["doc5_011-page3.jpg"], "All well here.")
        self.assertEqual(produced["doc5_011-page2.jpg"], self.ANALYSIS)
        self.assertEqual(
            scopes,
            {
                "doc5_011-page1.jpg": "part",
                "doc5_011-page2.jpg": "group",
                "doc5_011-page3.jpg": "part",
            },
        )

        info_lines = [r.getMessage() for r in captured.records if r.levelno == logging.INFO]
        self.assertTrue(
            any(
                "doc5_011" in line and "1 of 3" in line and "keep the group" in line
                for line in info_lines
            ),
            f"the INFO line did not name the group and the one held-back file: {info_lines!r}",
        )

    def test_a_displaced_or_unseated_file_whose_label_is_not_in_the_map(self) -> None:
        """The third shape, and the one that is not about the map at all.

        An untagged file and an explicit ``-page1`` file both claim the front
        side of the same variant; the untagged one is unseated (never sent to
        the model under any label) and resolves to ``Front`` -- a label a
        pure page-only ``transcriptions`` map never carries a key for, no
        matter how completely the model answered.
        """
        group = {
            "unseat3.jpg": "",
            "unseat3-page1.jpg": "",
            "unseat3-page2.jpg": "",
        }
        full_map = {"Page 1": "First leaf.", "Page 2": "Second leaf."}

        with self.assertLogs(_CORE_LOGGER, level=logging.INFO) as captured:
            records = self.records(group, transcriptions=full_map)

        self.assertEqual(records["unseat3-page1.jpg"]["caption"], "First leaf.")
        self.assertEqual(records["unseat3-page1.jpg"]["caption_scope"], "part")
        self.assertEqual(records["unseat3-page2.jpg"]["caption"], "Second leaf.")
        self.assertEqual(records["unseat3-page2.jpg"]["caption_scope"], "part")
        self.assertEqual(records["unseat3.jpg"]["caption_scope"], "group")
        self.assertEqual(records["unseat3.jpg"]["caption"], self.ANALYSIS)

        messages = [r.getMessage() for r in captured.records]
        self.assertTrue(
            any("both claim the front side" in m for m in messages),
            "the unseating itself must still be logged as it is today",
        )
        self.assertTrue(
            any(
                r.levelno == logging.INFO and "unseat3" in m and "keep the group" in m
                for r, m in zip(captured.records, messages, strict=True)
            ),
            f"no INFO line disclosed the fallback for the unseated file: {messages!r}",
        )


class TestTranscriptionsRidesEveryFileWhole(_CaptionBlockTestCase):
    """E10: the per-part map is not trimmed to the one entry a file's caption used.

    ``doc_sidecar.py`` reads a file's own entry out of the WHOLE map, so
    trimming ``transcriptions`` to the single part a file's ``caption`` drew
    from -- the obvious "while we're here" cut E10 names by name -- would
    silently break the sidecar. Checked here against every key, not just a
    count, so a regression that kept the right number of entries but the
    wrong ones would still be caught.
    """

    GROUP: ClassVar[dict[str, str]] = {
        "doc6_040-page1.jpg": "",
        "doc6_040-page2.jpg": "",
        "doc6_040-page3.jpg": "",
    }
    PAGES: ClassVar[dict[str, str]] = {
        "Page 1": "One.",
        "Page 2": "Two.",
        "Page 3": "Three.",
    }

    def test_every_page_key_is_present_on_every_files_record(self) -> None:
        records = self.records(self.GROUP, transcriptions=self.PAGES)

        for name, record in records.items():
            with self.subTest(file=name):
                self.assertEqual(record["transcriptions"], self.PAGES)

    def test_the_full_map_rides_even_though_the_caption_holds_one_entry(self) -> None:
        """States the contrast E10 is about explicitly: the record's two
        caption-shaped fields disagree in scope on purpose.
        """
        record = self.records(self.GROUP, transcriptions=self.PAGES)["doc6_040-page2.jpg"]

        self.assertEqual(record["caption"], "Two.")
        self.assertNotIn("One.", record["caption"])
        self.assertNotIn("Three.", record["caption"])
        self.assertEqual(set(record["transcriptions"]), {"Page 1", "Page 2", "Page 3"})
        self.assertEqual(record["transcriptions"]["Page 1"], "One.")
        self.assertEqual(record["transcriptions"]["Page 3"], "Three.")


class TestAnAlreadyProcessedArchiveKeepsItsFatCaption(_CaptionBlockTestCase):
    """R2 / E12, pinned as a decision rather than a bug.

    An archive a pre-0.5.0 release already wrote holds the whole group's
    block, byte-identical, on every file's ``XMP-dc:Description`` -- that is
    exactly what the group-block-only regime (still built today, still the
    E9 fallback) produces. E12 chose NOT to migrate that: the maintainer
    decision recorded in the plan is that reconciling it would only ever have
    fired when the model re-transcribed a page identically, which 0.4.0's own
    prose-flow change makes unlikely on exactly the first post-upgrade run --
    machinery that mostly would not fire, in the most sensitive function in
    the codebase.

    So the accepted behavior is not merely "nothing crashes"; it is that the
    old block is a STABLE FIXED POINT of the new per-file rule, measured here
    over three passes. Mechanically: the old block re-reads as ONE section
    (``[Page N]`` is not a recognized label, so a 0.4.0-era whole-document
    transcription is one long run of unattributed prose start to finish, per
    R2); this file's own fresh page text is then absorbed as a second,
    unlabelled section, and every one of ITS lines is already present,
    word-for-word, among the old section's lines -- so the cross-section
    line dedup drops the new section wholesale as an echo, and the file comes
    back holding exactly the block it went in with. photokin does not go
    looking for this shape to clean it up (deferred item in plan section 5,
    "a flag to keep the old group-wide caption" was declined for the same
    reason in reverse).
    """

    GROUP: ClassVar[dict[str, str]] = {
        "arc7_200-page1.jpg": "",
        "arc7_200-page2.jpg": "",
        "arc7_200-page3.jpg": "",
    }
    PAGES: ClassVar[dict[str, str]] = {
        "Page 1": "Dear Editor,",
        "Page 2": "I write regarding your April issue.",
        "Page 3": "Yours faithfully, R. Voss",
    }

    def _seed_pre_0_5_0_archive(self) -> dict[str, str]:
        """Return ``{filename: block}`` mimicking what 0.4.0 already wrote.

        Produced by the real group-block builder (no ``transcriptions`` at
        all, so every file starts empty and the block is exactly this run's
        own ``caption`` reply -- the whole document as one unlabelled
        transcription, which is what a 0.4.0 model reply looked like before
        ``transcriptions`` existed) rather than typed by hand, so the seed is
        provably what the builder actually emits and not a guess at it. It is
        deliberately built from ``analysis``, not ``transcriptions``: feeding
        the per-part map here would already exercise the per-file rule this
        class means to seed AROUND, not through.
        """
        whole_document = "\n".join(self.PAGES.values())
        seeded = self.one_block(self.GROUP, analysis=whole_document)
        return dict.fromkeys(self.GROUP, seeded)

    def test_the_seeded_block_is_group_scoped_before_the_upgrade_scenario_starts(self) -> None:
        """Non-vacuity check on the seed itself: if the fallback path stopped
        producing one shared, unlabelled block holding every page's text,
        the rest of this class would be proving nothing about R2 at all.
        """
        seeded_captions = self._seed_pre_0_5_0_archive()
        seeded_block = next(iter(seeded_captions.values()))
        self.assertEqual(len(set(seeded_captions.values())), 1)
        for text in self.PAGES.values():
            self.assertIn(text, seeded_block)

    def test_the_old_block_survives_a_run_with_a_full_transcriptions_map(self) -> None:
        seeded_captions = self._seed_pre_0_5_0_archive()
        seeded_block = next(iter(seeded_captions.values()))

        produced = self.blocks(seeded_captions, transcriptions=self.PAGES)

        for name, caption in produced.items():
            with self.subTest(file=name):
                self.assertEqual(
                    caption,
                    seeded_block,
                    "the pre-existing group block did not survive the per-file rule",
                )

    def test_the_scope_key_still_reads_part_even_though_the_text_did_not_change(self) -> None:
        """The subtle half of R2: the file's resolved part label IS in the
        map and IS what the block already said, so the record's disclosure
        is ``"part"`` -- correctly, since an attribution IS being made, it
        just happens to add no new text because the file already had it.
        ``caption_scope`` describes how the caption was BUILT, not whether it
        changed.
        """
        seeded_captions = self._seed_pre_0_5_0_archive()

        scopes = self.scopes(seeded_captions, transcriptions=self.PAGES)

        self.assertEqual(set(scopes.values()), {"part"})

    def test_the_fixed_point_holds_for_three_successive_passes(self) -> None:
        held = self._seed_pre_0_5_0_archive()
        seeded_block = next(iter(held.values()))

        for _ in range(3):
            held = self.blocks(held, transcriptions=self.PAGES)
            self.assertEqual(
                set(held.values()),
                {seeded_block},
                "the group block drifted from its seed under repeated -rw passes",
            )


class TestAnUnseatedFileDoesNotInheritAnotherFilesText(_CaptionBlockTestCase):
    """F1: resolving a label is not the same as having ridden the payload.

    ``resolve_part_label`` only maps a manifest slot to the name the payload
    vocabulary uses for it; it has no idea whether the payload actually
    carried that file. An untagged scan unseated by a real ``-page1`` file
    still resolves to ``Front`` -- the same label a genuine ``-front`` file in
    the same group travels under -- and before this was guarded, the lookup
    below found whatever text the ``-front`` file's own label earned and
    wrote it into the unseated file's Description under
    ``caption_scope: "part"``: an affirmative claim that the attribution was
    made on purpose, for a file that was never even sent to the model.
    ``analyzed_paths`` is the set the payload actually carried, and checking
    a file against it is what tells the two situations apart.
    """

    GROUP: ClassVar[dict[str, str]] = {
        "ltr.jpg": "",
        "ltr-front.jpg": "",
        "ltr-page1.jpg": "",
        "ltr-page2.jpg": "",
    }
    TRANSCRIPTIONS: ClassVar[dict[str, str]] = {
        "Front": "Cover sheet, filed 1962.",
        "Page 1": "Dear Committee,",
        "Page 2": "We regret to inform you.",
    }

    def test_the_unseated_untagged_file_does_not_inherit_the_fronts_text(self) -> None:
        records = self.records(self.GROUP, transcriptions=self.TRANSCRIPTIONS)

        # ltr.jpg lost its slot to ltr-page1.jpg (the multipage guardrail's
        # holder for the front side) and was never in the payload, yet
        # ``resolve_part_label`` still resolves it to "Front" -- the very
        # label ltr-front.jpg travelled under and DOES have an answer for.
        self.assertEqual(records["ltr.jpg"]["caption_scope"], "group")
        self.assertEqual(records["ltr.jpg"]["caption"], self.ANALYSIS)
        self.assertNotIn("Cover sheet, filed 1962.", records["ltr.jpg"]["caption"])

        # The file that actually rode the "Front" label gets its text, with
        # an affirmative scope -- proving the map lookup itself still works,
        # so the guard above is not merely suppressing every attribution.
        self.assertEqual(records["ltr-front.jpg"]["caption_scope"], "part")
        self.assertEqual(records["ltr-front.jpg"]["caption"], "Cover sheet, filed 1962.")

        self.assertEqual(records["ltr-page1.jpg"]["caption"], "Dear Committee,")
        self.assertEqual(records["ltr-page1.jpg"]["caption_scope"], "part")
        self.assertEqual(records["ltr-page2.jpg"]["caption"], "We regret to inform you.")
        self.assertEqual(records["ltr-page2.jpg"]["caption_scope"], "part")


class TestTheFallbackBlockOnlyDrawsFromFilesThatAlsoFellBack(_CaptionBlockTestCase):
    """F2: the fallback block is assembled from the fallen-back files alone.

    Before this fix the block a file with no page of its own fell back to was
    ``caption_block`` -- the ordinary whole-group intake, built from every
    file's stored caption regardless of whether that file got its own
    transcription this run. From the second ``-rw`` pass onward, an answered
    page's *stored* caption is the thin per-page text this rule itself just
    wrote, so sweeping it back into another file's fallback handed that file
    an answered page's words under a ``[Photo N]`` label -- attributing one
    page's writing to another, and growing by one step every pass a document
    stayed partially answered. The fix narrows the fallback's own intake to
    only the files that, this run, also had nothing of their own.
    """

    def test_pure_fallback_still_reproduces_the_ordinary_group_block(self) -> None:
        """No ``transcriptions`` at all: every file falls back together.

        This is the half that guards against over-correcting the narrowed
        intake -- it broke once already while this fix was being written,
        when a draft that filtered on "was this file sent to the model"
        rather than "did this file get its own page" left every file here
        counted as sent and so excluded from its own fallback, producing a
        block with none of the group's existing captions in it at all. When
        every file falls back together the result must still be exactly the
        historic whole-group merge: one identical, fully-labeled block on
        every file.
        """
        group = {
            "doc8_100-page1.jpg": "Filed at the courthouse, 1958.",
            "doc8_100-page2.jpg": "Second sheet, water damaged corner.",
            "doc8_100-back.jpg": "Postal stamp, illegible date.",
        }
        block = self.one_block(group)
        self.assertEqual(
            block,
            "[Photo 1] Filed at the courthouse, 1958.\n"
            "[Photo 2] Second sheet, water damaged corner.\n"
            "[Back] Postal stamp, illegible date.\n"
            f"{self.ANALYSIS}",
        )
        self.assertEqual(set(self.scopes(group).values()), {"group"})

    def test_a_partial_map_settles_at_pass_two_without_leaking_answered_pages(self) -> None:
        """Six pages, two never answered: the unanswered pair must never pick
        up an answered page's text, across repeated ``-rw`` passes.

        The honest limit, stated rather than hidden: unlabelled stored prose
        is attributed to the file it was read off on the very next read --
        pre-existing behavior every multipage file had before this change,
        not a regression it introduces -- so the two unanswered files do not
        settle until pass 2, not pass 1. What matters is that pass 2 onward
        never contains a *different* page's answered text, and that pass 2
        is itself a fixed point.
        """
        group = {f"doc9_050-page{n}.jpg": "" for n in range(1, 7)}
        pages = {
            "Page 1": "The deed was recorded in March.",
            "Page 2": "Two witnesses signed below.",
            "Page 5": "A survey map is attached separately.",
            "Page 6": "Filed with the county clerk.",
        }
        # Pages 3 and 4 are the hole: present in neither the map's keys nor
        # its values, so both fall back on every pass.
        held = dict(group)
        held_passes: list[dict[str, str]] = []
        for _ in range(3):
            held = self.blocks(held, transcriptions=pages)
            held_passes.append(dict(held))
        pass1, pass2, pass3 = held_passes

        self.assertNotEqual(
            pass1, pass2, "expected the unanswered pair to still be moving at pass 1"
        )
        self.assertEqual(pass2, pass3, "the unanswered pair did not settle by pass 2")

        for held_pass in (pass2, pass3):
            for name in ("doc9_050-page3.jpg", "doc9_050-page4.jpg"):
                for label, text in pages.items():
                    with self.subTest(name=name, label=label):
                        self.assertNotIn(
                            text,
                            held_pass[name],
                            f"{name} absorbed {label}'s answered text via the fallback block",
                        )


if __name__ == "__main__":
    unittest.main()
