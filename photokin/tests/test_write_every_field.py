"""Every field the Lightroom plug-in asks for reaches the file, without losing what is there.

The plug-in writes every canonical tag through photokin and then has Lightroom
re-read the file, so three things have to hold in the file itself, not just in
the changeset:

- ``EXIF:UserComment`` keeps the person's text and replaces only the previous
  run's ``[AI Analysis]`` block.
- A location write lands in the XMP tag Lightroom reads as well as in IPTC IIM,
  and a location already in the file is read first and kept.
- A capture date also lands in Date Created, at the precision the model
  claimed, and a date set only in Date Created counts as the file's own.

The end-to-end cases drive ``cli.main`` with the model stubbed and the real
ExifTool binary against a real JPEG and a real TIFF; they skip only when no
ExifTool is on PATH. The pure helpers behind them are covered first, with no
binary at all.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from typing import Any, ClassVar
from unittest.mock import patch

from PIL import Image

from photokin import cli, utils
from photokin.canonical import (
    _claimed_precision,
    _date_created_value,
    build_canonical_patch,
    canonical_values_from_metadata,
    canonical_values_from_patch,
    merge_user_comment,
    split_user_comment,
)
from photokin.changeset import diff_canonical_metadata
from photokin.exiftool.apply import apply_changeset
from photokin.exiftool.config import ExiftoolConfig
from photokin.exiftool.manifest import utf8_argfile

#: The plug-in's ``--exiftool-fields`` after this change, verbatim.
_PLUGIN_FIELDS = (
    "EXIF:UserComment,XMP-dc:Title,XMP-dc:Description,XMP-dc:Subject,"
    "EXIF:DateTimeOriginal,IPTC:Country-PrimaryLocationName,IPTC:Province-State,"
    "IPTC:City,IPTC:Sub-location"
)

#: Pinned so the machine's own environment cannot change what a run does.
_NEUTRAL_ENV = {
    "MEL_VERBOSE": "",
    "MEL_DEBUG": "",
    "EXIFTOOL_PATH": "",
    "EXIFTOOL_WRITE_ENABLED": "",
    "EXIFTOOL_FIELDS": "",
    "LLM_PROVIDER": "openai",
}

#: Both formats the plug-in hands photokin.
_FORMATS = ("jpg", "tif")

_READ_BACK_TAGS = (
    "EXIF:UserComment",
    "EXIF:DateTimeOriginal",
    "XMP-photoshop:DateCreated",
    "IPTC:DateCreated",
    "IPTC:TimeCreated",
    "IPTC:City",
    "XMP-photoshop:City",
)


class TestUserCommentMerge(unittest.TestCase):
    """The person's text is kept; only the AI block is replaced."""

    def test_user_text_is_kept_and_the_old_block_is_dropped(self) -> None:
        existing = "Grandma's note\n\n[AI Analysis on 2026-01-01]: old reading"

        merged = merge_user_comment(existing, "[AI Analysis on 2026-11-15]: new reading")

        self.assertEqual(merged, "Grandma's note\n\n[AI Analysis on 2026-11-15]: new reading")

    def test_a_comment_that_is_only_an_old_block_is_replaced_whole(self) -> None:
        merged = merge_user_comment("[AI Analysis]: old", "[AI Analysis]: new")

        self.assertEqual(merged, "[AI Analysis]: new")

    def test_a_comment_with_no_marker_is_all_user_text(self) -> None:
        self.assertEqual(split_user_comment("line one\n  line two  \n\n"), "line one\n  line two")

    def test_an_unmarked_ai_block_gets_the_marker_so_the_next_run_finds_it(self) -> None:
        once = merge_user_comment("note", "plain analysis")

        twice = merge_user_comment(once, "plain analysis")

        self.assertEqual(once, "note\n\n[AI Analysis]: plain analysis")
        self.assertEqual(twice, once)

    def test_no_ai_text_proposes_no_change(self) -> None:
        patch_, _meta = build_canonical_patch(
            {"caption": "A porch", "userComment": "Grandma's note"}, utils.Config()
        )

        self.assertNotIn("EXIF:UserComment", patch_)

    def test_an_unchanged_merge_proposes_no_change(self) -> None:
        existing = "Grandma's note\n\n[AI Analysis]: same"

        patch_, _meta = build_canonical_patch(
            {"ai_caption": "[AI Analysis]: same", "userComment": existing}, utils.Config()
        )

        self.assertNotIn("EXIF:UserComment", patch_)

    def test_the_before_snapshot_carries_the_files_own_comment(self) -> None:
        before = canonical_values_from_metadata({"userComment": "Grandma's note"}, utils.Config())

        self.assertEqual(before["EXIF:UserComment"], "Grandma's note")


class TestDateCreatedPrecision(unittest.TestCase):
    """Date Created never claims more of a date than the model did."""

    def test_pattern_decides_the_precision(self) -> None:
        cases = {"Y~": 1, "Y!": 1, "Y!M~": 2, "Y!M?": 1, "Y!M!D!": 3, "Y!M!D?": 2}
        for pattern, expected in cases.items():
            with self.subTest(pattern=pattern):
                self.assertEqual(_claimed_precision({"pattern": pattern}), expected)

    def test_iso_decides_when_the_pattern_is_unusable(self) -> None:
        cases = {"1952": 1, "1952-06": 2, "1952-06-01": 3, "1910s": 1, "": 1}
        for iso, expected in cases.items():
            with self.subTest(iso=iso):
                self.assertEqual(_claimed_precision({"pattern": "bogus", "iso": iso}), expected)

    def test_a_year_guess_is_written_as_a_year(self) -> None:
        meta = {"date_guess": {"pattern": "Y~", "iso": "1910s", "import_date": "1915-06-15"}}

        self.assertEqual(_date_created_value("1915-06-15", meta), "1915")

    def test_the_files_own_date_is_kept_whole(self) -> None:
        meta = {
            "date_guess": {"pattern": "Y~", "import_date": "1952-06-15"},
            "date_original": "1952:06:15 10:30:00",
        }

        self.assertEqual(_date_created_value("1952:06:15 10:30:00", meta), "1952:06:15 10:30:00")

    def test_a_decade_is_not_a_date(self) -> None:
        self.assertIsNone(_date_created_value("1910s", {}))

    def test_the_patch_carries_both_date_tags(self) -> None:
        meta = {
            "dateTimeOriginal": "1952-06-15",
            "date_guess": {
                "iso": "1952",
                "import_date": "1952-06-15",
                "pattern": "Y!",
                "confidence": 0.9,
            }
        }

        patch_, _meta = build_canonical_patch(meta, utils.Config())

        self.assertEqual(patch_["EXIF:DateTimeOriginal"]["value"], "1952-06-15")
        self.assertEqual(patch_["XMP-photoshop:DateCreated"]["value"], "1952")


class TestAKeywordLineBreakSettlesOnTheFirstRun(unittest.TestCase):
    """The diff folds a keyword's line break the way the write does."""

    def test_a_rerun_proposes_nothing_for_a_keyword_already_written(self) -> None:
        cfg = utils.Config()
        before = canonical_values_from_metadata({"keywords": ["family", "Family Reunion"]}, cfg)
        patch_, _meta = build_canonical_patch({"keywords": ["family", "Family\nReunion"]}, cfg)

        proposed = diff_canonical_metadata(before, canonical_values_from_patch(patch_))

        self.assertEqual((proposed["keywords_add"], proposed["keywords_remove"]), ([], []))


class TestDateCreatedTravelsWithTheCaptureDate(unittest.TestCase):
    """The diff proposes the two date tags together or not at all."""

    def test_a_date_change_brings_date_created_even_when_it_matches(self) -> None:
        before = {"EXIF:DateTimeOriginal": "1950", "XMP-photoshop:DateCreated": "1952"}
        after = {"EXIF:DateTimeOriginal": "1952-06-15", "XMP-photoshop:DateCreated": "1952"}

        proposed = diff_canonical_metadata(before, after)

        self.assertEqual(
            proposed["set"],
            {"EXIF:DateTimeOriginal": "1952-06-15", "XMP-photoshop:DateCreated": "1952"},
        )

    def test_date_created_never_changes_alone(self) -> None:
        before = {"EXIF:DateTimeOriginal": "1952-06-15", "XMP-photoshop:DateCreated": "1952:06:15"}
        after = {"EXIF:DateTimeOriginal": "1952-06-15", "XMP-photoshop:DateCreated": "1952"}

        self.assertEqual(diff_canonical_metadata(before, after)["set"], {})


def _require_exiftool() -> str:
    """Return the ExifTool path, or skip when the binary is absent."""
    exiftool = shutil.which("exiftool")
    if not exiftool:
        raise unittest.SkipTest("no exiftool binary on PATH; these cases need the real one")
    return exiftool


def _model_stub(reply: dict) -> Any:
    """Return a stand-in for every analyzer entry point that answers ``reply``."""

    def _analyze(first: Any, *_args: Any, **_kwargs: Any) -> dict:
        if not isinstance(first, str):
            head = first[0]
            first = head[1][0] if isinstance(head, tuple) else head
        return {"result": {first: json.loads(json.dumps(reply))}}

    return _analyze


@contextmanager
def _stubbed_model(reply: dict) -> Iterator[None]:
    """Replace the three model entry points for the block."""
    stub = _model_stub(reply)
    with (
        patch("photokin.core.analyze_photo", stub),
        patch("photokin.core.analyze_group_front_back", stub),
        patch("photokin.core.analyze_group_parts", stub),
    ):
        yield


class _RealExiftoolCase(unittest.TestCase):
    """A scratch folder, real images, and the plug-in's CLI call."""

    maxDiff = None

    def setUp(self) -> None:
        self.exiftool = _require_exiftool()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.work = scratch.name
        package_logger = logging.getLogger("photokin")
        self.addCleanup(package_logger.setLevel, package_logger.level)
        self.addCleanup(self._remove_cli_handlers)

    def _remove_cli_handlers(self) -> None:
        """Detach the handlers ``cli.main`` installs, so runs do not leak into each other."""
        for logger in (logging.getLogger("photokin"), logging.getLogger()):
            for handler in list(logger.handlers):
                if handler.get_name() in (cli._LOG_HANDLER_NAME, cli._LOG_FILE_HANDLER_NAME):
                    logger.removeHandler(handler)
                    handler.close()

    def image(
        self, extension: str, tags: dict[str, str] | None = None, folder_prefix: str = "tmp"
    ) -> str:
        """Create a real image in its own folder, seeded with ``tags``.

        Each value is seeded from a UTF-8 file (``-TAG<=file``), because an
        inline non-ASCII value reaches ExifTool on Windows as ``?``. The
        folder's name starts with ``folder_prefix``.
        """
        folder = tempfile.mkdtemp(prefix=folder_prefix, dir=self.work)
        path = os.path.join(folder, f"photo.{extension}")
        Image.new("RGB", (4, 4), "white").save(path)
        args = []
        for index, (tag, value) in enumerate((tags or {}).items()):
            value_path = os.path.join(self.work, f"{os.path.basename(folder)}-{index}.txt")
            with open(value_path, "w", encoding="utf-8", newline="") as handle:
                handle.write(value)
            args.append(f"-{tag}<={value_path}")
        if args:
            self.exiftool_run("-overwrite_original", *args, path)
        return path

    def exiftool_run(self, *args: str) -> str:
        """Run the real ExifTool and return its stdout.

        The arguments travel as a UTF-8 argument file on stdin, so an
        accented folder or value reaches ExifTool intact on Windows.
        """
        stdin = "\n".join(["-charset", "filename=utf8", *args]) + "\n"
        result = subprocess.run(
            [self.exiftool, "-@", "-"],
            input=stdin,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        return result.stdout

    def read_back(self, path: str) -> dict[str, str]:
        """Read the tags these cases check, as strings, keyed by the requested name.

        ``-G1`` reports the EXIF tags under ``ExifIFD``; they are renamed back
        to the ``EXIF:`` spelling the cases ask for.
        """
        args = ["-j", "-G1", *(f"-{tag}" for tag in _READ_BACK_TAGS), path]
        record = json.loads(self.exiftool_run(*args))[0]
        return {
            key.replace("ExifIFD:", "EXIF:"): str(value)
            for key, value in record.items()
            if key != "SourceFile"
        }

    def run_plugin_call(self, path: str, reply: dict) -> dict:
        """Run the plug-in's command on ``path``'s folder and return its changeset record."""
        folder = os.path.dirname(path)
        out_path = os.path.join(folder + "-out", "results.ndjson")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        argv = [
            folder,
            "--read",
            "--changeset",
            "true",
            "--exiftool-write",
            "true",
            "--exiftool-fields",
            _PLUGIN_FIELDS,
            "--exiftool-path",
            self.exiftool,
            "--output-file",
            out_path,
        ]
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, _NEUTRAL_ENV),
            patch.object(sys, "argv", ["photokin", *argv]),
            _stubbed_model(reply),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            try:
                cli.main()
            except SystemExit as exc:
                self.assertIn(exc.code, (0, None), stderr.getvalue())
        changeset = os.path.join(os.path.dirname(out_path), "results_changeset.ndjson")
        with open(changeset, encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(records), 1, stderr.getvalue())
        return records[0]


class TestUserCommentInTheFile(_RealExiftoolCase):
    """Acceptance cases 1-5 and 8: the comment, end to end."""

    REPLY: ClassVar[dict[str, Any]] = {
        "caption": "A porch",
        "keywords": ["family"],
        "ai_caption": "[AI Analysis]: A porch.",
    }

    def test_the_users_note_is_kept_above_the_ai_block(self) -> None:
        for extension in _FORMATS:
            with self.subTest(format=extension):
                path = self.image(extension, {"EXIF:UserComment": "Grandma's note"})

                self.run_plugin_call(path, self.REPLY)

                comment = self.read_back(path)["EXIF:UserComment"]
                self.assertRegex(comment, r"^Grandma's note\n\n\[AI Analysis[^\]]*\]: A porch\.$")

    def test_a_rerun_leaves_one_ai_block(self) -> None:
        for extension in _FORMATS:
            with self.subTest(format=extension):
                path = self.image(extension, {"EXIF:UserComment": "Grandma's note"})
                self.run_plugin_call(path, self.REPLY)

                self.run_plugin_call(path, {**self.REPLY, "ai_caption": "[AI Analysis]: A garden."})

                comment = self.read_back(path)["EXIF:UserComment"]
                self.assertEqual(comment.count("[AI Analysis"), 1, comment)
                self.assertTrue(comment.startswith("Grandma's note\n\n"), comment)
                self.assertTrue(comment.endswith("]: A garden."), comment)

    def test_an_old_ai_only_comment_is_replaced(self) -> None:
        for extension in _FORMATS:
            with self.subTest(format=extension):
                path = self.image(
                    extension, {"EXIF:UserComment": "[AI Analysis on 2026-11-15]: old"}
                )

                self.run_plugin_call(path, self.REPLY)

                comment = self.read_back(path)["EXIF:UserComment"]
                self.assertRegex(comment, r"^\[AI Analysis[^\]]*\]: A porch\.$")

    def test_no_ai_text_proposes_no_comment_change(self) -> None:
        for extension in _FORMATS:
            with self.subTest(format=extension):
                path = self.image(extension, {"EXIF:UserComment": "Grandma's note"})

                record = self.run_plugin_call(path, {"caption": "A porch", "keywords": ["family"]})

                self.assertNotIn("EXIF:UserComment", record["proposed_changes"]["set"])
                self.assertEqual(self.read_back(path)["EXIF:UserComment"], "Grandma's note")

    def test_instructions_are_not_copied_into_the_comment(self) -> None:
        for extension in _FORMATS:
            with self.subTest(format=extension):
                path = self.image(extension, {"IPTC:SpecialInstructions": "Handle with care"})

                self.run_plugin_call(path, self.REPLY)

                comment = self.read_back(path)["EXIF:UserComment"]
                self.assertNotIn("Handle with care", comment)
                self.assertRegex(comment, r"^\[AI Analysis[^\]]*\]: A porch\.$")

    def test_the_before_snapshot_holds_the_comment_and_location(self) -> None:
        for extension in _FORMATS:
            with self.subTest(format=extension):
                path = self.image(
                    extension,
                    {"EXIF:UserComment": "Grandma's note", "XMP-photoshop:City": "Oakland"},
                )

                record = self.run_plugin_call(path, self.REPLY)

                before = record["original_data"]["file_metadata"]
                self.assertEqual(before["EXIF:UserComment"], "Grandma's note")
                self.assertEqual(before["IPTC:City"], "Oakland")


class TestLocationInTheFile(_RealExiftoolCase):
    """Acceptance cases 6 and 7: written to both blocks, and read before written."""

    def test_an_iptc_write_also_lands_in_xmp(self) -> None:
        for extension in _FORMATS:
            with self.subTest(format=extension):
                path = self.image(extension, {"XMP-photoshop:City": "Oakland"})
                changeset = os.path.join(self.work, f"{extension}-changeset.ndjson")
                record = {"path": path, "proposed_changes": {"set": {"IPTC:City": "San Diego"}}}
                with open(changeset, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(record) + "\n")

                summary = apply_changeset(
                    changeset,
                    ExiftoolConfig(enabled=True, path=self.exiftool),
                    fields=("IPTC:City",),
                )

                self.assertEqual(summary["errors"], [])
                self.assertEqual(summary["tags_written"], 1)
                read = self.read_back(path)
                self.assertEqual(read["IPTC:City"], "San Diego")
                self.assertEqual(read["XMP-photoshop:City"], "San Diego")

    def test_a_curated_location_beats_a_confident_guess(self) -> None:
        reply = {
            "caption": "A street",
            "keywords": ["family"],
            "location_guess": {"country": "United States", "city": "San Diego", "confidence": 0.95},
        }
        for extension in _FORMATS:
            with self.subTest(format=extension):
                path = self.image(extension, {"XMP-photoshop:City": "Oakland"})

                record = self.run_plugin_call(path, reply)

                proposed = record["proposed_changes"]["set"]
                self.assertFalse([tag for tag in proposed if tag.startswith("IPTC:")], proposed)
                self.assertEqual(self.read_back(path)["XMP-photoshop:City"], "Oakland")


class TestDateInTheFile(_RealExiftoolCase):
    """Acceptance cases 9 and 10: Capture Time and Date Created, together."""

    @staticmethod
    def reply(iso: str, import_date: str, pattern: str, confidence: float = 0.95) -> dict:
        """A model answer carrying only a date guess worth writing."""
        return {
            "caption": "A porch",
            "keywords": ["family"],
            "date_guess": {
                "iso": iso,
                "import_date": import_date,
                "pattern": pattern,
                "confidence": confidence,
            },
        }

    def test_a_full_date_lands_in_both_tags_and_iim(self) -> None:
        for extension in _FORMATS:
            with self.subTest(format=extension):
                path = self.image(extension)

                self.run_plugin_call(path, self.reply("1952-06-01", "1952-06-01", "Y!M!D!"))

                read = self.read_back(path)
                self.assertEqual(read["EXIF:DateTimeOriginal"], "1952:06:01 00:00:00")
                self.assertEqual(read["XMP-photoshop:DateCreated"], "1952:06:01")
                self.assertEqual(read["IPTC:DateCreated"], "1952:06:01")

    def test_a_year_or_decade_is_never_written_as_a_full_date_created(self) -> None:
        cases = (("1952", "1952-06-15", "Y!", "1952"), ("1910s", "1915-06-15", "Y~", "1915"))
        for extension in _FORMATS:
            with self.subTest(format=extension):
                for iso, import_date, pattern, expected in cases:
                    with self.subTest(iso=iso):
                        path = self.image(extension)

                        self.run_plugin_call(path, self.reply(iso, import_date, pattern))

                        read = self.read_back(path)
                        self.assertEqual(read["XMP-photoshop:DateCreated"], expected)
                        self.assertNotIn("IPTC:DateCreated", read)

    def test_a_date_only_in_date_created_counts_as_dated(self) -> None:
        for extension in _FORMATS:
            with self.subTest(format=extension):
                path = self.image(extension, {"XMP-photoshop:DateCreated": "1948"})

                record = self.run_plugin_call(path, self.reply("1950", "1950-06-15", "Y~"))

                proposed = record["proposed_changes"]["set"]
                self.assertNotIn("EXIF:DateTimeOriginal", proposed)
                self.assertNotIn("XMP-photoshop:DateCreated", proposed)
                read = self.read_back(path)
                self.assertNotIn("EXIF:DateTimeOriginal", read)
                self.assertEqual(read["XMP-photoshop:DateCreated"], "1948")

    def test_a_changeset_without_date_created_still_writes_it(self) -> None:
        for extension in _FORMATS:
            with self.subTest(format=extension):
                path = self.image(extension)
                changeset = os.path.join(self.work, f"{extension}-date.ndjson")
                record = {
                    "path": path,
                    "proposed_changes": {"set": {"EXIF:DateTimeOriginal": "1952-06-01T10:30:00"}},
                }
                with open(changeset, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(record) + "\n")

                summary = apply_changeset(
                    changeset,
                    ExiftoolConfig(enabled=True, path=self.exiftool),
                    fields=("EXIF:DateTimeOriginal",),
                )

                self.assertEqual(summary["errors"], [])
                read = self.read_back(path)
                self.assertEqual(read["XMP-photoshop:DateCreated"], "1952:06:01 10:30:00")
                self.assertEqual(read["IPTC:DateCreated"], "1952:06:01")
                self.assertTrue(read["IPTC:TimeCreated"].startswith("10:30:00"))


class TestNonAsciiTextSurvivesTheRoundTrip(_RealExiftoolCase):
    """Accents and non-Latin-1 letters are read and written back exactly."""

    def test_a_city_outside_cp1252_does_not_fail_the_run(self) -> None:
        for extension in _FORMATS:
            with self.subTest(format=extension):
                path = self.image(extension, {"XMP-photoshop:City": "Łódź"})

                record = self.run_plugin_call(path, TestUserCommentInTheFile.REPLY)

                self.assertEqual(record["original_data"]["file_metadata"]["IPTC:City"], "Łódź")
                self.assertEqual(self.read_back(path)["XMP-photoshop:City"], "Łódź")

    def test_an_accented_note_is_kept_exactly(self) -> None:
        note = "Grand-mère’s note, Łódź"
        for extension in _FORMATS:
            with self.subTest(format=extension):
                path = self.image(extension, {"EXIF:UserComment": note})

                record = self.run_plugin_call(path, TestUserCommentInTheFile.REPLY)

                self.assertEqual(record["original_data"]["file_metadata"]["EXIF:UserComment"], note)
                self.assertTrue(
                    self.read_back(path)["EXIF:UserComment"].startswith(f"{note}\n\n[AI Analysis")
                )

    def test_a_non_ascii_location_is_written_exactly(self) -> None:
        for extension in _FORMATS:
            with self.subTest(format=extension):
                path = self.image(extension)
                changeset = os.path.join(self.work, f"{extension}-city.ndjson")
                record = {"path": path, "proposed_changes": {"set": {"IPTC:City": "Kraków"}}}
                with open(changeset, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps(record) + "\n")

                summary = apply_changeset(
                    changeset,
                    ExiftoolConfig(enabled=True, path=self.exiftool),
                    fields=("IPTC:City",),
                )

                self.assertEqual(summary["errors"], [])
                self.assertEqual(self.read_back(path)["XMP-photoshop:City"], "Kraków")


class TestALineBreakNeverSplitsAnArgument(_RealExiftoolCase):
    """A keyword carrying a line break stays one keyword and touches nothing else."""

    def test_the_argfile_is_not_used_for_an_argument_with_a_line_break(self) -> None:
        cmd = ["exiftool", "-XMP-dc:Subject+=Łódź\n-XMP-dc:Title=", "photo.jpg"]

        self.assertEqual(utf8_argfile(cmd), (cmd, None))

    def test_a_keyword_line_break_cannot_clear_another_tag(self) -> None:
        for extension in _FORMATS:
            with self.subTest(format=extension):
                path = self.image(extension, {"XMP-dc:Title": "Aunt May's porch"})
                changeset = os.path.join(self.work, f"{extension}-kw.ndjson")
                proposed = {
                    "set": {"XMP-dc:Description": "Café on the corner"},
                    "keywords_add": ["Łódź trip\n-XMP-dc:Title="],
                }
                with open(changeset, "w", encoding="utf-8") as handle:
                    handle.write(json.dumps({"path": path, "proposed_changes": proposed}) + "\n")

                summary = apply_changeset(
                    changeset,
                    ExiftoolConfig(enabled=True, path=self.exiftool),
                    fields=("XMP-dc:Description", "XMP-dc:Subject"),
                )

                self.assertEqual(summary["errors"], [])
                record = json.loads(
                    self.exiftool_run("-j", "-G1", "-XMP-dc:Title", "-XMP-dc:Subject", path)
                )[0]
                self.assertEqual(record["XMP-dc:Title"], "Aunt May's porch")
                self.assertEqual(record["XMP-dc:Subject"], "Łódź trip -XMP-dc:Title=")


class TestAnAccentedFolderIsReadAndWritten(_RealExiftoolCase):
    """A photo under ``Grand-mère Łódź`` is found, read and written."""

    def test_the_plugin_call_reads_and_writes_in_an_accented_folder(self) -> None:
        for extension in _FORMATS:
            with self.subTest(format=extension):
                path = self.image(
                    extension,
                    {"EXIF:UserComment": "Café note", "XMP-photoshop:City": "Kraków"},
                    folder_prefix="Grand-mère Łódź ",
                )
                reply = {
                    **TestUserCommentInTheFile.REPLY,
                    "caption": "Café on the corner\nsecond line",
                    "keywords": ["Łódź trip"],
                }

                record = self.run_plugin_call(path, reply)

                self.assertNotIn("suppressed", record["proposed_changes"])
                self.assertEqual(record["original_data"]["file_metadata"]["IPTC:City"], "Kraków")
                record_back = json.loads(
                    self.exiftool_run("-j", "-G1", "-XMP-dc:Description", "-XMP-dc:Subject", path)
                )[0]
                self.assertEqual(
                    record_back["XMP-dc:Description"], "Café on the corner\nsecond line"
                )
                self.assertIn("Łódź trip", json.dumps(record_back, ensure_ascii=False))
                self.assertTrue(
                    self.read_back(path)["EXIF:UserComment"].startswith("Café note\n\n")
                )


class TestTheBeforeSnapshotShowsTheFilesOwnDateCreated(_RealExiftoolCase):
    """A hand-typed Date Created is shown as itself, not rebuilt from the scan date."""

    def test_date_created_is_read_as_the_file_holds_it(self) -> None:
        path = self.image(
            "jpg",
            {"EXIF:DateTimeOriginal": "2021:04:09 10:00:00", "XMP-photoshop:DateCreated": "1948"},
        )

        record = self.run_plugin_call(path, TestUserCommentInTheFile.REPLY)

        self.assertEqual(
            record["original_data"]["file_metadata"]["XMP-photoshop:DateCreated"], "1948"
        )


if __name__ == "__main__":
    unittest.main()
