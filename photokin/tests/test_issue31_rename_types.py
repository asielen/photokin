"""Rename plans every catalogued photo type a manifest lists (issue #31)."""

from __future__ import annotations

import json
import os

from photokin.tests.test_cli_input_surface import _CliTestCase, _write_bytes

#: One file per extension from the issue's probe.
_EXTENSIONS = (
    "jpg", "png", "tif", "psd", "psb", "dng", "cr2", "nef", "arw", "heic", "webp", "mp4",
    "mov", "avi",
)


class TestRenameTypes(_CliTestCase):
    """A managed manifest plans every item; an unmanaged one plans photo and video types."""

    def _plan(self, names: list[str], managed_by: object, extra: tuple[str, ...] = ()) -> dict:
        folder = self.scratch()
        items = [{"path": _write_bytes(os.path.join(folder, name))} for name in names]
        for name in extra:
            _write_bytes(os.path.join(folder, name))
        manifest = os.path.join(folder, "export.json")
        with open(manifest, "w", encoding="utf-8") as handle:
            json.dump({"items": items, "managed_by": managed_by}, handle)
        plan_path = os.path.join(folder, "plan.json")

        self.run_cli([manifest, "--rename", "bw", "--plan-out", plan_path])

        with open(plan_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_a_lightroom_manifest_plans_every_catalogued_type(self) -> None:
        # Arrange
        names = [f"photo{n:02d}.{ext}" for n, ext in enumerate(_EXTENSIONS)]

        # Act
        plan = self._plan(names, {"app": "lightroom"})

        # Assert
        planned = {os.path.basename(entry["path"]) for entry in plan["entries"]}
        self.assertEqual(planned, set(names))

    def test_any_managed_by_plans_a_type_photokin_does_not_know(self) -> None:
        # Act
        plan = self._plan(["scan.tif", "page.pdf"], {"catalog": "x"})

        # Assert
        self.assertIn("page.pdf", {os.path.basename(e["path"]) for e in plan["entries"]})

    def test_an_unmanaged_manifest_plans_photo_types_and_reports_the_rest(self) -> None:
        # Act
        plan = self._plan(["scan.tif", "edit.psd", "raw.dng", "document.pdf"], None)

        # Assert
        planned = {os.path.basename(entry["path"]) for entry in plan["entries"]}
        self.assertEqual(planned, {"scan.tif", "edit.psd", "raw.dng"})
        self.assertIn(
            {"path": "document.pdf", "reason": "not a photo or video type"},
            [
                {"path": os.path.basename(r["path"]), "reason": r["reason"]}
                for r in plan["left_behind"]
            ],
        )

    def test_an_uncatalogued_psd_reserves_its_name(self) -> None:
        # Act: a.psd would be renamed onto bw-001.psd, which the catalog does not track.
        plan = self._plan(["a.psd"], {"app": "lightroom"}, extra=("bw-001.psd",))

        # Assert
        self.assertTrue(any("bw-001.psd" in e for e in plan["errors"]), plan["errors"])

    def test_a_listed_companion_is_not_also_reported_left_behind(self) -> None:
        # Act
        plan = self._plan(["a.tif", "a.md"], None)

        # Assert
        self.assertEqual(plan["left_behind"], [])
        companions = [c["target"] for e in plan["entries"] for c in e["companions"]]
        self.assertEqual(companions, ["bw-001.md"])

    def test_a_left_out_file_has_one_record_and_a_warning(self) -> None:
        # Act
        plan = self._plan(["a.tif", "a.pdf", "memo.pdf"], None)

        # Assert
        reasons = sorted(
            (os.path.basename(r["path"]), r["reason"]) for r in plan["left_behind"]
        )
        self.assertEqual(
            reasons,
            [
                ("a.pdf", "extension outside companion set"),
                ("memo.pdf", "not a photo or video type"),
            ],
        )
        self.assertIn("memo.pdf: not a photo or video type", plan["warnings"])

    def test_a_managed_plan_reserves_names_in_every_type_it_renames(self) -> None:
        # Act: page.pdf would be renamed onto an uncatalogued bw-001.pdf.
        plan = self._plan(["page.pdf"], {"app": "lightroom"}, extra=("bw-001.pdf",))

        # Assert
        self.assertTrue(any("bw-001.pdf" in e for e in plan["errors"]), plan["errors"])
