"""Tests for the rename-mode planner, ``photokin.rename.plan_rename``.

Pure-function tests only: every case here constructs an in-memory listing
and reads the returned plan dict back -- nothing touches a filesystem. The
worked examples in ``docs/rename-mode.md`` sections 1, 4.5 and 4.7 are
reproduced verbatim (as data, not paraphrased), because those are the
plan's own binding examples; everything else exercises one rule at a time,
per section 9's enumeration.
"""

import os
import unittest
from datetime import date
from unittest import mock

from photokin.rename import (
    DEFAULT_COMPANION_EXTENSIONS,
    RenameItem,
    _MissingDate,
    _natural_sort_key,
    _parse_photo_date,
    _PhotoDate,
    _render_date_format,
    _render_template,
    _tokenize_prefix_template,
    plan_rename,
)

_FOLDER = "/scans"


def _path(name: str) -> str:
    return os.path.normpath(f"{_FOLDER}/{name}")


def _plan(
    names,
    prefix,
    *,
    digits=3,
    disk_files=None,
    dates=None,
    orders=None,
    is_backs=None,
    versions=None,
    **kwargs,
):
    """Build a :func:`plan_rename` call from a bare list of filenames.

    ``dates``/``orders``/``is_backs``/``versions``, when given, are dicts
    keyed by filename overriding that one item's field; everything else
    defaults to "nothing known".
    """
    dates = dates or {}
    orders = orders or {}
    is_backs = is_backs or {}
    versions = versions or {}
    items = []
    for name in names:
        metadata = None
        if name in dates:
            metadata = {"EXIF:DateTimeOriginal": dates[name]}
        items.append(
            RenameItem(
                path=_path(name),
                metadata=metadata,
                order=orders.get(name),
                is_back=is_backs.get(name),
                version=versions.get(name),
            )
        )
    files = disk_files if disk_files is not None else [_path(name) for name in names]
    return plan_rename(
        folder=_FOLDER,
        disk_files=files,
        items=items,
        prefix_template=prefix,
        digits=digits,
        run_id="test-run",
        **kwargs,
    )


def _entry_for(plan, name):
    (entry,) = [e for e in plan["entries"] if e["path"] == _path(name)]
    return entry


class BriefExampleTests(unittest.TestCase):
    """Section 1's example, verbatim."""

    def test_brief_example(self) -> None:
        plan = _plan(
            ["file102.tif", "file105.tif", "file105b.tif", "file105b-back.tif"],
            "newname",
        )
        self.assertEqual(plan["errors"], [])
        self.assertEqual(_entry_for(plan, "file102.tif")["target"], "newname-001.tif")
        self.assertEqual(_entry_for(plan, "file105.tif")["target"], "newname-002.tif")
        self.assertEqual(_entry_for(plan, "file105b.tif")["target"], "newname-002b.tif")
        self.assertEqual(
            _entry_for(plan, "file105b-back.tif")["target"], "newname-002b-back.tif"
        )


class NumberingRestartTests(unittest.TestCase):
    """Section 4.5's example: interleaved dates restart their buckets independently."""

    def test_interleaved_dates_restart_independently(self) -> None:
        plan = _plan(
            ["scan_001.tif", "scan_002.tif", "scan_002-back.tif", "scan_003.tif", "scan_004.tif"],
            "{date:yymmdd}-bag-woodbury",
            dates={
                "scan_001.tif": "1952:06:01 00:00:00",
                "scan_002.tif": "1952:06:01 00:00:00",
                "scan_003.tif": "1961:09:14 00:00:00",
                "scan_004.tif": "1952:06:01 00:00:00",
            },
        )
        self.assertEqual(plan["errors"], [])
        self.assertEqual(
            _entry_for(plan, "scan_001.tif")["target"], "520601-bag-woodbury-001.tif"
        )
        self.assertEqual(
            _entry_for(plan, "scan_002.tif")["target"], "520601-bag-woodbury-002.tif"
        )
        self.assertEqual(
            _entry_for(plan, "scan_002-back.tif")["target"],
            "520601-bag-woodbury-002-back.tif",
        )
        self.assertEqual(
            _entry_for(plan, "scan_003.tif")["target"], "610914-bag-woodbury-001.tif"
        )
        self.assertEqual(
            _entry_for(plan, "scan_004.tif")["target"], "520601-bag-woodbury-003.tif"
        )


class FullerWorkedExampleTests(unittest.TestCase):
    """Section 4.7's example, verbatim, including the companion."""

    def test_fuller_worked_example(self) -> None:
        names = [
            "box3_017-b.tif",
            "box3_017.jpg",
            "box3_017.tif",
            "box3_017_back.tif",
            "box3_017b-back-crop.tif",
            "box3_020-page1.tif",
            "box3_020-page2.tif",
            "reunion.tif",
        ]
        disk_files = [_path(n) for n in names] + [_path("reunion.md")]
        plan = _plan(names, "bw", disk_files=disk_files)

        self.assertEqual(plan["errors"], [])
        self.assertEqual(_entry_for(plan, "box3_017-b.tif")["target"], "bw-001b.tif")
        self.assertEqual(
            _entry_for(plan, "box3_017-b.tif")["notes"], ["variant form normalized"]
        )
        self.assertEqual(_entry_for(plan, "box3_017.jpg")["target"], "bw-001.jpg")
        self.assertEqual(_entry_for(plan, "box3_017.tif")["target"], "bw-001.tif")
        # The .tif/.jpg pair shares one target stem.
        self.assertEqual(
            _entry_for(plan, "box3_017.jpg")["target_stem"],
            _entry_for(plan, "box3_017.tif")["target_stem"],
        )
        self.assertEqual(_entry_for(plan, "box3_017_back.tif")["target"], "bw-001-back.tif")
        self.assertEqual(
            _entry_for(plan, "box3_017_back.tif")["notes"], ["part separator normalized"]
        )
        self.assertEqual(
            _entry_for(plan, "box3_017b-back-crop.tif")["target"], "bw-001b-back-crop.tif"
        )
        self.assertEqual(
            _entry_for(plan, "box3_020-page1.tif")["target"], "bw-002-page1.tif"
        )
        self.assertEqual(
            _entry_for(plan, "box3_020-page2.tif")["target"], "bw-002-page2.tif"
        )
        self.assertEqual(_entry_for(plan, "reunion.tif")["target"], "bw-003.tif")

        reunion_entry = _entry_for(plan, "reunion.tif")
        self.assertEqual(len(reunion_entry["companions"]), 1)
        self.assertEqual(reunion_entry["companions"][0]["target"], "bw-003.md")
        self.assertEqual(reunion_entry["companions"][0]["path"], _path("reunion.md"))


class OrderTests(unittest.TestCase):
    def test_unnumbered_file_takes_its_alphabetical_place(self) -> None:
        plan = _plan(["apple.tif", "banana5.tif"], "x")
        self.assertEqual(_entry_for(plan, "apple.tif")["target"], "x-001.tif")
        self.assertEqual(_entry_for(plan, "banana5.tif")["target"], "x-002.tif")
        self.assertEqual(plan["order"], "name")

    def test_explicit_order_overrides_name_order(self) -> None:
        # Alphabetically "alpha" precedes "zeta", but an explicit order reverses them.
        plan = _plan(
            ["alpha.tif", "zeta.tif"],
            "x",
            orders={"alpha.tif": 2, "zeta.tif": 1},
        )
        self.assertEqual(plan["order"], "manifest")
        self.assertEqual(_entry_for(plan, "zeta.tif")["target"], "x-001.tif")
        self.assertEqual(_entry_for(plan, "alpha.tif")["target"], "x-002.tif")

    def test_natural_order_compares_digit_runs_numerically(self) -> None:
        plan = _plan(["file9.tif", "file10.tif"], "x", order_mode="natural")
        self.assertEqual(plan["order"], "natural")
        self.assertEqual(_entry_for(plan, "file9.tif")["target"], "x-001.tif")
        self.assertEqual(_entry_for(plan, "file10.tif")["target"], "x-002.tif")


class NormalizationTests(unittest.TestCase):
    """Each normalization alone (section 9)."""

    def test_dashed_variant_normalized(self) -> None:
        plan = _plan(["box3_017-b.tif"], "x")
        entry = _entry_for(plan, "box3_017-b.tif")
        self.assertEqual(entry["target"], "x-001b.tif")
        self.assertEqual(entry["notes"], ["variant form normalized"])

    def test_underscore_before_back_normalized(self) -> None:
        plan = _plan(["box3_017_back.tif"], "x")
        entry = _entry_for(plan, "box3_017_back.tif")
        self.assertEqual(entry["target"], "x-001-back.tif")
        self.assertEqual(entry["notes"], ["part separator normalized"])

    def test_dot_before_back_normalized(self) -> None:
        plan = _plan(["box3_017.back.tif"], "x")
        entry = _entry_for(plan, "box3_017.back.tif")
        self.assertEqual(entry["target"], "x-001-back.tif")
        self.assertEqual(entry["notes"], ["part separator normalized"])

    def test_page_suffix_retained_without_a_note(self) -> None:
        plan = _plan(["box3_020-page3.tif"], "x")
        entry = _entry_for(plan, "box3_020-page3.tif")
        self.assertEqual(entry["target"], "x-001-page3.tif")
        self.assertEqual(entry["notes"], [])

    def test_negative_underscore_normalized(self) -> None:
        plan = _plan(["box3_017_negative.tif"], "x")
        entry = _entry_for(plan, "box3_017_negative.tif")
        self.assertEqual(entry["target"], "x-001-negative.tif")
        self.assertEqual(entry["notes"], ["part separator normalized"])

    def test_crop_stacks_with_normalized_part(self) -> None:
        plan = _plan(["box3_017_back_crop.tif"], "x")
        entry = _entry_for(plan, "box3_017_back_crop.tif")
        self.assertEqual(entry["target"], "x-001-back-crop.tif")
        self.assertEqual(entry["notes"], ["part separator normalized"])


class PairAndCompanionTests(unittest.TestCase):
    def test_tif_jpg_pair_shares_target_stem(self) -> None:
        plan = _plan(["photo.tif", "photo.jpg"], "x")
        tif_entry = _entry_for(plan, "photo.tif")
        jpg_entry = _entry_for(plan, "photo.jpg")
        self.assertEqual(tif_entry["target_stem"], jpg_entry["target_stem"])
        self.assertEqual(tif_entry["target"], "x-001.tif")
        self.assertEqual(jpg_entry["target"], "x-001.jpg")

    def test_companions_listed_and_unlisted_extension_left_behind(self) -> None:
        disk_files = [_path("photo.tif"), _path("photo.md"), _path("photo.pdf")]
        plan = _plan(["photo.tif"], "x", disk_files=disk_files)
        entry = _entry_for(plan, "photo.tif")
        self.assertEqual(len(entry["companions"]), 1)
        self.assertEqual(entry["companions"][0]["path"], _path("photo.md"))
        self.assertEqual(entry["companions"][0]["target"], "x-001.md")
        self.assertEqual(
            plan["left_behind"], [{"path": _path("photo.pdf"), "reason": "extension outside companion set"}]
        )
        self.assertTrue(
            any("left behind" in w for w in plan["warnings"]),
            plan["warnings"],
        )

    def test_default_companion_extensions(self) -> None:
        self.assertEqual(DEFAULT_COMPANION_EXTENSIONS, frozenset({".md", ".json", ".xmp", ".txt"}))


class OverrideTests(unittest.TestCase):
    def test_is_back_true_materializes_back_suffix(self) -> None:
        plan = _plan(["photo1.tif"], "x", is_backs={"photo1.tif": True})
        entry = _entry_for(plan, "photo1.tif")
        self.assertEqual(entry["target"], "x-001-back.tif")
        self.assertEqual(entry["part"], "back")

    def test_is_back_false_promotes_back_to_front(self) -> None:
        plan = _plan(["photo2-back.tif"], "x", is_backs={"photo2-back.tif": False})
        entry = _entry_for(plan, "photo2-back.tif")
        self.assertEqual(entry["target"], "x-001-front.tif")
        self.assertEqual(entry["part"], "front")

    def test_version_override_materializes_variant_letter(self) -> None:
        plan = _plan(["photo3.tif"], "x", versions={"photo3.tif": "c"})
        entry = _entry_for(plan, "photo3.tif")
        self.assertEqual(entry["target"], "x-001c.tif")
        self.assertEqual(entry["variant"], "c")

    def test_version_override_does_not_trigger_normalization_note(self) -> None:
        # box3_017-b.tif's dashed variant would normally earn a note; an
        # explicit override replaces it outright, so the note (which is
        # about the ORIGINAL filename's spelling) does not apply.
        plan = _plan(["box3_017-b.tif"], "x", versions={"box3_017-b.tif": "q"})
        entry = _entry_for(plan, "box3_017-b.tif")
        self.assertEqual(entry["variant"], "q")
        self.assertNotIn("variant form normalized", entry["notes"])


class ValidationErrorTests(unittest.TestCase):
    def test_bystander_collision(self) -> None:
        disk_files = [_path("photoA.tif"), _path("x-001.tif")]
        plan = _plan(["photoA.tif"], "x", disk_files=disk_files)
        self.assertTrue(
            any("bystander" in e for e in plan["errors"]), plan["errors"]
        )

    def test_duplicate_targets_differing_only_in_case(self) -> None:
        plan = _plan(["foo.TIF", "foo.tif"], "x")
        self.assertTrue(
            any("duplicate target" in e for e in plan["errors"]), plan["errors"]
        )

    def test_digit_overflow(self) -> None:
        names = [f"item{i}.tif" for i in range(10)]
        plan = _plan(names, "x", digits=1)
        self.assertTrue(
            any("digit" in e or "needs more than" in e for e in plan["errors"]),
            plan["errors"],
        )

    def test_missing_date_without_undated_is_an_error(self) -> None:
        plan = _plan(["nodateshot.tif"], "{date}")
        self.assertTrue(any("date" in e for e in plan["errors"]), plan["errors"])
        entry = _entry_for(plan, "nodateshot.tif")
        self.assertIsNone(entry["target"])

    def test_missing_date_with_undated_literal_succeeds(self) -> None:
        plan = _plan(["nodateshot.tif"], "{date}", undated_literal="undated")
        self.assertEqual(plan["errors"], [])
        entry = _entry_for(plan, "nodateshot.tif")
        self.assertEqual(entry["target"], "undated-001.tif")

    def test_empty_rendered_prefix_is_an_error(self) -> None:
        plan = _plan(["105.tif"], "{orig}")
        self.assertTrue(
            any("empty" in e for e in plan["errors"]), plan["errors"]
        )

    def test_illegal_template_character_is_an_error(self) -> None:
        plan = _plan(["photo.tif"], "bad/prefix")
        self.assertTrue(plan["errors"])
        self.assertEqual(plan["entries"], [])


class ManifestCaseMismatchTests(unittest.TestCase):
    """A manifest item whose path case differs from what ``os.scandir``
    reports for the identical file must not become its own bystander.

    Answered with real filesystem identity, not by folding case: on a
    case-sensitive volume ``SAME.TIF`` and ``same.tif`` genuinely are two
    files, and folding would wrongly merge them. ``os.path.normcase`` cannot
    answer it either -- it is a no-op on POSIX, which is what made this fail
    on Linux while passing on Windows.
    """

    def test_case_mismatched_manifest_path_is_not_its_own_bystander(self) -> None:
        disk_path = _path("same-001.tif")
        manifest_path = _path("SAME-001.TIF")  # same file, spelled differently
        plan = plan_rename(
            folder=_FOLDER,
            disk_files=[disk_path],
            items=[RenameItem(path=manifest_path)],
            prefix_template="same",
            digits=3,
            run_id="test-run",
        )
        self.assertEqual(plan["errors"], [])


class CompanionLengthTests(unittest.TestCase):
    """The 255-byte rule (4.6) must measure a companion's own target, not
    just the image's -- a longer companion extension can push the companion
    past the limit even when the image's target is within it."""

    def test_long_companion_extension_pushes_past_255_bytes_alone(self) -> None:
        # 247-byte prefix: the .tif image target lands at exactly 255 bytes
        # (passes); the .json companion's longer extension pushes its own
        # target to 256.
        prefix = "p" * 247
        disk_files = [_path("photo.tif"), _path("photo.json")]
        plan = _plan(["photo.tif"], prefix, disk_files=disk_files)
        entry = _entry_for(plan, "photo.tif")
        self.assertEqual(len(entry["target"].encode("utf-8")), 255)
        companion_target = entry["companions"][0]["target"]
        self.assertEqual(len(companion_target.encode("utf-8")), 256)
        self.assertTrue(
            any(companion_target in e and "255 bytes" in e for e in plan["errors"]),
            plan["errors"],
        )


class VariantPageSuffixLengthTests(unittest.TestCase):
    """The "variant form normalized" note must not be derived from a suffix
    length rebuilt out of the parsed page number, since ``int()`` drops
    leading zeros -- a zero-padded page number makes that length wrong."""

    def test_digit_adjacent_variant_before_zero_padded_page_gets_no_false_note(
        self,
    ) -> None:
        # The variant "b" is already digit-adjacent (canonical); nothing
        # here should be reported as normalized.
        plan = _plan(["y5b-page007.tif"], "x")
        entry = _entry_for(plan, "y5b-page007.tif")
        self.assertNotIn("variant form normalized", entry["notes"])

    def test_dashed_variant_before_zero_padded_page_gets_the_note(self) -> None:
        # The variant "b" really is written after a "-" here, and really is
        # rewritten digit-adjacent in the target -- the note must fire.
        plan = _plan(["y-b-page07.tif"], "x")
        entry = _entry_for(plan, "y-b-page07.tif")
        self.assertIn("variant form normalized", entry["notes"])


class LeadingDashPrefixTests(unittest.TestCase):
    """``{folder}`` at a filesystem root renders empty, so a template like
    ``"{folder}-bag"`` must not be allowed to render a hostile leading-dash
    prefix (``"-bag"``) -- a leading ``-`` is trimmed the same way a
    trailing one already is.

    ``os.sep`` alone -- not a hardcoded ``"C:\\\\"`` -- is what drives a
    genuinely empty ``{folder}`` render portably: ``os.path.basename`` of
    the root is ``""`` on POSIX (``/``) exactly as it is at a Windows drive
    root (``os.path.abspath(os.sep)``, e.g. ``"D:\\\\"``), so this exercises
    the same rule on either platform instead of failing on Linux the way a
    literal drive letter does (CI-3)."""

    def test_folder_token_at_filesystem_root_does_not_leave_a_leading_dash(self) -> None:
        folder = os.path.abspath(os.sep)
        item_path = os.path.normpath(os.path.join(folder, "photo.tif"))
        plan = plan_rename(
            folder=folder,
            disk_files=[item_path],
            items=[RenameItem(path=item_path)],
            prefix_template="{folder}-bag",
            digits=3,
            run_id="test-run",
        )
        self.assertEqual(plan["errors"], [])
        (entry,) = plan["entries"]
        self.assertEqual(entry["target"], "bag-001.tif")
        self.assertFalse(entry["target"].startswith("-"))
        self.assertTrue(any("trimmed" in w for w in plan["warnings"]), plan["warnings"])


class EmptyBaseIdWarningTests(unittest.TestCase):
    """A filename that is only a part suffix parses to an empty base_id, so
    unrelated files sharing that empty key silently merge into one group.
    Grouping itself is unchanged; a warning naming the files is required."""

    def test_two_part_only_files_merging_under_empty_base_id_warns(self) -> None:
        plan = _plan(["_back.tif", "_front.tif"], "x")
        self.assertTrue(
            any("empty base id" in w for w in plan["warnings"]), plan["warnings"]
        )
        back_entry = _entry_for(plan, "_back.tif")
        front_entry = _entry_for(plan, "_front.tif")
        self.assertEqual(back_entry["target"], "x-001-back.tif")
        self.assertEqual(front_entry["target"], "x-001-front.tif")


class IdempotencyTests(unittest.TestCase):
    def test_replanning_already_clean_names_changes_nothing(self) -> None:
        first = _plan(
            ["box3_017-b.tif", "box3_017_back.tif", "reunion.tif"], "bw"
        )
        self.assertEqual(first["errors"], [])
        clean_names = [e["target"] for e in first["entries"]]

        second = _plan(clean_names, "bw")
        self.assertEqual(second["errors"], [])
        self.assertEqual(second["warnings"], [])
        for entry in second["entries"]:
            self.assertFalse(entry["changed"], entry)


class RoundTripThroughPlannerTests(unittest.TestCase):
    """The parse/render round trip, exercised through the planner itself
    (not just utils' own functions, which test_rename_grammar.py already
    covers combinatorially)."""

    def test_planned_targets_parse_back_to_the_same_tail(self) -> None:
        from photokin.utils import parse_media_filename

        names = [
            "a.tif",
            "b-c.tif",
            "d_back.tif",
            "e-page4.tif",
            "f-g-back-crop.tif",
        ]
        plan = _plan(names, "prefix")
        self.assertEqual(plan["errors"], [])
        for entry in plan["entries"]:
            round_tripped = parse_media_filename(entry["target"])
            self.assertEqual(round_tripped.variant_id, entry["variant"])
            expected_part = entry["part"] or "none"
            self.assertEqual(round_tripped.part_kind, expected_part)
            self.assertEqual(round_tripped.page_num, entry["page"])
            self.assertEqual(round_tripped.is_crop, entry["crop"])


class TemplateFormatTokenTests(unittest.TestCase):
    """Each FORMAT token, section 4.4 and section 9's template test list."""

    def setUp(self) -> None:
        self.photo_date = _PhotoDate(1952, 6, 1, partial=False)

    def test_yyyy(self) -> None:
        self.assertEqual(_render_date_format("yyyy", self.photo_date), "1952")

    def test_yy(self) -> None:
        self.assertEqual(_render_date_format("yy", self.photo_date), "52")

    def test_mmmm(self) -> None:
        self.assertEqual(_render_date_format("mmmm", self.photo_date), "June")

    def test_mmm(self) -> None:
        self.assertEqual(_render_date_format("mmm", self.photo_date), "Jun")

    def test_mm(self) -> None:
        self.assertEqual(_render_date_format("mm", self.photo_date), "06")

    def test_dd(self) -> None:
        self.assertEqual(_render_date_format("dd", self.photo_date), "01")

    def test_default_format(self) -> None:
        self.assertEqual(_render_date_format("yyyy-mm-dd", self.photo_date), "1952-06-01")

    def test_upper_and_lower_case_spellings_render_identically(self) -> None:
        lower = _render_date_format("yymmdd", self.photo_date)
        upper = _render_date_format("YYMMDD", self.photo_date)
        mixed = _render_date_format("YyMmDd", self.photo_date)
        self.assertEqual(lower, "520601")
        self.assertEqual(lower, upper)
        self.assertEqual(lower, mixed)

    def test_mm_always_means_month_not_minutes(self) -> None:
        # The whole reason this grammar exists: "mm" is never minutes.
        self.assertEqual(_render_date_format("mm", self.photo_date), "06")

    def test_percent_passthrough_to_strftime(self) -> None:
        self.assertEqual(_render_date_format("%j", self.photo_date), "153")

    def test_partial_date_renders_00_for_missing_parts(self) -> None:
        partial = _PhotoDate(1952, None, None, partial=True)
        self.assertEqual(_render_date_format("yyyy-mm-dd", partial), "1952-00-00")

    def test_partial_date_year_and_month_only(self) -> None:
        partial = _PhotoDate(1952, 6, None, partial=True)
        self.assertEqual(_render_date_format("yyyy-mm-dd", partial), "1952-06-00")


class TemplatePipelineTests(unittest.TestCase):
    """Whole-template behavior via :func:`plan_rename`."""

    def test_partial_date_flagged_in_the_preview(self) -> None:
        plan = _plan(["shot.tif"], "{date}", dates={"shot.tif": "1952"})
        entry = _entry_for(plan, "shot.tif")
        self.assertEqual(entry["target"], "1952-00-00-001.tif")
        self.assertIn("partial date", entry["notes"])

    def test_today_token_uses_the_real_date_by_default(self) -> None:
        plan = _plan(["shot.tif"], "{today:yyyy-mm-dd}")
        expected = date.today().strftime("%Y-%m-%d")  # noqa: DTZ011 (matches the planner's own local-date default)
        entry = _entry_for(plan, "shot.tif")
        self.assertEqual(entry["target"], f"{expected}-001.tif")

    def test_today_token_honors_override(self) -> None:
        plan = _plan(["shot.tif"], "{today:yyyy-mm-dd}", today=date(2020, 1, 15))
        entry = _entry_for(plan, "shot.tif")
        self.assertEqual(entry["target"], "2020-01-15-001.tif")

    def test_separator_always_present_even_with_a_digit_prefix(self) -> None:
        plan = _plan(
            ["shot.tif"], "newname{date:yyyy-mm-dd}", dates={"shot.tif": "1952:06:01"}
        )
        entry = _entry_for(plan, "shot.tif")
        self.assertEqual(entry["target"], "newname1952-06-01-001.tif")

    def test_trailing_dash_trimmed_not_doubled(self) -> None:
        with_dash = _plan(
            ["shot.tif"], "{date:yymmdd}-bag-", dates={"shot.tif": "1952:06:01"}
        )
        without_dash = _plan(
            ["shot.tif"], "{date:yymmdd}-bag", dates={"shot.tif": "1952:06:01"}
        )
        self.assertEqual(
            _entry_for(with_dash, "shot.tif")["target"],
            _entry_for(without_dash, "shot.tif")["target"],
        )
        self.assertTrue(any("trimmed" in w for w in with_dash["warnings"]), with_dash["warnings"])

    def test_orig_on_already_numbered_name_strips_number(self) -> None:
        plan = _plan(["newname-001.tif"], "{orig}")
        entry = _entry_for(plan, "newname-001.tif")
        self.assertEqual(entry["target"], "newname-001.tif")
        self.assertFalse(entry["changed"])

    def test_orig_keeps_the_prefix_renumbers_and_cleans_up(self) -> None:
        plan = _plan(["file105.tif", "file205.tif"], "{orig}")
        self.assertEqual(_entry_for(plan, "file105.tif")["target"], "file-001.tif")
        self.assertEqual(_entry_for(plan, "file205.tif")["target"], "file-002.tif")


class TokenizerTests(unittest.TestCase):
    def test_double_brace_is_a_literal_brace(self) -> None:
        pieces = _tokenize_prefix_template("a{{b")
        self.assertEqual(pieces, ["a{b"])

    def test_unknown_token_raises(self) -> None:
        with self.assertRaises(ValueError):
            _tokenize_prefix_template("{bogus}")

    def test_format_on_folder_token_raises(self) -> None:
        with self.assertRaises(ValueError):
            _tokenize_prefix_template("{folder:x}")

    def test_unterminated_brace_raises(self) -> None:
        with self.assertRaises(ValueError):
            _tokenize_prefix_template("prefix{date")


class RenderTemplateTests(unittest.TestCase):
    def test_missing_date_without_undated_raises(self) -> None:
        pieces = _tokenize_prefix_template("{date}")
        with self.assertRaises(_MissingDate):
            _render_template(
                pieces,
                photo_date=None,
                today=_PhotoDate(2020, 1, 1, partial=False),
                folder_name="scans",
                orig="orig",
                undated_literal=None,
            )

    def test_undated_literal_stands_in_for_date(self) -> None:
        pieces = _tokenize_prefix_template("{date}-bag")
        rendered, used_partial = _render_template(
            pieces,
            photo_date=None,
            today=_PhotoDate(2020, 1, 1, partial=False),
            folder_name="scans",
            orig="orig",
            undated_literal="undated",
        )
        self.assertEqual(rendered, "undated-bag")
        self.assertFalse(used_partial)


class SharedCompanionSingleOwnerTests(unittest.TestCase):
    """C6: a companion shared by a same-stem pair (4.3's ".tif/.jpg is one
    slot") must get exactly one owner, or ``_validate_targets`` sees the
    same rendered companion target twice and refuses the whole plan --
    photokin's own ``.md`` sidecars make this the feature's main use case."""

    def test_md_sidecar_shared_by_a_tif_jpg_pair_does_not_duplicate(self) -> None:
        names = ["photo.tif", "photo.jpg"]
        disk_files = [_path(n) for n in names] + [_path("photo.md")]
        plan = _plan(names, "x", disk_files=disk_files)

        self.assertEqual(plan["errors"], [])
        tif_entry = _entry_for(plan, "photo.tif")
        jpg_entry = _entry_for(plan, "photo.jpg")
        total_companions = len(tif_entry["companions"]) + len(jpg_entry["companions"])
        self.assertEqual(total_companions, 1, (tif_entry, jpg_entry))

    def test_owner_is_the_entry_whose_extension_sorts_first(self) -> None:
        # ".jpg" sorts before ".tif"; the companion belongs to that entry.
        names = ["photo.tif", "photo.jpg"]
        disk_files = [_path(n) for n in names] + [_path("photo.md")]
        plan = _plan(names, "x", disk_files=disk_files)
        jpg_entry = _entry_for(plan, "photo.jpg")
        tif_entry = _entry_for(plan, "photo.tif")
        self.assertEqual(len(jpg_entry["companions"]), 1)
        self.assertEqual(jpg_entry["companions"][0]["target"], "x-001.md")
        self.assertEqual(len(tif_entry["companions"]), 0)


class VersionOverrideValidationTests(unittest.TestCase):
    """C1: a non-letter ``version`` override must not be concatenated into
    the rendered filename -- the grammar supports exactly one letter."""

    def test_multi_letter_version_override_is_a_plan_error_not_a_filename(self) -> None:
        plan = _plan(["photo3.tif"], "x", versions={"photo3.tif": "blue"})
        self.assertTrue(
            any("photo3.tif" in e and "blue" in e for e in plan["errors"]), plan["errors"]
        )
        entry = _entry_for(plan, "photo3.tif")
        self.assertIsNotNone(entry["target"])
        self.assertNotIn("blue", entry["target"])

    def test_digit_version_override_is_a_plan_error(self) -> None:
        plan = _plan(["photo3.tif"], "x", versions={"photo3.tif": "5"})
        self.assertTrue(any("photo3.tif" in e for e in plan["errors"]), plan["errors"])

    def test_empty_version_override_still_clears_the_letter(self) -> None:
        # An empty override is documented behavior (clears an existing
        # variant), not the invalid case this fix targets.
        plan = _plan(["box3_017-b.tif"], "x", versions={"box3_017-b.tif": ""})
        self.assertEqual(plan["errors"], [])
        entry = _entry_for(plan, "box3_017-b.tif")
        self.assertIsNone(entry["variant"])


class PartialDateValidationTests(unittest.TestCase):
    """C5: an impossible month in a partial date must not crash the
    renderer -- it has to be rejected during parsing so the group falls to
    the ordinary missing-date handling."""

    def test_out_of_range_month_is_rejected_not_accepted(self) -> None:
        self.assertIsNone(_parse_photo_date("1952:13"))

    def test_implausible_year_is_rejected(self) -> None:
        self.assertIsNone(_parse_photo_date("0001"))

    def test_out_of_range_month_falls_back_to_missing_date_error(self) -> None:
        plan = _plan(["shot.tif"], "{date:mmmm}", dates={"shot.tif": "1952:13"})
        self.assertTrue(any("date" in e for e in plan["errors"]), plan["errors"])
        entry = _entry_for(plan, "shot.tif")
        self.assertIsNone(entry["target"])

    def test_out_of_range_month_with_undated_literal_does_not_crash(self) -> None:
        # Before the fix this raised IndexError inside _render_date_token
        # (_MONTH_NAMES[13 - 1]) the moment a group with month=13 got past
        # the missing-date check -- exercised here through {date:mmmm}.
        plan = _plan(
            ["shot.tif"], "{date:mmmm}", dates={"shot.tif": "1952:13"}, undated_literal="undated"
        )
        self.assertEqual(plan["errors"], [])
        entry = _entry_for(plan, "shot.tif")
        self.assertEqual(entry["target"], "undated-001.tif")


class FolderNormalizationTests(unittest.TestCase):
    """C9: the plan must not store a relative folder -- a later
    ``--rename-finish`` run from a different working directory would
    resolve it somewhere else."""

    def test_relative_folder_is_normalized_to_absolute_in_the_plan(self) -> None:
        item_path = os.path.join(".", "photo.tif")
        plan = plan_rename(
            folder=".",
            disk_files=[item_path],
            items=[RenameItem(path=item_path)],
            prefix_template="x",
            digits=3,
            run_id="test-run",
        )
        self.assertEqual(plan["errors"], [])
        self.assertTrue(os.path.isabs(plan["folder"]), plan["folder"])
        self.assertEqual(plan["folder"], os.path.normpath(os.path.abspath(".")))

    def test_folder_token_on_a_relative_folder_renders_the_real_name_not_a_dot(self) -> None:
        item_path = os.path.join(".", "photo.tif")
        plan = plan_rename(
            folder=".",
            disk_files=[item_path],
            items=[RenameItem(path=item_path)],
            prefix_template="{folder}-x",
            digits=3,
            run_id="test-run",
        )
        self.assertEqual(plan["errors"], [])
        (entry,) = plan["entries"]
        expected_name = os.path.basename(os.path.normpath(os.path.abspath(".")))
        self.assertEqual(entry["target"], f"{expected_name}-x-001.tif")


class NaturalOrderTieBreakTests(unittest.TestCase):
    """C10: ``--order natural`` discards case and a numeric run's own
    spelling, so two differently-spelled names can share a natural key --
    the ordinary ``(name.lower(), name)`` tie-break must decide those, so
    permuting the manifest does not change the assigned numbers."""

    def test_natural_key_breaks_ties_that_collapse_to_the_same_digit_value(self) -> None:
        # "file1.tif" and "file01.tif" both reduce to ('file', 1, '');
        # without a tie-break these keys are equal, not merely close.
        self.assertNotEqual(_natural_sort_key("file1.tif"), _natural_sort_key("file01.tif"))
        self.assertLess(_natural_sort_key("file01.tif"), _natural_sort_key("file1.tif"))

    def test_number_assignment_is_stable_across_manifest_permutations(self) -> None:
        forward = _plan(["file1.tif", "file01.tif"], "x", order_mode="natural")
        backward = _plan(["file01.tif", "file1.tif"], "x", order_mode="natural")
        self.assertEqual(
            _entry_for(forward, "file1.tif")["number"],
            _entry_for(backward, "file1.tif")["number"],
        )
        self.assertEqual(
            _entry_for(forward, "file01.tif")["number"],
            _entry_for(backward, "file01.tif")["number"],
        )


class PlatformIndependentCaseFoldingTests(unittest.TestCase):
    """CI-2 / the case-folding policy: the manifest-vs-disk identity check
    must not depend on ``os.path.normcase`` actually folding case -- it is
    a no-op on POSIX, which is exactly why the equivalent Windows-only test
    above is insufficient on its own. Patching ``normcase`` to the POSIX
    (identity) behavior here, even while running on Windows, reproduces
    what Ubuntu CI saw."""

    def test_case_mismatched_manifest_path_is_not_a_bystander_even_when_normcase_is_a_noop(
        self,
    ) -> None:
        disk_path = _path("same-001.tif")
        manifest_path = _path("SAME-001.TIF")
        with mock.patch("os.path.normcase", side_effect=lambda p: p):
            plan = plan_rename(
                folder=_FOLDER,
                disk_files=[disk_path],
                items=[RenameItem(path=manifest_path)],
                prefix_template="same",
                digits=3,
                run_id="test-run",
            )
        self.assertEqual(plan["errors"], [])


if __name__ == "__main__":
    unittest.main()
