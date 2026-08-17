"""Phase B2: folder and single-photo input are routed through the manifest pipeline.

Folder mode never analyzed a group whose primary front was absent -- every album
page set and every negative-only set -- which is the CRITICAL finding that opened
``docs/unified-input-pipeline.md``. Phase A made the loss loud; B2 removes it by
translating a folder into manifest items and handing them to
``process_manifest_stream``. ``photokin/tests/test_folder_mode.py`` covers the
folder entry point's own contract -- error isolation, sidecars, stream purity.
This module covers the routing itself:

* the headline regression: album pages, negative-only sets and back-only sets --
  the three categories a missing primary front used to drop -- reach the model;
* the one shape whose model call B2 deliberately changed: a group whose primary
  front has no back of its own now sends a variant's back, which is the answer
  manifest mode already gave;
* the central B2 invariant: folder input and a hand-written manifest over the
  same files produce the same model calls and the same records, so the two
  inputs really are one pipeline rather than two that agree today;
* ``--generate-manifest`` against checked-in goldens -- both the document it
  writes and the grouping that document describes -- the round trip back in
  through ``--manifest``, and the atomicity of the write itself;
* ``--process-all-variants``, dead in folder mode until B2, now changing what is
  sent;
* single-photo input with ``--back`` behaving as the two-item manifest it is
  translated into, including a back the filename grammar cannot read;
* an ordinary front/back folder still making exactly the model calls commit
  7bcaf2f made, since the point of B2 was to add the missing groups rather than
  to move the ones that already worked.

Every model entry point is mocked, so no provider client is ever built and
nothing here opens a socket. The image fixtures are empty placeholder files --
grouping reads filenames only. The checked-in fixture folder is copied into a
``TemporaryDirectory`` before each run so nothing is ever written into the
repository tree.
"""

import io
import json
import logging
import os
import shutil
import sys
import tempfile
import unittest
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from typing import ClassVar, NamedTuple
from unittest.mock import patch

from photokin import cli, core, utils

_CORE_LOGGER = "photokin.core"

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
#: A folder covering every suffix form the grammar knows: an album page set with
#: a crop of one page, a front with a back plus a "b" rescan of both plus a crop
#: of the front, a negative-only set, a second extension, and a non-image file
#: that must never reach the manifest.
_FOLDER_FIXTURE = os.path.join(_FIXTURES, "folder_routing")
#: The images that fixture holds, written out rather than listed so a test can
#: state what it expects independently of the listing code under test. The order
#: is the one ``utils.list_folder_images`` guarantees -- ``(name.lower(), name)``
#: -- because the pipeline's per-file emission order and its ``all_variant_files``
#: lists are deliberately input-ordered.
_FIXTURE_IMAGES = (
    "album-page1.jpg",
    "album-page2-crop.jpg",
    "album-page2.jpg",
    "box3_025-back.jpg",
    "box3_025-crop.jpg",
    "box3_025.jpg",
    "box3_025b-back.jpg",
    "box3_025b.jpg",
    "box3_026-negative.jpg",
    "box3_027.png",
)
_GOLDEN_MANIFEST = os.path.join(_FIXTURES, "manifests", "folder_routing_manifest.json")
_GOLDEN_GROUPING = os.path.join(_FIXTURES, "manifests", "folder_routing_grouping.json")
#: Stands in for the run's temporary directory in the goldens, which have to
#: compare equal from any checkout on any machine.
_FOLDER_TOKEN = "<FOLDER>"

# Blanked rather than removed: each is read through a falsy-default lookup, so an
# empty value pins the documented default whatever the developer's shell exports.
_NEUTRAL_ENV: dict[str, str] = {
    "MEL_VERBOSE": "",
    "MEL_DEBUG": "",
    "EXIFTOOL_PATH": "",
    "EXIFTOOL_WRITE_ENABLED": "",
    "EXIFTOOL_FIELDS": "",
}


class _Run(NamedTuple):
    """One analysis run: what the model was sent, what came back, what was logged."""

    calls: list[tuple]
    result: dict
    records: list[logging.LogRecord]


class _RecordingAnalyzers:
    """Stand-in for the three model entry points, recording their arguments.

    Every call is recorded as ``(callee, ..., write_sidecar)`` so a comparison
    against another run covers the file set, the part labels each file travelled
    under, and whether the shared analysis call was asked to write a sidecar --
    which is the whole of what folder mode delegates to it.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def photo(
        self,
        front_path: str,
        back_path: str | None = None,
        config: utils.Config | None = None,
        *,
        original_meta: dict | None = None,
        write_sidecar: bool = False,
    ) -> dict:
        """Record an ``analyze_photo`` call and return a minimal valid result."""
        self.calls.append(("photo", front_path, back_path, write_sidecar))
        return _reply(front_path)

    def front_back(
        self,
        front_paths: list[str] | None,
        back_paths: list[str] | None,
        config: utils.Config | None = None,
        *,
        original_meta: dict | None = None,
        write_sidecar: bool = False,
    ) -> dict:
        """Record an ``analyze_group_front_back`` call."""
        fronts, backs = list(front_paths or []), list(back_paths or [])
        self.calls.append(("front_back", tuple(fronts), tuple(backs), write_sidecar))
        return _reply((fronts + backs)[0])

    def parts(
        self,
        parts: list[tuple[str, list[str]]],
        config: utils.Config | None = None,
        *,
        original_meta: dict | None = None,
        write_sidecar: bool = False,
    ) -> dict:
        """Record an ``analyze_group_parts`` call."""
        self.calls.append(
            ("parts", tuple((label, tuple(paths)) for label, paths in parts), write_sidecar)
        )
        return _reply(next(p for _label, paths in parts for p in paths))


def _reply(front_path: str) -> dict:
    """Build the ``{"result": {path: record}}`` shape every analyzer returns."""
    return {"result": {front_path: {"caption": "A caption", "keywords": ["family"]}}}


@contextmanager
def _recording() -> Iterator[_RecordingAnalyzers]:
    """Replace the three model entry points with recorders for the block."""
    rec = _RecordingAnalyzers()
    with patch("photokin.core.analyze_photo", rec.photo), patch(
        "photokin.core.analyze_group_front_back", rec.front_back
    ), patch("photokin.core.analyze_group_parts", rec.parts):
        yield rec


def _sent(calls: Sequence[tuple]) -> list[tuple]:
    """Rewrite recorded calls to basenames, so two runs over two folders compare.

    Args:
        calls: A recorder's call log.

    Returns:
        The same log with every path replaced by its basename, which is the form
        the assertions below read as a description of what the model was sent.
    """

    def _short(value: object) -> object:
        if isinstance(value, str):
            return os.path.basename(value)
        if isinstance(value, tuple):
            return tuple(_short(item) for item in value)
        return value

    return [tuple(_short(field) for field in call) for call in calls]


def _tokenize(text: str, folder: str) -> str:
    """Make a written JSON document comparable against a checked-in golden.

    Args:
        text: The document as written, holding absolute paths.
        folder: The directory the run was given.

    Returns:
        The document with *folder* replaced by ``<FOLDER>`` and Windows path
        separators rewritten as forward slashes, so one golden serves every
        platform.
    """
    return text.replace(json.dumps(folder)[1:-1], _FOLDER_TOKEN).replace("\\\\", "/")


def _read(path: str) -> str:
    """Return the text of *path*, with line endings normalized by the text reader."""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class _FolderRoutingTestCase(unittest.TestCase):
    """Base giving each test scratch space and one way to run a folder."""

    #: Whole records and whole manifests are compared here; a truncated diff
    #: would say two runs differed without saying where.
    maxDiff = None

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.work: str = scratch.name

    def make_folder(self, *names: str) -> str:
        """Create a scratch folder holding empty placeholder files named *names*."""
        folder = os.path.join(self.work, "scans")
        os.makedirs(folder, exist_ok=True)
        for name in names:
            with open(os.path.join(folder, name), "w", encoding="utf-8"):
                pass
        return folder

    def copy_fixture_folder(self) -> str:
        """Copy the checked-in fixture folder into scratch space and return the copy.

        Copied rather than used in place so a run that writes -- a sidecar, a
        stray temp file -- can never touch the repository tree, and so the
        goldens are compared against a path that differs every run.
        """
        folder = os.path.join(self.work, "scans")
        shutil.copytree(_FOLDER_FIXTURE, folder)
        return folder

    def run_folder(self, folder: str, *, process_all_variants: bool = False) -> _Run:
        """Analyze *folder* with every model entry point recorded.

        Args:
            folder: Directory to analyze.
            process_all_variants: Send every variant of a group in one call.

        Returns:
            The run's model calls, its aggregate result and its log records.
        """
        config = utils.Config(process_all_variants=process_all_variants)
        with _recording() as rec, self.assertLogs(_CORE_LOGGER, level=logging.INFO) as captured:
            result = core.analyze_folder(folder, config)
        return _Run(rec.calls, result, captured.records)

    def run_manifest(self, manifest: dict | str, *, process_all_variants: bool = False) -> _Run:
        """Process *manifest* the way the folder path processes its synthesized one."""
        config = utils.Config(process_all_variants=process_all_variants)
        with _recording() as rec, self.assertLogs(_CORE_LOGGER, level=logging.INFO) as captured:
            result = core.process_manifest_stream(
                manifest=manifest,
                cfg=config,
                update_policy=core.UPDATE_MERGE_PER_VARIANT,
                strict_run_failures=True,
            )
        return _Run(rec.calls, result, captured.records)

    def basenames(self, paths: Iterable[str]) -> list[str]:
        """Return the sorted basenames of *paths*, for order-independent asserts."""
        return sorted(os.path.basename(p) for p in paths)

    def assert_nothing_skipped(self, run: _Run) -> None:
        """Assert no group was reported as skipped, which is the pre-B2 failure."""
        skipped = [
            record.getMessage()
            for record in run.records
            if record.getMessage().startswith("Skipping group")
        ]
        self.assertEqual(skipped, [], "a group was skipped; B2 exists to stop that")


class _CliTestCase(_FolderRoutingTestCase):
    """Runs ``cli.main`` in-process and cleans up the handler it installs.

    ``main`` attaches a stderr handler to the ``photokin`` logger, so process
    state is both an input and an output of every CLI test: a handler left
    behind binds an abandoned stream and double-prints into the next run's
    capture. Kept local to this module rather than imported from
    ``test_cli_preflight``: ``photokin/tests`` is not a package, so a
    cross-module test import would depend on how the runner happens to have set
    up ``sys.path``.
    """

    def setUp(self) -> None:
        super().setUp()
        package_logger = logging.getLogger("photokin")
        self.addCleanup(package_logger.setLevel, package_logger.level)
        self.addCleanup(self._remove_cli_handlers)
        self._remove_cli_handlers()

    def _remove_cli_handlers(self) -> None:
        """Detach every handler ``cli.main`` installed, from both logger scopes."""
        for logger in (logging.getLogger("photokin"), logging.getLogger()):
            for handler in list(logger.handlers):
                if handler.get_name() == cli._LOG_HANDLER_NAME:
                    logger.removeHandler(handler)
                    handler.close()

    def run_cli(self, argv: list[str]) -> tuple[int | None, str, str, list[tuple]]:
        """Run ``cli.main`` with *argv* and every model entry point recorded.

        Args:
            argv: Arguments after the program name.

        Returns:
            ``(exit code, stdout, stderr, model calls)``, the exit code being
            ``None`` when ``main`` returned without raising ``SystemExit``.
        """
        stdout, stderr = io.StringIO(), io.StringIO()
        code: int | None = None
        # Hydration is manifest mode's and does nothing to either input here --
        # neither a folder item nor a generated one carries a ``metadata`` dict,
        # which is all ``hydrate_user_comments`` acts on, and B2 leaves folder
        # hydration to Phase C. Stubbed so the run does not go looking for an
        # ExifTool binary whose presence differs between machines.
        with (
            patch.dict(os.environ, _NEUTRAL_ENV),
            patch.object(sys, "argv", ["photokin", *argv]),
            patch("photokin.cli.make_manifest_hydrator", return_value=lambda items: None),
            _recording() as rec,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, stdout.getvalue(), stderr.getvalue(), rec.calls


class TestAlbumPagesReachTheModel(_FolderRoutingTestCase):
    """The regression test for the CRITICAL finding that opened the whole plan.

    ``core.py:913`` skipped any group whose primary front was absent, which is
    every album page set: ``album-page1.jpg`` and ``album-page2.jpg`` parse as
    pages, neither is a plain front, so the group was dropped and the completion
    line counted only what survived. The README documents album pages as a
    supported input. Phase A made the loss visible and left it; this is the test
    that says it is gone.
    """

    _LOST = (
        "REGRESSION -- the CRITICAL audit finding is back: an album page set was "
        "not analyzed. Folder mode skipped every group with no plain front scan, "
        "so multipage albums and negatives were silently dropped from every run "
        "while the completion line still read clean. See section 1 of "
        "docs/unified-input-pipeline.md."
    )

    def test_an_album_page_set_is_sent_to_the_model(self) -> None:
        folder = self.make_folder("album-page1.jpg", "album-page2.jpg")

        run = self.run_folder(folder)

        self.assertEqual(_sent(run.calls), [("photo", "album-page1.jpg", None, False)], self._LOST)
        self.assert_nothing_skipped(run)

    def test_both_pages_are_recorded_and_the_page_map_names_them(self) -> None:
        folder = self.make_folder("album-page1.jpg", "album-page2.jpg")

        run = self.run_folder(folder)

        self.assertEqual(
            self.basenames(run.result["results"]),
            ["album-page1.jpg", "album-page2.jpg"],
            self._LOST,
        )
        self.assertEqual(run.result["errors"], {})
        pages = run.result["results"][os.path.join(folder, "album-page1.jpg")][
            "all_variant_files"
        ]["pages"]
        self.assertEqual(
            {num: [os.path.basename(p) for p in paths] for num, paths in pages.items()},
            {"1": ["album-page1.jpg"], "2": ["album-page2.jpg"]},
        )

    def test_an_album_beside_an_ordinary_photo_costs_the_ordinary_photo_nothing(self) -> None:
        # The old behavior was not "the album fails" but "the album is absent":
        # the run analyzed the ordinary photo, reported one group and exited 0.
        # Pinning both groups is what distinguishes the fix from a louder skip.
        folder = self.make_folder("album-page1.jpg", "album-page2.jpg", "box3_025.jpg")

        run = self.run_folder(folder)

        self.assertEqual(
            _sent(run.calls),
            [("photo", "album-page1.jpg", None, False), ("photo", "box3_025.jpg", None, False)],
            self._LOST,
        )
        self.assertEqual(
            self.basenames(run.result["results"]),
            ["album-page1.jpg", "album-page2.jpg", "box3_025.jpg"],
        )


class TestNegativeOnlyGroupsReachTheModel(_FolderRoutingTestCase):
    """The other half of the same finding: a negative-only set had no front either.

    ``README.md:254`` promises negatives are analyzed. In folder mode they never
    were, for the same structural reason album pages were not, and B1's slot
    rework is what makes analyzing them safe: a negative has its own slot and its
    own ``Negative`` part, so it cannot be handed to the model as the front of
    the print it is a negative of.
    """

    def test_a_negative_only_group_is_sent_to_the_model(self) -> None:
        folder = self.make_folder("box3_026-negative.jpg", "box3_025.jpg")

        run = self.run_folder(folder)

        self.assertEqual(
            _sent(run.calls),
            [
                ("photo", "box3_025.jpg", None, False),
                ("photo", "box3_026-negative.jpg", None, False),
            ],
            "a negative-only group was not analyzed; folder mode used to skip "
            "every group whose primary front was absent",
        )
        self.assert_nothing_skipped(run)

    def test_the_negative_is_recorded_as_a_negative(self) -> None:
        folder = self.make_folder("box3_026-negative.jpg")

        run = self.run_folder(folder)

        negative = os.path.join(folder, "box3_026-negative.jpg")
        self.assertEqual(list(run.result["results"]), [negative])
        self.assertEqual(
            run.result["results"][negative]["all_variant_files"]["negatives"], [negative]
        )

    def test_under_all_variants_it_travels_as_a_negative_part(self) -> None:
        # Not as a "Front": the part label is what the model is told the image
        # is, and B1 gave negatives their own slot precisely so a negative is
        # never described as the print.
        folder = self.make_folder("box3_026-negative.jpg")

        run = self.run_folder(folder, process_all_variants=True)

        self.assertEqual(
            _sent(run.calls), [("parts", (("Negative", ("box3_026-negative.jpg",)),), False)]
        )


class TestBackOnlyGroupsReachTheModel(_FolderRoutingTestCase):
    """The third group folder mode used to skip, and the least obvious of them.

    A folder holding only the reverse of a print -- the front never scanned, or
    scanned into a different folder -- parses as a group whose only file is a
    back. That is the same "no primary front image" condition that dropped album
    pages and negatives, so 7bcaf2f logged the skip and analyzed nothing:

    ``Skipping group 'box3_030': no primary front image; 1 file(s) not
    analyzed: box3_030-back.jpg`` -- 0 results, exit 0.

    Manifest mode at that same commit analyzed it, so this is B1 semantics
    arriving in folder mode rather than anything B2 invented. A back carries
    the handwriting, the dates and the names, which is the text content the
    whole tool exists to read, so dropping it is the same class of loss as
    dropping an album page.
    """

    _LOST = (
        "REGRESSION -- a back-only group was not analyzed. Folder mode skipped "
        "every group with no plain front scan, which is the CRITICAL finding in "
        "section 1 of docs/unified-input-pipeline.md; a back scan is usually the "
        "one carrying the handwriting, so this is a lossy run that reads as clean."
    )

    def test_a_back_only_group_is_sent_to_the_model(self) -> None:
        folder = self.make_folder("box3_030-back.jpg")

        run = self.run_folder(folder)

        # The back argument is ``None``, not the file again: it fills the
        # payload's front slot because a one-file group has nothing else to put
        # there, and sending it as both sides would pay for the upload twice and
        # tell the model the sheet is its own reverse. That is B1's "no path is
        # sent under two labels" invariant, and analyzing these groups at all is
        # only safe because it holds.
        self.assertEqual(
            _sent(run.calls), [("photo", "box3_030-back.jpg", None, False)], self._LOST
        )
        self.assert_nothing_skipped(run)

    def test_the_back_is_recorded_rather_than_counted_as_skipped(self) -> None:
        folder = self.make_folder("box3_030-back.jpg")

        run = self.run_folder(folder)

        back = os.path.join(folder, "box3_030-back.jpg")
        self.assertEqual(list(run.result["results"]), [back], self._LOST)
        self.assertEqual(run.result["errors"], {})
        # Filling the front slot of the payload did not make it a front in the
        # record: Lightroom fans metadata out over these lists, so a back filed
        # as a front would write the wrong side's keywords.
        variants = run.result["results"][back]["all_variant_files"]
        self.assertEqual(
            {"front": variants["front"], "back": variants["back"]},
            {"front": [], "back": [back]},
            "a back-only group was recorded as though the scan were a front",
        )

    def test_a_back_only_group_beside_an_ordinary_photo_costs_it_nothing(self) -> None:
        # 7bcaf2f's failure here was not a loud one: it analyzed the ordinary
        # photo, reported one group, and exited 0 with the back absent.
        folder = self.make_folder("box3_025.jpg", "box3_030-back.jpg")

        run = self.run_folder(folder)

        self.assertEqual(
            _sent(run.calls),
            [("photo", "box3_025.jpg", None, False), ("photo", "box3_030-back.jpg", None, False)],
            self._LOST,
        )
        self.assertEqual(
            self.basenames(run.result["results"]), ["box3_025.jpg", "box3_030-back.jpg"]
        )


class TestAVariantsBackIsPairedWithThePrimaryFront(_FolderRoutingTestCase):
    """An accepted behavior change, pinned here so it stays a decision.

    Where a group's primary front has no back of its own but a variant scan
    does, folder mode used to send the front alone and never send the back at
    all::

        7bcaf2f : photo(front=box3_025.jpg, back=None)
        B2      : photo(front=box3_025.jpg, back=box3_025b-back.jpg)

    This is not a B2 invention -- 7bcaf2f's *manifest* mode already made the
    second call for the same three files, so it is B1 grouping arriving in
    folder mode, which is what parity means. The maintainer decided to keep it:
    the variants are scans of one object, so that back is the object's back, and
    the old behavior was ignoring an available scan of it.

    The cost is bounded and worth stating: one extra image on the one call, and
    only for groups shaped this way. A group whose primary front has its own
    back is untouched -- ``TestOrdinaryFolderIsUnchangedFrom7bcaf2f`` pins that
    -- because the primary's own back outranks a variant's for the slot.
    """

    _FIXTURE = ("box3_025.jpg", "box3_025b.jpg", "box3_025b-back.jpg")

    def test_the_variants_back_is_sent_as_the_groups_back(self) -> None:
        folder = self.make_folder(*self._FIXTURE)

        run = self.run_folder(folder)

        self.assertEqual(
            _sent(run.calls),
            [("photo", "box3_025.jpg", "box3_025b-back.jpg", False)],
            "the group's only back scan is no longer being sent. 7bcaf2f's folder "
            "mode sent photo(box3_025.jpg, None) here and manifest mode sent the "
            "back; B2 chose the manifest answer deliberately, so a bare None back "
            "means folder mode has drifted out of parity again",
        )

    def test_this_is_the_answer_manifest_mode_gave_before_the_routing(self) -> None:
        # The literal above could be satisfied by a folder-only rule that
        # happens to agree today. What makes it correct is that it is the same
        # answer the manifest pipeline produces for the same three files, which
        # is the whole justification for accepting the change.
        folder = self.make_folder(*self._FIXTURE)
        paths = [os.path.join(folder, name) for name in sorted(self._FIXTURE)]

        folder_run = self.run_folder(folder)
        manifest_run = self.run_manifest({"items": [{"path": p} for p in paths]})

        self.assertEqual(
            folder_run.calls,
            manifest_run.calls,
            "folder input and the equivalent manifest disagree about this group, "
            "so the variant-back pairing is a folder-mode quirk rather than the "
            "parity it was accepted as",
        )

    def test_a_primary_with_its_own_back_still_prefers_it(self) -> None:
        # The bound on the change: adding the primary's own back to the same
        # fixture must put the pairing back where 7bcaf2f had it, so this is one
        # extra image for one group shape and not a general re-pairing.
        folder = self.make_folder(*self._FIXTURE, "box3_025-back.jpg")

        run = self.run_folder(folder)

        self.assertEqual(
            _sent(run.calls),
            [("photo", "box3_025.jpg", "box3_025-back.jpg", False)],
            "a variant's back displaced the primary front's own back; the "
            "accepted change was to use a variant's back when the primary has "
            "none, not to prefer one over the primary's",
        )

    def test_every_file_is_still_recorded(self) -> None:
        folder = self.make_folder(*self._FIXTURE)

        run = self.run_folder(folder)

        self.assertEqual(self.basenames(run.result["results"]), sorted(self._FIXTURE))
        self.assertEqual(run.result["errors"], {})


class TestFolderManifestParity(_FolderRoutingTestCase):
    """The central B2 invariant: one folder, one manifest, one pipeline.

    Folder input is not "handled like" a manifest, it *is* a manifest -- so a
    fixture covering every suffix form must produce the same model calls and the
    same records either way. The manifest here is hand-written rather than taken
    from ``build_folder_manifest``, and spells the same files differently
    (forward slashes, a redundant ``./`` segment, a quoted and padded entry), so
    the test cannot pass by comparing a builder against itself and no input
    spelling can leak into the output.

    The two runs are given the same directory, so parity means the records are
    equal outright rather than equal after some allowance -- the path spelling
    that differs is the input's, and the point is that it does not survive into
    the result.
    """

    def _hand_written_manifest(self, folder: str) -> dict:
        """Spell the fixture folder's files as a person would write them.

        The names come from ``_FIXTURE_IMAGES`` rather than from
        ``list_folder_images``, so the manifest side of the comparison does not
        share the folder side's listing code: a listing that started dropping
        files would otherwise drop them from both runs and stay green.

        Args:
            folder: The scratch copy of the fixture folder.

        Returns:
            A manifest whose ``items`` carry a path and nothing else, each
            spelled in a form ``normalize_path`` has to repair.
        """
        posix = folder.replace(os.sep, "/")
        items: list[dict] = []
        for index, name in enumerate(_FIXTURE_IMAGES):
            spelled = f"{posix}/./{name}" if index % 2 else f"{posix}/{name}"
            items.append({"path": f'  "{spelled}"  '})
        return {"items": items}

    def test_the_hand_written_manifest_is_not_just_the_builder_output(self) -> None:
        # Guards the guard: if the spellings above ever collapse into the
        # builder's own, every assertion below becomes a tautology.
        folder = self.copy_fixture_folder()

        hand = self._hand_written_manifest(folder)
        built = core.build_folder_manifest(folder)

        self.assertNotEqual(hand["items"], built["items"])
        self.assertEqual(len(hand["items"]), len(built["items"]))

    def test_folder_and_manifest_input_send_the_model_the_same_files(self) -> None:
        folder = self.copy_fixture_folder()
        manifest = self._hand_written_manifest(folder)

        for process_all_variants in (False, True):
            with self.subTest(process_all_variants=process_all_variants):
                folder_run = self.run_folder(folder, process_all_variants=process_all_variants)
                manifest_run = self.run_manifest(
                    manifest, process_all_variants=process_all_variants
                )

                self.assertEqual(
                    folder_run.calls,
                    manifest_run.calls,
                    "folder input and the equivalent manifest disagree about what "
                    "the model is sent, so B2 routed one input through the "
                    "pipeline and left the other beside it",
                )

    def test_folder_and_manifest_input_produce_the_same_records(self) -> None:
        folder = self.copy_fixture_folder()
        manifest = self._hand_written_manifest(folder)

        for process_all_variants in (False, True):
            with self.subTest(process_all_variants=process_all_variants):
                folder_run = self.run_folder(folder, process_all_variants=process_all_variants)
                manifest_run = self.run_manifest(
                    manifest, process_all_variants=process_all_variants
                )

                self.assertEqual(folder_run.result, manifest_run.result)
                self.assertEqual(folder_run.result["errors"], {})
                # Not implied by the equality above: two identically empty runs
                # would satisfy it. Every image in the fixture must have a
                # record, and the non-image must not.
                self.assertEqual(
                    self.basenames(folder_run.result["results"]), sorted(_FIXTURE_IMAGES)
                )

    def test_both_inputs_report_the_same_run(self) -> None:
        folder = self.copy_fixture_folder()

        folder_run = self.run_folder(folder)
        manifest_run = self.run_manifest(self._hand_written_manifest(folder))

        self.assertEqual(
            [record.getMessage() for record in folder_run.records],
            [record.getMessage() for record in manifest_run.records],
        )


class TestGeneratedManifestGolden(_CliTestCase):
    """``--generate-manifest`` writes the run it is about to describe, or would.

    The flag exists so the suffix grammar can be inspected without paying for a
    model call, which makes its output a contract in its own right. The goldens
    are checked in beside the fixture folder, so adding a file to the fixture
    shows up as a diff rather than as silence.
    """

    def test_the_written_document_matches_the_golden(self) -> None:
        folder = self.copy_fixture_folder()
        out_path = os.path.join(self.work, "generated.json")

        code, stdout, _stderr, calls = self.run_cli(
            ["--folder", folder, "--generate-manifest", out_path]
        )

        self.assertIsNone(code)
        self.assertEqual(stdout, "", "the flag describes the run; it does not print it")
        self.assertEqual(calls, [], "--generate-manifest called the model")
        self.assertEqual(_tokenize(_read(out_path), folder), _read(_GOLDEN_MANIFEST))

    def test_the_grouping_the_document_describes_matches_the_golden(self) -> None:
        # The manifest itself is a path list: the pages, crops, negatives and
        # variant letters it covers only become visible once the same bucket
        # loop the run uses has read it. That derivation is what this pins.
        folder = self.copy_fixture_folder()
        out_path = os.path.join(self.work, "generated.json")

        self.run_cli(["--folder", folder, "--generate-manifest", out_path])

        with open(out_path, "r", encoding="utf-8") as handle:
            buckets = core.build_manifest_buckets(json.load(handle)["items"])
        described = [
            {
                "group": key,
                "files": [
                    {
                        "file": os.path.basename(entry["path"]),
                        "part": entry["part_kind"],
                        "page": entry["page_num"],
                        "version": entry["version"],
                        "crop": entry["is_crop"],
                    }
                    for entry in entries
                ],
            }
            for key, entries in buckets.items()
        ]
        self.assertEqual(described, json.loads(_read(_GOLDEN_GROUPING)))

    def test_the_summary_line_counts_the_files_and_the_groups(self) -> None:
        folder = self.copy_fixture_folder()
        out_path = os.path.join(self.work, "generated.json")

        _code, _stdout, stderr, _calls = self.run_cli(
            ["--folder", folder, "--generate-manifest", out_path]
        )

        # Ten of the eleven files: notes.txt is not an image and never enters
        # the manifest, so the count is also the listing filter's assertion.
        self.assertIn("Wrote manifest for 10 file(s) in 4 group(s)", stderr)
        self.assertIn("no model call was made", stderr)


class TestGeneratedManifestRoundTrip(_CliTestCase):
    """A generated manifest fed back through ``--manifest`` reproduces its folder run.

    This is what makes the flag a debugging aid rather than a report: what it
    writes has to be runnable, and running it has to give the same answer as
    running the folder it came from. It is also the only test here that puts
    folder *and* manifest input through ``cli.main``, so it covers the wiring --
    update policy, sidecar flag, stdout document -- that the direct calls above
    skip.
    """

    def test_the_generated_manifest_reproduces_the_folder_run(self) -> None:
        folder = self.copy_fixture_folder()
        out_path = os.path.join(self.work, "generated.json")
        self.run_cli(["--folder", folder, "--generate-manifest", out_path])

        folder_code, folder_stdout, _err, folder_calls = self.run_cli(["--folder", folder])
        manifest_code, manifest_stdout, _err, manifest_calls = self.run_cli(
            ["--manifest", out_path]
        )

        self.assertEqual([folder_code, manifest_code], [None, None])
        self.assertEqual(
            _sent(manifest_calls),
            _sent(folder_calls),
            "the manifest --generate-manifest wrote for this folder groups it "
            "differently from the folder itself",
        )
        self.assertEqual(json.loads(manifest_stdout), json.loads(folder_stdout))
        self.assertEqual(len(json.loads(folder_stdout)["results"]), 10)


class TestGeneratedManifestAtomicWrite(_CliTestCase):
    """The flag overwrites a real file in the user's tree, so how it writes matters.

    ``--generate-manifest`` is the flag a user points at the same path twice
    while adjusting a folder, so the destination usually already holds the
    previous answer. Writing through a sibling temp file and ``os.replace``
    means the reader of that path sees the old manifest or the new one and never
    a half-written document -- including when the write fails, which is the case
    that used to be reachable: the code unlinked the destination before calling
    ``os.replace``, opening a window in which neither file existed.

    A real temporary directory rather than a mocked filesystem: ``os.replace``
    over an existing file is exactly the platform behavior being relied on, so a
    fake would assert the assumption instead of testing it.
    """

    def _folder_and_out(self) -> tuple[str, str]:
        """Return a fixture folder copy and a manifest destination beside it."""
        return self.copy_fixture_folder(), os.path.join(self.work, "generated.json")

    def test_overwriting_an_existing_manifest_replaces_its_contents(self) -> None:
        folder, out_path = self._folder_and_out()
        stale = '{"items": [{"path": "stale.jpg"}]}\n'
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(stale)

        code, _stdout, _stderr, calls = self.run_cli(
            ["--folder", folder, "--generate-manifest", out_path]
        )

        self.assertIsNone(code)
        self.assertEqual(calls, [])
        written = _read(out_path)
        self.assertNotEqual(written, stale, "the pre-existing manifest was left in place")
        self.assertEqual(
            [os.path.basename(item["path"]) for item in json.loads(written)["items"]],
            list(_FIXTURE_IMAGES),
        )

    def test_a_successful_write_leaves_no_temp_file_behind(self) -> None:
        folder, out_path = self._folder_and_out()

        self.run_cli(["--folder", folder, "--generate-manifest", out_path])

        self.assertFalse(
            os.path.exists(out_path + ".tmp"),
            "the sibling temp file survived a successful write, so the next run "
            "over this path starts by truncating a leftover",
        )
        # Named explicitly as well: the destination is the only thing the flag
        # is allowed to add to the directory it writes into.
        self.assertEqual(
            sorted(os.listdir(self.work)), ["generated.json", "scans"],
            "--generate-manifest left something other than its destination behind",
        )

    _PREVIOUS = '{"items": [{"path": "previous.jpg"}]}\n'

    def _with_a_previous_manifest(self) -> str:
        """Put a complete earlier manifest at the destination and return its path."""
        out_path = os.path.join(self.work, "generated.json")
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(self._PREVIOUS)
        return out_path

    def test_a_write_that_fails_while_serializing_keeps_the_previous_manifest(self) -> None:
        out_path = self._with_a_previous_manifest()

        def _dump_then_fail(obj: object, handle: object, **kwargs: object) -> None:
            """Write a truncated fragment, then fail the way a full disk does."""
            handle.write('{"items": [')  # type: ignore[attr-defined]
            raise OSError(28, "No space left on device")

        with patch("photokin.cli.json.dump", _dump_then_fail), self.assertRaises(OSError):
            cli._write_generated_manifest({"items": []}, out_path)

        self.assertEqual(
            _read(out_path),
            self._PREVIOUS,
            "a --generate-manifest that failed while serializing destroyed the "
            "manifest already at that path; serializing into a sibling temp file "
            "exists so the destination is untouched until a whole document exists",
        )
        self.assertFalse(
            os.path.exists(out_path + ".tmp"),
            "the truncated fragment was left beside the destination",
        )

    def test_a_replace_that_fails_keeps_the_previous_manifest(self) -> None:
        """The window the redundant unlink used to open, and the only injection that sees it.

        Failing during serialization cannot distinguish the two write sequences,
        because it happens before either of them touches the destination. This
        one fails ``os.replace`` itself -- a sharing violation from a sync client
        or a scanner holding the file, which is ordinary on Windows and the
        reason the destination is a photo directory's neighbour. Unlinking first
        means the old manifest is already gone when that raises and the
        ``finally`` then clears the temp file, so the failure ends with neither
        document on disk. ``os.replace`` overwrites atomically on both platforms,
        so the unlink bought nothing for it.
        """
        out_path = self._with_a_previous_manifest()

        def _replace_denied(src: str, dst: str) -> None:
            raise PermissionError(13, "The process cannot access the file")

        with patch("photokin.cli.os.replace", _replace_denied), self.assertRaises(PermissionError):
            cli._write_generated_manifest({"items": []}, out_path)

        # Existence first, and separately: the destination being *absent* is the
        # regression itself, and letting the read below raise would report it as
        # a FileNotFoundError from the helper rather than as what it is.
        self.assertTrue(
            os.path.exists(out_path),
            "a --generate-manifest whose final rename failed left the destination "
            "holding neither the old manifest nor the new one -- the user's "
            "previous manifest is simply gone. Nothing may unlink the destination "
            "ahead of os.replace: the replace is already atomic on Windows and "
            "POSIX both, so removing first only buys a window in which the file "
            "does not exist",
        )
        self.assertEqual(
            _read(out_path),
            self._PREVIOUS,
            "the destination survived the failed rename but no longer holds the "
            "manifest that was there; a failed write must not change it at all",
        )
        self.assertFalse(
            os.path.exists(out_path + ".tmp"),
            "the temp file was left beside the destination after a failed replace",
        )


class TestProcessAllVariantsWorksInFolderMode(_FolderRoutingTestCase):
    """The flag was accepted, documented and dead in folder mode before B2.

    ``core.py:915``: the group-aware branch lived in the manifest path, which
    folder input never entered, so ``--process-all-variants`` changed nothing at
    all -- both settings analyzed the same single primary file and the flag read
    as a no-op the user had no way to detect.
    """

    def _mixed_folder(self) -> str:
        return self.make_folder(
            "album-page1.jpg",
            "album-page2.jpg",
            "box3_025.jpg",
            "box3_025-back.jpg",
            "box3_025b.jpg",
        )

    def test_the_flag_changes_what_the_model_is_sent(self) -> None:
        folder = self._mixed_folder()

        off = self.run_folder(folder)
        on = self.run_folder(folder, process_all_variants=True)

        self.assertNotEqual(
            _sent(off.calls),
            _sent(on.calls),
            "--process-all-variants is a no-op in folder mode again",
        )

    def test_with_the_flag_off_only_the_primary_pair_is_sent(self) -> None:
        folder = self._mixed_folder()

        run = self.run_folder(folder)

        self.assertEqual(
            _sent(run.calls),
            [
                ("photo", "album-page1.jpg", None, False),
                ("photo", "box3_025.jpg", "box3_025-back.jpg", False),
            ],
        )

    def test_with_the_flag_on_each_group_travels_whole_in_one_call(self) -> None:
        folder = self._mixed_folder()

        run = self.run_folder(folder, process_all_variants=True)

        # The versioned front leading the unversioned one is what the manifest
        # path has always done here -- ``variant_list_sorted`` sorts ``None``
        # last -- and B2 changed nothing about it. Pinned as observed rather
        # than as intended: folder input reaches this ordering for the first
        # time, so this is where a later decision to change it will surface.
        self.assertEqual(
            _sent(run.calls),
            [
                (
                    "parts",
                    (("Page 1", ("album-page1.jpg",)), ("Page 2", ("album-page2.jpg",))),
                    False,
                ),
                (
                    "front_back",
                    ("box3_025b.jpg", "box3_025.jpg"),
                    ("box3_025-back.jpg",),
                    False,
                ),
            ],
        )


class TestSinglePhotoModeMatchesATwoItemManifest(_CliTestCase):
    """``--back`` is the one place folder-style input carries a real override.

    A filename says what it can; ``--back`` says "this file is the reverse of
    that one" whatever it is called. B2 turns that into ``is_back`` plus a shared
    ``group`` on a two-item manifest, which is the only way the pair stays one
    object: without the group key the two files bucket separately and the run
    makes two calls and pays twice.
    """

    def _photo_run(self, argv: list[str]) -> tuple[dict, list[tuple]]:
        """Run single-photo mode through the CLI, returning its stdout and calls."""
        code, stdout, _stderr, calls = self.run_cli(argv)
        self.assertIsNone(code)
        return json.loads(stdout), calls

    def test_a_conforming_back_matches_the_manifest_a_user_would_write(self) -> None:
        folder = self.make_folder("box3_025.jpg", "box3_025-back.jpg")
        front = os.path.join(folder, "box3_025.jpg")
        back = os.path.join(folder, "box3_025-back.jpg")

        single, single_calls = self._photo_run([front, "--back", back])
        # No overrides at all: two paths whose names the grammar already reads
        # as a front and its back. That is the manifest the pair translates to.
        equivalent = self.run_manifest({"items": [{"path": front}, {"path": back}]})

        self.assertEqual(single_calls, equivalent.calls)
        self.assertEqual(single, equivalent.result)
        self.assertEqual(self.basenames(single["results"]), ["box3_025-back.jpg", "box3_025.jpg"])

    def test_a_back_the_grammar_cannot_read_still_forms_one_group(self) -> None:
        folder = self.make_folder("box3_025.jpg", "reverse.jpg")
        front = os.path.join(folder, "box3_025.jpg")
        back = os.path.join(folder, "reverse.jpg")

        single, single_calls = self._photo_run([front, "--back", back])
        # What --back has to overcome: on names alone these are two objects.
        by_name = self.run_manifest({"items": [{"path": front}, {"path": back}]})

        self.assertEqual(_sent(single_calls), [("photo", "box3_025.jpg", "reverse.jpg", False)])
        self.assertEqual(
            _sent(by_name.calls),
            [("photo", "box3_025.jpg", None, False), ("photo", "reverse.jpg", None, False)],
            "the fixture no longer demonstrates anything: these names group on "
            "their own, so --back is not being asked to do any work",
        )
        # Both files are still accounted for, and the back is tagged as one.
        self.assertEqual(self.basenames(single["results"]), ["box3_025.jpg", "reverse.jpg"])
        self.assertIn("back", single["results"][back]["keywords"])

    def test_the_explicit_manifest_it_builds_says_the_same_thing(self) -> None:
        folder = self.make_folder("box3_025.jpg", "reverse.jpg")
        front = os.path.join(folder, "box3_025.jpg")
        back = os.path.join(folder, "reverse.jpg")

        built = core.build_single_photo_manifest(front, back)

        self.assertEqual(
            built["items"],
            [
                {"path": front, "group": "box3_025"},
                {"path": back, "group": "box3_025", "is_back": True},
            ],
        )
        self.assertEqual(built["source"], {"type": "single", "path": front})


class TestOrdinaryFolderIsUnchangedFrom7bcaf2f(_FolderRoutingTestCase):
    """B2 was to add the missing groups, not to move the ones that already worked.

    The literals below were captured by running commit 7bcaf2f's ``analyze_folder``
    over this fixture with the analyzers recorded. Everything in it -- a lone
    front, a front with a back and a "b" rescan of both, a second extension --
    was analyzed correctly before the routing, so any difference here is
    collateral damage rather than the fix.

    One thing does change, deliberately and documented as Breaking change #2:
    ``results`` holds one entry per file instead of one per group. That is
    asserted below as the *only* difference, so the shape change stays a
    decision rather than something the next reader has to rediscover.
    """

    _FIXTURE: ClassVar[tuple[str, ...]] = (
        "box3_024.jpg",
        "box3_025.jpg",
        "box3_025-back.jpg",
        "box3_025b.jpg",
        "box3_025b-back.jpg",
        "box3_026.png",
    )
    #: Captured from 7bcaf2f. ``write_sidecar`` is part of the tuple because the
    #: old folder path passed it explicitly, keeping the sidecar write to itself.
    _BASELINE_CALLS: ClassVar[list[tuple]] = [
        ("photo", "box3_024.jpg", None, False),
        ("photo", "box3_025.jpg", "box3_025-back.jpg", False),
        ("photo", "box3_026.png", None, False),
    ]
    #: Captured from 7bcaf2f: ``{"front": [...], "back": [...]}`` for the one
    #: group in the fixture that holds more than a single file.
    _BASELINE_VARIANT_FILES: ClassVar[dict[str, list[str]]] = {
        "front": ["box3_025.jpg", "box3_025b.jpg"],
        "back": ["box3_025-back.jpg", "box3_025b-back.jpg"],
    }
    #: Captured from 7bcaf2f: one result key per group, the primary front.
    _BASELINE_RESULT_KEYS: ClassVar[list[str]] = ["box3_024.jpg", "box3_025.jpg", "box3_026.png"]

    def test_the_model_receives_exactly_what_7bcaf2f_sent_it(self) -> None:
        run = self.run_folder(self.make_folder(*self._FIXTURE))

        self.assertEqual(
            _sent(run.calls),
            self._BASELINE_CALLS,
            "an ordinary front/back folder changed: B2 was meant to add the "
            "groups folder mode dropped, not to re-route the ones it handled",
        )

    def test_the_variant_file_lists_are_the_ones_7bcaf2f_recorded(self) -> None:
        folder = self.make_folder(*self._FIXTURE)

        run = self.run_folder(folder)

        variants = run.result["results"][os.path.join(folder, "box3_025.jpg")][
            "all_variant_files"
        ]
        self.assertEqual(
            {
                side: [os.path.basename(p) for p in variants[side]]
                for side in self._BASELINE_VARIANT_FILES
            },
            self._BASELINE_VARIANT_FILES,
            "Lightroom fans metadata out over these lists, so a change here "
            "changes which files an unchanged plug-in writes",
        )

    def test_the_only_change_is_the_documented_per_file_result_shape(self) -> None:
        folder = self.make_folder(*self._FIXTURE)

        run = self.run_folder(folder)

        recorded = self.basenames(run.result["results"])
        # Breaking change #2: every 7bcaf2f key survives, spelled the same, and
        # the files that used to have no entry at all now have one.
        self.assertEqual(
            [name for name in recorded if name in self._BASELINE_RESULT_KEYS],
            self._BASELINE_RESULT_KEYS,
        )
        self.assertEqual(
            [name for name in recorded if name not in self._BASELINE_RESULT_KEYS],
            ["box3_025-back.jpg", "box3_025b-back.jpg", "box3_025b.jpg"],
        )
        self.assertEqual(run.result["errors"], {})

    def test_a_folder_of_ordinary_scans_loses_nothing_and_says_so(self) -> None:
        run = self.run_folder(self.make_folder(*self._FIXTURE))

        completion = [
            record
            for record in run.records
            if record.getMessage().startswith("Batch completed")
        ]
        self.assertEqual(len(completion), 1)
        self.assertEqual(completion[0].levelno, logging.INFO)
        self.assertIn("0 file(s) displaced or dropped", completion[0].getMessage())


if __name__ == "__main__":
    unittest.main()
