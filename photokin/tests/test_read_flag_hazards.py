"""What ``-r`` makes newly reachable, and the guards that keep it safe.

Reading a file's own metadata turns on code paths that were dead while folder
items carried nothing but a path. Each class here pins one of them against the
behavior it replaces, so a regression cannot pass by staying silent:

- the ``DATE:`` interlock, the only human veto on the date-correction heuristic
- ExifTool batching, without which a large folder reads nothing at all
- the caption block: what it is, how a group's captions are merged into it, and
  the fact that it has to survive being re-fed its own output on every file
- title precedence, which depends on where the original title came from
- which file of a group its shared metadata is taken from
- and, going the other way, which of those values a file overrules with its own
"""

import difflib
import itertools
import json
import os
import tempfile
import typing
import unittest
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

from photokin import core, utils
from photokin.canonical import build_canonical_patch
from photokin.exiftool import ExiftoolConfig
from photokin.exiftool.hydrate import hydrate_item_metadata, make_manifest_hydrator
from photokin.exiftool.manifest import (
    _ARGV_BUDGET,
    DEFAULT_EXIFTOOL_FIELDS,
    run_exiftool_json,
)


def _touch(directory: str, name: str) -> str:
    """Create an empty file and return its path."""
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8"):
        pass
    return path


def _tag_hydrator(store: dict[str, dict[str, Any]]) -> Callable[[list[dict]], None]:
    """Return a hydrator reading ``store`` as if it were the files' own tags."""

    def _records(*, exiftool_path, files, fields, timeout_sec=None, **_kw):
        return [
            {"SourceFile": f.replace("\\", "/"), **store.get(utils.normalize_path(f), {})}
            for f in files
        ]

    def _hydrate(items: list[dict]) -> None:
        with patch(
            "photokin.exiftool.hydrate.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch("photokin.exiftool.manifest.run_exiftool_json", _records):
            hydrate_item_metadata(items, ExiftoolConfig())

    return _hydrate


def _run(
    items: list[dict],
    reply: dict,
    *,
    hydrator=None,
    cfg=None,
    from_files: bool = False,
) -> dict:
    """Run one manifest through the stream with a stubbed provider.

    ``from_files`` is the caller's provenance claim about what ``hydrator``
    supplied, which is what the CLI sets from ``-r``. It is deliberately not
    derived from ``hydrator`` here: the two being independent is the thing
    :class:`TestTitlePrecedenceDependsOnProvenance` has to be able to vary.
    """

    def _answer(first: str) -> dict:
        return {"result": {first: json.loads(json.dumps(reply))}}

    def _analyze(front, back=None, config=None, *, original_meta=None, write_sidecar=False):
        return _answer(front)

    def _front_back(fronts, backs, config=None, *, original_meta=None, write_sidecar=False):
        return _answer((list(fronts or []) + list(backs or []))[0])

    def _parts(parts, config=None, *, original_meta=None, write_sidecar=False):
        return _answer(next(path for _label, paths in parts for path in paths))

    # All three entry points, not just the pair one: a group holding a second
    # scan of a side is sent as a set, so a case about several captions in one
    # group reaches ``analyze_group_front_back`` and would otherwise call the
    # real provider.
    with patch("photokin.core.analyze_photo", _analyze), patch(
        "photokin.core.analyze_group_front_back", _front_back
    ), patch("photokin.core.analyze_group_parts", _parts):
        return core.process_manifest_stream(
            manifest={"items": items},
            cfg=cfg or utils.Config(),
            metadata_hydrator=hydrator,
            titles_may_be_from_files=from_files,
        )


class TestTheDateKeywordInterlockSurvivesHydration(unittest.TestCase):
    """A hand-reviewed ``DATE:`` marker still vetoes the date-correction rule.

    ``-r`` reads ``EXIF:DateTimeOriginal``, which is what brings the gap
    heuristic to life in folder mode -- it compares the file's date against the
    model's inference and rewrites the file's when they diverge widely. Its only
    human interlock is a ``DATE:`` keyword, and that keyword lives in
    ``XMP:Subject``. Reading the date without it would arm the heuristic and
    disable its safety on the very photos an archivist has already dated.
    """

    #: 0.75 clears date_override_confidence_threshold (0.7) and stays under the
    #: precise variant (0.8), so the 32-year gap is judged against the wide
    #: date_override_year_gap (20) and nothing but the keyword can stop the
    #: rewrite. It has to clear the override gate rather than the write gate:
    #: this file already holds a date, so the rule under test is the one that
    #: replaces one, which is the dearer of the two on purpose.
    REPLY: typing.ClassVar[dict[str, Any]] = {
        "caption": "Two men beside a car",
        "keywords": ["automobile"],
        "date_guess": {
            "iso": "1920",
            "import_date": "1920-01-01",
            "confidence": 0.75,
            "pattern": "Y~",
        },
    }

    def _merged(self, subject: list[str]) -> dict:
        work = tempfile.mkdtemp(prefix="pk-datekw-")
        path = _touch(work, "box3_014.jpg")
        store = {
            utils.normalize_path(path): {
                "EXIF:DateTimeOriginal": "1952:06:01 00:00:00",
                "XMP:Subject": subject,
            }
        }
        out = _run([{"path": path}], self.REPLY, hydrator=_tag_hydrator(store))
        return out["results"][path]

    def test_the_keywords_tag_is_in_the_read_set(self):
        self.assertIn("XMP:Subject", DEFAULT_EXIFTOOL_FIELDS)

    def test_a_reviewed_date_keyword_stops_the_rewrite(self):
        merged = self._merged(["family", "DATE: Y!"])
        self.assertEqual(merged["dateTimeOriginal"], "1952:06:01 00:00:00")
        self.assertNotIn("dateTimeOriginal", merged["_merge"]["overrides"])
        # And no second, contradicting marker is added beside the human's.
        markers = [k for k in merged["keywords"] if k.upper().startswith("DATE:")]
        self.assertEqual(markers, ["DATE: Y!"])

    def test_the_file_keywords_reach_the_record(self):
        merged = self._merged(["family", "DATE: Y!"])
        self.assertIn("family", merged["keywords"])

    def test_without_the_marker_the_heuristic_still_fires(self):
        """Non-vacuity: the veto is the keyword, not something else in the setup."""
        merged = self._merged(["family"])
        self.assertEqual(merged["dateTimeOriginal"], "1920-01-01")
        self.assertIn("dateTimeOriginal", merged["_merge"]["overrides"])

    def test_a_single_valued_subject_is_not_split_into_characters(self):
        """ExifTool returns a one-value tag as a bare string, not a list."""
        merged = self._merged("DATE: Y!")
        self.assertEqual(merged["dateTimeOriginal"], "1952:06:01 00:00:00")

    def test_the_marker_still_vetoes_when_the_item_already_has_other_keywords(self):
        """"Missing or empty" must not blind the interlock to a marker on disk.

        An item can arrive with its own keywords already set -- a Lightroom
        export, a prior non-photokin tool -- unrelated to the ``DATE:``
        marker a human wrote onto the *file* in an earlier photokin run.
        ``dateTimeOriginal`` is still missing from the manifest, so it still
        arms the heuristic; the file's ``XMP:Subject`` has to be checked for
        the marker regardless, or the interlock protects nothing whenever a
        caller happens to have supplied any keywords at all.
        """
        work = tempfile.mkdtemp(prefix="pk-datekw-existing-")
        path = _touch(work, "box3_015.jpg")
        store = {
            utils.normalize_path(path): {
                "EXIF:DateTimeOriginal": "1952:06:01 00:00:00",
                "XMP:Subject": ["family", "DATE: Y!"],
            }
        }
        out = _run(
            [{"path": path, "metadata": {"keywords": ["some-other-tag"]}}],
            self.REPLY,
            hydrator=_tag_hydrator(store),
        )
        merged = out["results"][path]
        self.assertEqual(merged["dateTimeOriginal"], "1952:06:01 00:00:00")
        self.assertNotIn("dateTimeOriginal", merged["_merge"]["overrides"])
        # The caller's own keyword survives; the marker is added beside it
        # rather than the whole list being replaced by the file's.
        self.assertIn("some-other-tag", merged["keywords"])
        self.assertIn("DATE: Y!", merged["keywords"])


class TestTheReadIsBatchedForLargeFolders(unittest.TestCase):
    """A folder too large for one command line is still read in full.

    Windows caps a command line at 32767 characters and fails past it with
    ``[WinError 206]``, which ``subprocess`` raises as ``FileNotFoundError`` --
    which ``run_exiftool_json`` used to re-wrap as "ExifTool not found" and the
    hydrator swallowed as a warning. The run then paid for every model call with
    an un-hydrated prompt and exited 0.
    """

    def _capture(self, count: int, name_width: int = 60):
        directory = "C:\\archive\\box3" if os.name == "nt" else "/archive/box3"
        files = [os.path.join(directory, f"{i:0{name_width}d}.jpg") for i in range(count)]
        seen: list[list[str]] = []

        class _Proc:
            returncode = 0
            stderr = ""

            def __init__(self, cmd):
                paths = [a for a in cmd if not a.startswith("-") and a.endswith(".jpg")]
                seen.append(paths)
                self.stdout = json.dumps(
                    [{"SourceFile": p.replace("\\", "/"), "XMP:Title": "T"} for p in paths]
                )

        with patch(
            "photokin.exiftool.manifest.subprocess.run",
            lambda cmd, **kw: _Proc(cmd),
        ):
            records = run_exiftool_json(
                exiftool_path="exiftool",
                files=files,
                fields=list(DEFAULT_EXIFTOOL_FIELDS),
            )
        return files, seen, records

    def test_a_small_list_is_still_one_invocation(self):
        _files, seen, records = self._capture(10)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(records), 10)

    def test_a_large_list_is_split_and_read_in_full(self):
        files, seen, records = self._capture(900)
        self.assertGreater(len(seen), 1)
        # Every path is requested exactly once, in the order it was given.
        self.assertEqual([p for batch in seen for p in batch], files)
        self.assertEqual(len(records), len(files))

    def test_no_invocation_exceeds_the_budget(self):
        _files, seen, _records = self._capture(900)
        for batch in seen:
            self.assertLessEqual(sum(len(p) + 3 for p in batch), _ARGV_BUDGET)

    def test_hydration_covers_every_item_of_a_large_folder(self):
        directory = "C:\\archive\\box9" if os.name == "nt" else "/archive/box9"
        items = [{"path": os.path.join(directory, f"{i:060d}.jpg")} for i in range(700)]
        store = {utils.normalize_path(it["path"]): {"XMP:Title": "T"} for it in items}
        _tag_hydrator(store)(items)
        self.assertEqual(sum(1 for it in items if it.get("metadata")), len(items))


class _CaptionBlockTestCase(unittest.TestCase):
    """A group of scans, each holding its own caption, run end to end.

    Every case below is written as ``{filename: the caption that file already
    holds}``, because that is what the feature is about: the filenames decide the
    group's shape and so its labels, and the captions are what gets merged. The
    files are real placeholders in a scratch directory, so the grammar reading
    them is the one a folder run reads.
    """

    #: Whole caption blocks are compared, and a truncated diff would say two
    #: runs differed without saying where.
    maxDiff = None

    #: This run's own transcription (the model's ``caption`` reply), held fixed
    #: everywhere, so any difference between two blocks comes from the files
    #: rather than from the model. Named for what it stood in for before this
    #: run's own transcription and the model's separate ``ai_caption``
    #: interpretation were pulled apart into Description and UserComment.
    ANALYSIS = "Two people outside a bakery."

    def records(
        self,
        captions: dict[str, str],
        *,
        group_by: str = utils.GROUP_BY_OBJECT,
        analysis: str | None = None,
        transcriptions: dict[str, str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Run one set of files and return each one's record, keyed by basename.

        The captions are supplied through the ExifTool stand-in rather than as
        manifest metadata, so every case travels the ``-r`` path the growth bug
        lived on: what the run reads is what a previous run wrote into the file.

        Args:
            captions: ``{filename: existing caption}``; an empty value is a file
                holding none.
            group_by: Grouping granularity, as ``--group-by`` sets it.
            analysis: What the model returns for this run.
            transcriptions: The per-part map the model returns, or ``None`` for
                a reply carrying none. A multipage group with a map is the one
                shape whose caption is built per file rather than per group.

        Returns:
            ``{basename: merged record}`` for every file.
        """
        work = tempfile.mkdtemp(prefix="pk-block-")
        paths = {name: _touch(work, name) for name in captions}
        store = {
            utils.normalize_path(path): (
                {"XMP:Description": captions[name]} if captions[name] else {}
            )
            for name, path in paths.items()
        }
        reply: dict[str, Any] = {
            "caption": self.ANALYSIS if analysis is None else analysis,
            "keywords": [],
        }
        if transcriptions is not None:
            reply["transcriptions"] = transcriptions
        out = _run(
            [{"path": paths[name]} for name in captions],
            reply,
            hydrator=_tag_hydrator(store),
            cfg=utils.Config(group_by=group_by),
            from_files=True,
        )
        return {os.path.basename(path): out["results"][path] for path in paths.values()}

    def blocks(self, captions: dict[str, str], **kwargs: Any) -> dict[str, str]:
        """Return each file's caption, keyed by basename.

        Args:
            captions: ``{filename: existing caption}``, as :meth:`records` takes.
            **kwargs: Passed straight to :meth:`records`.

        Returns:
            ``{basename: caption}`` for every file.
        """
        return {
            name: record.get("caption") or ""
            for name, record in self.records(captions, **kwargs).items()
        }

    def scopes(self, captions: dict[str, str], **kwargs: Any) -> dict[str, str | None]:
        """Return each file's disclosed ``caption_scope``, keyed by basename.

        Args:
            captions: ``{filename: existing caption}``, as :meth:`records` takes.
            **kwargs: Passed straight to :meth:`records`.

        Returns:
            ``{basename: caption_scope}``, the value being ``None`` on a record
            that carries no such key.
        """
        return {
            name: record.get("caption_scope")
            for name, record in self.records(captions, **kwargs).items()
        }

    def one_block(self, captions: dict[str, str], **kwargs: Any) -> str:
        """Return the single block a group produced, asserting every file has it.

        Identical-on-every-file is the feature for every group except a
        multipage document: a print, its back and a rescan are one object, and
        which of them someone opens a year later is an accident of how they were
        browsing. It is asserted here rather than in one test of its own so no
        case below can accidentally assert about a block only one file of the
        group received. The document case has :meth:`page_blocks` instead, which
        makes the opposite claim just as hard to sidestep.
        """
        produced = self.blocks(captions, **kwargs)
        self.assertEqual(
            sorted(produced), sorted(captions), "a file came back with no record"
        )
        distinct = set(produced.values())
        self.assertEqual(
            len(distinct),
            1,
            "the files of one group hold different captions. The block is built "
            f"for the group and written to every member of it: {produced!r}",
        )
        return distinct.pop()

    def page_blocks(self, captions: dict[str, str], **kwargs: Any) -> dict[str, str]:
        """Return a document's per-file captions, asserting no two files match.

        The multipage counterpart of :meth:`one_block`, and it exists for the
        same reason. The failure this feature is most likely to regress into is
        every page quietly holding the whole book again, and a case asserting
        only that page 1 reads "Dear Ruth," would still pass under it: page 1's
        own text is the first thing in the group block too.
        """
        produced = self.blocks(captions, **kwargs)
        self.assertEqual(
            sorted(produced), sorted(captions), "a file came back with no record"
        )
        self.assertEqual(
            len(set(produced.values())),
            len(produced),
            "two files of a document hold the same caption, so at least one of "
            f"them is not carrying its own page: {produced!r}",
        )
        return produced


class TestTheBlockIsTheWholeGroupsStory(_CaptionBlockTestCase):
    """Every file carries every scan's caption, labelled by where it came from.

    A print, its back and a rescan of it are one object, so whichever of them
    someone opens should tell the whole story of that object rather than a third
    of it. That is only possible if the block is built for the GROUP -- each
    file's own caption attributed to that file while it is still known which file
    it came off -- and then written to all of them. A block computed per file
    would carry that file's own caption as a personal preamble, no two would
    match, and opening one file would tell you just what it told you before.
    """

    #: The worked example, and the one the README shows: two scans of a print,
    #: the second of which was scanned back and front.
    GROUP: typing.ClassVar[dict[str, str]] = {
        "box3_017.jpg": "Caption A",
        "box3_017b.jpg": "Caption B",
        "box3_017b-back.jpg": "Back of Photo B",
    }

    def test_the_block_is_the_documented_shape(self):
        self.assertEqual(
            self.one_block(self.GROUP),
            "[Photo A] Caption A\n"
            "[Photo B] Caption B\n"
            "[Back] Back of Photo B\n"
            f"{self.ANALYSIS}",
        )

    def test_the_default_grouping_is_where_this_had_to_start_working(self):
        """``--group-by object`` is the default, and it used to label nothing.

        The labelling code was reachable only under ``pair`` and ``none``: the
        object path reused the model's own caption verbatim and added no labels
        at all, so the overwhelming majority of runs saw none of this.
        """
        self.assertEqual(utils.Config().group_by, utils.GROUP_BY_OBJECT)
        self.assertIn("[Photo A] Caption A", self.one_block(self.GROUP))

    def test_a_lone_scan_is_not_labelled(self):
        # The overwhelmingly common case: one file, no back, nothing to tell
        # apart. No brackets are introduced into an archive that has no variants
        # in it.
        self.assertEqual(
            self.one_block({"box3_030.jpg": "Grandma on the porch"}),
            f"Grandma on the porch\n{self.ANALYSIS}",
        )

    def test_a_single_variant_with_a_back_is_labelled_without_letters(self):
        self.assertEqual(
            self.one_block(
                {"box3_031.jpg": "Ruth and Sam", "box3_031-back.jpg": "pencil note"}
            ),
            "[Photo] Ruth and Sam\n"
            "[Back] pencil note\n"
            f"{self.ANALYSIS}",
        )

    def test_the_letter_is_decided_per_role_so_a_lone_back_keeps_none(self):
        """The rule that stops "[Back B]" appearing beside a single back.

        Whether a letter is needed is asked of each role separately: there are
        two photos, so the photos are lettered; there is one back, so it is not.
        One answer for the whole group would letter the back too, naming a
        variant the reader has no reason to care about.
        """
        block = self.one_block(self.GROUP)

        self.assertIn("[Photo A]", block)
        self.assertIn("[Photo B]", block)
        self.assertIn("[Back] Back of Photo B", block)
        self.assertNotIn("[Back A]", block)
        self.assertNotIn("[Back B]", block)

    def test_two_backs_are_lettered_and_so_are_the_photos(self):
        # The complement: with two of each role, both roles get letters. The
        # order is the group's own scan order -- part kind, then variant -- so
        # the photos are listed together and then the backs, rather than the
        # block alternating sides.
        self.assertEqual(
            self.one_block(
                {
                    "box3_032.jpg": "front A",
                    "box3_032-back.jpg": "back A",
                    "box3_032b.jpg": "front B",
                    "box3_032b-back.jpg": "back B",
                }
            ),
            "[Photo A] front A\n"
            "[Photo B] front B\n"
            "[Back A] back A\n"
            "[Back B] back B\n"
            f"{self.ANALYSIS}",
        )

    def test_the_unlettered_scan_is_variant_a_only_beside_a_lettered_one(self):
        """Where "[Photo A]" comes from, and where it must not be invented.

        A bare ``box3_017.jpg`` beside ``box3_017b.jpg`` IS variant A -- that is
        precisely why its sibling is lettered 'b' and not 'a' -- so printing it
        as "[Photo A]" makes the letters in the block the letters on disk. With
        no lettered sibling there is nothing to disambiguate and an A would be
        invented, which is why a print and its own crop get a bare "[Photo]".
        """
        self.assertIn("[Photo A] Caption A", self.one_block(self.GROUP))

        self.assertEqual(
            self.one_block(
                {"box3_033.jpg": "the print", "box3_033-crop.jpg": "the detail"}
            ),
            "[Photo] the print\n"
            "[Photo] the detail\n"
            f"{self.ANALYSIS}",
        )

    def test_a_real_variant_a_keeps_the_letter_to_itself(self):
        # Two files may not claim one label, so an explicit 'a' in the group
        # takes "[Photo A]" and the unlettered scan falls back to "[Photo]".
        self.assertEqual(
            self.one_block(
                {
                    "box3_034.jpg": "the unlettered scan",
                    "box3_034a.jpg": "the a scan",
                    "box3_034b.jpg": "the b scan",
                }
            ),
            "[Photo] the unlettered scan\n"
            "[Photo A] the a scan\n"
            "[Photo B] the b scan\n"
            f"{self.ANALYSIS}",
        )

    def test_a_front_is_never_labelled_as_the_back(self):
        """The mislabelling this replaces, pinned so it cannot come back.

        A variant that HAD a back got its caption labelled with the back's role,
        so under ``--group-by pair`` the caption written onto a FRONT file read
        "[Back]". The de-duplication that branch existed for is kept -- one
        caption is still one line, however many files hold it, which the case
        below this one pins -- but not by saying that a front is a back.
        """
        block = self.one_block(
            {"box3_035.jpg": "the front's own caption", "box3_035-back.jpg": ""},
            group_by=utils.GROUP_BY_PAIR,
        )

        self.assertIn("[Photo] the front's own caption", block)
        self.assertNotIn("[Back] the front's own caption", block)

    def test_one_caption_held_by_two_files_is_written_once(self):
        # What that back-labelling branch was protecting, kept.
        self.assertEqual(
            self.one_block(
                {
                    "box3_036.jpg": "Ruth and Sam outside the bakery",
                    "box3_036-back.jpg": "Ruth and Sam outside the bakery",
                }
            ),
            "[Photo] Ruth and Sam outside the bakery\n"
            f"{self.ANALYSIS}",
        )

    def test_pages_are_told_apart_by_number_not_a_letter_none_of_them_have(self):
        """A multi-page document has no variant letters -- the pages need one anyway.

        ``multiple_fronts`` is true of a 3-page letter exactly as it is of two
        rescans, so the pages are labelled. But none of them carry a variant
        letter -- ``-pageN`` is a different token from a variant letter -- so
        without a fallback every page's label collapses to the identical bare
        "[Photo]", merging three distinct captions under one indistinguishable
        heading. The page number is what the filenames already call them, so
        that is what disambiguates them.

        A document's files normally carry a caption each now rather than this
        one block. This reply carries no ``transcriptions``, which is the
        fallback: there is no per-part text to attribute, so the group block is
        what every file gets and the label rule above is what builds it. That
        makes this both the original case and the pin on the fallback -- the
        model returning nothing per part must leave a document exactly where it
        was before per-page captions existed.
        """
        pages = {
            "box5_010-page1.jpg": "Dear Ruth,",
            "box5_010-page2.jpg": "I hope this finds you well.",
            "box5_010-page3.jpg": "Love, Sam",
        }
        self.assertEqual(
            self.one_block(pages),
            "[Photo 1] Dear Ruth,\n"
            "[Photo 2] I hope this finds you well.\n"
            "[Photo 3] Love, Sam\n"
            f"{self.ANALYSIS}",
        )
        self.assertEqual(
            set(self.scopes(pages).values()),
            {"group"},
            "a document that fell back to the group block did not say so",
        )


class TestADocumentGivesEachPageItsOwnCaption(_CaptionBlockTestCase):
    """Each page of a document carries its own page, not the whole book.

    The group block is right for a print, its back and a rescan: they are one
    object, and which of them someone opens a year later is an accident of how
    they were browsing. A 63-page letter is not one object. Writing the whole
    transcription into all 63 files made every page's Description 63x redundant
    and told the reader who opened page 37 about page 1 -- and the ``.md``
    sidecar, which has always preferred this file's own part, disagreed with
    the Description of the same file.

    The trigger is document-ness, not size: ``multipage_present`` already means
    "an ordered sequence of pages rather than views of one object". Within such
    a group the rule is uniform -- a back gets the back's own text, and two
    scans of one page both get that page's.
    """

    #: A three page letter, no file holding a caption of its own yet.
    LETTER: typing.ClassVar[dict[str, str]] = {
        "box5_020-page1.jpg": "",
        "box5_020-page2.jpg": "",
        "box5_020-page3.jpg": "",
    }
    PAGES: typing.ClassVar[dict[str, str]] = {
        "Page 1": "Dear Ruth,",
        "Page 2": "I hope this finds you well.",
        "Page 3": "Love, Sam",
    }

    def test_each_page_holds_its_own_part_and_nothing_else(self):
        self.assertEqual(
            self.page_blocks(self.LETTER, transcriptions=self.PAGES),
            {
                "box5_020-page1.jpg": "Dear Ruth,",
                "box5_020-page2.jpg": "I hope this finds you well.",
                "box5_020-page3.jpg": "Love, Sam",
            },
        )

    def test_a_page_carries_no_label(self):
        """The file holds exactly one part's text, so there is nothing to tell
        it apart from -- the rule a lone scan already follows. A label would
        also have to be one the intake recognizes on the next read or it would
        be attributed a second time, and the only spelling that would fit is
        the "[Page N]" the section dedup must never be taught.
        """
        for name, caption in self.page_blocks(
            self.LETTER, transcriptions=self.PAGES
        ).items():
            with self.subTest(file=name):
                self.assertNotIn("[", caption)

    def test_the_pages_say_which_regime_they_are_in(self):
        self.assertEqual(
            set(self.scopes(self.LETTER, transcriptions=self.PAGES).values()), {"part"}
        )

    def test_a_back_in_a_document_gets_the_backs_own_text(self):
        """Attribution follows part-ness or it does not.

        A rule reading "pages get their own text but the back gets everything"
        would be two rules, and the back of a page is no more the whole book
        than the page is.
        """
        self.assertEqual(
            self.page_blocks(
                {
                    "box5_021-page1.jpg": "",
                    "box5_021-page2.jpg": "",
                    "box5_021-back.jpg": "",
                },
                transcriptions={
                    "Page 1": "Dear Ruth,",
                    "Page 2": "Love, Sam",
                    "Back": "Written on the reverse in pencil.",
                },
            ),
            {
                "box5_021-page1.jpg": "Dear Ruth,",
                "box5_021-page2.jpg": "Love, Sam",
                "box5_021-back.jpg": "Written on the reverse in pencil.",
            },
        )

    def test_two_scans_of_one_page_share_that_pages_text(self):
        """The half of this change that needed no code at all.

        The payload is built per PART with a list of paths, so a page and its
        rescan travel under one label and resolve back to it. They are scans of
        the same physical sheet, so they hold the same text -- which is the
        variants-still-combine rule, arrived at by the same route rather than
        by an exception.
        """
        produced = self.blocks(
            {
                "box5_022-page1.jpg": "",
                "box5_022-page2.jpg": "",
                "box5_022b-page2.jpg": "",
            },
            transcriptions={"Page 1": "Dear Ruth,", "Page 2": "Love, Sam"},
        )
        self.assertEqual(produced["box5_022-page2.jpg"], "Love, Sam")
        self.assertEqual(produced["box5_022b-page2.jpg"], "Love, Sam")
        self.assertEqual(produced["box5_022-page1.jpg"], "Dear Ruth,")

    def test_a_page_the_model_did_not_answer_keeps_the_group_block(self):
        """The mixed folder, made legible rather than mysterious.

        ``transcriptions`` is optional by design, and a partial map is what a
        long document actually produces when something goes wrong part way
        through. Inventing an attribution nothing supports is the one thing
        this codebase refuses to do, so the unanswered file keeps exactly the
        caption it would have had before -- and says which regime it is in, so
        a reader is not left inferring it from length.
        """
        group = {
            "box5_023-page1.jpg": "",
            "box5_023-page2.jpg": "",
            "box5_023-page3.jpg": "",
        }
        answered = {"Page 1": "Dear Ruth,", "Page 2": "I hope this finds you well."}

        produced = self.blocks(group, transcriptions=answered)
        self.assertEqual(produced["box5_023-page1.jpg"], "Dear Ruth,")
        self.assertEqual(produced["box5_023-page3.jpg"], self.ANALYSIS)
        self.assertEqual(
            self.scopes(group, transcriptions=answered),
            {
                "box5_023-page1.jpg": "part",
                "box5_023-page2.jpg": "part",
                "box5_023-page3.jpg": "group",
            },
        )

    def test_a_group_of_views_of_one_object_is_left_exactly_as_it_was(self):
        """The common case, untouched, including the absence of the new key.

        ``caption_scope`` is written only inside a document, where the two
        regimes can differ file to file. Everywhere else the caption is
        group-scoped by design and stamping that on every record in an archive
        of ordinary photographs would be noise in a value users read.
        """
        pair = {"box3_050.jpg": "Ruth and Sam", "box3_050-back.jpg": "pencil note"}
        transcriptions = {"Front": "Ruth and Sam", "Back": "pencil note"}

        self.assertEqual(
            self.one_block(pair, transcriptions=transcriptions),
            f"[Photo] Ruth and Sam\n[Back] pencil note\n{self.ANALYSIS}",
        )
        self.assertEqual(set(self.scopes(pair, transcriptions=transcriptions).values()),
                         {None})

    def test_no_page_ever_receives_a_siblings_stored_caption(self):
        """The trap in this change, and the one a single run cannot see.

        The group's intake sweep absorbs every file's existing caption into one
        block. Left group-wide for a document, it would hand every page every
        other page's stored text on the first ``-rw`` -- and from the pass
        after that, that text is the file's own stored caption, so the change
        has quietly undone itself while a test that only checks "page 2 says
        page 2" still passes.
        """
        produced = self.blocks(
            {
                "box5_024-page1.jpg": "an archivist's note about the first sheet",
                "box5_024-page2.jpg": "a different note about the second sheet",
            },
            transcriptions={"Page 1": "Dear Ruth,", "Page 2": "Love, Sam"},
        )

        self.assertEqual(
            produced["box5_024-page1.jpg"],
            "an archivist's note about the first sheet\nDear Ruth,",
        )
        self.assertNotIn("second sheet", produced["box5_024-page1.jpg"])
        self.assertNotIn("first sheet", produced["box5_024-page2.jpg"])

    def test_the_pages_do_not_depend_on_the_order_they_arrived_in(self):
        """Permutation invariance, kept where it still applies.

        A group block is one value and has to be the same whatever order the
        folder was listed in. Per-page captions are several values, so the
        claim becomes "the same MAP" -- which is the same property and the same
        class of bug, since the per-file build reads a group-wide part map and
        a group-wide relabel set.
        """
        answers = {
            tuple(sorted(self.blocks(dict(order), transcriptions=self.PAGES).items()))
            for order in itertools.permutations(self.LETTER.items())
        }
        self.assertEqual(len(answers), 1, f"arrival order leaked into a caption: {answers!r}")

    def test_a_map_whose_value_is_not_text_falls_back_instead_of_failing(self):
        """``transcriptions`` is model-written, so its values are whatever the
        model sent. A page that came back as a list of lines is valid JSON; the
        group has already been paid for by the time it gets here, so an
        unusable value declines to attribute that file rather than taking the
        whole group down with it.
        """
        group = {"box5_025-page1.jpg": "", "box5_025-page2.jpg": ""}
        produced = self.blocks(
            group,
            transcriptions={"Page 1": ["Dear", "Ruth"], "Page 2": "Love, Sam"},
        )

        self.assertEqual(produced["box5_025-page1.jpg"], self.ANALYSIS)
        self.assertEqual(produced["box5_025-page2.jpg"], "Love, Sam")

    def test_the_scope_key_is_a_record_field_and_never_a_written_tag(self):
        """``caption_scope`` is disclosure for whoever reads the record -- the
        plug-in, the NDJSON stream, a sidecar. It is not a tag, and it must not
        become one: the canonical patch is what reaches the user's files.
        """
        for name, record in self.records(
            self.LETTER, transcriptions=self.PAGES
        ).items():
            patch, _patch_meta = build_canonical_patch(record, utils.Config())
            with self.subTest(file=name):
                self.assertEqual(record["caption_scope"], "part")
                self.assertNotIn("caption_scope", patch)
                self.assertNotIn(
                    "part",
                    [str(entry.get("value")) for entry in patch.values()],
                )

    def test_four_consecutive_runs_are_byte_identical_from_the_first(self):
        """The README's promise, on the path this change created.

        Under ``-rw`` the caption written here is exactly what the next run
        reads back out of the file, so anything that is not a fixed point grows
        without bound into the user's photographs. Carrying no label is what
        makes this settle on run 1 rather than run 2: an unlabelled caption is
        read back as one unlabelled section, and this run's own fresh text for
        the same page is then recognized as a restatement of it.
        """
        held = dict(self.LETTER)
        passes: list[dict[str, str]] = []
        for _run in range(4):
            held = self.blocks(held, transcriptions=self.PAGES)
            passes.append(dict(held))

        self.assertEqual(
            passes[1:],
            [passes[0], passes[0], passes[0]],
            "a per-page caption is not a fixed point of its own intake",
        )
        self.assertEqual(
            passes[0],
            {
                "box5_020-page1.jpg": "Dear Ruth,",
                "box5_020-page2.jpg": "I hope this finds you well.",
                "box5_020-page3.jpg": "Love, Sam",
            },
        )


class TestTheCaptionUpdateRules(_CaptionBlockTestCase):
    """What happens to a caption a photo already has.

    Four rules, applied with no second model call: a near-identical caption
    changes nothing, a materially different one is added beside what is there, a
    partial block has its missing sections filled in, and prose nobody labelled
    is preserved. The merge is per label rather than whole-string, which is what
    makes the third possible at all -- a section can be filled in with no risk to
    the sections already written.
    """

    def test_a_materially_different_caption_is_kept_and_added_to(self):
        # Rule (b), the default the other three are exceptions to: nothing a
        # file already said is dropped in favour of something new.
        self.assertEqual(
            self.one_block(
                {
                    "box3_040.jpg": "Ruth and Sam outside the bakery",
                    "box3_040b.jpg": "Ruth and Edith outside the bakery",
                }
            ),
            "[Photo A] Ruth and Sam outside the bakery\n"
            "[Photo B] Ruth and Edith outside the bakery\n"
            f"{self.ANALYSIS}",
        )

    def test_a_partial_block_has_only_its_missing_section_filled_in(self):
        """Rule (c), and the reason the block is labelled in the first place.

        One file was enriched by an earlier run and holds "[Photo A]"; the group
        has since gained a second scan. Merging per label matches that line and
        leaves it exactly as it is while "[Photo B]" is added. A whole-string
        comparison would find the two captions unequal and either append the old
        block again or overwrite it.
        """
        self.assertEqual(
            self.one_block(
                {
                    "box3_041.jpg": "[Photo A] Caption A",
                    "box3_041b.jpg": "Caption B",
                }
            ),
            "[Photo A] Caption A\n"
            "[Photo B] Caption B\n"
            f"{self.ANALYSIS}",
        )

    def test_a_change_in_one_section_cannot_disturb_another(self):
        # "Per label, never whole-string" stated directly: the A section is
        # edited between two runs and the B section is byte-identical across
        # them.
        before = self.one_block(
            {"box3_042.jpg": "Caption A", "box3_042b.jpg": "Caption B"}
        )
        after = self.one_block(
            {
                "box3_042.jpg": "Caption A, corrected to 1949",
                "box3_042b.jpg": "Caption B",
            }
        )

        self.assertIn("[Photo B] Caption B", before)
        self.assertIn("[Photo B] Caption B", after)
        self.assertNotEqual(before, after)

    def test_unlabelled_prose_is_preserved_and_attributed_to_its_own_file(self):
        """Rule (d): a caption a human typed is never lost, and is always keyed.

        At intake it is known exactly which file the prose was read off, so
        there is no unattributable text at that stage -- which is what lets even
        a hand-typed caption take part in the per-label merge on later runs
        rather than being carried along as an unmergeable preamble.
        """
        self.assertEqual(
            self.one_block(
                {
                    "box3_043.jpg": "Grandma on the porch, Ohio, summer 1948",
                    "box3_043b.jpg": "",
                }
            ),
            "[Photo A] Grandma on the porch, Ohio, summer 1948\n"
            f"{self.ANALYSIS}",
        )

    def test_multi_line_prose_takes_one_label_for_the_whole_run(self):
        # A note's paragraphs are one thought. Labelling each line separately
        # would make them sections that later runs could de-duplicate and
        # reorder independently of one another.
        self.assertEqual(
            self.one_block(
                {
                    "box3_044.jpg": "Grandma on the porch.\n\nOhio, summer 1948.",
                    "box3_044b.jpg": "",
                }
            ),
            "[Photo A] Grandma on the porch.\n"
            "\n"
            "Ohio, summer 1948.\n"
            f"{self.ANALYSIS}",
        )


class TestNearIdenticalCaptionsAreNotDuplicated(_CaptionBlockTestCase):
    """Rule (a), and the knob it turns on.

    Two files of one group often hold the same caption typed twice -- copied
    between them by hand, or round-tripped through tags that disagree about
    quoting. Writing both grows the block by a near-twin line. Skipping too
    eagerly throws away a correction someone made, which is not recoverable, so
    both directions are pinned and the second is the one that matters.
    """

    #: Pairs that are one caption typed twice: every row differs only in
    #: punctuation, quoting, spacing or case, and no word changes.
    SAME: typing.ClassVar[tuple[tuple[str, str, str], ...]] = (
        ("a trailing period", "Ruth and Sam outside the bakery",
         "Ruth and Sam outside the bakery."),
        ("case and spacing", "Ruth and Sam outside  the bakery",
         "ruth and sam outside the bakery"),
        ("an inner comma", "Ruth and Sam, outside the bakery",
         "Ruth and Sam outside the bakery"),
        ("a curly apostrophe", "Grandma’s porch, summer 1948",
         "Grandma's porch, summer 1948"),
        ("an em dash for a hyphen", "Ohio - summer 1948", "Ohio — summer 1948"),
        ("changed quote marks", 'Ruth said "hello" here', "Ruth said 'hello' here"),
    )

    #: Pairs that must both survive. The first three are superficially similar
    #: and materially different -- one name, one year, one added word -- which is
    #: the class of edit a loose threshold destroys.
    DIFFERENT: typing.ClassVar[tuple[tuple[str, str, str], ...]] = (
        ("one year", "Ruth and Sam outside the bakery, 1948",
         "Ruth and Sam outside the bakery, 1949"),
        ("one name", "Ruth and Sam outside the bakery",
         "Ruth and Edith outside the bakery"),
        ("one added word", "Ruth and Sam outside the bakery",
         "Ruth and Sam outside the old bakery"),
        ("a whole different caption", "Ruth and Sam outside the bakery",
         "A man in uniform beside a jeep"),
    )

    def _pair_block(self, first: str, second: str) -> str:
        """Return the block for two scans of one print holding *first*/*second*."""
        return self.one_block({"box3_050.jpg": first, "box3_050b.jpg": second})

    def test_a_restatement_is_written_once(self):
        for label, first, second in self.SAME:
            with self.subTest(differing_by=label):
                self.assertEqual(
                    self._pair_block(first, second),
                    f"[Photo A] {first}\n{self.ANALYSIS}",
                    "a caption differing only in punctuation, quoting or case "
                    "was written a second time; the block gains a near-twin "
                    "line on every run that way",
                )

    def test_a_material_difference_is_always_kept(self):
        for label, first, second in self.DIFFERENT:
            with self.subTest(differing_by=label):
                self.assertEqual(
                    self._pair_block(first, second),
                    f"[Photo A] {first}\n[Photo B] {second}\n"
                    f"{self.ANALYSIS}",
                    "a caption that says something different was discarded as a "
                    "restatement. Losing a correction someone typed cannot be "
                    "undone; carrying an extra line can",
                )

    def test_the_predicate_agrees_with_the_pipeline_on_every_row(self):
        """The same table on the predicate, so a failure says which half moved.

        End to end, a wrong skip and a wrong keep differ by one line, which is a
        weak signal to debug from.
        """
        for label, first, second in self.SAME:
            with self.subTest(same=label):
                self.assertTrue(core._captions_are_near_identical(first, second))
        for label, first, second in self.DIFFERENT:
            with self.subTest(different=label):
                self.assertFalse(core._captions_are_near_identical(first, second))

    def test_no_single_ratio_threshold_could_have_done_this(self):
        """Why the word comparison decides and the ratio only guards the residue.

        Skipping is ``ratio >= threshold``, so every SAME row demands a threshold
        at or below its score and every DIFFERENT row one strictly above.
        Measured on the normalized text those demands contradict each other --
        the worst SAME row scores BELOW the best DIFFERENT one, because ``ratio``
        is relative to length and a changed year in a long block moves it less
        than a changed quote mark in a short one. This is the measurement the
        constant's comment records, executed, so the comment cannot go stale.
        """
        scored = {
            name: [
                difflib.SequenceMatcher(
                    None,
                    core._normalize_caption_text(first),
                    core._normalize_caption_text(second),
                ).ratio()
                for _label, first, second in rows
            ]
            for name, rows in (("same", self.SAME), ("different", self.DIFFERENT))
        }

        self.assertLess(
            min(scored["same"]),
            max(scored["different"]),
            "the two ranges no longer overlap, so a plain ratio threshold would "
            "now work and the word comparison could be reconsidered",
        )
        # Which is why the word comparison is NECESSARY rather than an
        # alternative: the ratio is consulted only once the words already match,
        # so it is never in a position to discard a changed name, year or place.
        # Reached the other way round it could, and provably did -- a 656-char
        # transcription with its year corrected scored 0.99847 against the old
        # 0.998 gate and the correction was dropped. Asserted as a property over
        # length, because that failure only appears once a block is long enough
        # for one changed word to be a small fraction of it.
        #
        # The filler has to actually vary: repeating one short sentence dozens
        # of times (an earlier version of this fixture did) crosses difflib's
        # own autojunk threshold, which stops treating the repeated text as a
        # match at all and collapses the ratio to near zero -- so the assertion
        # below would pass even against a ratio-only implementation with no
        # threshold whatsoever, proving nothing about the word gate. Varied
        # prose keeps the ratio where a real long transcription's would sit.
        stale = (
            "27 november 44. The stonework had survived the shelling, though the "
            "roofline was gone and the windows on the east side were boarded over "
            "with whatever timber could be found. Someone had chalked a name and "
            "a date onto the garden wall, half legible under the soot. The "
            "fountain in the courtyard was dry and cracked, and a cart wheel "
            "leaned against it where it had been left, rusting, since the spring. "
            "Two of the shutters still bore their painted numbers. A stray dog "
            "slept in the shade of the archway most afternoons, and the "
            "neighbors said it had belonged to the family that used to keep the "
            "corner shop before the war reached this street and left the block "
            "the way it stands now, scarred but standing."
        )
        fixed = stale.replace("november 44", "november 45", 1)
        self.assertNotEqual(stale, fixed)
        # The ratio a plain (non-word-gated) implementation would have scored
        # this pair, pinned so a future edit to the filler cannot quietly drop
        # back below the near-identical floor and defang the assertion below.
        self.assertGreater(
            difflib.SequenceMatcher(
                None, core._normalize_caption_text(stale), core._normalize_caption_text(fixed)
            ).ratio(),
            core._CAPTION_NEAR_IDENTICAL_RATIO,
            "the fixture's ratio dropped to or below the near-identical floor, "
            "so it no longer demonstrates a ratio-only implementation would "
            "have wrongly discarded this correction",
        )
        self.assertFalse(
            core._captions_are_near_identical(stale, fixed),
            "a corrected year in a long transcription was discarded as a "
            "restatement; one substituted character in a length-n block scores "
            "(n-1)/n, so any ratio high enough to pass short captions will "
            "swallow a real edit in a long one",
        )
        # And the floor that does remain is below every same-words row, so it
        # only ever fires on punctuation heavy enough to change how a line reads.
        self.assertLessEqual(
            core._CAPTION_NEAR_IDENTICAL_RATIO,
            min(scored["same"]),
            "the ratio floor now rejects captions that differ only in "
            "punctuation, so the block will grow a near-twin line every run",
        )


class TestTheCaptionBlockIsPermutationInvariant(_CaptionBlockTestCase):
    """One group, one block, whatever order its files arrived in.

    The block is a property of the object, so a folder listed in another order
    has to produce the same one -- and it is now assembled from four files'
    captions rather than from one, which is four chances for arrival order to
    leak into a value written into the user's photographs. Phase B1 exists for
    this class of bug; the intake sweep is ordered by ``_slot_rank_key`` for the
    same reason every other choice in the bucket loop is.

    The group here is four scans of one object, which is exactly the shape that
    still gets one block. A document's pages get one caption each, and the same
    invariant restated for them -- the same MAP whatever the order -- is in
    ``TestADocumentGivesEachPageItsOwnCaption``.
    """

    #: Four files, all carrying captions, spanning both roles and both variants,
    #: so no two of them rank alike. Every value differs on purpose: identical
    #: captions would be invariant under permutation however the sweep was
    #: written, which is the shape that lets an ordering bug pass unnoticed.
    GROUP: typing.ClassVar[dict[str, str]] = {
        "box3_060.jpg": "the print",
        "box3_060-back.jpg": "pencil on the back",
        "box3_060b.jpg": "the rescan",
        "box3_060b-back.jpg": "pencil on the rescan's back",
    }

    def test_all_twenty_four_orderings_give_one_answer(self):
        answers = {
            self.one_block(dict(order))
            for order in itertools.permutations(self.GROUP.items())
        }

        self.assertEqual(
            len(answers),
            1,
            "the caption written into every file of the group depends on the "
            f"order the files were listed in: {sorted(answers)!r}",
        )

    def test_the_one_answer_holds_all_four_captions(self):
        """Non-vacuity: an invariant of nothing would satisfy the test above."""
        self.assertEqual(
            self.one_block(self.GROUP),
            "[Photo A] the print\n"
            "[Photo B] the rescan\n"
            "[Back A] pencil on the back\n"
            "[Back B] pencil on the rescan's back\n"
            f"{self.ANALYSIS}",
        )


class TestCaptionJoinIsIdempotent(_CaptionBlockTestCase):
    """Re-reading a caption photokin already wrote does not grow it.

    ``-r`` hydrates ``XMP:Description`` into ``caption``, so the block written
    back is exactly what the next run reads as each file's own caption. Anything
    the intake does not recognize as its own output it attributes a second time,
    and the caption gains a copy of itself on every ``-rw`` pass -- which is the
    bug that shipped this phase.

    The steady state is not "one file holds the block": it is EVERY file holding
    it, because that is what the previous run wrote. That is the case most likely
    to double, and it is the one the runs below re-feed.

    Every shape below is a group of views of one object or a lone file, so all
    of them still hold one block. The same promise for a document, whose files
    now each hold their own page, is
    ``TestADocumentGivesEachPageItsOwnCaption`` -- carrying no label is what
    makes that one settle on the first run rather than the second.
    """

    ORIGINAL = "Grandma on the porch, Ohio, summer 1948"
    GENERATED = "An older woman seated on a wooden porch"

    #: The caption shapes a real archive holds. The multi-file rows are what the
    #: labelled block is for; the single-file rows are the common case it has to
    #: leave alone.
    SHAPES: typing.ClassVar[dict[str, dict[str, str]]] = {
        "unlabelled human prose": {"handwritten.jpg": ORIGINAL},
        "a multi-paragraph caption": {
            "note.jpg": "Grandma on the porch.\n\nOhio, summer 1948."
        },
        "a file holding nothing at all": {"box3_019.jpg": ""},
        "two variants and one back": {
            "box3_017.jpg": "Caption A",
            "box3_017b.jpg": "Caption B",
            "box3_017b-back.jpg": "Back of Photo B",
        },
        "a caption already in the labelled form": {
            "box3_018.jpg": "[Photo] Ruth and Sam",
            "box3_018-back.jpg": "[Back] pencil note",
        },
        "a pair, each side holding its own": {
            "box3_025.jpg": "Ruth outside the bakery",
            "box3_025-back.jpg": "Ruth, 1948",
        },
    }

    def _caption(self, existing: str) -> str:
        """Run one lone file whose own tags hold *existing*."""
        return self.blocks({"handwritten.jpg": existing}, analysis=self.GENERATED)[
            "handwritten.jpg"
        ]

    def _three_runs(
        self, captions: dict[str, str], *, group_by: str = utils.GROUP_BY_OBJECT
    ) -> list[dict[str, str]]:
        """Run three times, feeding each pass what the one before it wrote.

        This is ``-rw`` in a loop, and it is the shape the growth bug shipped in:
        the caption a run writes into a photograph is the caption the next run
        reads back out of it. Every file is fed the block it received rather than
        one file being singled out, so from pass two onward the whole group is in
        the steady state a settled archive is actually in -- which is the case
        most likely to double.

        Returns:
            One ``{basename: caption}`` per pass.
        """
        produced: list[dict[str, str]] = []
        held = dict(captions)
        for _pass in range(3):
            held = self.blocks(held, group_by=group_by)
            produced.append(dict(held))
        return produced

    def test_three_consecutive_runs_leave_every_shape_byte_identical(self):
        """The non-negotiable one.

        A fixed point after the first pass, for every shape an archive holds:
        prose a human typed, a caption with paragraph breaks, a block already
        carrying this run's own labels, and the multi-file groups the labels
        exist for. One extra copy per run is unbounded growth into the user's
        photographs, and it has already shipped once.
        """
        for label, captions in self.SHAPES.items():
            with self.subTest(shape=label):
                first, second, third = self._three_runs(captions)

                self.assertEqual(
                    second,
                    first,
                    "re-reading the block photokin wrote changed it. Under -rw "
                    "that block IS the next run's input, so anything which is "
                    "not a fixed point of the intake grows without bound",
                )
                self.assertEqual(third, first)

    def test_the_three_runs_are_stable_at_every_grouping(self):
        """The same claim across the axis, since each mode builds the block from
        a different payload: ``object`` sends a multi-variant group in one call,
        ``pair`` splits it per variant, ``none`` per file.
        """
        for group_by in utils.GROUP_BY_VALUES:
            with self.subTest(group_by=group_by):
                first, second, third = self._three_runs(
                    self.SHAPES["two variants and one back"], group_by=group_by
                )
                self.assertEqual([second, third], [first, first])

    def test_re_labelling_never_doubles_a_label(self):
        # The specific failure: intake that does not recognize its own output
        # attributes it again, and "[Photo A] Caption A" becomes
        # "[Photo A] [Photo A] Caption A".
        for index, produced in enumerate(
            self._three_runs(self.SHAPES["two variants and one back"]), start=1
        ):
            for name, block in produced.items():
                with self.subTest(run=index, file=name):
                    self.assertNotIn("[Photo A] [Photo", block)
                    self.assertNotIn("[Photo] [Photo", block)
                    self.assertNotIn("[Back] [Back", block)
                    self.assertEqual(block.count("[Photo A]"), 1)

    def test_a_reworded_transcription_is_kept_beside_not_silently_replaced(self):
        """This run's own transcription is caption content now, not disposable analysis.

        Nothing marks it as regenerable the way the old ``[AI Analysis]`` tail
        did, so a model that returns different wording on a later pass -- OCR
        settling on a clearer reading of the same handwriting, say -- is judged
        by the same rule (b) as any other materially different caption: kept
        beside what is already there, never silently dropped. Losing a real
        correction because it looked like a reword is the failure this
        architecture exists to avoid; the price is that two genuinely distinct
        readings from two different runs both survive rather than the second
        quietly overwriting the first.
        """
        settled = self.one_block({"box3_017.jpg": "Caption A"})

        reworded = self.blocks(
            {"box3_017.jpg": settled}, analysis="Two people outside a bakery in Ohio."
        )["box3_017.jpg"]

        self.assertEqual(
            reworded,
            f"Caption A\n{self.ANALYSIS}\nTwo people outside a bakery in Ohio.",
        )

    def test_the_original_is_kept_and_this_runs_transcription_appended(self):
        # A lone scan with no back earns no section label of its own, and
        # neither does this run's own transcription -- it is filed the same
        # way a human's caption would be.
        self.assertEqual(
            self._caption(self.ORIGINAL),
            f"{self.ORIGINAL}\n{self.GENERATED}",
        )

    def test_re_reading_the_join_returns_it_unchanged(self):
        once = self._caption(self.ORIGINAL)
        self.assertEqual(self._caption(once), once)
        self.assertEqual(self._caption(self._caption(once)), once)

    def test_a_file_with_no_original_caption_is_stable_too(self):
        first = self._caption("")
        self.assertEqual(first, self.GENERATED)
        self.assertEqual(self._caption(first), first)

    def test_a_multi_paragraph_caption_keeps_its_breaks_and_is_stable(self):
        original = "Grandma on the porch.\n\nOhio, summer 1948."
        once = self._caption(original)
        self.assertEqual(once, f"{original}\n{self.GENERATED}")
        self.assertEqual(self._caption(once), once)

    def test_a_block_an_older_release_wrote_is_kept_rather_than_re_labelled(self):
        """``[Front]`` is read and never written, so an enriched archive settles.

        photokin wrote "[Front] ..." before the wording became "[Photo]", and
        those lines are somebody's metadata now. Intake recognizes them as
        already labelled and takes them verbatim, which is what keeps a re-run
        over an archive an older release enriched from attributing every one of
        them a second time.

        The fixture is a group that labels -- a pair -- and the legacy line is
        the FIRST line of the file's caption, which is what an older release
        wrote onto a photo that had no caption of its own. Both halves matter: in
        an unlabelled group nothing is prepended to anything, and a legacy line
        in second place is a continuation line either way, so neither shape can
        tell a reader whether the legacy spelling is understood.
        """
        produced = self.blocks(
            {
                "box3_070.jpg": f"[Front] {self.GENERATED}",
                "box3_070-back.jpg": "pencil on the back",
            }
        )
        once = produced["box3_070.jpg"]

        self.assertEqual(
            once,
            f"[Front] {self.GENERATED}\n"
            "[Back] pencil on the back\n"
            f"{self.ANALYSIS}",
            "a line an older release labelled was attributed a second time; "
            f'"[Photo] [Front] ..." is the doubling this prevents: {once!r}',
        )
        self.assertNotIn("[Photo] [Front]", once)

        # And it is a fixed point, so an archive enriched by an older release
        # settles on the first re-run rather than growing on every one.
        settled = self.blocks(
            {"box3_070.jpg": once, "box3_070-back.jpg": once}
        )["box3_070.jpg"]
        self.assertEqual(settled, once)


class TestTitlePrecedenceDependsOnProvenance(unittest.TestCase):
    """A caller's title outranks the model's; a file's own title does not.

    Scanner software writes "Scanned Image" into ``XMP:Title``, so once ``-r``
    reads it, boilerplate that beat a transcription would make reading the file
    strictly worse than not reading it. A manifest title is the opposite kind of
    evidence -- a human typed it -- and this branch is all that stands between it
    and being overwritten in the file.

    Which of the two a title is cannot be worked out from inside the run, so the
    caller states it: ``titles_may_be_from_files`` is set by ``-r`` and by
    nothing else. Running a hydrator is not the same claim, and the tests below
    hold one of the two fixed while varying the other to say so.
    """

    def _merged_title(self, model_title, original_title, *, hydrated):
        work = tempfile.mkdtemp(prefix="pk-title-")
        path = _touch(work, "DSC_0042.jpg")
        item: dict[str, Any] = {"path": path}
        hydrator = None
        if original_title and hydrated:
            hydrator = _tag_hydrator(
                {utils.normalize_path(path): {"XMP:Title": original_title}}
            )
        elif original_title:
            item["metadata"] = {"title": original_title}
        reply: dict[str, Any] = {"caption": "A young woman in a cap and gown", "keywords": []}
        if model_title:
            reply["title"] = model_title
        record = _run([item], reply, hydrator=hydrator, from_files=hydrated)["results"][path]
        return record.get("title"), record["_merge"]["overrides"]

    def test_a_model_title_beats_a_title_read_out_of_the_file(self):
        title, overrides = self._merged_title(
            "Wedding Day 1952", "Scanned Image", hydrated=True
        )
        self.assertEqual(title, "Wedding Day 1952")
        self.assertNotIn("title", overrides)

    def test_the_transcription_is_what_reaches_the_file_under_the_read(self):
        """The narrowing has to survive as far as the write, not just the record.

        ``-r`` exists to improve the prompt; if the boilerplate it reads came
        back out at the other end it would overwrite a real transcription in
        ``XMP-dc:Title``, which is the only step of this that is not reversible.
        """
        work = tempfile.mkdtemp(prefix="pk-title-read-patch-")
        path = _touch(work, "DSC_0044.jpg")
        out = _run(
            [{"path": path}],
            {"caption": "c", "title": "Wedding Day 1952", "keywords": []},
            hydrator=_tag_hydrator(
                {utils.normalize_path(path): {"XMP:Title": "Scanned Image"}}
            ),
            from_files=True,
        )
        patch_, _meta = build_canonical_patch(out["results"][path], utils.Config())
        self.assertEqual(patch_["XMP-dc:Title"]["value"], "Wedding Day 1952")

    def test_a_manifest_title_beats_the_model_with_no_read(self):
        title, overrides = self._merged_title(
            "Untitled Scan", "Aunt Edith's wedding, St Marys", hydrated=False
        )
        self.assertEqual(title, "Aunt Edith's wedding, St Marys")
        self.assertIn("title", overrides)

    def test_a_manifest_title_reaches_the_file_it_is_written_to(self):
        work = tempfile.mkdtemp(prefix="pk-title-patch-")
        path = _touch(work, "DSC_0043.jpg")
        cfg = utils.Config()
        out = _run(
            [{"path": path, "metadata": {"title": "Mom's graduation, June 1961"}}],
            {"caption": "c", "title": "KODAK SAFETY FILM", "keywords": []},
        )
        patch_, _meta = build_canonical_patch(out["results"][path], cfg)
        self.assertEqual(
            patch_["XMP-dc:Title"]["value"], "Mom's graduation, June 1961"
        )

    def test_an_embedders_own_hydrator_does_not_narrow_the_rule(self):
        """One hydrator, two provenance claims, two answers.

        ``photokin/README.md`` invites embedders to hydrate from a database or a
        sidecar format, and a title out of a genealogy database is a human's
        words -- the same evidence as an inline manifest title, and it has to
        beat the model's transcription the same way. Inferring the claim from
        "a hydrator was passed" instead re-opened that data loss through the
        public seam, so the two are varied independently here: the hydrator and
        the title it supplies are identical in both rows and only the claim
        differs.
        """
        work = tempfile.mkdtemp(prefix="pk-title-embedder-")
        path = _touch(work, "DSC_0045.jpg")

        def _from_the_database(items: list[dict]) -> None:
            """An embedder's own hydrator: titles come from a catalogue, not a file."""
            for item in items:
                item.setdefault("metadata", {})["title"] = "Mom's graduation, June 1961"

        expected = {
            False: "Mom's graduation, June 1961",  # the default, and every embedder
            True: "KODAK SAFETY FILM",  # only -r, which reads XMP:Title
        }
        for from_files, winner in expected.items():
            with self.subTest(titles_may_be_from_files=from_files):
                out = _run(
                    [{"path": path}],
                    {"caption": "c", "title": "KODAK SAFETY FILM", "keywords": []},
                    hydrator=_from_the_database,
                    from_files=from_files,
                )
                record = out["results"][path]
                self.assertEqual(record.get("title"), winner)
                patch_, _meta = build_canonical_patch(record, utils.Config())
                self.assertEqual(patch_["XMP-dc:Title"]["value"], winner)

    def test_a_blank_model_title_is_not_a_transcription(self):
        """Whitespace is not a title the model read off the print.

        The narrowing only makes sense against something the model actually
        transcribed, so both sides are stripped before either is called
        non-empty. Without that, a reply carrying ``"   "`` counts as a
        transcription, suppresses the original, and the record ends up with a
        title of whitespace where a real one was -- worse than either input.
        """
        for blank in ("   ", "\n", "\t "):
            with self.subTest(model_title=repr(blank)):
                title, overrides = self._merged_title(
                    blank, "Aunt Ruth's kitchen", hydrated=True
                )
                self.assertEqual(title, "Aunt Ruth's kitchen")
                self.assertIn("title", overrides)

    def test_an_original_title_wins_when_the_model_returned_none(self):
        for hydrated in (True, False):
            with self.subTest(hydrated=hydrated):
                title, overrides = self._merged_title(
                    None, "Aunt Ruth's kitchen", hydrated=hydrated
                )
                self.assertEqual(title, "Aunt Ruth's kitchen")
                self.assertIn("title", overrides)

    def test_a_model_title_stands_when_there_is_no_original(self):
        for hydrated in (True, False):
            with self.subTest(hydrated=hydrated):
                title, overrides = self._merged_title(
                    "Wedding Day 1952", None, hydrated=hydrated
                )
                self.assertEqual(title, "Wedding Day 1952")
                self.assertNotIn("title", overrides)

    def test_a_manifest_title_survives_a_run_that_also_hydrates_a_sibling(self):
        """The run-wide -r bit must not smear a file's own claim onto another's.

        ``titles_may_be_from_files`` is one bit for the whole run, but whether
        *this* item's title came from a file is knowable precisely:
        ``hydrate_item_metadata`` never fills a title the item already has.
        Two items in the same ``-r`` run -- one with a manifest title, one
        with none -- must each keep their own provenance rather than the
        coarser run-wide bit deciding both from whichever is true anywhere.
        """
        work = tempfile.mkdtemp(prefix="pk-title-provenance-")
        typed_path = _touch(work, "DSC_0050.jpg")
        scanned_path = _touch(work, "DSC_0051.jpg")
        store = {
            utils.normalize_path(typed_path): {"XMP:Title": "Scanned Image"},
            utils.normalize_path(scanned_path): {"XMP:Title": "Scanned Image"},
        }
        with patch(
            "photokin.exiftool.hydrate.resolve_exiftool_path", return_value="/fake/exiftool"
        ), patch(
            "photokin.exiftool.manifest.run_exiftool_json",
            lambda *, exiftool_path, files, fields, timeout_sec=None, **_kw: [
                {"SourceFile": f.replace("\\", "/"), **store.get(utils.normalize_path(f), {})}
                for f in files
            ],
        ):
            out = _run(
                [
                    {"path": typed_path, "metadata": {"title": "Ellis Island Arrival"}},
                    {"path": scanned_path},
                ],
                {"caption": "c", "title": "Wedding Day 1952", "keywords": []},
                hydrator=make_manifest_hydrator(ExiftoolConfig()),
                from_files=True,
            )
        # The manifest title was never touched by hydration (it was not
        # missing), so it keeps outranking the model outright.
        self.assertEqual(out["results"][typed_path]["title"], "Ellis Island Arrival")
        # The sibling's title genuinely came from the file, so the model's
        # transcription still wins for it.
        self.assertEqual(out["results"][scanned_path]["title"], "Wedding Day 1952")


class TestGroupMetadataComesFromTheObjectNotItsSupportingScans(unittest.TestCase):
    """A group's shared metadata is read off the front print.

    ``-`` (0x2D) sorts before ``.`` (0x2E), so a path-ordered scan puts every
    ``-back``/``-crop``/``-negative`` sibling ahead of the bare front scan. With
    folder items carrying metadata for the first time, that handed the model a
    negative's caption as the object's and left the front print's own reaching
    nowhere.
    """

    GROUP: typing.ClassVar[list[dict[str, Any]]] = [
        {
            "path": "box3_025-back.jpg",
            "part_kind": "back",
            "is_crop": False,
            "page_num": None,
            "metadata": {"userComment": "back note"},
        },
        {
            "path": "box3_025-crop.jpg",
            "part_kind": "none",
            "is_crop": True,
            "page_num": None,
            "metadata": {"title": "crop title", "caption": "THE CROP"},
        },
        {
            "path": "box3_025-negative.jpg",
            "part_kind": "negative",
            "is_crop": False,
            "page_num": None,
            "metadata": {"title": "negative title", "caption": "THE NEGATIVE STRIP"},
        },
        {
            "path": "box3_025.jpg",
            "part_kind": "none",
            "is_crop": False,
            "page_num": None,
            "metadata": {"title": "front title", "caption": "THE FRONT PRINT"},
        },
    ]

    def test_the_front_scans_values_win(self):
        combined = utils.combine_group_metadata(list(self.GROUP))
        self.assertEqual(combined["title"], "front title")
        self.assertEqual(combined["caption"], "THE FRONT PRINT")

    def test_a_sibling_still_supplies_what_the_front_lacks(self):
        combined = utils.combine_group_metadata(list(self.GROUP))
        self.assertEqual(combined["userComment"], "back note")

    def test_the_answer_is_invariant_under_permutation(self):
        answers = {
            json.dumps(utils.combine_group_metadata(list(order)), sort_keys=True)
            for order in itertools.permutations(self.GROUP)
        }
        self.assertEqual(len(answers), 1)

    def test_preferred_still_outranks_the_part_order(self):
        group = [dict(it) for it in self.GROUP]
        for item in group:
            if item["path"] == "box3_025-negative.jpg":
                item["preferred"] = True
        combined = utils.combine_group_metadata(group)
        self.assertEqual(combined["title"], "negative title")

    def test_entries_with_no_part_kind_fall_back_to_the_path(self):
        """A caller passing raw manifest items keeps the previous ordering."""
        combined = utils.combine_group_metadata(
            [
                {"path": "b.jpg", "metadata": {"title": "second"}},
                {"path": "a.jpg", "metadata": {"title": "first"}},
            ]
        )
        self.assertEqual(combined["title"], "first")


class TestEachFileKeepsTheDateItWasReadFrom(unittest.TestCase):
    """The other half of the same question: a file's own date beats its group's.

    The group answer above is one value standing for the whole object, and
    ``merge_original_sources`` is what lets a file overrule it with its own.
    That only works if the two are spelled alike, and they are not: ``-r`` files
    the value it read under ``dateTimeOriginal``, while only
    ``combine_group_metadata`` produces the ``date`` spelling the picker asks
    for. Without the alias reading the one when the other is empty, a file's own
    date is invisible at the key that is consulted, the group's is not, and every
    file in a group silently inherits the front print's -- a back rescanned three
    years later comes back stamped with the day the front was scanned.
    """

    #: No ``date_guess``, so nothing here can be the date-correction heuristic
    #: rewriting ``dateTimeOriginal``; what each record holds is what it was read
    #: from.
    REPLY: typing.ClassVar[dict[str, Any]] = {
        "caption": "A porch in summer",
        "keywords": [],
    }

    FRONT_DATE = "2019:01:01 10:00:00"
    BACK_DATE = "2022:09:09 12:00:00"

    def _group(self, back_metadata: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        """Run one front/back pair and return its records keyed by basename."""
        work = tempfile.mkdtemp(prefix="pk-own-date-")
        front = _touch(work, "box3_025.jpg")
        back = _touch(work, "box3_025-back.jpg")
        items: list[dict[str, Any]] = [
            {"path": front, "metadata": {"dateTimeOriginal": self.FRONT_DATE}},
            {"path": back},
        ]
        if back_metadata is not None:
            items[1]["metadata"] = back_metadata
        out = _run(items, self.REPLY, from_files=True)
        return {os.path.basename(p): r for p, r in out["results"].items()}

    def test_the_back_keeps_its_own_date_rather_than_the_fronts(self):
        records = self._group({"dateTimeOriginal": self.BACK_DATE})
        self.assertEqual(records["box3_025.jpg"]["dateTimeOriginal"], self.FRONT_DATE)
        self.assertEqual(records["box3_025-back.jpg"]["dateTimeOriginal"], self.BACK_DATE)

    def test_the_evidence_line_records_each_files_own_date_too(self):
        # "date_original" is what merge banks as the file's evidence, and the
        # gap heuristic reads the same value, so a wrong one here is a wrong
        # rewrite later rather than only a wrong report.
        records = self._group({"dateTimeOriginal": self.BACK_DATE})
        self.assertEqual(records["box3_025-back.jpg"]["date_original"], self.BACK_DATE)

    def test_a_file_with_no_date_of_its_own_still_inherits_the_groups(self):
        # The bound on the rule above: the group answer is still the fallback,
        # which is what lets an undated back carry the object's date at all.
        records = self._group(None)
        self.assertEqual(records["box3_025-back.jpg"]["dateTimeOriginal"], self.FRONT_DATE)

    def test_the_picker_reads_both_spellings_at_either_rank(self):
        # Stated directly on the helper, since the pipeline can only exercise
        # the combination it happens to produce.
        self.assertEqual(
            utils.merge_original_sources({"dateTimeOriginal": self.BACK_DATE}, {})["date"],
            self.BACK_DATE,
        )
        self.assertEqual(
            utils.merge_original_sources({}, {"dateTimeOriginal": self.FRONT_DATE})["date"],
            self.FRONT_DATE,
        )


class TestTheConvenienceWrapperIsNotLessExpressive(unittest.TestCase):
    """``analyze_manifest`` forwards the provenance claim it wraps.

    ``core.analyze_manifest`` is the historically non-streaming signature, and
    ``photokin/README.md`` points embedders at ``titles_may_be_from_files`` as
    the way to say where their titles came from. A wrapper that accepted the
    hydrator but dropped the claim would answer ``False`` for a caller that had
    asked for ``True``, silently giving them different title precedence from the
    function they think they are calling -- and dropping a keyword argument is
    invisible at the call site in a way a missing one is not.
    """

    def test_the_claim_reaches_the_stream(self) -> None:
        """Both values arrive, so the wrapper is not hard-coded either way."""
        work = tempfile.mkdtemp(prefix="pk-wrapper-")
        path = _touch(work, "DSC_0045.jpg")

        def _from_the_database(items: list[dict]) -> None:
            for item in items:
                item.setdefault("metadata", {})["title"] = "Mom's graduation, June 1961"

        def _analyze(front, back=None, config=None, *, original_meta=None, write_sidecar=False):
            return {"result": {front: {"caption": "c", "title": "KODAK SAFETY FILM",
                                       "keywords": []}}}

        # Same pairing as the embedder case above: False is every embedder, True
        # is photokin's own -r, and the wrapper must be able to say both.
        for from_files, winner in ((False, "Mom's graduation, June 1961"),
                                   (True, "KODAK SAFETY FILM")):
            with self.subTest(titles_may_be_from_files=from_files):
                with patch("photokin.core.analyze_photo", _analyze):
                    out = core.analyze_manifest(
                        {"items": [{"path": path}]},
                        utils.Config(),
                        metadata_hydrator=_from_the_database,
                        titles_may_be_from_files=from_files,
                    )
                self.assertEqual(
                    out["results"][path].get("title"),
                    winner,
                    "analyze_manifest dropped titles_may_be_from_files instead of "
                    "forwarding it, so the wrapper and the function it wraps disagree",
                )


class TestTheFileNeverOverwritesWhatTheInputAlreadyCarried(unittest.TestCase):
    """``-r`` fills gaps; it does not correct the caller.

    The whole read is conditional on a key being absent, and that condition is
    what makes ``-r`` safe to leave on: a Lightroom title, a ``--meta`` date, an
    archivist's caption all outrank whatever the file happens to hold. Before
    these cases the guard was pinned for ``userComment`` alone -- keeping it
    there and dropping it for the other four tags passed the whole suite -- so a
    read that silently replaced a human's title with scanner boilerplate would
    have shipped green. Every tag is asserted, and each is asserted on its own,
    because the guard is evaluated per key and a per-key regression is exactly
    what a single combined assertion would miss.
    """

    #: One value per hydrated key, all of them different from the caller's, so a
    #: leak shows up as the file's value rather than as an ambiguous match.
    FROM_FILE = {
        "EXIF:DateTimeOriginal": "2019:04:11 14:22:03",
        "EXIF:UserComment": "scanner batch 41",
        "XMP:Description": "Scanned document",
        "XMP:Title": "Scanned Image",
        "XMP:Subject": ["Scans", "Untagged"],
    }
    #: What the caller already supplied, keyed the way an item's metadata is.
    FROM_CALLER = {
        "dateTimeOriginal": "1952:06:01 00:00:00",
        "userComment": "box 3, envelope 12",
        "caption": "Ruth and Sam outside the bakery",
        "title": "Mom's graduation, June 1961",
        "keywords": ["Family", "Bakery"],
    }

    def _hydrate_with(self, metadata: dict) -> dict:
        """Hydrate one item carrying ``metadata`` and return the result."""
        with tempfile.TemporaryDirectory() as work:
            path = _touch(work, "box3_025.jpg")
            item = {"path": path, "metadata": dict(metadata)}
            _tag_hydrator({utils.normalize_path(path): dict(self.FROM_FILE)})([item])
            return item["metadata"]

    def test_no_supplied_value_is_replaced_by_the_files_own(self) -> None:
        """A caller value survives the read, one subtest per key.

        Each row holds four keys and omits the fifth, rather than holding all
        five. An item with nothing missing returns at ``if not paths_needing``
        before the write-back loop is ever entered, so the all-five shape asserts
        only what :meth:`test_an_item_holding_every_key_is_not_queried_at_all`
        already asserts more strongly. Omitting one key is what forces the query,
        runs the loop, and so actually exercises the per-key filter on write-back.
        """
        for omitted in self.FROM_CALLER:
            held = {k: v for k, v in self.FROM_CALLER.items() if k != omitted}
            with self.subTest(omitted=omitted):
                hydrated = self._hydrate_with(held)
                for key, supplied in held.items():
                    self.assertEqual(
                        hydrated[key],
                        supplied,
                        f"-r overwrote {key}, which the caller had already supplied; "
                        f"the read fills gaps and must never correct its caller",
                    )
                self.assertIn(omitted, hydrated, "the one gap should have been filled")

    def test_an_empty_value_counts_as_missing_for_every_key(self) -> None:
        """Empty is a gap, not a value -- and it is a gap for all five keys.

        ``hydrate_item_metadata`` documents itself as filling the keys an item
        "is missing or holds empty", which is two branches, and the guard has to
        be per-key on both of them. Pinning only the absent branch leaves the
        empty branch free to be special-cased down to ``userComment`` -- the very
        asymmetry this class exists to remove -- and that mutation passes a suite
        that checks absence alone. Whitespace is swept alongside ``""`` because a
        guard written as ``if not meta.get(key)`` treats ``"   "`` as a real
        value and silently stops filling it.
        """
        blanks: tuple[Any, ...] = ("", "   ", "\t\n")
        for key in self.FROM_CALLER:
            # A list-valued key is empty as [] or as a list of blank strings;
            # a string-valued one cannot be [].
            empties = (*blanks, []) if key == "keywords" else blanks
            for empty in empties:
                with self.subTest(key=key, empty=repr(empty)):
                    hydrated = self._hydrate_with({key: empty})
                    self.assertNotIn(
                        hydrated.get(key),
                        (empty, None),
                        f"{key} was left holding {empty!r}: an empty value is a gap "
                        f"the read fills, not a caller value it must preserve",
                    )

    def test_each_key_is_guarded_on_its_own(self) -> None:
        """Holding one key does not protect the rest, nor they it.

        The complement of the case above: with a single key supplied, that key
        must survive and every other must be filled from the file. A guard
        written against "the item has some metadata" rather than against each
        key would pass the first test and fail this one.
        """
        for held in self.FROM_CALLER:
            with self.subTest(held=held):
                hydrated = self._hydrate_with({held: self.FROM_CALLER[held]})
                self.assertEqual(hydrated[held], self.FROM_CALLER[held])
                filled = set(hydrated) - {held}
                self.assertEqual(
                    filled,
                    set(self.FROM_CALLER) - {held},
                    "the other four keys should have been read out of the file",
                )

    def test_an_item_holding_every_key_is_not_queried_at_all(self) -> None:
        """Nothing missing means no subprocess -- the batching's own precondition."""
        asked: list[list[str]] = []

        def _records(*, exiftool_path, files, fields, timeout_sec=None, **_kw):
            asked.append(list(files))
            return []

        with tempfile.TemporaryDirectory() as work:
            path = _touch(work, "box3_025.jpg")
            item = {"path": path, "metadata": dict(self.FROM_CALLER)}
            with patch(
                "photokin.exiftool.hydrate.resolve_exiftool_path",
                return_value="/fake/exiftool",
            ), patch("photokin.exiftool.manifest.run_exiftool_json", _records):
                hydrate_item_metadata([item], ExiftoolConfig())

        self.assertEqual(asked, [], "a fully-populated item must not be read")
        self.assertEqual(item["metadata"], self.FROM_CALLER)


if __name__ == "__main__":
    unittest.main()
