"""Tests for the rename grammar's inverse: ``canonicalize_stem`` and
``render_media_filename`` in ``photokin.utils``.

``canonicalize_stem`` is a lenient pre-pass in front of
``parse_media_filename``; ``render_media_filename`` is its output-side
inverse. The load-bearing property tying the two together to the existing
parser is the round trip in ``RoundTripPropertyTests`` below: for any tail a
group can carry, rendering it and parsing the result back must recover the
same ``variant_id``, ``part_kind``, ``page_num`` and ``is_crop`` that went
in. That is generated combinatorially rather than spot-checked, because the
whole point of the grammar is that every combination of variant/part/crop
composes -- a handful of hand-picked cases would not catch a combination the
author did not think to write down.
"""

import itertools
import os
import tempfile
import unittest
from unittest import mock

from photokin.utils import (
    ParsedName,
    canonicalize_stem,
    casefold_filename,
    parse_media_filename,
    paths_are_same_file,
    render_media_filename,
)


class CanonicalizeStemSeparatorTests(unittest.TestCase):
    """Each part/crop separator, normalized on its own."""

    def test_dashed_variant_left_alone(self) -> None:
        # The dashed variant form is already what the parser reads; it is
        # not a separator canonicalize_stem has any business touching.
        stem, notes = canonicalize_stem("box3_017-b")
        self.assertEqual(stem, "box3_017-b")
        self.assertEqual(notes, [])

    def test_underscore_before_back(self) -> None:
        stem, notes = canonicalize_stem("box3_017_back")
        self.assertEqual(stem, "box3_017-back")
        self.assertEqual(notes, ["part separator normalized"])

    def test_dot_before_back(self) -> None:
        stem, notes = canonicalize_stem("box3_017.back")
        self.assertEqual(stem, "box3_017-back")
        self.assertEqual(notes, ["part separator normalized"])

    def test_space_before_back(self) -> None:
        stem, notes = canonicalize_stem("box3_017 back")
        self.assertEqual(stem, "box3_017-back")
        self.assertEqual(notes, ["part separator normalized"])

    def test_underscore_before_front(self) -> None:
        stem, notes = canonicalize_stem("box3_017_front")
        self.assertEqual(stem, "box3_017-front")
        self.assertEqual(notes, ["part separator normalized"])

    def test_underscore_before_negative(self) -> None:
        stem, notes = canonicalize_stem("box3_017_negative")
        self.assertEqual(stem, "box3_017-negative")
        self.assertEqual(notes, ["part separator normalized"])

    def test_dashed_page_retained(self) -> None:
        # Already canonical: no rewrite, no note.
        stem, notes = canonicalize_stem("box3_020-page3")
        self.assertEqual(stem, "box3_020-page3")
        self.assertEqual(notes, [])

    def test_underscore_before_page(self) -> None:
        stem, notes = canonicalize_stem("box3_020_page3")
        self.assertEqual(stem, "box3_020-page3")
        self.assertEqual(notes, ["part separator normalized"])

    def test_crop_stacks_on_normalized_part(self) -> None:
        # Both separators need normalizing; canonicalize_stem is a single
        # right-to-left pass, not two independent searches, so this exercises
        # the crop-then-part order the docstring promises.
        stem, notes = canonicalize_stem("box3_017_back_crop")
        self.assertEqual(stem, "box3_017-back-crop")
        self.assertEqual(notes, ["part separator normalized"])

    def test_crop_normalized_over_already_dashed_part(self) -> None:
        stem, notes = canonicalize_stem("box3_017-back_crop")
        self.assertEqual(stem, "box3_017-back-crop")
        self.assertEqual(notes, ["part separator normalized"])

    def test_already_canonical_crop_and_part_untouched(self) -> None:
        stem, notes = canonicalize_stem("box3_017-back-crop")
        self.assertEqual(stem, "box3_017-back-crop")
        self.assertEqual(notes, [])

    def test_plain_stem_untouched(self) -> None:
        stem, notes = canonicalize_stem("box3_017")
        self.assertEqual(stem, "box3_017")
        self.assertEqual(notes, [])


class CanonicalizeStemVariantScopeTests(unittest.TestCase):
    """The grammar reads only ``-b`` and digit-adjacent ``5b``; widening
    that is explicitly out of scope (rename-mode plan, section 11), so
    ``canonicalize_stem`` must never rewrite ``_b`` or ``.b``."""

    def test_underscore_b_left_alone(self) -> None:
        stem, notes = canonicalize_stem("box3_017_b")
        self.assertEqual(stem, "box3_017_b")
        self.assertEqual(notes, [])

    def test_dot_b_left_alone(self) -> None:
        stem, notes = canonicalize_stem("box3_017.b")
        self.assertEqual(stem, "box3_017.b")
        self.assertEqual(notes, [])

    def test_underscore_b_before_back_left_alone(self) -> None:
        # "_b" immediately before a correctly-dashed part suffix: the "b" is
        # not a word canonicalize_stem's part patterns match, so only the
        # part separator (already "-") is inspected, and nothing changes.
        stem, notes = canonicalize_stem("box3_017_b-back")
        self.assertEqual(stem, "box3_017_b-back")
        self.assertEqual(notes, [])


class RenderMediaFilenameTests(unittest.TestCase):
    """Direct checks on the rendered spelling, independent of round-tripping."""

    def test_bare_number(self) -> None:
        parsed = ParsedName(base_id="ignored", variant_id=None, part_kind="none", page_num=None)
        self.assertEqual(
            render_media_filename("newname", 1, 3, parsed, ".tif"), "newname-001.tif"
        )

    def test_variant_written_directly_after_digits(self) -> None:
        parsed = ParsedName(base_id="ignored", variant_id="b", part_kind="none", page_num=None)
        self.assertEqual(render_media_filename("bw", 1, 3, parsed, ".tif"), "bw-001b.tif")

    def test_variant_and_back(self) -> None:
        parsed = ParsedName(base_id="ignored", variant_id="b", part_kind="back", page_num=None)
        self.assertEqual(
            render_media_filename("bw", 1, 3, parsed, ".tif"), "bw-001b-back.tif"
        )

    def test_page_number_carried(self) -> None:
        parsed = ParsedName(base_id="ignored", variant_id=None, part_kind="page", page_num=2)
        self.assertEqual(
            render_media_filename("bw", 2, 3, parsed, ".tif"), "bw-002-page2.tif"
        )

    def test_crop_stacks_last(self) -> None:
        parsed = ParsedName(
            base_id="ignored", variant_id="b", part_kind="back", page_num=None, is_crop=True
        )
        self.assertEqual(
            render_media_filename("bw", 1, 3, parsed, ".tif"), "bw-001b-back-crop.tif"
        )

    def test_digit_width_respected(self) -> None:
        parsed = ParsedName(base_id="ignored", variant_id=None, part_kind="none", page_num=None)
        self.assertEqual(render_media_filename("bw", 7, 5, parsed, ".tif"), "bw-00007.tif")

    def test_trailing_dash_on_prefix_not_doubled(self) -> None:
        parsed = ParsedName(base_id="ignored", variant_id=None, part_kind="none", page_num=None)
        self.assertEqual(render_media_filename("bw-", 1, 3, parsed, ".tif"), "bw-001.tif")

    def test_empty_prefix_after_trim_is_an_error(self) -> None:
        parsed = ParsedName(base_id="ignored", variant_id=None, part_kind="none", page_num=None)
        with self.assertRaises(ValueError):
            render_media_filename("--", 1, 3, parsed, ".tif")

    def test_page_kind_without_page_num_is_an_error(self) -> None:
        parsed = ParsedName(base_id="ignored", variant_id=None, part_kind="page", page_num=None)
        with self.assertRaises(ValueError):
            render_media_filename("bw", 1, 3, parsed, ".tif")


def _generate_tails() -> "list[tuple[str | None, str, int | None, bool]]":
    """Every combination of variant / part kind / page number / crop that the
    grammar can carry, as ``(variant_id, part_kind, page_num, is_crop)``.

    Combinatorial by construction (an ``itertools.product`` over the axes,
    not a hand-picked list) so a combination nobody thought to write down
    still gets exercised -- the point of ``4.2``'s round-trip property.
    """
    variants: list[str | None] = [None, "b", "z"]
    part_kinds = ["none", "front", "back", "negative", "page"]
    page_nums = [1, 2, 15]
    crops = [False, True]

    tails: list[tuple[str | None, str, int | None, bool]] = []
    for variant, part_kind, is_crop in itertools.product(variants, part_kinds, crops):
        if part_kind == "page":
            for page_num in page_nums:
                tails.append((variant, part_kind, page_num, is_crop))
        else:
            tails.append((variant, part_kind, None, is_crop))
    return tails


class RoundTripPropertyTests(unittest.TestCase):
    """``parse_media_filename(render_media_filename(...))`` must recover the
    same tail for every combination the grammar can express."""

    def test_round_trip_over_every_generated_tail(self) -> None:
        tails = _generate_tails()
        self.assertGreater(len(tails), 20)  # guard against the generator collapsing
        for variant_id, part_kind, page_num, is_crop in tails:
            with self.subTest(
                variant=variant_id, part=part_kind, page=page_num, crop=is_crop
            ):
                parsed = ParsedName(
                    base_id="ignored-on-render",
                    variant_id=variant_id,
                    part_kind=part_kind,
                    page_num=page_num,
                    is_crop=is_crop,
                )
                rendered = render_media_filename("newname", 3, 3, parsed, ".tif")
                round_tripped = parse_media_filename(rendered)

                self.assertEqual(round_tripped.variant_id, variant_id)
                self.assertEqual(round_tripped.part_kind, part_kind)
                self.assertEqual(round_tripped.page_num, page_num)
                self.assertEqual(round_tripped.is_crop, is_crop)

    def test_round_trip_with_multi_character_prefix_and_wide_digits(self) -> None:
        # A prefix that itself contains digits and a dash is the case the
        # plan's example table exists to cover (526010601-bag-...-002b-back):
        # the number must stay unambiguous from the prefix's own digits.
        for variant_id, part_kind, page_num, is_crop in _generate_tails():
            with self.subTest(
                variant=variant_id, part=part_kind, page=page_num, crop=is_crop
            ):
                parsed = ParsedName(
                    base_id="ignored-on-render",
                    variant_id=variant_id,
                    part_kind=part_kind,
                    page_num=page_num,
                    is_crop=is_crop,
                )
                rendered = render_media_filename(
                    "520601-bag-woodbury", 12, 4, parsed, ".jpg"
                )
                round_tripped = parse_media_filename(rendered)

                self.assertEqual(round_tripped.variant_id, variant_id)
                self.assertEqual(round_tripped.part_kind, part_kind)
                self.assertEqual(round_tripped.page_num, page_num)
                self.assertEqual(round_tripped.is_crop, is_crop)


class CanonicalizeStemThenParseTests(unittest.TestCase):
    """``canonicalize_stem`` feeds ``parse_media_filename``: a stem with a
    loose separator must parse the same after canonicalizing as an
    already-canonical stem with the same tail parses on its own."""

    def test_loose_separators_parse_like_the_canonical_form(self) -> None:
        cases = [
            ("box3_017_back", "box3_017-back"),
            ("box3_017.front", "box3_017-front"),
            ("box3_017 negative", "box3_017-negative"),
            ("box3_020_page4", "box3_020-page4"),
            ("box3_017_back_crop", "box3_017-back-crop"),
        ]
        for loose, canonical in cases:
            with self.subTest(loose=loose):
                canonicalized, _notes = canonicalize_stem(loose)
                self.assertEqual(canonicalized, canonical)
                loose_parsed = parse_media_filename(canonicalized + ".tif")
                canonical_parsed = parse_media_filename(canonical + ".tif")
                self.assertEqual(loose_parsed, canonical_parsed)


class CaseHelperTests(unittest.TestCase):
    """The two case questions, which are not the same question.

    Conflating them is what put Ubuntu CI red while Windows stayed green: the
    code answered both with ``os.path.normcase``, which folds case on Windows
    and nothing at all on POSIX.
    """

    def test_target_collision_folds_case_on_every_platform(self) -> None:
        # Whether two planned names collide is a property of the PLAN -- of
        # where the files may later live -- not of the machine planning it. A
        # plan that emits both of these is unsafe on macOS and Windows however
        # it was produced, so folding must not depend on the running OS.
        self.assertEqual(casefold_filename("NEW-001.TIF"), casefold_filename("new-001.tif"))
        self.assertNotEqual(casefold_filename("new-001.tif"), casefold_filename("new-002.tif"))

    def test_folding_survives_a_normcase_that_does_nothing(self) -> None:
        # The POSIX no-op, made explicit: patching normcase away must not
        # change the answer, because the answer never consulted it.
        with mock.patch("os.path.normcase", side_effect=lambda value: value):
            self.assertEqual(
                casefold_filename("NEW-001.TIF"), casefold_filename("new-001.tif")
            )

    def test_identity_is_the_filesystem_s_answer_not_a_folded_string(self) -> None:
        # Whether two paths name one file is the filesystem's to answer. On a
        # case-sensitive volume these are two files and folding would wrongly
        # merge them; on a case-insensitive one they are the same file and a
        # case-sensitive compare would wrongly split it. Only the filesystem
        # knows which volume this is.
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "photo.tif")
            with open(path, "w", encoding="utf-8"):
                pass
            self.assertTrue(paths_are_same_file(path, path))
            other = os.path.join(folder, "other.tif")
            with open(other, "w", encoding="utf-8"):
                pass
            self.assertFalse(paths_are_same_file(path, other))

    def test_identity_falls_back_when_a_path_cannot_be_stat_d(self) -> None:
        # A target does not exist yet, so there is nothing to stat and the
        # question has to be answered on the strings alone.
        with tempfile.TemporaryDirectory() as folder:
            missing = os.path.join(folder, "gone.tif")
            self.assertTrue(paths_are_same_file(missing, missing))
            self.assertFalse(paths_are_same_file(missing, os.path.join(folder, "other.tif")))


if __name__ == "__main__":
    unittest.main()
