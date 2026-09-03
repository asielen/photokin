"""Regression tests for the manifest grouping path in ``process_manifest_stream``.

Each case here covers a way the grouping stage could hand the model the wrong
file, or hand it nothing, while the run still exits clean. They assert on the
call the model layer actually received, since that -- not the returned record --
is what the caller pays for and what the Lightroom plug-in cannot inspect.

The provider is mocked out entirely and nothing here touches the network or
writes to the repository tree; the one case that needs a manifest on disk uses a
``TemporaryDirectory``.
"""
import itertools
import json
import logging
import os
import re
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from photokin import core, utils

_COMPLETION_PREFIX = "Batch completed"

#: Reads the completion line's own count back out of the rendered message rather
#: than off ``record.args``. The wording is what a reader acts on, so a case that
#: stopped matching it has stopped testing what it claims to.
_UNSENT_COUNT_RE = re.compile(r"(\d+) file\(s\) recorded without being sent to the model")


class _RecordingAnalyzers:
    """Stand-in for the three model entry points, recording their arguments."""

    def __init__(self, keywords: list[str] | None = None) -> None:
        self.calls: list[tuple] = []
        #: The ``original_meta`` of each call, in call order and parallel to
        #: :attr:`calls`. Kept in a list of its own rather than folded into a
        #: call tuple because 79 call sites assert on that tuple's shape, and
        #: because only the permutation sweep needs it: forwarded metadata is
        #: the one part of a call that is computed from every item of a group
        #: rather than from one file, so it is where an order-dependent answer
        #: hides once more than one item in a group carries metadata.
        self.metas: list[dict | None] = []
        #: Returned as the analysis keywords. A ``PC*`` entry here makes the
        #: version the analysis was filed under visible in the emitted records,
        #: since ``PC*`` codes are the one keyword class scoped per variant.
        self.keywords: list[str] = list(keywords or [])

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
        self.calls.append(("photo", front_path, back_path))
        self.metas.append(original_meta)
        return {"result": {front_path: {"caption": "c", "keywords": list(self.keywords)}}}

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
        self.calls.append(("front_back", tuple(fronts), tuple(backs)))
        self.metas.append(original_meta)
        key = (fronts + backs + ["?"])[0]
        return {"result": {key: {"caption": "c", "keywords": list(self.keywords)}}}

    def parts(
        self,
        parts: list[tuple[str, list[str]]],
        config: utils.Config | None = None,
        *,
        original_meta: dict | None = None,
        write_sidecar: bool = False,
    ) -> dict:
        """Record an ``analyze_group_parts`` call."""
        self.calls.append(("parts", tuple((label, tuple(paths)) for label, paths in parts)))
        self.metas.append(original_meta)
        flat = [p for _, paths in parts for p in paths]
        return {"result": {(flat or ["?"])[0]: {"caption": "c", "keywords": list(self.keywords)}}}


@contextmanager
def _recording(
    keywords: list[str] | None = None, *, real_patch_builder: bool = False
) -> Iterator[_RecordingAnalyzers]:
    """Patch the three analyzers with recorders for the duration of the block.

    Args:
        keywords: Keywords the stand-in analyses return.
        real_patch_builder: Leave ``build_canonical_patch`` alone. It is stubbed
            by default because these cases assert on the model call, not on the
            patch -- but the changeset diffs the record against the patch, so a
            stubbed empty patch makes every keyword the file already had look
            like a proposed deletion.
    """
    rec = _RecordingAnalyzers(keywords)
    with patch("photokin.core.analyze_photo", rec.photo), patch(
        "photokin.core.analyze_group_front_back", rec.front_back
    ), patch("photokin.core.analyze_group_parts", rec.parts):
        if real_patch_builder:
            yield rec
        else:
            with patch("photokin.core.build_canonical_patch", return_value=({}, {})):
                yield rec


def _labelled_payload(calls: list[tuple]) -> list[tuple[str, str]]:
    """Flatten recorded calls into the ``(label, path)`` pairs the model received.

    Args:
        calls: The recorder's call log for one run.

    Returns:
        Every image the run sent, paired with the part label it was sent under.
        ``analyze_photo``'s two positional arguments are labels in their own
        right -- it uploads them separately and tells the model which is which.
    """
    payload: list[tuple[str, str]] = []
    for call in calls:
        if call[0] == "photo":
            payload.extend(
                (label, path)
                for label, path in (("front", call[1]), ("back", call[2]))
                if path
            )
        elif call[0] == "front_back":
            payload.extend(("Front", path) for path in call[1])
            payload.extend(("Back", path) for path in call[2])
        else:
            payload.extend(
                (label, path) for label, paths in call[1] for path in paths
            )
    return payload


def _completion_record(log_records: list[logging.LogRecord]) -> logging.LogRecord:
    """Return the run's single "Batch completed" summary record.

    The level is deliberately not filtered on: a run that placed every file
    summarizes itself at INFO and one that did not at WARNING, so which of the
    two it chose is a behavior to assert rather than a detail to search past.

    Args:
        log_records: Every record the run logged.

    Returns:
        The completion record.

    Raises:
        AssertionError: If the run logged anything other than one such line.
    """
    found = [r for r in log_records if r.getMessage().startswith(_COMPLETION_PREFIX)]
    if len(found) != 1:
        raise AssertionError(
            f"expected exactly one completion line, got {[r.getMessage() for r in found]}"
        )
    return found[0]


def _unsent_count(record: logging.LogRecord) -> int:
    """Return the number of unsent files the completion line reports.

    Args:
        record: The completion record.

    Returns:
        The reported count.

    Raises:
        AssertionError: If the line no longer carries that clause, since a case
            asserting on a count it cannot find would silently stop asserting.
    """
    match = _UNSENT_COUNT_RE.search(record.getMessage())
    if match is None:
        raise AssertionError(f"the completion line changed wording: {record.getMessage()}")
    return int(match.group(1))


def _p(path: str) -> str:
    """Normalize a fixture path the same way the manifest loader does.

    ``utils.normalize_path`` is annotated ``str | None`` because it passes
    ``None`` straight through; this helper only ever hands it a real path.
    """
    return cast(str, utils.normalize_path(path))


class ManifestGroupingTestCase(unittest.TestCase):
    """Shared runner for manifest grouping cases."""

    #: The signatures compared below run to a few kilobytes, and a truncated
    #: diff would say a permutation differed without saying where.
    maxDiff = None

    #: The ``original_meta`` of every model call the last run made, in call
    #: order. Stashed here rather than returned so the three ``run_*`` helpers
    #: keep the tuple shape their 79 call sites unpack; read it only where the
    #: forwarded metadata is the thing under test. Declared without a default so
    #: reading it before a run raises rather than quietly answering ``[]``.
    last_metas: list[dict | None]

    def run_manifest_records(
        self,
        manifest: dict | str,
        *,
        group_by: str = utils.GROUP_BY_OBJECT,
        model_keywords: list[str] | None = None,
        changeset_writer=None,
    ) -> tuple[list[tuple], list[dict], list[logging.LogRecord]]:
        """Process one manifest and return its calls, records and log records.

        Captures from INFO, so the completion line is in the result whichever
        level it chose -- it drops to INFO exactly when every listed file reached
        the payload, which makes the level itself worth asserting. Every run logs
        that line, so the capture can never come back empty.

        Args:
            manifest: A manifest dict, or a path to one on disk.
            group_by: Grouping granularity, one of ``utils.GROUP_BY_VALUES``.
            model_keywords: Keywords the mocked analysis returns.
            changeset_writer: Receives each changeset NDJSON line, or ``None``
                to emit no changeset. The run id is generated, since nothing
                here asserts on it. Supplying one also keeps the real
                ``build_canonical_patch``, which the changeset diffs against.

        Returns:
            A ``(calls, records, log_records)`` triple.
        """
        cfg = utils.Config(dry_run=True, group_by=group_by)
        lines: list[str] = []
        with (
            self.assertLogs("photokin.core", level="INFO") as logs,
            _recording(
                model_keywords, real_patch_builder=changeset_writer is not None
            ) as rec,
        ):
            core.process_manifest_stream(
                manifest=manifest,
                cfg=cfg,
                ndjson_writer=lines.append,
                changeset_writer=changeset_writer,
            )
        self.last_metas = rec.metas
        return rec.calls, [json.loads(line) for line in lines], logs.records

    def run_manifest_source(
        self,
        manifest: dict | str,
        *,
        group_by: str = utils.GROUP_BY_OBJECT,
        model_keywords: list[str] | None = None,
        changeset_writer=None,
    ) -> tuple[list[tuple], list[dict], list[str]]:
        """Process one manifest and return its model calls, records and warnings.

        Args:
            manifest: A manifest dict, or a path to one on disk.
            group_by: Grouping granularity, one of ``utils.GROUP_BY_VALUES``.
            model_keywords: Keywords the mocked analysis returns.
            changeset_writer: Receives each changeset NDJSON line, or ``None``
                to emit no changeset. The run id is generated, since nothing
                here asserts on it. Supplying one also keeps the real
                ``build_canonical_patch``, which the changeset diffs against.

        Returns:
            A ``(calls, records, warnings)`` triple, the warnings rendered the
            way ``assertLogs`` renders them.
        """
        calls, records, log_records = self.run_manifest_records(
            manifest,
            group_by=group_by,
            model_keywords=model_keywords,
            changeset_writer=changeset_writer,
        )
        warnings = [
            f"{r.levelname}:{r.name}:{r.getMessage()}"
            for r in log_records
            if r.levelno >= logging.WARNING
        ]
        return calls, records, warnings

    def run_manifest(
        self,
        items: list[dict],
        *,
        group_by: str = utils.GROUP_BY_OBJECT,
        model_keywords: list[str] | None = None,
        changeset_writer=None,
    ) -> tuple[list[tuple], list[dict], list[str]]:
        """Process an in-memory ``items`` array. See ``run_manifest_source``."""
        return self.run_manifest_source(
            {"items": items},
            group_by=group_by,
            model_keywords=model_keywords,
            changeset_writer=changeset_writer,
        )

    def run_items_records(
        self,
        items: list[dict],
        *,
        group_by: str = utils.GROUP_BY_OBJECT,
    ) -> tuple[list[tuple], list[dict], list[logging.LogRecord]]:
        """Process an in-memory ``items`` array. See ``run_manifest_records``."""
        return self.run_manifest_records({"items": items}, group_by=group_by)

    def assert_no_path_sent_under_two_labels(self, calls: list[tuple]) -> None:
        """Assert no single file was handed to the model as two different parts.

        Args:
            calls: The recorder's call log for one run.
        """
        labels_by_path: dict[str, set[str]] = {}
        for label, path in _labelled_payload(calls):
            labels_by_path.setdefault(path, set()).add(label)
        doubled = {p: sorted(ls) for p, ls in labels_by_path.items() if len(ls) > 1}
        self.assertEqual(
            doubled,
            {},
            "the same file was sent to the model under two labels. Every part is "
            "uploaded, billed and described separately, so this pays twice to "
            f"tell the model an image is a side it is not: {doubled}",
        )

    def assert_every_file_sent_or_disclosed(
        self, items: list[dict], calls: list[tuple], warnings: list[str]
    ) -> None:
        """Assert each listed file either reached the model or was named in a warning.

        Meaningful for a group payload, where the payload is the whole slot
        map; the pair path carries one front and, at most, its own back.

        Args:
            items: The manifest's ``items`` array.
            calls: The recorder's call log for one run.
            warnings: The warnings the run emitted.
        """
        sent = {path for _label, path in _labelled_payload(calls)}
        for item in items:
            path = _p(item["path"])
            if path in sent:
                continue
            name = os.path.basename(path)
            self.assertTrue(
                any(name in warning for warning in warnings),
                f"{path} was listed in the manifest, was not sent to the model, "
                f"and nothing said so: {warnings}",
            )

    def assert_one_group(self, records: list[dict], paths: list[str]) -> dict:
        """Assert every record names the same group and return its variant map.

        Args:
            records: The NDJSON records emitted by a run.
            paths: The normalized paths the single group is expected to hold,
                in the order the manifest listed them.

        Returns:
            The group's ``all_variant_files`` map.
        """
        self.assertEqual(sorted(rec["path"] for rec in records), sorted(paths))
        maps = [rec["result"]["all_variant_files"] for rec in records]
        for variant_map in maps:
            self.assertEqual(variant_map["all"], paths)
        return maps[0]


class TestCropSlotOccupancy(ManifestGroupingTestCase):
    """A crop yields its parent's slot, but only the slot it actually contends for."""

    def test_crop_never_displaces_its_parent_in_either_order(self):
        for label, items in (
            ("scan first", [{"path": "s/b025.jpg"}, {"path": "s/b025-crop.jpg"}]),
            ("crop first", [{"path": "s/b025-crop.jpg"}, {"path": "s/b025.jpg"}]),
        ):
            with self.subTest(label):
                calls, _records, warnings = self.run_manifest(items)
                self.assertEqual(
                    calls,
                    [("photo", _p("s/b025.jpg"), None)],
                    "the crop took the real scan's place, and which file the model "
                    f"sees depended on which the manifest listed first ({label})",
                )
                self.assertTrue(
                    any("recorded but not analyzed" in w for w in warnings),
                    f"the dropped crop was not disclosed: {warnings}",
                )

    def test_a_crop_with_no_parent_is_analyzed_and_disclosed_rather_than_promoted(self):
        # Per the naming-conventions note in README.md: with nothing uncropped to
        # stand for the side, the crop is analyzed in its place "and says so". So
        # the contract is neither a raise nor a silent promotion to a primary
        # scan -- it is a record, a warning, and an entry in the crop map.
        calls, records, warnings = self.run_manifest([{"path": "s/lone-crop.jpg"}])
        self.assertEqual(calls, [("photo", _p("s/lone-crop.jpg"), None)])
        self.assertEqual([rec["path"] for rec in records], [_p("s/lone-crop.jpg")])
        self.assertEqual([rec["status"] for rec in records], ["ok"])
        variant_map = records[0]["result"]["all_variant_files"]
        self.assertEqual(variant_map["crops"], {":none": [_p("s/lone-crop.jpg")]})
        self.assertTrue(
            any("no uncropped original" in w and "lone-crop.jpg" in w for w in warnings),
            f"an orphan crop stood in for the object without saying so: {warnings}",
        )

    def test_cropped_front_survives_when_the_group_also_holds_an_uncropped_back(self):
        # The crop is the only front-side file. Testing crop-ness across the
        # whole group instead of per slot would drop it and send the back twice.
        items = [{"path": "s/a-crop.jpg"}, {"path": "s/a-back.jpg"}]
        calls, _records, warnings = self.run_manifest(items)
        self.assertEqual(calls, [("photo", _p("s/a-crop.jpg"), _p("s/a-back.jpg"))])
        self.assertTrue(any("no uncropped original" in w for w in warnings))

    def test_cropped_front_survives_under_pair_too(self):
        # ``pair`` cannot separate a crop from its parent, and it is not a rule
        # anyone had to write: ``parse_media_filename`` strips ``-crop`` before
        # it reads the part suffix or the variant letter, so a crop always
        # carries its parent's base id and version.
        items = [{"path": "s/a-crop.jpg"}, {"path": "s/a-back.jpg"}]
        calls, _records, _warnings = self.run_manifest(
            items, group_by=utils.GROUP_BY_PAIR
        )
        self.assertEqual(calls, [("photo", _p("s/a-crop.jpg"), _p("s/a-back.jpg"))])

    def test_crop_ness_is_tested_per_slot_even_when_the_group_is_full_of_originals(self):
        # "Does this group contain anything uncropped" answers a resounding yes
        # here -- an uncropped back and a whole uncropped second variant -- and
        # still says nothing about whether variant a has a front to send. The
        # test is whether anything uncropped claims the same (version, part).
        items = [
            {"path": "s/a2-crop.jpg"},
            {"path": "s/a2-back.jpg"},
            {"path": "s/a2b.jpg"},
            {"path": "s/a2b-back.jpg"},
        ]
        for group_by in utils.GROUP_BY_VALUES:
            with self.subTest(group_by=group_by):
                calls, _records, _warnings = self.run_manifest(items, group_by=group_by)
                self.assertIn(
                    _p("s/a2-crop.jpg"),
                    [path for _label, path in _labelled_payload(calls)],
                    "the unversioned variant's only front-side file was dropped "
                    "because some other slot in the group held an original",
                )
                self.assert_no_path_sent_under_two_labels(calls)

    def test_a_crop_still_loses_to_the_original_in_its_own_slot(self):
        # The other half of the per-slot rule: an uncropped back in the group
        # must not save a crop whose own parent is right there beside it.
        items = [
            {"path": "s/a3.jpg"},
            {"path": "s/a3-crop.jpg"},
            {"path": "s/a3-back.jpg"},
        ]
        calls, _records, warnings = self.run_manifest(items)
        self.assertEqual(calls, [("photo", _p("s/a3.jpg"), _p("s/a3-back.jpg"))])
        self.assertTrue(
            any("recorded but not analyzed" in w and "a3-crop.jpg" in w for w in warnings)
        )

    def test_a_back_that_only_exists_as_a_crop_is_still_sent(self):
        items = [{"path": "s/b025.jpg"}, {"path": "s/b025-back-crop.jpg"}]
        calls, _records, _warnings = self.run_manifest(items)
        self.assertEqual(
            calls, [("photo", _p("s/b025.jpg"), _p("s/b025-back-crop.jpg"))]
        )

    def test_the_orphan_crop_warning_names_every_crop_that_was_analyzed(self):
        # Two crops, two variants, no original. Each is the sole claimant of its
        # own slot, so each stands in for the object at its own variant letter,
        # and with the group travelling whole both are analyzed and both say so.
        # Before C1 only the first was sent and the second was "recorded but not
        # analyzed" -- a scan paid for by the user and never looked at.
        items = [{"path": "s/a025-crop.jpg"}, {"path": "s/a025b-crop.jpg"}]
        calls, _records, warnings = self.run_manifest(items)
        self.assertEqual(
            calls,
            [("front_back", (_p("s/a025b-crop.jpg"), _p("s/a025-crop.jpg")), ())],
        )
        analyzed = [w for w in warnings if "no uncropped original" in w]
        self.assertEqual(len(analyzed), 2, warnings)
        self.assertEqual(
            sorted("a025b-crop.jpg" in w for w in analyzed), [False, True]
        )
        self.assertEqual(
            [w for w in warnings if "recorded but not analyzed" in w],
            [],
            "a crop the payload actually carried was reported as unanalyzed",
        )

    def test_preferred_crop_left_out_of_the_group_payload_is_disclosed(self):
        # The payload is built from the slot map, which a crop never wins from
        # its own original, so a preferred crop is recorded rather than analyzed
        # and has to be named as such. README.md carries the carve-out.
        items = [{"path": "s/h1.jpg"}, {"path": "s/h1-crop.jpg", "preferred": True}]
        # ``object`` and ``pair`` only: under ``none`` the crop is its own group,
        # so there is no original beside it to lose the slot to and it is
        # analyzed as an object in its own right. That is the mode's documented
        # price, not a hole in this rule.
        for group_by in (utils.GROUP_BY_OBJECT, utils.GROUP_BY_PAIR):
            with self.subTest(group_by=group_by):
                calls, _records, warnings = self.run_manifest(items, group_by=group_by)
                self.assertEqual(
                    [path for _label, path in _labelled_payload(calls)],
                    [_p("s/h1.jpg")],
                    "a preferred crop must not be analyzed while its own "
                    "uncropped original is listed, at either granularity",
                )
                self.assertTrue(
                    any(
                        "recorded but not analyzed" in w and "h1-crop.jpg" in w
                        for w in warnings
                    ),
                    f"the preferred crop was dropped silently: {warnings}",
                )

    def test_a_preferred_crop_does_not_take_the_group_down_with_it(self):
        # The primary is the file named as analyzed. Letting a crop that missed
        # the payload hold that name asks the group result for a key no part of
        # the payload produced, and the whole group fails.
        items = [
            {"path": "s/h3.jpg"},
            {"path": "s/h3-back.jpg"},
            {"path": "s/h3-crop.jpg", "preferred": True},
        ]
        calls, records, _warnings = self.run_manifest(items)
        self.assertEqual(calls, [("photo", _p("s/h3.jpg"), _p("s/h3-back.jpg"))])
        self.assertEqual(
            {rec["status"] for rec in records},
            {"ok"},
            "a preferred crop took every file in its group down with it: "
            f"{[rec.get('error') for rec in records if rec['status'] != 'ok']}",
        )

    def test_preferred_takes_a_slot_it_is_allowed_to_take(self):
        # ``preferred`` losing a slot is a crop rule, not a general one: between
        # two ordinary files in one slot the explicit choice wins, and the
        # collision warning names the file that was actually analyzed.
        items = [
            {"path": "s/pf1.jpg", "group": "pf"},
            {"path": "s/pf2.jpg", "group": "pf", "preferred": True},
        ]
        # Not under ``none``: an explicit ``group`` override has no effect on a
        # key that is the file's own path, so the two never contest a slot.
        for group_by in (utils.GROUP_BY_OBJECT, utils.GROUP_BY_PAIR):
            with self.subTest(group_by=group_by):
                calls, _records, warnings = self.run_manifest(items, group_by=group_by)
                self.assertEqual(
                    [path for _label, path in _labelled_payload(calls)],
                    [_p("s/pf2.jpg")],
                )
                collisions = [w for w in warnings if "claim the same" in w]
                self.assertEqual(len(collisions), 1, warnings)
                self.assertIn(f"analyzing {_p('s/pf2.jpg')}", collisions[0])

    def test_a_crop_is_filed_under_the_slot_label_its_variant_ended_up_with(self):
        # The untagged slot of a multipage group becomes page 1, so the crop of
        # that file belongs under the label it ended up with, not ':none'.
        items = [
            {"path": "s/cm1.jpg"},
            {"path": "s/cm1-crop.jpg"},
            {"path": "s/cm1-page2.jpg"},
        ]
        _calls, records, _warnings = self.run_manifest(items)
        variant_map = records[0]["result"]["all_variant_files"]
        self.assertEqual(variant_map["pages"], {
            "1": [_p("s/cm1.jpg")], "2": [_p("s/cm1-page2.jpg")]
        })
        self.assertEqual(variant_map["crops"], {":page:1": [_p("s/cm1-crop.jpg")]})


class TestPageSlots(ManifestGroupingTestCase):
    """``-page0`` is a legal name and owns a slot of its own."""

    def test_page_zero_does_not_evict_page_one(self):
        items = [{"path": "s/al-page0.jpg"}, {"path": "s/al-page1.jpg"}]
        calls, records, _warnings = self.run_manifest(items)
        self.assertEqual(
            calls,
            [(
                "parts",
                (
                    ("Page 0", (_p("s/al-page0.jpg"),)),
                    ("Page 1", (_p("s/al-page1.jpg"),)),
                ),
            )],
        )
        pages = records[0]["result"]["all_variant_files"]["pages"]
        self.assertEqual(
            pages, {"0": [_p("s/al-page0.jpg")], "1": [_p("s/al-page1.jpg")]}
        )

    def test_page_zero_is_not_relabelled_as_page_one(self):
        items = [{"path": "s/al-page0.jpg"}, {"path": "s/al-page2.jpg"}]
        calls, _records, _warnings = self.run_manifest(items)
        labels = [label for label, _paths in calls[0][1]]
        self.assertEqual(labels, ["Page 0", "Page 2"])

    def test_neither_page_is_dropped_and_neither_collides(self):
        # An ``entry["page_num"] or 1`` default addresses page 0 to the page 1
        # slot, where the two contend and one is lost. Assert the shape that
        # exposes it from both ends: both files reach the model, and the slot
        # map holds two distinct pages rather than one contested one.
        items = [{"path": "s/pz-page0.jpg"}, {"path": "s/pz-page1.jpg"}]
        calls, records, warnings = self.run_manifest(items)
        self.assertEqual(
            sorted(path for _label, path in _labelled_payload(calls)),
            sorted([_p("s/pz-page0.jpg"), _p("s/pz-page1.jpg")]),
            "one of the two pages never reached the model",
        )
        self.assertEqual(
            [w for w in warnings if "claim the same" in w],
            [],
            "page 0 and page 1 were addressed to the same slot",
        )
        self.assertEqual(
            records[0]["result"]["all_variant_files"]["pages"],
            {"0": [_p("s/pz-page0.jpg")], "1": [_p("s/pz-page1.jpg")]},
        )

    def test_page_zero_owns_its_slot_in_the_single_pair_path_too(self):
        items = [{"path": "s/pz2-page0.jpg"}, {"path": "s/pz2-page1.jpg"}]
        _calls, records, warnings = self.run_manifest(items)
        self.assertEqual(
            [w for w in warnings if "claim the same" in w], [], warnings
        )
        self.assertEqual(
            sorted(records[0]["result"]["all_variant_files"]["pages"]), ["0", "1"]
        )


class TestPreferredBack(ManifestGroupingTestCase):
    """Every back of a group is sent, whichever of them ``preferred`` names.

    Through B2 this class pinned which single back a group sent, because only
    one could travel and ``preferred`` was how a caller chose it -- and the B1
    defect it guards is a ``preferred`` back being discarded outright while
    resolving the primary. C1 retires the primary, so the question changes
    shape: the group sends every back it holds, and the way to fail the guard
    now is for one of them to go missing rather than for the wrong one to win.

    ``preferred`` has not become inert. It remains the tie-break for two files
    contesting one ``(version, part)`` slot --
    ``TestCropSlotOccupancy.test_preferred_takes_a_slot_it_is_allowed_to_take``
    pins that -- and it still nominates the file the analysis is filed under.
    """

    def test_a_preferred_versioned_back_is_sent_rather_than_discarded(self):
        items = [
            {"path": "s/k1.jpg"},
            {"path": "s/k1-back.jpg"},
            {"path": "s/k1b-back.jpg", "preferred": True},
        ]
        calls, _records, _warnings = self.run_manifest(items)
        self.assertEqual(
            calls,
            [(
                "front_back",
                (_p("s/k1.jpg"),),
                (_p("s/k1b-back.jpg"), _p("s/k1-back.jpg")),
            )],
        )

    def test_a_preferred_unversioned_back_is_sent_rather_than_discarded(self):
        # The front carries version 'b', so a version lookup alone would drop
        # k1-back.jpg -- which is the shape the B1 defect was found on.
        items = [
            {"path": "s/k1b.jpg"},
            {"path": "s/k1-back.jpg", "preferred": True},
            {"path": "s/k1b-back.jpg"},
        ]
        calls, _records, _warnings = self.run_manifest(items)
        self.assertEqual(
            calls,
            [(
                "front_back",
                (_p("s/k1b.jpg"),),
                (_p("s/k1b-back.jpg"), _p("s/k1-back.jpg")),
            )],
        )

    def test_the_same_group_without_the_flag_sends_the_same_files(self):
        # The consequence, stated directly: with every back in the payload there
        # is nothing left for ``preferred`` to choose between, so setting it on
        # a back no longer changes what the model is shown.
        plain = [
            {"path": "s/k1b.jpg"},
            {"path": "s/k1-back.jpg"},
            {"path": "s/k1b-back.jpg"},
        ]
        preferred = [plain[0], dict(plain[1], preferred=True), plain[2]]
        plain_calls, _records, _warnings = self.run_manifest(plain)
        preferred_calls, _records, _warnings = self.run_manifest(preferred)
        self.assertEqual(plain_calls, preferred_calls)

    def test_a_pc_code_reaches_every_scan_of_the_object(self):
        # A PC* code is a short identifier the model transcribes off the print
        # itself (prompts_photo_ai/image_rules.txt:97), so it describes the
        # physical object, not the one scan the model happened to be shown.
        # Both files here are scans of the same print, so both must carry it.
        # This also retires a failure mode rather than re-scoping it: the code
        # can no longer be filed against the wrong variant, because every
        # variant gets it. Only one analysis runs per group, so the previous
        # per-variant scoping meant a rescan silently lost its sibling's code.
        items = [{"path": "s/pv1.jpg"}, {"path": "s/pv1b-back.jpg", "preferred": True}]
        calls, records, _warnings = self.run_manifest(
            items, model_keywords=["PC123", "tree"]
        )
        self.assertEqual(
            calls, [("photo", _p("s/pv1.jpg"), _p("s/pv1b-back.jpg"))]
        )
        keywords = {rec["path"]: rec["result"]["keywords"] for rec in records}
        for path in (_p("s/pv1.jpg"), _p("s/pv1b-back.jpg")):
            self.assertIn(
                "PC123",
                keywords[path],
                f"{path} is a scan of the same print, so it must carry the code",
            )

    def test_a_pc_code_reaches_a_variant_rescan_at_every_granularity(self):
        # The case per-variant scoping actually lost: pc3b.jpg is a second scan
        # of the same print, and only one analysis runs for the group, so under
        # the old rule the code never reached it. ``pair`` and ``none`` analyze
        # it directly, so the code is read off its own scan there instead --
        # either way the rescan ends up carrying it.
        items = [
            {"path": "s/pc3.jpg"},
            {"path": "s/pc3b.jpg"},
            {"path": "s/pc3-back.jpg"},
        ]
        for group_by in utils.GROUP_BY_VALUES:
            with self.subTest(group_by=group_by):
                _calls, records, _warnings = self.run_manifest(
                    items,
                    model_keywords=["PC-R-123", "portrait"],
                    group_by=group_by,
                )
                keywords = {rec["path"]: rec["result"]["keywords"] for rec in records}
                self.assertIn(
                    "PC-R-123",
                    keywords[_p("s/pc3b.jpg")],
                    "the variant rescan lost the code read off its sibling",
                )

    def test_that_scoping_matches_the_same_group_without_the_preferred_flag(self):
        base = [{"path": "s/pv2.jpg"}, {"path": "s/pv2b-back.jpg"}]
        preferred = [dict(base[0]), dict(base[1], preferred=True)]
        _calls, plain_records, _warnings = self.run_manifest(
            base, model_keywords=["PC456"]
        )
        _calls, preferred_records, _warnings = self.run_manifest(
            preferred, model_keywords=["PC456"]
        )
        self.assertEqual(
            {rec["path"]: rec["result"]["keywords"] for rec in plain_records},
            {rec["path"]: rec["result"]["keywords"] for rec in preferred_records},
            "naming the back as preferred, which changed nothing about which "
            "files were sent, moved the per-variant keywords onto another file",
        )


class TestPermutationInvariance(ManifestGroupingTestCase):
    """Nothing the grouper decides may depend on the order the manifest listed.

    This is the property the whole of Phase B1 exists to establish, and the
    regression test for the defect that started it: before the fix, a crop and
    its parent resolved to the same slot and ``setdefault`` made it first-wins,
    so whichever file the manifest happened to name first was the one sent to
    the model and the other was dropped with no warning. The same held for a
    negative, which degraded into an untagged front. Reordering the items of a
    manifest must not change one bit of what the model is asked to look at.

    The ``front``/``back``/``all``/``variants`` lists and the NDJSON emission
    order stay input-ordered on purpose, so they are excluded here; everything
    that decides which file reaches the model is not.
    """

    #: Every item carries metadata, and the values deliberately conflict.
    #:
    #: B1 restricted these groups to one metadata-bearing item apiece, because
    #: ``combine_group_metadata`` was then first-non-empty over *arrival* order:
    #: a second populated item would have made the sweep fail for a reason B1
    #: was not about. C3 replaced that scan with a sorted one -- crop rank, then
    #: part rank, then path -- so the group's answer no longer depends on which
    #: item the manifest happened to name first, and the restriction became a
    #: gap rather than a precaution. Lifting it is what puts the sorted scan
    #: under the same 24-permutation sweep as everything else, and ``-r`` makes
    #: the widened shape the ordinary one: every item hydrates, so every item
    #: arrives populated.
    #:
    #: The values conflict on purpose. Identical metadata would be invariant
    #: under permutation no matter how the scan was written, which is the shape
    #: that would let a first-non-empty regression pass unnoticed.
    GROUPS = (
        [
            {"path": "s/g1.jpg", "metadata": {"title": "g1 front"}},
            {"path": "s/g1-crop.jpg", "metadata": {"title": "g1 crop"}},
            {"path": "s/g1-back.jpg", "metadata": {"caption": "g1 back caption"}},
            {"path": "s/g1b-back.jpg", "metadata": {"caption": "g1b back caption"}},
        ],
        [
            {"path": "s/g2-page0.jpg", "metadata": {"title": "page 0"}},
            {"path": "s/g2-page1.jpg", "metadata": {"title": "page 1"}},
            {"path": "s/g2-page1-crop.jpg", "metadata": {"title": "page 1 crop"}},
            {"path": "s/g2-negative.jpg", "metadata": {"title": "negative"}},
        ],
        [
            {"path": "s/g3-crop.jpg", "metadata": {"userComment": "crop note"}},
            {"path": "s/g3-back.jpg", "metadata": {"userComment": "back note"}},
            {
                "path": "s/g3b.jpg",
                "preferred": True,
                "metadata": {"userComment": "preferred note", "title": "g3b"},
            },
            {
                "path": "s/g3_back.jpg",
                "is_back": True,
                "metadata": {"userComment": "override-back note"},
            },
        ],
        # Front, back and one negative per variant: the negative must not
        # become the front from any starting position, nor supply the group's
        # metadata over the print's.
        [
            {"path": "s/g4.jpg", "metadata": {"title": "g4 print"}},
            {"path": "s/g4-negative.jpg", "metadata": {"title": "g4 negative"}},
            {"path": "s/g4-back.jpg", "metadata": {"dateTimeOriginal": "1971:03:02 09:00:00"}},
            {"path": "s/g4b-negative.jpg", "metadata": {"title": "g4b negative"}},
        ],
        # All four overrides at once, on names the filename grammar reads
        # differently or cannot read at all.
        [
            {"path": "s/g5.jpg", "metadata": {"title": "g5 front", "keywords": ["Family"]}},
            {
                "path": "s/g5_back.jpg",
                "is_back": True,
                "metadata": {"title": "g5 back", "keywords": ["Bakery"]},
            },
            {
                "path": "s/IMG_77.jpg",
                "group": "g5",
                "is_crop": True,
                "metadata": {"title": "g5 crop", "keywords": ["Crop"]},
            },
            {
                "path": "s/g5x.jpg",
                "version": "c",
                "metadata": {"title": "g5 variant c", "keywords": ["Variant"]},
            },
        ],
    )

    def _signature(self, items: list[dict], group_by: str) -> str:
        calls, records, warnings = self.run_manifest(items, group_by=group_by)
        metas = self.last_metas
        slots = {
            rec["path"]: {
                key: rec["result"]["all_variant_files"].get(key)
                for key in ("pages", "crops", "negatives", "displaced")
            }
            for rec in records
        }
        return json.dumps(
            {
                "calls": calls,
                "slots": slots,
                # Paired with the call it belongs to, so a metadata answer that
                # moved between calls is a diff and not a re-sort. Without this
                # the sweep cannot see the forwarded metadata at all, and every
                # item could carry a different title with the run still looking
                # identical -- which is what made lifting the one-item-per-group
                # restriction worth doing rather than cosmetic.
                "metas": [
                    [call, meta] for call, meta in zip(calls, metas, strict=True)
                ],
                "warnings": sorted(warnings),
            },
            sort_keys=True,
            indent=2,
        )

    def test_every_grouping_decision_is_invariant_under_permutation(self):
        for group_index, group in enumerate(self.GROUPS):
            for group_by in utils.GROUP_BY_VALUES:
                baseline_order = [item["path"] for item in group]
                expected = self._signature(list(group), group_by)
                for permutation in itertools.permutations(group):
                    order = [item["path"] for item in permutation]
                    with self.subTest(
                        group=group_index,
                        group_by=group_by,
                        order=order,
                    ):
                        self.assertEqual(
                            self._signature(list(permutation), group_by),
                            expected,
                            "PHASE B1 REGRESSION: manifest listing order changed the "
                            "grouping. The same files in a different order must send "
                            "the model the same call and record the same slots -- "
                            "order-dependence here is silent data loss on the plug-in "
                            f"contract.\n  baseline order: {baseline_order}\n"
                            f"  failing order:  {order}",
                        )


class TestNegativeIsNotAnUntaggedFront(ManifestGroupingTestCase):
    """``part_kind == "negative"`` owns a slot and a label of its own.

    Before Phase B1 the bucket loop had no branch for it, so a negative fell
    through to the same untagged ``none`` slot as the real scan and could be
    handed to the model as the group's front.
    """

    def test_a_negative_never_becomes_the_primary_in_either_order(self):
        for label, items in (
            ("scan first", [{"path": "s/n1.jpg"}, {"path": "s/n1-negative.jpg"}]),
            ("negative first", [{"path": "s/n1-negative.jpg"}, {"path": "s/n1.jpg"}]),
        ):
            with self.subTest(label):
                calls, records, _warnings = self.run_manifest(items)
                self.assertEqual(
                    calls,
                    [(
                        "parts",
                        (
                            ("Front", (_p("s/n1.jpg"),)),
                            ("Negative", (_p("s/n1-negative.jpg"),)),
                        ),
                    )],
                    "the negative displaced the real scan as the group's front",
                )
                variant_map = records[0]["result"]["all_variant_files"]
                self.assertEqual(
                    variant_map.get("negatives"),
                    [_p("s/n1-negative.jpg")],
                    "the negative was not recorded in a slot of its own",
                )

    def test_a_negative_does_not_outrank_a_versioned_front(self):
        # The negative is unversioned and the front is not, which is the shape
        # that would have let master selection prefer the negative on rank. The
        # separate "no negative in the master pool" filter is gone with
        # ``pick_master_index``; ``_PART_RANK["negative"] == 4`` does the job on
        # its own, and the part label the front travels under proves it.
        items = [{"path": "s/n5-negative.jpg"}, {"path": "s/n5b.jpg"}]
        calls, _records, _warnings = self.run_manifest(items)
        self.assertEqual(
            calls,
            [(
                "parts",
                (
                    ("Front", (_p("s/n5b.jpg"),)),
                    ("Negative", (_p("s/n5-negative.jpg"),)),
                ),
            )],
        )

    def test_a_negative_reaches_the_model_under_its_own_label(self):
        items = [{"path": "s/n3-negative.jpg"}, {"path": "s/n3.jpg"}, {"path": "s/n3-back.jpg"}]
        calls, _records, _warnings = self.run_manifest(items)
        self.assertEqual(
            calls,
            [(
                "parts",
                (
                    ("Front", (_p("s/n3.jpg"),)),
                    ("Back", (_p("s/n3-back.jpg"),)),
                    ("Negative", (_p("s/n3-negative.jpg"),)),
                ),
            )],
        )

    def test_a_negative_is_the_front_side_when_the_group_holds_only_it_and_a_back(self):
        # PHASE B1 REGRESSION GUARD. The rule that keeps a negative from being
        # promoted over a real scan removed negatives from the candidate list
        # outright, so a group whose only front-side file *is* the negative had
        # nothing left to fall back to: it sent the back as the front as well,
        # and dropped the negative without a word. Commit 525e9a6 sends
        # photo(negative, back) here, which is also the only answer that keeps
        # both listed files in the payload. Since C1 the group holds a negative
        # and so takes the labelled part form, but the property the guard exists
        # for is the same one: both files travel, exactly once each.
        for label, items in (
            ("negative first", [{"path": "s/w1-negative.jpg"}, {"path": "s/w1-back.jpg"}]),
            ("back first", [{"path": "s/w1-back.jpg"}, {"path": "s/w1-negative.jpg"}]),
        ):
            with self.subTest(label):
                calls, _records, _warnings = self.run_manifest(items)
                self.assertEqual(
                    sorted(_labelled_payload(calls)),
                    [
                        ("Back", _p("s/w1-back.jpg")),
                        ("Negative", _p("s/w1-negative.jpg")),
                    ],
                    "a group holding a negative and a back has exactly one "
                    "front-side file and it is the negative; sending anything "
                    "else here means the back was sent twice or the negative "
                    f"was dropped ({label})",
                )
                self.assert_no_path_sent_under_two_labels(calls)

    def test_that_group_sends_both_parts_under_their_own_labels(self):
        items = [{"path": "s/w1-negative.jpg"}, {"path": "s/w1-back.jpg"}]
        calls, _records, warnings = self.run_manifest(items)
        self.assertEqual(
            calls,
            [(
                "parts",
                (
                    ("Back", (_p("s/w1-back.jpg"),)),
                    ("Negative", (_p("s/w1-negative.jpg"),)),
                ),
            )],
        )
        self.assert_every_file_sent_or_disclosed(items, calls, warnings)

    def test_a_negative_beside_a_real_front_is_still_not_the_front_side(self):
        # The fallback above must not become a back door: with a front listed,
        # the negative keeps its own part and the front keeps the front role.
        items = [
            {"path": "s/w3-negative.jpg"},
            {"path": "s/w3.jpg"},
            {"path": "s/w3-back.jpg"},
        ]
        calls, _records, _warnings = self.run_manifest(items)
        self.assertEqual(
            calls,
            [(
                "parts",
                (
                    ("Front", (_p("s/w3.jpg"),)),
                    ("Back", (_p("s/w3-back.jpg"),)),
                    ("Negative", (_p("s/w3-negative.jpg"),)),
                ),
            )],
        )

    def test_a_lone_negative_is_labelled_negative_rather_than_front(self):
        calls, records, _warnings = self.run_manifest([{"path": "s/n2-negative.jpg"}])
        self.assertEqual(calls, [("parts", (("Negative", (_p("s/n2-negative.jpg"),)),))])
        self.assertEqual(
            records[0]["result"]["all_variant_files"]["negatives"],
            [_p("s/n2-negative.jpg")],
        )

    def test_negatives_are_addressed_per_variant_not_per_stem(self):
        # The retired folder grouper binned negatives at stem level with an
        # unconditional assignment, so two that differ only by variant letter
        # overwrote each other. This path -- now the only one -- keeps both.
        items = [{"path": "s/n4-negative.jpg"}, {"path": "s/n4b-negative.jpg"}]
        calls, records, _warnings = self.run_manifest(items)
        self.assertEqual(
            calls,
            [(
                "parts",
                (("Negative", (_p("s/n4b-negative.jpg"), _p("s/n4-negative.jpg"))),),
            )],
        )
        self.assertEqual(
            records[0]["result"]["all_variant_files"]["negatives"],
            [_p("s/n4b-negative.jpg"), _p("s/n4-negative.jpg")],
        )


class TestPayloadInvariants(ManifestGroupingTestCase):
    """Two rules hold for every group, whatever the manifest asked for.

    (a) No path is ever sent under two labels. Each part is uploaded, billed and
        described separately, so a file handed over as two of them costs twice
        and asserts something false about the object.
    (b) No listed file leaves the payload in silence. Manifest mode owes every
        listed file a record; where it cannot also owe it a model call, it has
        to name the file and the reason.

    The shapes below are the ones that resolve one file into more than one role,
    or resolve a role to nothing: a group with no front side, a manifest that
    lists a path twice, and one that lists it twice under contradicting flags.
    """

    SHAPES = (
        ("negative and back, no front", [
            {"path": "s/iv1-negative.jpg"},
            {"path": "s/iv1-back.jpg"},
        ]),
        ("back only", [{"path": "s/iv2-back.jpg"}]),
        ("two backs, no front", [
            {"path": "s/iv3-back.jpg"},
            {"path": "s/iv3b-back.jpg"},
        ]),
        ("the same path listed twice", [{"path": "s/iv4.jpg"}, {"path": "s/iv4.jpg"}]),
        ("the same path listed twice, contradicting flags", [
            {"path": "s/iv5.jpg"},
            {"path": "s/iv5.jpg", "is_back": True},
        ]),
        ("a crop and its parent", [{"path": "s/iv6.jpg"}, {"path": "s/iv6-crop.jpg"}]),
        ("two files claiming one variant's front side", [
            {"path": "s/iv7.jpg"},
            {"path": "s/iv7-back.jpg", "is_back": False},
        ]),
        ("an untagged file in a multipage group", [
            {"path": "s/iv8-page1.jpg"},
            {"path": "s/iv8.jpg"},
            {"path": "s/iv8-page2.jpg"},
        ]),
    )

    def test_no_file_is_ever_sent_under_two_labels(self):
        for label, items in self.SHAPES:
            for group_by in utils.GROUP_BY_VALUES:
                with self.subTest(label, group_by=group_by):
                    calls, _records, _warnings = self.run_manifest(
                        items, group_by=group_by
                    )
                    self.assert_no_path_sent_under_two_labels(calls)

    def test_no_listed_file_leaves_the_group_payload_in_silence(self):
        for label, items in self.SHAPES:
            with self.subTest(label):
                calls, _records, warnings = self.run_manifest(items)
                self.assert_every_file_sent_or_disclosed(items, calls, warnings)

    def test_every_shape_still_records_every_listed_file(self):
        for label, items in self.SHAPES:
            for group_by in utils.GROUP_BY_VALUES:
                with self.subTest(label, group_by=group_by):
                    _calls, records, _warnings = self.run_manifest(
                        items, group_by=group_by
                    )
                    self.assertEqual({rec["status"] for rec in records}, {"ok"})
                    self.assertEqual(
                        sorted({rec["path"] for rec in records}),
                        sorted({_p(item["path"]) for item in items}),
                    )

    def test_a_group_with_no_front_side_sends_its_back_once(self):
        calls, _records, _warnings = self.run_manifest([{"path": "s/iv2-back.jpg"}])
        self.assertEqual(
            calls,
            [("photo", _p("s/iv2-back.jpg"), None)],
            "the group's only file was sent as both the front and the back",
        )


class TestFrontSideRoleCollision(ManifestGroupingTestCase):
    """One file per variant fills the front side, and the loser is disclosed.

    An untagged file and one explicitly flagged ``is_back: false`` sit in
    different slots but feed the same role, as does an untagged file in a group
    that already has a ``-page1``. Only one of them can travel in the payload,
    so the other has to be named rather than overwritten by whichever assignment
    happened to run last.
    """

    OVERRIDE_PAIR = ({"path": "s/fr1.jpg"}, {"path": "s/fr1-back.jpg", "is_back": False})

    def test_only_one_of_them_is_sent_and_the_other_is_named(self):
        # ``object`` and ``pair`` only: the collision is between two files of one
        # group, and under ``none`` there is no group for them to collide in.
        for group_by in (utils.GROUP_BY_OBJECT, utils.GROUP_BY_PAIR):
            with self.subTest(group_by=group_by):
                calls, _records, warnings = self.run_manifest(
                    list(self.OVERRIDE_PAIR), group_by=group_by
                )
                sent = [path for _label, path in _labelled_payload(calls)]
                self.assertEqual(sent, [_p("s/fr1-back.jpg")])
                self.assertTrue(
                    any(
                        "claim the front side" in warning and "fr1.jpg" in warning
                        for warning in warnings
                    ),
                    f"the file that lost the front side was dropped in silence: {warnings}",
                )

    def test_object_and_pair_send_the_same_file(self):
        by_object, _records, _warnings = self.run_manifest(list(self.OVERRIDE_PAIR))
        by_pair, _records, _warnings = self.run_manifest(
            list(self.OVERRIDE_PAIR), group_by=utils.GROUP_BY_PAIR
        )
        self.assertEqual(
            {path for _label, path in _labelled_payload(by_object)},
            {path for _label, path in _labelled_payload(by_pair)},
            "--group-by changed which of the two the model saw, though both "
            "files carry no variant letter and so form one group either way",
        )

    def test_the_record_names_the_file_that_could_not_be_sent(self):
        _calls, records, _warnings = self.run_manifest(list(self.OVERRIDE_PAIR))
        variant_map = records[0]["result"]["all_variant_files"]
        self.assertEqual(variant_map["displaced"], {":none": [_p("s/fr1.jpg")]})
        self.assertEqual(
            variant_map["front"],
            [_p("s/fr1.jpg"), _p("s/fr1-back.jpg")],
            "the front list is every front-side file in the group and stays that "
            "way; 'displaced' is what tells the reader which of them was sent",
        )

    def test_an_untagged_file_cannot_stow_away_in_a_multipage_group(self):
        items = [
            {"path": "s/fr2-page1.jpg"},
            {"path": "s/fr2.jpg"},
            {"path": "s/fr2-page2.jpg"},
        ]
        # Not under ``none``: with one file per group there is no multipage set
        # for anything to stow away in, and every page is analyzed alone.
        for group_by in (utils.GROUP_BY_OBJECT, utils.GROUP_BY_PAIR):
            with self.subTest(group_by=group_by):
                calls, records, warnings = self.run_manifest(items, group_by=group_by)
                self.assertNotIn(
                    _p("s/fr2.jpg"),
                    [path for _label, path in _labelled_payload(calls)],
                    "an untagged file took a page slot that was already filled",
                )
                self.assertEqual(
                    records[0]["result"]["all_variant_files"]["displaced"],
                    {":none": [_p("s/fr2.jpg")]},
                )
                self.assertTrue(any("fr2.jpg" in warning for warning in warnings))

    def test_an_untagged_file_still_becomes_page_one_when_the_slot_is_free(self):
        items = [{"path": "s/fr3.jpg"}, {"path": "s/fr3-page2.jpg"}]
        calls, records, _warnings = self.run_manifest(items)
        self.assertEqual(
            calls,
            [(
                "parts",
                (
                    ("Page 1", (_p("s/fr3.jpg"),)),
                    ("Page 2", (_p("s/fr3-page2.jpg"),)),
                ),
            )],
        )
        self.assertNotIn("displaced", records[0]["result"]["all_variant_files"])


class TestSlotCollisionAccounting(ManifestGroupingTestCase):
    """A file that loses a contested slot is accounted for however it lost it.

    One ``(version, part)`` slot can be contested two ways. Two filenames can
    parse straight into the same address -- a TIFF master beside the JPEG
    derivative of the same scan, which is ordinary archival practice -- or an
    override can steer one file onto a front side another already holds. Both
    warn, both cost the loser its place in the payload, and both leave it with a
    record taken from the winner's analysis.

    Only the second used to reach ``all_variant_files.displaced`` and the
    completion line's count, so a run that warned about a TIFF beside its JPEG
    closed by reporting nothing displaced at all. These cases pin the two shapes
    against each other, and pin the count to the payload the recorder actually
    saw rather than to the rule that assembled it.
    """

    #: One scan in two formats: same stem, same slot. One file is analyzed and
    #: the result is recorded against both, which is the saving.
    EXTENSION_PAIR = ({"path": "s/box3_025.tif"}, {"path": "s/box3_025.jpg"})

    #: ``is_back: false`` puts the second file on the front side the first
    #: already claims, so the two sit in different slots feeding one role.
    OVERRIDE_PAIR = ({"path": "s/ov1.jpg"}, {"path": "s/ov1-back.jpg", "is_back": False})

    #: ``(label, items)`` covering every way a listed file can miss the payload,
    #: plus shapes that lose nothing so the invariant below cannot go vacuous.
    SHAPES = (
        ("an ordinary front and back", [{"path": "s/sc1.jpg"}, {"path": "s/sc1-back.jpg"}]),
        ("a TIFF master beside its JPEG derivative", list(EXTENSION_PAIR)),
        ("an override-driven collision", list(OVERRIDE_PAIR)),
        ("a crop beside its parent", [{"path": "s/sc2.jpg"}, {"path": "s/sc2-crop.jpg"}]),
        (
            "an untagged file beside an explicit page 1",
            [
                {"path": "s/sc3.jpg"},
                {"path": "s/sc3-page1.jpg"},
                {"path": "s/sc3-page2.jpg"},
            ],
        ),
        (
            "one scan in three formats",
            [{"path": "s/sc4.jpg"}, {"path": "s/sc4.png"}, {"path": "s/sc4.tif"}],
        ),
    )

    def assert_the_loser_is_accounted_for(
        self,
        items: tuple[dict, ...],
        *,
        sent: str,
        unsent: str,
        warning_fragment: str,
    ) -> None:
        """Assert the warning, the record and the completion line tell one story.

        Args:
            items: The manifest's ``items`` array.
            sent: Path of the file the model is expected to be shown.
            unsent: Path of the file that loses the slot to it.
            warning_fragment: Text the per-group warning must carry, so the case
                cannot pass on a warning about something else entirely.
        """
        calls, records, log_records = self.run_items_records(list(items))

        self.assertEqual(
            [path for _label, path in _labelled_payload(calls)],
            [_p(sent)],
            "the collision changed which file the model was shown, or how many",
        )
        self.assertEqual(
            sorted(rec["path"] for rec in records),
            sorted(_p(item["path"]) for item in items),
            "the loser of the slot still gets a record -- that is what makes this "
            "a saving rather than a loss, and the wording rests on it",
        )
        self.assertEqual([rec["status"] for rec in records], ["ok"] * len(items))

        named = [
            r.getMessage()
            for r in log_records
            if r.levelno >= logging.WARNING
            and warning_fragment in r.getMessage()
            and os.path.basename(_p(unsent)) in r.getMessage()
        ]
        self.assertEqual(
            len(named), 1, f"no warning named the loser: {[r.getMessage() for r in log_records]}"
        )

        for rec in records:
            self.assertEqual(
                rec["result"]["all_variant_files"].get("displaced"),
                {":none": [_p(unsent)]},
                "a warning named a file the record does not disclose",
            )

        completion = _completion_record(log_records)
        self.assertEqual(
            _unsent_count(completion),
            1,
            f"a warning says {os.path.basename(_p(unsent))} never reached the "
            f"payload and the completion line contradicts it: {completion.getMessage()}",
        )
        self.assertEqual(
            completion.levelno,
            logging.WARNING,
            "a file missed the payload, so the summary must not read as clean",
        )

    def test_a_tiff_master_beside_its_jpeg_derivative_is_counted(self):
        self.assert_the_loser_is_accounted_for(
            self.EXTENSION_PAIR,
            sent="s/box3_025.tif",
            unsent="s/box3_025.jpg",
            warning_fragment="claim the same none slot",
        )

    def test_an_override_driven_collision_is_counted(self):
        self.assert_the_loser_is_accounted_for(
            self.OVERRIDE_PAIR,
            sent="s/ov1-back.jpg",
            unsent="s/ov1.jpg",
            warning_fragment="claim the front side",
        )

    def test_the_two_collision_shapes_are_reported_identically(self):
        # Two collisions of the same kind, differing only in how the two files
        # arrived at one slot. Compared as a whole rather than field by field so
        # a future divergence in any part of the accounting shows up here.
        def accounting(items: tuple[dict, ...]) -> tuple:
            _calls, records, log_records = self.run_items_records(list(items))
            completion = _completion_record(log_records)
            displaced = records[0]["result"]["all_variant_files"].get("displaced") or {}
            return (
                _unsent_count(completion),
                completion.levelno,
                sorted(displaced),
                [len(paths) for _slot, paths in sorted(displaced.items())],
            )

        self.assertEqual(
            accounting(self.EXTENSION_PAIR),
            accounting(self.OVERRIDE_PAIR),
            "the same loss is reported one way when two filenames parse into one "
            "slot and another way when an override puts them there",
        )

    def test_the_count_is_exactly_what_the_model_calls_left_out(self):
        # The invariant, asserted against the recorder rather than against the
        # rules that built the payload: an accounting site that forgets to
        # register its loser fails here whichever rule it belongs to.
        for label, items in self.SHAPES:
            with self.subTest(label):
                calls, _records, log_records = self.run_items_records(items)
                sent = {path for _label, path in _labelled_payload(calls)}
                unsent = {_p(item["path"]) for item in items} - sent
                completion = _completion_record(log_records)
                self.assertEqual(
                    _unsent_count(completion),
                    len(unsent),
                    f"the summary and the payload disagree about {sorted(unsent)}: "
                    f"{completion.getMessage()}",
                )
                self.assertEqual(
                    completion.levelno,
                    logging.WARNING if unsent else logging.INFO,
                    f"the summary chose the wrong level for {sorted(unsent)}",
                )

    def test_every_unsent_file_the_summary_counts_is_named_in_a_warning(self):
        # The invariant read the other way round. A count with no warning behind
        # it would be as useless as a warning with no count.
        for label, items in self.SHAPES:
            with self.subTest(label):
                calls, _records, log_records = self.run_items_records(items)
                sent = {path for _label, path in _labelled_payload(calls)}
                warnings = [
                    r.getMessage() for r in log_records if r.levelno >= logging.WARNING
                ]
                for path in {_p(item["path"]) for item in items} - sent:
                    name = os.path.basename(path)
                    self.assertTrue(
                        any(name in warning for warning in warnings),
                        f"{name} never reached the model and nothing said so: {warnings}",
                    )

    def test_a_path_that_won_two_slots_is_sent_and_so_is_not_counted(self):
        # One path listed twice under contradicting flags wins two addresses and
        # travels under the better of them. The address it gives up is still
        # disclosed -- that is what ``displaced`` is for -- but the file itself
        # reached the model, so counting it would restate the contradiction the
        # other way round.
        items = [{"path": "s/tl1.jpg"}, {"path": "s/tl1.jpg", "is_back": True}]
        calls, records, log_records = self.run_items_records(items)

        self.assertEqual(
            [path for _label, path in _labelled_payload(calls)], [_p("s/tl1.jpg")]
        )
        self.assertTrue(
            any("sending it once" in r.getMessage() for r in log_records),
            [r.getMessage() for r in log_records],
        )
        self.assertEqual(
            records[0]["result"]["all_variant_files"]["displaced"],
            {":back": [_p("s/tl1.jpg")]},
            "the slot it gave up is not disclosed",
        )
        completion = _completion_record(log_records)
        self.assertEqual(_unsent_count(completion), 0)
        self.assertEqual(completion.levelno, logging.INFO)


class TestDuplicateListing(ManifestGroupingTestCase):
    """A path listed twice repeats a file; it does not contest a slot."""

    def test_an_exact_duplicate_is_not_reported_as_a_collision(self):
        calls, _records, warnings = self.run_manifest(
            [{"path": "s/dl1.jpg"}, {"path": "s/dl1.jpg"}]
        )
        self.assertEqual(calls, [("photo", _p("s/dl1.jpg"), None)])
        self.assertEqual(
            [w for w in warnings if "claim the same" in w],
            [],
            "a file was reported as losing a slot to itself",
        )

    def test_a_genuine_collision_is_still_reported_once_per_distinct_file(self):
        items = [
            {"path": "s/dl2.jpg", "group": "dl2"},
            {"path": "s/dl2.jpg", "group": "dl2"},
            {"path": "s/dl3.jpg", "group": "dl2"},
        ]
        _calls, _records, warnings = self.run_manifest(items)
        collisions = [w for w in warnings if "claim the same" in w]
        self.assertEqual(len(collisions), 1, warnings)
        self.assertIn("2 file(s) claim the same none slot", collisions[0])
        self.assertIn("dl3.jpg", collisions[0])

    def test_a_duplicated_crop_stands_in_for_the_object_only_once(self):
        items = [{"path": "s/dl4-crop.jpg"}, {"path": "s/dl4-crop.jpg"}]
        _calls, records, warnings = self.run_manifest(items)
        self.assertEqual(
            len([w for w in warnings if "no uncropped original" in w]), 1, warnings
        )
        self.assertEqual(
            records[0]["result"]["all_variant_files"]["crops"],
            {":none": [_p("s/dl4-crop.jpg")]},
        )


class TestReadmeSampleManifest(ManifestGroupingTestCase):
    """The manifest published in README.md must do what README.md says it does.

    ``README.md`` (manifest mode, the ``batch.json`` sample) declares one
    physical object as ``scans/box3_017.jpg`` plus ``scans/box3_017_back.jpg``
    with ``"is_back": true``, and the surrounding prose states that the flag is
    what folds the two files "into one group and one model call rather than two
    unrelated photos". Before Phase B1 the flag was read off the item and
    discarded, the underscore form was not recognized by the filename grammar,
    and the sample formed two groups and made two model calls.
    """

    #: Copied verbatim from the README's ``batch.json`` block.
    SAMPLE = {
        "items": [
            {"path": "scans/box3_017.jpg"},
            {"path": "scans/box3_017_back.jpg", "is_back": True},
        ],
        "photo_context_text": "Church family photos, mostly New Jersey, 1930s-1950s.",
    }

    def test_the_sample_forms_one_group_with_a_front_and_a_back(self):
        calls, records, warnings = self.run_manifest_source(self.SAMPLE)
        self.assertEqual(
            calls,
            [("photo", _p("scans/box3_017.jpg"), _p("scans/box3_017_back.jpg"))],
            "the README's own sample did not resolve to one front/back call",
        )
        variant_map = self.assert_one_group(
            records, [_p("scans/box3_017.jpg"), _p("scans/box3_017_back.jpg")]
        )
        self.assertEqual(variant_map["front"], [_p("scans/box3_017.jpg")])
        self.assertEqual(variant_map["back"], [_p("scans/box3_017_back.jpg")])
        self.assertTrue(
            any("box3_017_back.jpg" in w and "is_back" in w for w in warnings),
            f"the override that did the grouping was not logged: {warnings}",
        )

    def test_the_sample_is_one_group_under_pair_too(self):
        # Neither file carries a variant letter, so ``pair`` keys on the bare
        # group key and the sample stays one object -- the stability that makes
        # ``pair`` safe to reach for on ordinary input.
        calls, _records, _warnings = self.run_manifest_source(
            self.SAMPLE, group_by=utils.GROUP_BY_PAIR
        )
        self.assertEqual(
            calls,
            [("photo", _p("scans/box3_017.jpg"), _p("scans/box3_017_back.jpg"))],
        )

    def test_group_by_none_splits_the_sample_the_is_back_flag_joined(self):
        # The escape hatch doing what it says: the flag still marks the back,
        # but there is no group for it to join, so the pair costs two calls and
        # the back is analyzed as handwriting with no photo beside it.
        calls, records, _warnings = self.run_manifest_source(
            self.SAMPLE, group_by=utils.GROUP_BY_NONE
        )
        self.assertEqual(
            calls,
            [
                ("photo", _p("scans/box3_017.jpg"), None),
                ("photo", _p("scans/box3_017_back.jpg"), None),
            ],
        )
        keywords = {rec["path"]: rec["result"]["keywords"] for rec in records}
        self.assertIn("back", keywords[_p("scans/box3_017_back.jpg")])

    def test_the_sample_groups_the_same_way_when_listed_back_first(self):
        reversed_sample = dict(self.SAMPLE, items=list(reversed(self.SAMPLE["items"])))
        calls, _records, _warnings = self.run_manifest_source(reversed_sample)
        self.assertEqual(
            calls,
            [("photo", _p("scans/box3_017.jpg"), _p("scans/box3_017_back.jpg"))],
        )

    def test_the_sample_loads_and_groups_from_a_manifest_file_on_disk(self):
        with TemporaryDirectory() as tmp_dir:
            manifest_path = os.path.join(tmp_dir, "batch.json")
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump(self.SAMPLE, handle)
            calls, records, _warnings = self.run_manifest_source(manifest_path)
        self.assertEqual(
            calls,
            [("photo", _p("scans/box3_017.jpg"), _p("scans/box3_017_back.jpg"))],
        )
        self.assertEqual(len(records), 2)


class TestExplicitOverrides(ManifestGroupingTestCase):
    """An explicit item flag beats the filename, in both directions.

    ``README.md`` states the flags exist for files that do not follow the
    naming conventions, so a filename that overruled them would leave them
    inert in exactly the case they are there for.
    """

    def test_is_back_true_makes_a_front_name_a_back(self):
        items = [{"path": "s/ob1.jpg"}, {"path": "s/ob1_back.jpg", "is_back": True}]
        calls, records, _warnings = self.run_manifest(items)
        self.assertEqual(calls, [("photo", _p("s/ob1.jpg"), _p("s/ob1_back.jpg"))])
        variant_map = self.assert_one_group(
            records, [_p("s/ob1.jpg"), _p("s/ob1_back.jpg")]
        )
        self.assertEqual(variant_map["back"], [_p("s/ob1_back.jpg")])

    def test_is_back_false_makes_a_back_name_a_front(self):
        items = [{"path": "s/ov1.jpg"}, {"path": "s/ov1-back.jpg", "is_back": False}]
        calls, records, warnings = self.run_manifest(items)
        variant_map = self.assert_one_group(
            records, [_p("s/ov1.jpg"), _p("s/ov1-back.jpg")]
        )
        self.assertEqual(variant_map["back"], [])
        self.assertEqual(
            variant_map["front"], [_p("s/ov1.jpg"), _p("s/ov1-back.jpg")]
        )
        self.assertIsNone(
            calls[0][2], f"a file flagged is_back=false was sent as the back: {calls}"
        )
        self.assertTrue(any("is_back" in w and "ov1-back.jpg" in w for w in warnings))

    def test_is_back_does_not_strip_a_back_that_is_only_part_of_a_word(self):
        # 'feedback' ends in 'back' but nothing separates it, so the group-key
        # repair must leave it alone rather than regroup the file under 'feed'.
        items = [{"path": "s/feed.jpg"}, {"path": "s/feedback.jpg", "is_back": True}]
        _calls, records, _warnings = self.run_manifest(items)
        groups = {
            tuple(rec["result"]["all_variant_files"]["all"]) for rec in records
        }
        self.assertEqual(
            groups,
            {(_p("s/feed.jpg"),), (_p("s/feedback.jpg"),)},
            "'feedback.jpg' was mis-repaired into the 'feed' group",
        )

    def test_is_crop_true_keeps_an_unmarked_file_out_of_the_payload(self):
        items = [
            {"path": "s/oc2.jpg"},
            {"path": "s/oc2-detail.jpg", "group": "oc2", "is_crop": True},
        ]
        calls, records, warnings = self.run_manifest(items)
        self.assertEqual(calls, [("photo", _p("s/oc2.jpg"), None)])
        variant_map = records[0]["result"]["all_variant_files"]
        self.assertEqual(variant_map["crops"], {":none": [_p("s/oc2-detail.jpg")]})
        self.assertTrue(
            any("recorded but not analyzed" in w and "oc2-detail.jpg" in w for w in warnings)
        )

    def test_is_crop_false_takes_a_crop_name_out_of_the_crop_map(self):
        items = [{"path": "s/oc1.jpg"}, {"path": "s/oc1-crop.jpg", "is_crop": False}]
        _calls, records, warnings = self.run_manifest(items)
        variant_map = records[0]["result"]["all_variant_files"]
        self.assertNotIn(
            "crops",
            variant_map,
            "a file flagged is_crop=false was still recorded as a crop",
        )
        self.assertTrue(any("is_crop" in w and "oc1-crop.jpg" in w for w in warnings))

    def test_an_explicit_version_replaces_the_one_read_off_the_filename(self):
        # Both files carry the letter 'b' in their names; clearing it puts them
        # in the same unversioned slot pair rather than variant 'b'.
        items = [
            {"path": "s/ov4b.jpg", "version": ""},
            {"path": "s/ov4b-back.jpg", "version": ""},
        ]
        calls, records, warnings = self.run_manifest(items)
        self.assertEqual(calls, [("photo", _p("s/ov4b.jpg"), _p("s/ov4b-back.jpg"))])
        variants = records[0]["result"]["all_variant_files"]["variants"]
        self.assertEqual([entry["version"] for entry in variants], [None, None])
        self.assertEqual(len([w for w in warnings if "version" in w]), 2)

    def test_an_explicit_version_can_split_a_back_onto_its_own_variant(self):
        items = [{"path": "s/ov3.jpg"}, {"path": "s/ov3-back.jpg", "version": "b"}]
        calls, records, _warnings = self.run_manifest(items)
        self.assertEqual(calls, [("photo", _p("s/ov3.jpg"), _p("s/ov3-back.jpg"))])
        variants = records[0]["result"]["all_variant_files"]["variants"]
        self.assertEqual([entry["version"] for entry in variants], [None, "b"])

    def test_group_unifies_two_names_the_grammar_cannot_relate(self):
        items = [
            {"path": "s/IMG_9001.jpg", "group": "roll7"},
            {"path": "s/DSC_2231.jpg", "group": "roll7", "is_back": True},
        ]
        calls, records, _warnings = self.run_manifest(items)
        self.assertEqual(calls, [("photo", _p("s/IMG_9001.jpg"), _p("s/DSC_2231.jpg"))])
        self.assert_one_group(records, [_p("s/IMG_9001.jpg"), _p("s/DSC_2231.jpg")])

    def test_base_id_is_accepted_as_an_alias_for_group(self):
        items = [
            {"path": "s/aa.jpg", "base_id": "zz"},
            {"path": "s/bb.jpg", "base_id": "zz", "is_back": True},
        ]
        calls, records, _warnings = self.run_manifest(items)
        self.assertEqual(calls, [("photo", _p("s/aa.jpg"), _p("s/bb.jpg"))])
        self.assert_one_group(records, [_p("s/aa.jpg"), _p("s/bb.jpg")])

    def test_group_wins_when_it_disagrees_with_base_id(self):
        items = [
            {"path": "s/cc.jpg", "group": "gg", "base_id": "bb"},
            {"path": "s/dd.jpg", "group": "gg", "is_back": True},
        ]
        calls, records, warnings = self.run_manifest(items)
        self.assertEqual(calls, [("photo", _p("s/cc.jpg"), _p("s/dd.jpg"))])
        self.assert_one_group(records, [_p("s/cc.jpg"), _p("s/dd.jpg")])
        self.assertTrue(any("conflicts with group" in w for w in warnings))

    def test_lua_style_boolean_spellings_are_honored(self):
        for raw_value in (True, "true", "TRUE", "yes", 1):
            with self.subTest(value=raw_value):
                items = [
                    {"path": "s/sb1.jpg"},
                    {"path": "s/sb1x.jpg", "group": "sb1", "is_back": raw_value},
                ]
                calls, _records, _warnings = self.run_manifest(items)
                self.assertEqual(
                    calls, [("photo", _p("s/sb1.jpg"), _p("s/sb1x.jpg"))]
                )

    def test_an_unreadable_flag_is_reported_and_leaves_the_filename_in_charge(self):
        items = [{"path": "s/ub1-back.jpg", "is_back": "maybe"}]
        _calls, records, warnings = self.run_manifest(items)
        self.assertEqual(records[0]["result"]["all_variant_files"]["back"], [_p("s/ub1-back.jpg")])
        self.assertTrue(
            any("unrecognized is_back" in w for w in warnings),
            f"an unreadable flag was dropped without a word: {warnings}",
        )

    def test_a_null_flag_means_unspecified_rather_than_false(self):
        items = [{"path": "s/nb1.jpg"}, {"path": "s/nb1-back.jpg", "is_back": None}]
        calls, records, _warnings = self.run_manifest(items)
        self.assertEqual(calls, [("photo", _p("s/nb1.jpg"), _p("s/nb1-back.jpg"))])
        self.assertEqual(
            records[0]["result"]["all_variant_files"]["back"], [_p("s/nb1-back.jpg")]
        )


class TestPluginContractRegression(ManifestGroupingTestCase):
    """Manifests with no crops, negatives or overrides group exactly as before.

    These are the shapes the Lightroom plug-in actually emits, and this repo
    holds no copy of it, so nothing else here can catch a change to them. The
    expected values were captured by running the pre-Phase-B1 tree (commit
    525e9a6) over the same manifests.
    """

    ORDINARY = [{"path": "s/box3_025.jpg"}, {"path": "s/box3_025-back.jpg"}]
    TWO_VARIANTS = [
        {"path": "s/v1.jpg"},
        {"path": "s/v1-back.jpg"},
        {"path": "s/v1b.jpg"},
        {"path": "s/v1b-back.jpg"},
    ]

    def assert_no_grouping_warnings(self, warnings: list[str]) -> None:
        """Assert an unremarkable manifest produced no grouping diagnostics."""
        self.assertEqual(
            warnings, [], f"an ordinary manifest started warning: {warnings}"
        )

    def test_front_and_back_send_the_same_single_pair_as_before(self):
        calls, records, warnings = self.run_manifest(self.ORDINARY)
        self.assertEqual(
            calls, [("photo", _p("s/box3_025.jpg"), _p("s/box3_025-back.jpg"))]
        )
        self.assert_no_grouping_warnings(warnings)
        self.assertEqual(
            records[0]["result"]["all_variant_files"],
            {
                "front": [_p("s/box3_025.jpg")],
                "back": [_p("s/box3_025-back.jpg")],
                "variants": [
                    {
                        "path": _p("s/box3_025.jpg"),
                        "version": None,
                        "is_back": False,
                        "preferred": False,
                    },
                    {
                        "path": _p("s/box3_025-back.jpg"),
                        "version": None,
                        "is_back": True,
                        "preferred": False,
                    },
                ],
                "all": [_p("s/box3_025.jpg"), _p("s/box3_025-back.jpg")],
            },
        )

    def test_front_and_back_group_the_same_under_pair(self):
        # The default and ``pair`` agree on any group with no variant letters,
        # which is the overwhelmingly common shape, so a plug-in manifest keeps
        # its group id whichever of the two is selected.
        calls, _records, warnings = self.run_manifest(
            self.ORDINARY, group_by=utils.GROUP_BY_PAIR
        )
        self.assertEqual(
            calls, [("photo", _p("s/box3_025.jpg"), _p("s/box3_025-back.jpg"))]
        )
        self.assert_no_grouping_warnings(warnings)

    def test_two_variants_now_travel_together_rather_than_leaving_two_scans_unsent(self):
        # CHANGED IN C1, deliberately. 525e9a6 and every tree through B2 sent
        # ``photo(v1.jpg, v1-back.jpg)`` and never showed the model v1b.jpg or
        # v1b-back.jpg at all, though both were recorded. Retiring the primary
        # sends all four. Same one call, four images instead of two.
        calls, records, warnings = self.run_manifest(self.TWO_VARIANTS)
        self.assertEqual(
            calls,
            [(
                "front_back",
                (_p("s/v1b.jpg"), _p("s/v1.jpg")),
                (_p("s/v1b-back.jpg"), _p("s/v1-back.jpg")),
            )],
        )
        self.assert_no_grouping_warnings(warnings)
        variant_map = records[0]["result"]["all_variant_files"]
        self.assertEqual(variant_map["front"], [_p("s/v1.jpg"), _p("s/v1b.jpg")])
        self.assertEqual(
            variant_map["back"], [_p("s/v1-back.jpg"), _p("s/v1b-back.jpg")]
        )
        for key in ("crops", "negatives", "pages"):
            self.assertNotIn(key, variant_map)

    def test_pair_analyzes_each_rescan_on_its_own(self):
        # The value that comes closest to the retired default, and still does
        # not reproduce it: one call per rescan rather than one for the group.
        # No value of the axis brings the primary-pair payload back.
        calls, _records, warnings = self.run_manifest(
            self.TWO_VARIANTS, group_by=utils.GROUP_BY_PAIR
        )
        self.assertEqual(
            calls,
            [
                ("photo", _p("s/v1.jpg"), _p("s/v1-back.jpg")),
                ("photo", _p("s/v1b.jpg"), _p("s/v1b-back.jpg")),
            ],
        )
        self.assert_no_grouping_warnings(warnings)

    def test_every_listed_file_still_gets_exactly_one_record(self):
        for group_by in utils.GROUP_BY_VALUES:
            with self.subTest(group_by=group_by):
                _calls, records, _warnings = self.run_manifest(
                    self.TWO_VARIANTS, group_by=group_by
                )
                self.assertEqual(
                    sorted(rec["path"] for rec in records),
                    sorted(_p(item["path"]) for item in self.TWO_VARIANTS),
                )
                self.assertEqual({rec["status"] for rec in records}, {"ok"})


class TestPartialGroupFailure(ManifestGroupingTestCase):
    """A group that fails part-way through its per-file loop keeps what it banked.

    The loop banks one record at a time, so a group can raise with some of its
    files already recorded and their ``ok`` lines already on the stream. Handing
    the whole group to the error path then keys those paths under both
    ``results`` and ``errors`` and puts two contradicting lines on the stream for
    one file, which a consumer that applies ``results`` and then reports
    ``errors`` acts on twice.

    The failure is injected at a fixed point rather than provoked, so the test
    cannot go vacuous; in the wild it is anything the per-file loop touches that
    grouping does not, inline metadata carrying a non-string caption being the
    reachable one.
    """

    ITEMS = ({"path": "s/pf1.jpg"}, {"path": "s/pf1-back.jpg"})

    def _run_failing_on_the_second_file(self) -> tuple[dict, list[dict]]:
        """Process a two-file group whose second per-file patch build raises.

        Returns:
            The stream's aggregate and the NDJSON records it emitted.
        """
        lines: list[str] = []
        rec = _RecordingAnalyzers()
        with self.assertLogs("photokin.core", level="ERROR"):
            with patch("photokin.core.analyze_photo", rec.photo), patch(
                "photokin.core.build_canonical_patch",
                side_effect=[({}, {}), RuntimeError("patch build failed")],
            ):
                aggregate = core.process_manifest_stream(
                    manifest={"items": list(self.ITEMS)},
                    cfg=utils.Config(dry_run=True),
                    ndjson_writer=lines.append,
                )
        return aggregate, [json.loads(line) for line in lines]

    def test_a_banked_record_is_not_also_reported_as_an_error(self):
        aggregate, _records = self._run_failing_on_the_second_file()
        self.assertEqual(sorted(aggregate["results"]), [_p("s/pf1.jpg")])
        self.assertEqual(sorted(aggregate["errors"]), [_p("s/pf1-back.jpg")])
        self.assertEqual(
            sorted(set(aggregate["results"]) & set(aggregate["errors"])),
            [],
            "a path was reported as both succeeded and failed",
        )

    def test_the_stream_carries_exactly_one_line_per_path(self):
        _aggregate, records = self._run_failing_on_the_second_file()
        self.assertEqual(
            [(rec["path"], rec["status"]) for rec in records],
            [(_p("s/pf1.jpg"), "ok"), (_p("s/pf1-back.jpg"), "error")],
        )

    def test_every_file_of_the_group_is_still_accounted_for(self):
        aggregate, _records = self._run_failing_on_the_second_file()
        self.assertEqual(
            sorted(set(aggregate["results"]) | set(aggregate["errors"])),
            sorted(_p(item["path"]) for item in self.ITEMS),
        )


class TestPairKeyCannotCollide(ManifestGroupingTestCase):
    """``pair`` builds its bucket key by joining, so the join must be injective.

    ``|`` was chosen because it is illegal in a Windows filename -- true of the
    keys the grammar derives there, and of nothing else. A manifest may state
    ``group`` outright, which the README documents for names the grammar cannot
    parse at all and which ``_manifest_group_override`` accepts verbatim, and
    POSIX filenames take the character happily. An unescaped join therefore lets
    ``group="album|b"`` and a filename-derived ``("album", "b")`` spell one key:
    two unrelated objects in one model call, sharing one caption, date, location
    and keyword set, and writing them to both files under one changeset
    ``group_id``.

    Escaping by doubling the separator does not fix it, which is why the check
    below is a brute force over the whole cross product rather than a list of
    shapes somebody thought of. Doubling leaves a half's trailing run touching
    the joining separator, and the two merge into one run that can be split more
    than one way.
    """

    #: Small but exhaustive in kind: it holds the separator, the escape
    #: character, the empty half, and each of them leading, trailing, doubled
    #: and adjacent to the other. Both halves are escaped character by
    #: character, so a scheme injective here cannot be defeated by a longer
    #: string built from the same characters.
    ALPHABET = ("", "a", "|", "\\", "a|", "|a", "\\|", "||")

    #: Two objects that spell the same pair key under a broken join. Each is a
    #: reachable form: an explicit key on any platform, a derived key on the
    #: platforms where the separator is a legal filename character, and the
    #: boundary pair that defeats separator doubling specifically.
    COLLIDING = (
        (
            "an explicit group override",
            [
                {"path": "s/wedding_1965.jpg", "group": "album|b"},
                {"path": "s/reunion_010b.jpg", "group": "album"},
            ],
        ),
        (
            "a posix filename holding the separator",
            [{"path": "s/box3_025|b.jpg"}, {"path": "s/box3_025b.jpg"}],
        ),
        (
            "a separator run touching the join",
            [
                {"path": "s/wedding.jpg", "group": "a|", "version": "a"},
                {"path": "s/funeral.jpg", "group": "a", "version": "|a"},
            ],
        ),
    )

    def test_the_key_is_injective_over_the_whole_cross_product(self):
        # Every ``(group_key, version)`` the alphabet can spell, including the
        # ``None`` version an entry with no variant letter carries. Two pairs
        # sharing a key are two objects sharing one analysis and one changeset
        # ``group_id``.
        source: dict[str, tuple[str, str | None]] = {}
        collisions: list[str] = []
        for group_key, version in itertools.product(
            self.ALPHABET, self.ALPHABET + (None,)
        ):
            key = core._pair_bucket_key(group_key, version)
            if key in source:
                collisions.append(
                    f"{source[key]!r} and {(group_key, version)!r} both spell {key!r}"
                )
            else:
                source[key] = (group_key, version)
        self.assertEqual(
            collisions,
            [],
            f"the pair key is not injective over {self.ALPHABET!r}",
        )
        self.assertEqual(
            len(source), len(self.ALPHABET) * (len(self.ALPHABET) + 1)
        )

    def test_a_trailing_separator_does_not_merge_with_the_join(self):
        # The doubling scheme's own failure, named so the regression is obvious:
        # it wrote ``a|`` as ``a||`` and then joined on ``|``, so the boundary
        # run was indistinguishable from an escaped separator opening the second
        # half, and both pairs spelled ``a|||a``.
        self.assertNotEqual(
            core._pair_bucket_key("a|", "a"),
            core._pair_bucket_key("a", "|a"),
            "('a|', 'a') and ('a', '|a') spell one key",
        )

    def test_two_objects_that_spell_one_key_stay_two_buckets(self):
        for label, items in self.COLLIDING:
            for group_by in utils.GROUP_BY_VALUES:
                with self.subTest(label, group_by=group_by):
                    buckets = core.build_manifest_buckets(items, group_by=group_by)
                    self.assertEqual(
                        sorted(
                            sorted(entry["path"] for entry in entries)
                            for entries in buckets.values()
                        ),
                        sorted([[_p(item["path"])] for item in items]),
                        "two unrelated objects were merged into one bucket, so "
                        "they share one analysis and one changeset group_id",
                    )

    def test_they_are_two_model_calls_rather_than_one_merged_one(self):
        for label, items in self.COLLIDING:
            with self.subTest(label):
                calls, _records, _warnings = self.run_manifest(
                    items, group_by=utils.GROUP_BY_PAIR
                )
                self.assertEqual(
                    sorted(_labelled_payload(calls)),
                    sorted(("front", _p(item["path"])) for item in items),
                    "the two files were sent in one call as two scans of one print",
                )
                self.assertEqual(len(calls), 2)

    def test_an_ordinary_group_keeps_the_key_object_gives_it(self):
        # The escape must not move the changeset ``group_id`` of a key that
        # holds no separator, which is every key the grammar derives on Windows.
        items = [
            {"path": "s/box3_025.jpg"},
            {"path": "s/box3_025-back.jpg"},
            {"path": "s/box3_025b.jpg"},
        ]
        self.assertEqual(
            list(core.build_manifest_buckets(items, group_by=utils.GROUP_BY_OBJECT)),
            ["box3_025"],
        )
        self.assertEqual(
            list(core.build_manifest_buckets(items, group_by=utils.GROUP_BY_PAIR)),
            ["box3_025", "box3_025|b"],
        )


class TestPartMarkersOnlyStripWhatTheGroupApplies(ManifestGroupingTestCase):
    """A marker is removed to undo a leak, so there has to be a leak to undo.

    ``merge_original_sources`` merges the group's metadata keywords into every
    record, so the marker naming one file would otherwise land on all of them.
    That is the entire reason the fan-out strips markers -- and it means a
    marker no file in the group carries cannot have leaked from anywhere: it is
    a keyword the caller applied by hand, and removing it makes the emitted
    record drop it and the changeset propose deleting it from the catalog.

    The same holds one level down. Whether a marker leaked is a property of the
    file, not of the group: a print carrying a hand-tagged "Negative" in its own
    metadata keeps it however many of its siblings really are negatives, because
    the keyword was on the file before anything was merged into it.
    """

    def keywords_by_name(
        self, items: list[dict], *, group_by: str = utils.GROUP_BY_OBJECT
    ) -> dict[str, list[str]]:
        """Return ``{basename: emitted keywords}`` for one run over *items*."""
        _calls, records, _warnings = self.run_manifest(items, group_by=group_by)
        return {
            os.path.basename(rec["path"]): rec["result"]["keywords"] for rec in records
        }

    def removals_by_name(self, items: list[dict]) -> dict[str, list[str]]:
        """Return ``{basename: proposed keywords_remove}`` for one run.

        The emitted record and the changeset are two separate statements: the
        record is what the file becomes, the changeset is the instruction sent
        to the catalog. A keyword can survive in the first and still be proposed
        for deletion in the second, so both are asserted.
        """
        changeset: list[str] = []
        self.run_manifest(items, changeset_writer=changeset.append)
        return {
            os.path.basename(doc["path"]): doc["proposed_changes"]["keywords_remove"]
            for doc in map(json.loads, changeset)
        }

    def test_a_hand_applied_marker_survives_a_group_holding_no_such_part(self):
        # A print scanned from a negative and tagged "Negative" in Lightroom, or
        # a scan somebody filed under "Back" -- neither name is one the grammar
        # reads as a part, and there is no sibling for a marker to leak from.
        for marker in sorted(utils.PART_MARKER_KEYWORDS):
            with self.subTest(marker=marker):
                spelling = marker.title()
                keywords = self.keywords_by_name(
                    [{
                        "path": "s/scan001.jpg",
                        "metadata": {"keywords": [spelling, "Family"]},
                    }]
                )
                self.assertEqual(
                    keywords["scan001.jpg"],
                    [spelling, "Family"],
                    f"{spelling!r} was the caller's own keyword and the run "
                    "proposed deleting it",
                )

    def test_the_marker_still_comes_off_the_files_it_does_not_describe(self):
        # The leak the strip exists for, and the reason it cannot simply go:
        # the marker rides the group's metadata onto the print beside it.
        for marker, sibling in (("negative", "s/m1-negative.jpg"), ("back", "s/m2-back.jpg")):
            with self.subTest(marker=marker):
                print_path = sibling.replace(f"-{marker}", "")
                keywords = self.keywords_by_name([
                    {"path": print_path},
                    {"path": sibling, "metadata": {"keywords": [marker.title(), "Family"]}},
                ])
                self.assertNotIn(
                    marker.title(),
                    keywords[os.path.basename(_p(print_path))],
                    f"the sibling's {marker!r} marker leaked onto the print",
                )
                self.assertIn(
                    "Family",
                    keywords[os.path.basename(_p(print_path))],
                    "the rest of the group's metadata keywords must still arrive",
                )
                self.assertIn(
                    marker,
                    [kw.lower() for kw in keywords[os.path.basename(_p(sibling))]],
                    "the file the marker describes must keep it",
                )

    def test_the_file_it_describes_keeps_it_at_every_granularity(self):
        for group_by in utils.GROUP_BY_VALUES:
            with self.subTest(group_by=group_by):
                keywords = self.keywords_by_name(
                    [{"path": "s/m3.jpg"}, {"path": "s/m3-negative.jpg"}],
                    group_by=group_by,
                )
                self.assertIn("negative", keywords["m3-negative.jpg"])
                self.assertNotIn("negative", keywords["m3.jpg"])

    def test_a_hand_applied_marker_survives_a_sibling_that_is_that_part(self):
        # The strip set is the group's markers, so a group that really does hold
        # a negative -- or a back -- used to take the keyword off every other
        # file in it, including the one that had carried it all along. Which
        # markers a file received from a sibling is decided per file, and the
        # answer is read from the file's own metadata before the merge.
        for marker, sibling in (("negative", "s/m4-negative.jpg"), ("back", "s/m5-back.jpg")):
            with self.subTest(marker=marker):
                spelling = marker.title()
                print_path = sibling.replace(f"-{marker}", "")
                items = [
                    {"path": print_path, "metadata": {"keywords": [spelling, "Trip"]}},
                    {"path": sibling},
                ]
                name = os.path.basename(_p(print_path))

                keywords = self.keywords_by_name(items)

                self.assertIn(
                    spelling,
                    keywords[name],
                    f"{spelling!r} was this file's own keyword and the group "
                    f"holding a real {marker} destroyed it",
                )
                self.assertEqual(
                    [kw for kw in keywords[name] if kw.strip().lower() == marker],
                    [spelling],
                    f"{marker!r} was re-added beside the caller's own spelling",
                )
                self.assertEqual(
                    self.removals_by_name(items)[name],
                    [],
                    "the changeset proposed deleting the caller's keyword from "
                    "the catalog",
                )
                self.assertIn(
                    marker,
                    [kw.lower() for kw in keywords[os.path.basename(_p(sibling))]],
                    "the file the marker describes must still keep it",
                )


class TestHydrationFailureSuppressesWrites(ManifestGroupingTestCase):
    """A file ``-r`` asked for and could not read gets no proposed writes.

    Unread is not empty: the changeset diffs the model's answer against the
    file's before-snapshot, and for a file whose read failed that snapshot is
    emptiness -- every proposed write would overwrite whatever the file really
    holds. The hydrator marks such items with
    ``utils.HYDRATION_FAILED_KEY`` and the emitter proposes nothing for them,
    while their unaffected siblings keep their writes.
    """

    def test_a_marked_file_gets_an_empty_proposed_changes(self):
        changeset: list[str] = []
        items = [
            {"path": "s/scan001.jpg", utils.HYDRATION_FAILED_KEY: True},
            {"path": "s/scan002.jpg"},
        ]
        _calls, _records, warnings = self.run_manifest(
            items, model_keywords=["Family"], changeset_writer=changeset.append
        )

        docs = {os.path.basename(d["path"]): d for d in map(json.loads, changeset)}
        self.assertEqual(
            docs["scan001.jpg"]["proposed_changes"],
            {"set": {}, "keywords_add": [], "keywords_remove": []},
        )
        self.assertNotEqual(
            docs["scan002.jpg"]["proposed_changes"]["keywords_add"],
            [],
            "the unmarked sibling must still get its writes",
        )
        self.assertTrue(
            any("could not read this file" in w for w in warnings),
            "the suppression must be announced, not silent",
        )


if __name__ == "__main__":
    unittest.main()
