"""Tests for ``--sidecar-md`` and its emit-loop writer (document-mode Phase 2, W5).

Drives ``process_manifest_stream`` (through ``core.analyze_folder``, so folder
input and its manifest translation are exercised together) with the provider
boundary stubbed the way ``photokin/tests/test_group_analyzer_payload.py`` and
``photokin/tests/test_manifest_grouping.py`` do: the three analyzer entry
points (``analyze_photo``, ``analyze_group_front_back``, ``analyze_group_parts``)
are replaced with a recorder that returns a controllable reply, so the real
grouping, merge and emit-loop code runs unmodified and only the model call
itself is faked.

The fixture folder is ``photokin/tests/fixtures/folder_routing``, which already
holds a front/back pair with a "b" variant rescan and a crop of the front
(``box3_025*``), a negative-only group (``box3_026-negative.jpg``), a two-page
album group (``album-page*``, with a crop of page 2) and one unrelated single
file (``box3_027.png``) -- exactly the shapes ``docs/document-mode-contract.md``
section 8's gate has to be tested against. It is copied into a scratch
``TemporaryDirectory`` before every test, never used in place.
"""

import io
import logging
import os
import shutil
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr
from unittest.mock import patch

from photokin import cli, core, utils

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
_FOLDER_FIXTURE = os.path.join(_FIXTURES, "folder_routing")

#: The images the fixture folder holds (``notes.txt`` is not one and must never
#: earn a sidecar). Listed here rather than derived from a listing call so the
#: expectations below are independent of the code under test.
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
_CROPS = frozenset({"album-page2-crop.jpg", "box3_025-crop.jpg"})


def _stem(name: str) -> str:
    """Return *name* with its extension replaced by ``.md``."""
    return os.path.splitext(name)[0] + ".md"


class _StubAnalyzers:
    """Stand-in for the three model entry points, returning a controllable reply.

    ``category_map`` and ``transcriptions_map`` are looked up by a substring of
    the basenames one call carries -- the fixture's family prefix, e.g.
    ``"box3_025"`` or ``"album"`` -- so one instance can drive an entire folder
    run while each group's category and transcriptions are set independently.
    That is exactly what auto mode's per-group gate needs to be tested against:
    one run, several groups, several verdicts.
    """

    def __init__(
        self,
        category_map: dict[str, str] | None = None,
        default_category: str = "Portrait",
        transcriptions_map: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.category_map = category_map or {}
        self.default_category = default_category
        self.transcriptions_map = transcriptions_map or {}
        self.calls: list[tuple] = []

    def _lookup(self, paths: list[str], table: dict) -> object:
        for key, value in table.items():
            if any(key in os.path.basename(p) for p in paths):
                return value
        return None

    def _reply(self, primary: str, paths: list[str]) -> dict:
        category = self._lookup(paths, self.category_map) or self.default_category
        record: dict = {
            "caption": "A caption",
            "keywords": ["family"],
            "category": category,
        }
        transcriptions = self._lookup(paths, self.transcriptions_map)
        if transcriptions is not None:
            record["transcriptions"] = transcriptions
        return {"result": {primary: record}}

    def photo(
        self,
        front_path: str,
        back_path: str | None = None,
        config: utils.Config | None = None,
        *,
        original_meta: dict | None = None,
        write_sidecar: bool = False,
    ) -> dict:
        """Record an ``analyze_photo`` call and return a controllable reply."""
        self.calls.append(("photo", front_path, back_path))
        paths = [p for p in (front_path, back_path) if p]
        return self._reply(front_path, paths)

    def front_back(
        self,
        front_paths: list[str] | None,
        back_paths: list[str] | None,
        config: utils.Config | None = None,
        *,
        original_meta: dict | None = None,
        write_sidecar: bool = False,
    ) -> dict:
        """Record an ``analyze_group_front_back`` call and return a controllable reply."""
        fronts, backs = list(front_paths or []), list(back_paths or [])
        self.calls.append(("front_back", tuple(fronts), tuple(backs)))
        paths = fronts + backs
        return self._reply(paths[0], paths)

    def parts(
        self,
        parts: list[tuple[str, list[str]]],
        config: utils.Config | None = None,
        *,
        original_meta: dict | None = None,
        write_sidecar: bool = False,
    ) -> dict:
        """Record an ``analyze_group_parts`` call and return a controllable reply."""
        self.calls.append(("parts", tuple((label, tuple(p)) for label, p in parts)))
        flat = [p for _, ps in parts for p in ps]
        return self._reply(flat[0], flat)


@contextmanager
def _stubbed(rec: _StubAnalyzers) -> Iterator[None]:
    """Replace the three model entry points with *rec* for the block."""
    with (
        patch("photokin.core.analyze_photo", rec.photo),
        patch("photokin.core.analyze_group_front_back", rec.front_back),
        patch("photokin.core.analyze_group_parts", rec.parts),
    ):
        yield


class _SidecarMdTestCase(unittest.TestCase):
    """Shared scratch space and folder runner for the cases below."""

    maxDiff = None

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.work: str = scratch.name

    def copy_fixture_folder(self) -> str:
        """Copy the checked-in fixture folder into scratch space and return the copy.

        Copied rather than used in place: this is the one test module in the
        suite that means to write files beside the fixture's images, and a
        write into the repository tree would be a bug in the test rather than
        the behavior it means to cover.
        """
        folder = os.path.join(self.work, "scans")
        shutil.copytree(_FOLDER_FIXTURE, folder)
        return folder

    def run_folder(
        self,
        folder: str,
        *,
        sidecar_md: str,
        category_map: dict[str, str] | None = None,
        default_category: str = "Portrait",
        transcriptions_map: dict[str, dict[str, str]] | None = None,
        group_by: str = utils.GROUP_BY_OBJECT,
    ) -> tuple[dict, _StubAnalyzers]:
        """Analyze *folder* under the given ``sidecar_md`` mode and stubbed replies."""
        cfg = utils.Config(group_by=group_by, sidecar_md=sidecar_md)
        rec = _StubAnalyzers(category_map, default_category, transcriptions_map)
        with _stubbed(rec):
            result = core.analyze_folder(folder, cfg)
        return result, rec

    def md_files(self, folder: str) -> set[str]:
        """Return the basenames of every ``.md`` file anywhere under *folder*."""
        found: set[str] = set()
        for _root, _dirs, files in os.walk(folder):
            found.update(name for name in files if name.endswith(".md"))
        return found

    def read(self, path: str) -> str:
        """Return the text of *path*."""
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()


class TestSidecarMdOff(_SidecarMdTestCase):
    """The default: nothing new is written, whatever the model says."""

    def test_off_writes_no_markdown_sidecar(self) -> None:
        folder = self.copy_fixture_folder()

        result, _rec = self.run_folder(
            folder, sidecar_md=utils.SIDECAR_MD_OFF, default_category="Document"
        )

        self.assertEqual(self.md_files(folder), set())
        self.assertEqual(result["errors"], {})


class TestSidecarMdAll(_SidecarMdTestCase):
    """Manual mode: every emitted file except crops, whatever the category."""

    def test_all_writes_a_sidecar_for_every_file_except_crops(self) -> None:
        folder = self.copy_fixture_folder()

        result, _rec = self.run_folder(
            folder, sidecar_md=utils.SIDECAR_MD_ALL, default_category="Portrait"
        )

        expected = {_stem(name) for name in _FIXTURE_IMAGES if name not in _CROPS}
        self.assertEqual(self.md_files(folder), expected)
        self.assertEqual(result["errors"], {})

    def test_a_crop_never_gets_a_sidecar_even_under_all(self) -> None:
        folder = self.copy_fixture_folder()

        self.run_folder(folder, sidecar_md=utils.SIDECAR_MD_ALL)

        written = self.md_files(folder)
        for crop in _CROPS:
            self.assertNotIn(_stem(crop), written, "a crop was given its own sidecar (D9)")


class TestSidecarMdAuto(_SidecarMdTestCase):
    """Auto mode: gated on the group's merged category, per D2."""

    def test_a_portrait_category_writes_nothing(self) -> None:
        folder = self.copy_fixture_folder()

        self.run_folder(
            folder, sidecar_md=utils.SIDECAR_MD_AUTO, default_category="Portrait"
        )

        self.assertEqual(self.md_files(folder), set())

    def test_a_document_category_writes_the_full_group(self) -> None:
        folder = self.copy_fixture_folder()

        self.run_folder(
            folder,
            sidecar_md=utils.SIDECAR_MD_AUTO,
            default_category="Portrait",
            category_map={"box3_025": "Document"},
        )

        self.assertEqual(
            self.md_files(folder),
            {"box3_025.md", "box3_025-back.md", "box3_025b.md", "box3_025b-back.md"},
        )

    def test_a_postcard_category_is_written(self) -> None:
        folder = self.copy_fixture_folder()

        self.run_folder(
            folder,
            sidecar_md=utils.SIDECAR_MD_AUTO,
            default_category="Portrait",
            category_map={"album": "Postcard"},
        )

        self.assertEqual(self.md_files(folder), {"album-page1.md", "album-page2.md"})

    def test_a_photo_page_category_is_not_written(self) -> None:
        # D2: an album page of mounted photos stays photo-like, unlike Document
        # and Postcard.
        folder = self.copy_fixture_folder()

        self.run_folder(
            folder,
            sidecar_md=utils.SIDECAR_MD_AUTO,
            default_category="Portrait",
            category_map={"album": "Photo Page"},
        )

        self.assertEqual(self.md_files(folder), set())


class TestSidecarMdPerFileTranscription(_SidecarMdTestCase):
    """A back and a variant rescan each get their own file under their own name."""

    def test_each_file_carries_its_own_part_under_its_own_filename(self) -> None:
        folder = self.copy_fixture_folder()

        self.run_folder(
            folder,
            sidecar_md=utils.SIDECAR_MD_ALL,
            transcriptions_map={
                "box3_025": {"Front": "Front transcription text.", "Back": "Back transcription text."}
            },
        )

        front = self.read(os.path.join(folder, "box3_025.md"))
        variant_front = self.read(os.path.join(folder, "box3_025b.md"))
        back = self.read(os.path.join(folder, "box3_025-back.md"))
        variant_back = self.read(os.path.join(folder, "box3_025b-back.md"))

        # Variant rescans of one part share that part's transcription text --
        # they are scans of the same physical object -- but each still writes
        # under its own filename.
        self.assertIn("Front transcription text.", front)
        self.assertIn("Front transcription text.", variant_front)
        self.assertIn("Back transcription text.", back)
        self.assertIn("Back transcription text.", variant_back)
        self.assertIn('source_file: "box3_025.jpg"', front)
        self.assertIn('source_file: "box3_025b.jpg"', variant_front)
        self.assertIn('source_file: "box3_025-back.jpg"', back)
        self.assertIn('source_file: "box3_025b-back.jpg"', variant_back)


class TestSidecarMdRerun(_SidecarMdTestCase):
    """A re-run overwrites its sidecar rather than appending to it."""

    def test_a_rerun_overwrites_the_previous_content(self) -> None:
        folder = self.copy_fixture_folder()
        target = os.path.join(folder, "box3_026-negative.md")

        self.run_folder(
            folder,
            sidecar_md=utils.SIDECAR_MD_ALL,
            transcriptions_map={"box3_026": {"Negative": "First pass text."}},
        )
        first = self.read(target)
        self.assertIn("First pass text.", first)

        self.run_folder(
            folder,
            sidecar_md=utils.SIDECAR_MD_ALL,
            transcriptions_map={"box3_026": {"Negative": "Second pass text."}},
        )
        second = self.read(target)

        self.assertNotIn("First pass text.", second)
        self.assertIn("Second pass text.", second)
        # Overwritten, not appended: exactly one frontmatter fence pair.
        self.assertEqual(second.count("---"), 2)


class TestSidecarMdUnwritableDestination(_SidecarMdTestCase):
    """A destination the writer cannot open warns but never takes the group down."""

    def test_an_unwritable_destination_warns_and_the_record_still_lands(self) -> None:
        folder = self.copy_fixture_folder()
        target = os.path.join(os.path.abspath(folder), "box3_026-negative.md")
        real_open = open

        def _flaky_open(path: object, mode: str = "r", *args: object, **kwargs: object) -> object:
            if "w" in mode and os.path.abspath(str(path)) == target:
                raise OSError(13, "Permission denied")
            return real_open(path, mode, *args, **kwargs)  # type: ignore[call-overload]

        with (
            patch("photokin.doc_sidecar.open", _flaky_open),
            self.assertLogs("photokin.doc_sidecar", level=logging.WARNING) as logs,
        ):
            result, _rec = self.run_folder(folder, sidecar_md=utils.SIDECAR_MD_ALL)

        self.assertTrue(
            any("box3_026-negative.jpg" in record.getMessage() for record in logs.records),
            "the warning did not name the file whose sidecar could not be written",
        )
        self.assertFalse(os.path.exists(target))

        neg_path = os.path.join(folder, "box3_026-negative.jpg")
        self.assertIn(neg_path, result["results"], "a sidecar failure took the analysis down with it")
        self.assertEqual(result["errors"], {})
        # Every other file's sidecar still landed -- one bad destination in the
        # run does not stop the rest of the batch from writing theirs.
        self.assertIn("box3_025.md", self.md_files(folder))


class TestSidecarMdStemCollisionAcrossExtensions(_SidecarMdTestCase):
    """Two group members whose names differ only by extension share one destination.

    A TIFF master kept beside its JPEG derivative is the commonest shape in a
    scanning archive -- this codebase's own grouping code calls it out by
    name (it is also what the payload's own slot-collision rule already
    picks the TIFF to analyze) -- and ``sidecar_path_for`` drops the
    extension the same way the JSON sidecar's ``<stem>.json`` already does.
    The JSON sidecar never had to care, since it is written once per group
    from the primary; the markdown one is written per file, so the
    collision is real: both files still reach the per-file emit loop (the
    slot collision only decides which one is SENT to the model, not which
    ones get fanned-out records), and without a guard the second file's
    write would silently erase the first's, with nothing said about it.
    """

    def test_the_first_claim_keeps_the_destination_and_the_clash_is_logged(
        self,
    ) -> None:
        folder = os.path.join(self.work, "scans")
        os.makedirs(folder)
        jpg_path = os.path.join(folder, "box3_025.jpg")
        tif_path = os.path.join(folder, "box3_025.tif")
        for path in (jpg_path, tif_path):
            with open(path, "w", encoding="utf-8"):
                pass

        with self.assertLogs("photokin.core", level="WARNING") as logged:
            result, _rec = self.run_folder(folder, sidecar_md=utils.SIDECAR_MD_ALL)

        self.assertEqual(result["errors"], {})
        # Both files still get their own fanned-out record and reach the
        # sidecar gate -- the slot collision above only decided which one
        # was analyzed, not which ones get emitted.
        self.assertEqual(
            {os.path.basename(p) for p in result["results"]},
            {"box3_025.jpg", "box3_025.tif"},
        )
        self.assertEqual(
            self.md_files(folder), {"box3_025.md"},
            "the collision produced more than one file, or none at all",
        )
        content = self.read(os.path.join(folder, "box3_025.md"))
        # The owner is settled by rank, not by the order the folder happened to
        # be listed in, so it is the TIFF master -- the same file the slot
        # collision already chose to send to the model. A sidecar describing an
        # analysis should carry the name of the file that was analyzed, and a
        # folder read in a different order must still produce this same result.
        self.assertIn('source_file: "box3_025.tif"', content)
        self.assertNotIn('source_file: "box3_025.jpg"', content)
        self.assertTrue(
            any(
                "share one sidecar destination" in line
                and "box3_025.tif" in line
                and "box3_025.jpg" in line
                for line in logged.output
            ),
            logged.output,
        )


class TestSidecarMdFlagValidation(unittest.TestCase):
    """argparse-level rejection of an unknown ``--sidecar-md`` value."""

    def setUp(self) -> None:
        self.addCleanup(self._remove_cli_handlers)

    def _remove_cli_handlers(self) -> None:
        """Detach any handler ``cli.main`` installed, so tests never leak state."""
        for logger_obj in (logging.getLogger("photokin"), logging.getLogger()):
            for handler in list(logger_obj.handlers):
                if handler.get_name() in (cli._LOG_HANDLER_NAME, cli._LOG_FILE_HANDLER_NAME):
                    logger_obj.removeHandler(handler)
                    handler.close()

    def test_argparse_rejects_an_unknown_sidecar_md_value(self) -> None:
        stderr = io.StringIO()
        with (
            patch.object(sys, "argv", ["photokin", "--sidecar-md", "bogus"]),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as ctx,
        ):
            cli.main()

        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--sidecar-md", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
