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

    def _plan(self, names: list[str], managed_by: object) -> dict:
        folder = self.scratch()
        items = [{"path": _write_bytes(os.path.join(folder, name))} for name in names]
        manifest = os.path.join(folder, "export.json")
        with open(manifest, "w", encoding="utf-8") as handle:
            json.dump({"items": items, "managed_by": managed_by}, handle)
        plan_path = os.path.join(folder, "plan.json")

        code, _stdout, stderr = self.run_cli([manifest, "--rename", "bw", "--plan-out", plan_path])

        self.assertIsNone(code, stderr)
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
