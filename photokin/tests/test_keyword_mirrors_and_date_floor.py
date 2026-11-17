"""Keywords reach every tag Lightroom reads, and an undated scan gets its date.

Three rules, each checked first on the pure helpers and then end to end against
the real ExifTool:

- A keyword change made to ``XMP-dc:Subject`` is made to
  ``XMP-lr:HierarchicalSubject`` and ``IPTC:Keywords`` too, without flattening
  a hierarchy; ``-r`` reads all three, and re-proposes a keyword one of them
  lacks so the write repairs it.
- A date guess is written from a 0.4 floor when the file has no date or a date
  far from the guess; below the usual bar it is tagged ``AI Inferred Date``.
- ``DATE: <pattern>`` is added only when a date is written, and a ``DATE:``
  keyword beside ``AI Inferred Date`` is not a person's sign-off.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from typing import Any, ClassVar
from unittest.mock import patch

from photokin import core, utils
from photokin.canonical import build_canonical_patch, iim_holds_keyword
from photokin.changeset import diff_canonical_metadata
from photokin.exiftool.apply import _keyword_delta_args
from photokin.exiftool.config import ExiftoolConfig
from photokin.exiftool.hydrate import hydrate_item_metadata
from photokin.exiftool.manifest import exiftool_records_to_manifest_items, record_keywords
from photokin.merge import merge_record_with_original
from photokin.tests.test_write_every_field import _RealExiftoolCase, _stubbed_model

_KEYWORD_TAGS = ("XMP-dc:Subject", "XMP-lr:HierarchicalSubject", "IPTC:Keywords")


def _guess(confidence: float, import_date: str = "1912-07-01", pattern: str = "Y~") -> dict:
    """A model reply carrying only a date guess and the model's own DATE: keyword."""
    return {
        "keywords": ["family", f"DATE: {pattern}"],
        "date_guess": {
            "iso": import_date[:4],
            "import_date": import_date,
            "confidence": confidence,
            "pattern": pattern,
        },
    }


def _merge(reply: dict, original: dict | None = None) -> dict:
    """Merge ``reply`` against ``original`` under the default config."""
    merged, _report = merge_record_with_original(reply, original or {}, utils.Config())
    return merged


class TestTheDateRule(unittest.TestCase):
    """The table in the issue, row by row."""

    def test_an_undated_file_takes_a_guess_from_the_floor(self) -> None:
        for confidence, inferred in ((0.4, True), (0.59, True), (0.6, False), (0.9, False)):
            with self.subTest(confidence=confidence):
                merged = _merge(_guess(confidence))

                self.assertEqual(merged["dateTimeOriginal"], "1912-07-01")
                self.assertIn("DATE: Y~", merged["keywords"])
                self.assertEqual(
                    utils.AI_INFERRED_DATE_KEYWORD in merged["keywords"], inferred
                )

    def test_below_the_floor_it_stays_a_suggestion(self) -> None:
        merged = _merge(_guess(0.39))

        patch_, patch_meta = build_canonical_patch(merged, utils.Config())

        self.assertNotIn("dateTimeOriginal", merged)
        self.assertEqual(merged["keywords"], ["family"])
        self.assertNotIn("EXIF:DateTimeOriginal", patch_)
        self.assertEqual(patch_meta["EXIF:DateTimeOriginal"]["suggested"], "1912-07-01")

    def test_a_far_date_is_replaced_from_the_floor(self) -> None:
        scan_date = {"date": "2021:04:09 10:00:00"}
        for confidence, inferred in ((0.45, True), (0.7, False)):
            with self.subTest(confidence=confidence):
                merged = _merge(_guess(confidence), scan_date)

                self.assertEqual(merged["dateTimeOriginal"], "1912-07-01")
                self.assertEqual(
                    utils.AI_INFERRED_DATE_KEYWORD in merged["keywords"], inferred
                )

    def test_a_near_date_keeps_the_old_thresholds(self) -> None:
        cases = (
            ("a year guess, however confident", _guess(0.95, "1925-07-01", "Y~"), False),
            ("a precise guess more than 5 years off", _guess(0.85, "1925-03-14", "Y!M!D!"), True),
            ("a precise guess under the bar", _guess(0.75, "1925-03-14", "Y!M!D!"), False),
        )
        for label, reply, written in cases:
            with self.subTest(label):
                merged = _merge(reply, {"date": "1935:01:01 00:00:00"})

                self.assertEqual(merged["dateTimeOriginal"] == reply["date_guess"]["import_date"], written)
                self.assertEqual(any(utils.is_date_keyword(kw) for kw in merged["keywords"]), written)
                self.assertNotIn(utils.AI_INFERRED_DATE_KEYWORD, merged["keywords"])

    def test_a_reviewed_date_is_never_written_over(self) -> None:
        for file_date in ("", "2021:04:09 10:00:00"):
            with self.subTest(file_date=file_date):
                merged = _merge(
                    _guess(0.95), {"date": file_date, "keywords": ["DATE: Y!"]}
                )

                self.assertNotEqual(merged.get("dateTimeOriginal"), "1912-07-01")
                self.assertEqual(merged["keywords"], ["DATE: Y!", "family"])

    def test_an_import_date_that_is_not_a_date_writes_nothing(self) -> None:
        merged = _merge(_guess(0.9, "1910s"))

        self.assertNotIn("dateTimeOriginal", merged)

    def test_the_floor_is_a_setting(self) -> None:
        cfg = utils.Config(date_min_confidence=0.5)

        merged, _report = merge_record_with_original(_guess(0.45), {}, cfg)

        self.assertNotIn("dateTimeOriginal", merged)


class TestAnInferredDateStaysOpen(unittest.TestCase):
    """``AI Inferred Date`` keeps the ``DATE:`` keyword beside it from locking the date."""

    INFERRED: ClassVar[list[str]] = ["Box-Florence", "DATE: Y~", utils.AI_INFERRED_DATE_KEYWORD]

    def test_only_a_date_keyword_without_the_tag_is_a_sign_off(self) -> None:
        self.assertTrue(utils.has_reviewed_date_keyword(["DATE: Y~"]))
        self.assertFalse(utils.has_reviewed_date_keyword(self.INFERRED))
        self.assertFalse(utils.has_reviewed_date_keyword(["ai inferred date", "DATE: Y~"]))
        self.assertFalse(utils.has_reviewed_date_keyword(["family"]))

    def test_a_later_date_replaces_both_keywords(self) -> None:
        merged = _merge(
            _guess(0.9, "1952-06-15", "Y!M!D!"),
            {"date": "1912:07:01 00:00:00", "keywords": self.INFERRED},
        )

        self.assertEqual(merged["dateTimeOriginal"], "1952-06-15")
        self.assertEqual(merged["keywords"], ["Box-Florence", "family", "DATE: Y!M!D!"])

    def test_a_declined_guess_leaves_the_earlier_keywords(self) -> None:
        merged = _merge(_guess(0.3), {"date": "1912:07:01", "keywords": self.INFERRED})

        self.assertEqual(merged["keywords"], [*self.INFERRED, "family"])


class TestReadingEveryKeywordTag(unittest.TestCase):
    """``record_keywords`` and the two readers built on it."""

    def test_a_hierarchy_counts_as_present_but_never_becomes_flat(self) -> None:
        keywords, unmirrored, hierarchy = record_keywords(
            {
                "XMP-dc:Subject": ["family", "Ruth"],
                "XMP-lr:HierarchicalSubject": ["family", "People|Ruth", "Places|Ohio|Akron"],
                "IPTC:Keywords": ["family", "Ruth", "scan"],
            }
        )

        self.assertEqual(keywords, ["family", "Ruth", "scan"])
        self.assertEqual(unmirrored, ["scan"])
        self.assertEqual(hierarchy, ["People|Ruth", "Places|Ohio|Akron"])

    def test_a_keyword_iim_cannot_hold_is_not_missing_from_it(self) -> None:
        long_keyword = "x" * 65
        unmirrored = record_keywords(
            {
                "XMP-dc:Subject": ["北京", long_keyword, "Grand-mère"],
                "XMP-lr:HierarchicalSubject": ["北京", long_keyword, "Grand-mère"],
            }
        ).unmirrored

        self.assertEqual(unmirrored, ["Grand-mère"])
        self.assertFalse(iim_holds_keyword("北京"))
        self.assertFalse(iim_holds_keyword(long_keyword))
        self.assertTrue(iim_holds_keyword("Grand-mère"))

    def test_no_keyword_anywhere_is_none(self) -> None:
        self.assertEqual(record_keywords({"XMP-dc:Title": "x"}), (None, [], []))

    def test_the_manifest_reader_unions_and_marks(self) -> None:
        record = {
            "SourceFile": "a.jpg",
            "XMP-dc:Subject": ["family"],
            "IPTC:Keywords": ["family", "ohio"],
        }

        everything = exiftool_records_to_manifest_items([record], [])[0]["metadata"]
        subject_only = exiftool_records_to_manifest_items([record], ["XMP:Subject"])[0]["metadata"]

        self.assertEqual(everything["keywords"], ["family", "ohio"])
        self.assertEqual(everything[utils.UNMIRRORED_KEYWORDS_KEY], ["family", "ohio"])
        # Only one tag was asked for, so nothing is known to be missing.
        self.assertNotIn(utils.UNMIRRORED_KEYWORDS_KEY, subject_only)


def _hydrate(item: dict, record: dict) -> dict:
    """Hydrate ``item`` from a stubbed ExifTool ``record`` and return its metadata."""

    def _records(**_kwargs: Any) -> list[dict]:
        return [{"SourceFile": item["path"], **record}]

    with (
        patch("photokin.exiftool.hydrate.resolve_exiftool_path", return_value="exiftool"),
        patch("photokin.exiftool.manifest.run_exiftool_json", _records),
    ):
        hydrate_item_metadata([item], ExiftoolConfig())
    return item["metadata"]


class TestHydratingKeywords(unittest.TestCase):
    """``-r`` fills keywords from every tag, and rescues a date marker with its tag."""

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.path = os.path.join(scratch.name, "florence-032.jpg")
        open(self.path, "wb").close()

    def test_keywords_held_only_in_the_mirrors_are_read(self) -> None:
        meta = _hydrate(
            {"path": self.path},
            {
                "XMP-dc:Subject": ["family"],
                "XMP-lr:HierarchicalSubject": ["family", "Box-Florence"],
                "IPTC:Keywords": ["family", "Box-Florence"],
            },
        )

        self.assertEqual(meta["keywords"], ["family", "Box-Florence"])
        self.assertEqual(meta[utils.UNMIRRORED_KEYWORDS_KEY], ["Box-Florence"])

    def test_an_inferred_date_marker_is_rescued_with_its_tag(self) -> None:
        file_keywords = ["Box-Florence", "DATE: Y~", utils.AI_INFERRED_DATE_KEYWORD]
        meta = _hydrate(
            {"path": self.path, "metadata": {"keywords": ["from the catalog"]}},
            {"XMP-dc:Subject": file_keywords, "XMP-lr:HierarchicalSubject": file_keywords},
        )

        self.assertEqual(
            meta["keywords"],
            ["from the catalog", "DATE: Y~", utils.AI_INFERRED_DATE_KEYWORD],
        )
        self.assertFalse(utils.has_reviewed_date_keyword(meta["keywords"]))


class TestTheKeywordWrite(unittest.TestCase):
    """The diff re-proposes what a mirror lacks; the applier writes every tag."""

    def test_an_unmirrored_keyword_is_added_again(self) -> None:
        before = {"XMP-dc:Subject": ["family", "Box-Florence"]}
        after = {"XMP-dc:Subject": ["family", "Box-Florence", "beach"]}

        proposed = diff_canonical_metadata(before, after, ["Box-Florence"])
        plain = diff_canonical_metadata(before, after)

        self.assertEqual(proposed["keywords_add"], ["Box-Florence", "beach"])
        self.assertEqual(plain["keywords_add"], ["beach"])

    def test_each_change_reaches_every_tag_iim_can_hold(self) -> None:
        args = _keyword_delta_args(
            {"keywords_add": ["beach", "北京"], "keywords_remove": ["old"]}, "a.jpg", []
        )

        self.assertEqual(
            args,
            [
                *(f"-{tag}-=old" for tag in _KEYWORD_TAGS),
                *(op for tag in _KEYWORD_TAGS for op in (f"-{tag}-=beach", f"-{tag}+=beach")),
                *(op for tag in _KEYWORD_TAGS[:2] for op in (f"-{tag}-=北京", f"-{tag}+=北京")),
            ],
        )


class TestAuditFindings(unittest.TestCase):
    """What the first review of this change found, each pinned where it was found."""

    def test_a_backs_inferred_date_does_not_reopen_the_fronts_sign_off(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            front = os.path.join(work, "florence-032.jpg")
            back = os.path.join(work, "florence-032-back.jpg")
            for path in (front, back):
                open(path, "wb").close()
            items = [
                {
                    "path": front,
                    "metadata": {"keywords": ["family", "DATE: Y~"], "dateTimeOriginal": "1940:07:01"},
                },
                {
                    "path": back,
                    "metadata": {
                        "keywords": ["family", "DATE: Y~", utils.AI_INFERRED_DATE_KEYWORD],
                        "dateTimeOriginal": "1915:07:01",
                    },
                },
            ]
            with _stubbed_model(_guess(0.45, "1915-07-01")):
                results = core.process_manifest_stream(
                    manifest={"items": items}, cfg=utils.Config()
                )["results"]

        self.assertEqual(results[front]["dateTimeOriginal"], "1940:07:01")
        self.assertNotIn(utils.AI_INFERRED_DATE_KEYWORD, results[front]["keywords"])
        self.assertIn(utils.AI_INFERRED_DATE_KEYWORD, results[back]["keywords"])

    def test_a_fronts_sign_off_still_locks_its_bare_back(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            front = os.path.join(work, "florence-037.jpg")
            back = os.path.join(work, "florence-037-back.jpg")
            for path in (front, back):
                open(path, "wb").close()
            items = [
                {
                    "path": front,
                    "metadata": {"keywords": ["family", "DATE: Y!"], "dateTimeOriginal": "1940:07:01"},
                },
                {"path": back},
            ]
            with _stubbed_model(_guess(0.45, "1915-07-01")):
                results = core.process_manifest_stream(
                    manifest={"items": items}, cfg=utils.Config()
                )["results"]

        self.assertEqual(results[back]["dateTimeOriginal"], "1940:07:01")
        self.assertIn("DATE: Y!", results[back]["keywords"])
        self.assertNotIn(utils.AI_INFERRED_DATE_KEYWORD, results[back]["keywords"])

    def run_print(self, front_meta: dict, back_meta: dict, reply: dict) -> dict[str, dict]:
        """Run a front and back with their own metadata; return results by basename."""
        with tempfile.TemporaryDirectory() as work:
            items = []
            for name, meta in (("florence-041.jpg", front_meta), ("florence-041-back.jpg", back_meta)):
                path = os.path.join(work, name)
                open(path, "wb").close()
                items.append({"path": path, "metadata": meta})
            with _stubbed_model(reply):
                results = core.process_manifest_stream(
                    manifest={"items": items}, cfg=utils.Config()
                )["results"]
        return {os.path.basename(path): record for path, record in results.items()}

    def test_a_back_with_its_own_inferred_date_keeps_its_own_markers(self) -> None:
        results = self.run_print(
            {"keywords": ["family", "DATE: Y!"], "dateTimeOriginal": "1940:07:01"},
            {
                "keywords": ["family", "DATE: Y~", utils.AI_INFERRED_DATE_KEYWORD],
                "dateTimeOriginal": "1915:07:01",
            },
            _guess(0.45, "1915-07-01"),
        )

        self.assertEqual(
            [kw for kw in results["florence-041-back.jpg"]["keywords"] if utils.is_date_marker(kw)],
            ["DATE: Y~", utils.AI_INFERRED_DATE_KEYWORD],
        )

    def test_a_front_signed_off_in_a_single_string_locks_its_back(self) -> None:
        results = self.run_print(
            {"keywords": "DATE: Y!", "dateTimeOriginal": "1940:07:01"}, {}, _guess(0.45, "1915-07-01")
        )

        self.assertEqual(results["florence-041-back.jpg"]["dateTimeOriginal"], "1940:07:01")
        self.assertIn("DATE: Y!", results["florence-041-back.jpg"]["keywords"])

    def test_a_hierarchy_level_is_never_added_to_the_hierarchy_flat(self) -> None:
        before = {"XMP-dc:Subject": ["family"]}
        after = {"XMP-dc:Subject": ["family", "Ruth", "beach"]}

        proposed = diff_canonical_metadata(before, after, None, ["People|Ruth"])
        args = _keyword_delta_args(proposed, "a.jpg", [])

        self.assertEqual(proposed["keywords_in_hierarchy"], ["Ruth"])
        self.assertNotIn("-XMP-lr:HierarchicalSubject+=Ruth", args)
        self.assertIn("-XMP-dc:Subject+=Ruth", args)
        self.assertIn("-IPTC:Keywords+=Ruth", args)
        self.assertIn("-XMP-lr:HierarchicalSubject+=beach", args)

    def test_iim_fits_a_keyword_in_64_bytes_of_utf8_too(self) -> None:
        self.assertFalse(iim_holds_keyword("é" * 40))
        self.assertTrue(iim_holds_keyword("é" * 32))


class TestTheIssuesBatchInTheFile(_RealExiftoolCase):
    """The first 0.7.0 run, replayed: an undated scan Lightroom had keyworded."""

    LIGHTROOM = ("Box-Florence", "RChurch Reviewed 20221012")

    def seeded(self, hierarchy: tuple[str, ...]) -> str:
        """A JPEG whose keyword tags hold what a Lightroom metadata save left."""
        path = self.image("jpg")
        args = [f"-XMP-dc:Subject={kw}" for kw in self.LIGHTROOM]
        args += [f"-IPTC:Keywords={kw}" for kw in self.LIGHTROOM]
        args += [f"-XMP-lr:HierarchicalSubject={kw}" for kw in hierarchy]
        self.exiftool_run("-overwrite_original", *args, path)
        return path

    def keyword_tags(self, path: str) -> dict[str, list[str]]:
        """The file's three keyword tags, each as a list."""
        record = json.loads(
            self.exiftool_run("-j", "-G1", *(f"-{tag}" for tag in _KEYWORD_TAGS), path)
        )[0]
        return {
            tag: [record[tag]] if isinstance(record.get(tag), str) else record.get(tag, [])
            for tag in _KEYWORD_TAGS
        }

    def test_a_low_confidence_date_is_written_and_every_tag_agrees(self) -> None:
        path = self.seeded((*self.LIGHTROOM, "Places|Florence"))
        reply = {**_guess(0.45, "1915-07-01"), "keywords": ["family", "北京", "DATE: Y~"]}

        self.run_plugin_call(path, reply)

        tags = self.keyword_tags(path)
        written = [*self.LIGHTROOM, "family", "北京", "DATE: Y~", utils.AI_INFERRED_DATE_KEYWORD]
        self.assertEqual(sorted(tags["XMP-dc:Subject"]), sorted(written))
        self.assertEqual(
            sorted(tags["XMP-lr:HierarchicalSubject"]), sorted([*written, "Places|Florence"])
        )
        self.assertEqual(sorted(tags["IPTC:Keywords"]), sorted(set(written) - {"北京"}))
        self.assertEqual(self.read_back(path)["XMP-photoshop:DateCreated"], "1915")

    def test_a_second_run_proposes_no_keyword_change(self) -> None:
        path = self.seeded(self.LIGHTROOM)
        reply = {**_guess(0.45, "1915-07-01"), "keywords": ["family", "北京", "DATE: Y~"]}
        self.run_plugin_call(path, reply)

        again = self.run_plugin_call(path, reply)["proposed_changes"]

        self.assertEqual((again["keywords_add"], again["keywords_remove"]), ([], []))

    def test_a_hierarchy_leaf_missing_from_iim_gets_no_flat_copy(self) -> None:
        path = self.image("jpg")
        args = [f"-XMP-dc:Subject={kw}" for kw in ("Ruth", "People", "family")]
        args += [f"-XMP-lr:HierarchicalSubject={kw}" for kw in ("People|Ruth", "family")]
        self.exiftool_run("-overwrite_original", *args, path)

        self.run_plugin_call(path, {"keywords": ["family"]})

        tags = self.keyword_tags(path)
        self.assertEqual(sorted(tags["XMP-lr:HierarchicalSubject"]), ["People|Ruth", "family"])
        self.assertEqual(sorted(tags["IPTC:Keywords"]), ["People", "Ruth", "family"])

    def test_a_stale_mirror_is_repaired(self) -> None:
        path = self.seeded(self.LIGHTROOM)
        self.exiftool_run("-overwrite_original", "-XMP-dc:Subject+=beach", path)

        self.run_plugin_call(path, {"keywords": ["family"]})

        tags = self.keyword_tags(path)
        for tag in _KEYWORD_TAGS:
            with self.subTest(tag=tag):
                self.assertEqual(
                    sorted(tags[tag]), sorted([*self.LIGHTROOM, "beach", "family"])
                )


if __name__ == "__main__":
    unittest.main()
