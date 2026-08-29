"""Tests for ``--rename`` and its executor commands on the CLI surface.

Follows ``test_cli_input_surface.py``'s own style and reuses its
``_CliTestCase``/``run_cli``/fixture helpers rather than rebuilding them:
this module exercises the wiring in ``cli.py`` -- flag refusals, the
``-w`` bundle expansion, the ``managed_by`` guard, exit codes -- not the
planner or the executor, both of which have their own test modules
(``test_rename_planner.py``, ``test_rename_apply.py``). Every case here
runs inside a scratch :class:`tempfile.TemporaryDirectory`; nothing here
renames a file outside one.
"""

import json
import os
import unittest
from unittest.mock import patch

from photokin import rename_apply
from photokin.tests.test_cli_input_surface import (
    _CliTestCase,
    _write_bytes,
    _write_manifest,
)


def _names(folder: str) -> list[str]:
    """Return the sorted basenames directly inside *folder*."""
    return sorted(os.listdir(folder))


def _non_journal_names(folder: str) -> list[str]:
    """Basenames inside *folder*, excluding the rename journal and changeset.

    A successful apply/undo/finish leaves its own audit trail behind (the
    journal ``<folder>_rename-<run_id>.ndjson`` and, under ``--changeset``,
    ``<folder>_changeset.ndjson``) -- both expected artifacts, not part of
    what "the images are back where they started" means.
    """
    return sorted(n for n in os.listdir(folder) if not n.endswith(".ndjson"))


class TestRenamePreviewWritesNothing(_CliTestCase):
    """``--rename PREFIX`` alone is the preview: it touches nothing on disk."""

    def test_bare_rename_leaves_every_file_exactly_as_it_was(self) -> None:
        folder = self.make_folder("box3_017.jpg", "box3_017-b.tif")
        before = _names(folder)

        code, _stdout, stderr = self.run_cli([folder, "--rename", "bw"])

        self.assertIsNone(code)
        self.assertEqual(_names(folder), before)
        self.assertIn("Rename plan for", stderr)
        self.assertIn("box3_017.jpg  ->  bw-001.jpg", stderr)
        self.assertIn("box3_017-b.tif  ->  bw-001b.tif", stderr)
        # The 5.6 sentence is on every preview, not only a managed_by one.
        self.assertIn("must be renamed through that application", stderr)

    def test_preview_never_enters_the_analysis_stream(self) -> None:
        folder = self.make_folder("box3_017.jpg")
        with patch("photokin.cli.process_manifest_stream") as stream:
            code, _stdout, _stderr = self.run_cli([folder, "--rename", "bw"])
        self.assertIsNone(code)
        stream.assert_not_called()


class TestWriteBundleExpandsInRenameMode(_CliTestCase):
    """``-w`` means ``--changeset true`` and apply the plan (section 7)."""

    def test_dash_w_renames_the_files_and_records_a_changeset(self) -> None:
        folder = self.make_folder("box3_017.jpg", "box3_017-b.tif")

        code, _stdout, stderr = self.run_cli([folder, "--rename", "bw", "-w"])

        self.assertIsNone(code)
        self.assertEqual(_non_journal_names(folder), sorted(["bw-001.jpg", "bw-001b.tif"]))
        self.assertIn("Rename apply: applied", stderr)

        changeset_path = os.path.join(folder, f"{os.path.basename(folder)}_changeset.ndjson")
        self.assertTrue(os.path.isfile(changeset_path))
        with open(changeset_path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        records = [json.loads(line) for line in lines if line.strip()]
        self.assertEqual(len(records), 2)
        self.assertTrue(all(r["kind"] == "rename" for r in records))
        self.assertEqual(
            sorted((r["from"], r["to"]) for r in records),
            sorted(
                [
                    ("box3_017.jpg", "bw-001.jpg"),
                    ("box3_017-b.tif", "bw-001b.tif"),
                ]
            ),
        )

    def test_changeset_true_alone_records_without_renaming(self) -> None:
        """--changeset true without -w is a member of the bundle, not the whole of it."""
        folder = self.make_folder("box3_017.jpg")
        before = _names(folder)

        code, _stdout, _stderr = self.run_cli(
            [folder, "--rename", "bw", "--changeset", "true"]
        )

        self.assertIsNone(code)
        # The image itself is untouched -- only the changeset artifact is new.
        self.assertEqual(_non_journal_names(folder), before)
        changeset_path = os.path.join(folder, f"{os.path.basename(folder)}_changeset.ndjson")
        self.assertTrue(os.path.isfile(changeset_path))

    def test_dash_w_beside_a_contradicting_changeset_false_is_refused(self) -> None:
        folder = self.make_folder("box3_017.jpg")
        before = _names(folder)

        code, stdout, stderr = self.run_cli(
            [folder, "--rename", "bw", "-w", "--changeset", "false"]
        )

        self.assertEqual(code, 2)
        self.assertEqual(_names(folder), before)
        self.assertIn("`-w` means --changeset true and apply the plan", stderr)
        self.assertEqual(stdout, "")


class TestIncompatibleFlagsAreRefused(_CliTestCase):
    """``--exiftool-write`` and ``--output-file`` have no meaning in rename mode."""

    def test_exiftool_write_true_is_refused(self) -> None:
        folder = self.make_folder("box3_017.jpg")
        before = _names(folder)

        code, stdout, stderr = self.run_cli(
            [folder, "--rename", "bw", "--exiftool-write", "true"]
        )

        self.assertEqual(code, 2)
        self.assertEqual(_names(folder), before)
        self.assertIn("has nothing to do", stderr)
        self.assertEqual(stdout, "")

    def test_output_file_is_refused(self) -> None:
        folder = self.make_folder("box3_017.jpg")
        before = _names(folder)

        code, stdout, stderr = self.run_cli(
            [folder, "--rename", "bw", "--output-file", os.path.join(folder, "out.json")]
        )

        self.assertEqual(code, 2)
        self.assertEqual(_names(folder), before)
        self.assertIn("would never be written", stderr)
        self.assertIn("use --plan-out", stderr)
        self.assertEqual(stdout, "")


class TestManagedByGuardsTheWriteSwitch(_CliTestCase):
    """A ``managed_by`` manifest (6.1) makes ``-w`` a usage error; ``--plan-out`` still works."""

    def _managed_manifest(self, folder: str) -> str:
        image = _write_bytes(os.path.join(folder, "box3_017.jpg"))
        manifest_path = _write_manifest(
            folder,
            [{"path": image}],
            name="lightroom-export.json",
        )
        with open(manifest_path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        document["managed_by"] = {"app": "lightroom", "catalog": "/Volumes/Archive/archive.lrcat"}
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        return manifest_path

    def test_dash_w_is_refused_for_a_managed_manifest(self) -> None:
        folder = self.scratch()
        manifest_path = self._managed_manifest(folder)
        before = _names(folder)

        code, stdout, stderr = self.run_cli([manifest_path, "--rename", "bw", "-w"])

        self.assertEqual(code, 2)
        self.assertEqual(_names(folder), before)
        self.assertIn("exported by lightroom", stderr)
        self.assertIn("apply the plan through lightroom", stderr)
        self.assertEqual(stdout, "")

    def test_plan_out_still_works_for_a_managed_manifest(self) -> None:
        folder = self.scratch()
        manifest_path = self._managed_manifest(folder)
        plan_path = os.path.join(folder, "plan.json")
        before = _names(folder)

        code, _stdout, stderr = self.run_cli(
            [manifest_path, "--rename", "bw", "--plan-out", plan_path]
        )

        self.assertIsNone(code)
        # Nothing in the source folder was renamed; a plan is not an apply.
        self.assertEqual(sorted(n for n in _names(folder) if n != "plan.json"), before)
        with open(plan_path, "r", encoding="utf-8") as handle:
            plan = json.load(handle)
        self.assertEqual(plan["managed_by"], {"app": "lightroom", "catalog": "/Volumes/Archive/archive.lrcat"})
        self.assertEqual(len(plan["entries"]), 1)
        self.assertEqual(plan["entries"][0]["target"], "bw-001.jpg")
        self.assertIn("Rename plan for", stderr)


class TestExitCodes(_CliTestCase):
    """2 for usage/validation, 1 for an executor failure, 0 (None here) otherwise."""

    def test_a_plan_that_cannot_be_rendered_exits_2(self) -> None:
        # No EXIF date on a placeholder file and no --undated: {date} cannot
        # be rendered for the one group, which is a validation error (4.6),
        # not merely a warning -- even though nothing was ever asked to write.
        folder = self.make_folder("box3_017.jpg")

        code, stdout, stderr = self.run_cli([folder, "--rename", "{date}-bw"])

        self.assertEqual(code, 2)
        self.assertIn("no date available", stderr)
        self.assertEqual(stdout, "")

    def test_already_clean_exits_0(self) -> None:
        folder = self.make_folder("box3_017.jpg")
        first_code, _out, _err = self.run_cli([folder, "--rename", "bw", "-w"])
        self.assertIsNone(first_code)

        second_code, _out, stderr = self.run_cli([folder, "--rename", "bw", "-w"])

        self.assertIsNone(second_code)
        self.assertIn("nothing_to_do", stderr)

    def test_a_preflight_refusal_while_applying_exits_2(self) -> None:
        folder = self.make_folder("box3_017.jpg")
        problem = rename_apply.RenamePreflightError(
            "The folder no longer matches the plan; nothing was renamed.",
            "Re-run --rename to make a fresh plan.",
        )
        with patch("photokin.cli.rename_apply.apply_plan", side_effect=problem):
            code, stdout, stderr = self.run_cli([folder, "--rename", "bw", "-w"])
        self.assertEqual(code, 2)
        self.assertIn("nothing was renamed", stderr)
        self.assertEqual(stdout, "")

    def test_an_executor_failure_exits_1(self) -> None:
        folder = self.make_folder("box3_017.jpg")
        fake_report = rename_apply.ApplyReport(
            status=rename_apply.STATUS_ROLLED_BACK,
            journal_path=os.path.join(folder, "fake_rename-x.ndjson"),
        )
        with patch("photokin.cli.rename_apply.apply_plan", return_value=fake_report):
            code, _stdout, stderr = self.run_cli([folder, "--rename", "bw", "-w"])
        self.assertEqual(code, 1)
        self.assertIn("rolled_back", stderr)

    def test_dry_run_with_dash_w_applies_nothing(self) -> None:
        folder = self.make_folder("box3_017.jpg")
        before = _names(folder)

        code, _stdout, stderr = self.run_cli(
            [folder, "--rename", "bw", "-w", "--dry-run"]
        )

        self.assertIsNone(code)
        self.assertEqual(_names(folder), before)
        self.assertIn("would_apply", stderr)


class TestModeFlagsDoNotCombine(_CliTestCase):
    """Each of ``--rename``/``--rename-undo``/``--rename-resume``/``--rename-finish``
    runs on its own; two together is a usage error, not "do both"."""

    def test_rename_and_rename_undo_conflict(self) -> None:
        folder = self.make_folder("box3_017.jpg")

        code, stdout, stderr = self.run_cli(
            [folder, "--rename", "bw", "--rename-undo"]
        )

        self.assertEqual(code, 2)
        self.assertIn("only one can drive this run", stderr)
        self.assertEqual(stdout, "")

    def test_rename_and_generate_manifest_conflict(self) -> None:
        folder = self.make_folder("box3_017.jpg")

        code, _stdout, stderr = self.run_cli(
            [folder, "--rename", "bw", "--generate-manifest", os.path.join(folder, "m.json")]
        )

        self.assertEqual(code, 2)
        self.assertIn("only one can drive this run", stderr)


class TestRenameUndoAndResume(_CliTestCase):
    """``--rename-undo``/``--rename-resume`` read the journal a previous ``-w`` wrote."""

    def test_undo_restores_the_original_names(self) -> None:
        folder = self.make_folder("box3_017.jpg", "box3_017-b.tif")
        original = _names(folder)
        apply_code, _out, _err = self.run_cli([folder, "--rename", "bw", "-w"])
        self.assertIsNone(apply_code)
        self.assertNotEqual(_non_journal_names(folder), original)

        undo_code, _out, stderr = self.run_cli([folder, "--rename-undo"])

        self.assertIsNone(undo_code)
        self.assertEqual(_non_journal_names(folder), original)
        self.assertIn("Rename undo: undone", stderr)

    def test_resume_with_no_open_journal_is_refused(self) -> None:
        folder = self.make_folder("box3_017.jpg")

        code, stdout, stderr = self.run_cli([folder, "--rename-resume"])

        self.assertEqual(code, 2)
        self.assertIn("no ", stderr)
        self.assertIn("journal was found", stderr)
        self.assertEqual(stdout, "")


class TestRenameFinish(_CliTestCase):
    """``--rename-finish PLAN``: companions only, for images a catalog already renamed."""

    def test_finish_renames_only_the_companion(self) -> None:
        folder = self.scratch()
        image = _write_bytes(os.path.join(folder, "box3_017.jpg"))
        with open(os.path.join(folder, "box3_017.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nsource_file: box3_017.jpg\n---\ntranscript\n")
        plan_path = os.path.join(folder, "plan.json")

        plan_code, _out, _err = self.run_cli(
            [folder, "--rename", "bw", "--plan-out", plan_path]
        )
        self.assertIsNone(plan_code)

        # The catalog application has already renamed the image; photokin's
        # job is only to bring the companion along.
        os.rename(image, os.path.join(folder, "bw-001.jpg"))

        finish_code, _out, stderr = self.run_cli(["--rename-finish", plan_path])

        self.assertIsNone(finish_code)
        self.assertEqual(
            _non_journal_names(folder), sorted(["bw-001.jpg", "bw-001.md", "plan.json"])
        )
        self.assertIn("Rename finish:", stderr)


if __name__ == "__main__":
    unittest.main()
