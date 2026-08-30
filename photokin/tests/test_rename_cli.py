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
from typing import Any
from unittest.mock import patch

from photokin import cli_messages, rename_apply
from photokin.tests.test_cli_input_surface import (
    _CliTestCase,
    _write_bytes,
    _write_manifest,
)


def _names(folder: str) -> list[str]:
    """Return the sorted basenames directly inside *folder*."""
    return sorted(os.listdir(folder))


def _snapshot(folder: str) -> dict[str, tuple[int, float]]:
    """Return ``{name: (size, mtime)}`` for every entry directly inside *folder*.

    Stronger than :func:`_names`: a rename that swaps two files back and
    forth, or one that overwrites a file's bytes without changing its name,
    would both pass a names-only comparison but fail this one.
    """
    result = {}
    for name in os.listdir(folder):
        st = os.stat(os.path.join(folder, name))
        result[name] = (st.st_size, st.st_mtime)
    return result


def _non_journal_names(folder: str) -> list[str]:
    """Basenames inside *folder*, excluding the rename journal and changeset.

    A successful apply/undo/finish leaves its own audit trail behind (the
    journal ``<folder>_rename-<run_id>.ndjson`` and, under ``--changeset``,
    ``<folder>_changeset.ndjson``) -- both expected artifacts, not part of
    what "the images are back where they started" means.
    """
    return sorted(n for n in os.listdir(folder) if not n.endswith(".ndjson"))


def _changeset_path(folder: str) -> str:
    """Return the changeset path a run over *folder* derives for itself."""
    return os.path.join(folder, f"{os.path.basename(folder)}_changeset.ndjson")


def _rename_records(changeset_path: str) -> list[dict[str, Any]]:
    """Return the ``kind: "rename"`` records the changeset at *changeset_path* carries.

    A changeset that was never written is an empty list: "this run claimed no
    rename" is the property under test, and a missing file claims none.
    """
    if not os.path.isfile(changeset_path):
        return []
    with open(changeset_path, encoding="utf-8") as handle:
        records = [json.loads(line) for line in handle if line.strip()]
    return [record for record in records if record.get("kind") == "rename"]


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

        changeset_path = _changeset_path(folder)
        self.assertTrue(os.path.isfile(changeset_path))
        records = _rename_records(changeset_path)
        self.assertEqual(len(records), 2)
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
        self.assertTrue(os.path.isfile(_changeset_path(folder)))

    def test_an_apply_refused_by_preflight_records_no_rename(self) -> None:
        """A refused apply leaves no rename in the audit trail.

        ``apply_plan``'s own preflight runs after the plan is built and can
        still stop the run with nothing renamed; the changeset exists to be
        trusted later, so it must not record what that run did not do.
        """
        folder = self.make_folder("box3_017.jpg", "box3_017-b.tif")
        before = _non_journal_names(folder)
        problem = rename_apply.RenamePreflightError(
            "The folder no longer matches the plan; nothing was renamed.",
            "Re-run --rename to make a fresh plan.",
        )

        with patch("photokin.cli.rename_apply.apply_plan", side_effect=problem):
            code, _stdout, _stderr = self.run_cli([folder, "--rename", "bw", "-w"])

        self.assertEqual(code, 2)
        self.assertEqual(_rename_records(_changeset_path(folder)), [])
        self.assertEqual(_non_journal_names(folder), before)

    def test_a_rolled_back_apply_records_no_rename(self) -> None:
        """``rolled_back`` means every file is back where it started, so the
        audit trail must not say the renames happened."""
        folder = self.make_folder("box3_017.jpg")
        fake_report = rename_apply.ApplyReport(
            status=rename_apply.STATUS_ROLLED_BACK,
            journal_path=os.path.join(folder, "fake_rename-x.ndjson"),
        )

        with patch("photokin.cli.rename_apply.apply_plan", return_value=fake_report):
            code, _stdout, _stderr = self.run_cli([folder, "--rename", "bw", "-w"])

        self.assertEqual(code, 1)
        self.assertEqual(_rename_records(_changeset_path(folder)), [])

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


#: The object shape 6.1 documents, the one a wrapper is expected to send.
_LIGHTROOM = {"app": "lightroom", "catalog": "/Volumes/Archive/archive.lrcat"}

#: The shapes 6.1 also invites ("any shape the wrapper wants"). Each marks the
#: archive as catalog-tracked exactly as the object above does.
_NON_OBJECT_MANAGED_BY: tuple[Any, ...] = ("Lightroom Classic", ["Lightroom Classic"], True, 7)


class TestManagedByGuardsTheWriteSwitch(_CliTestCase):
    """A ``managed_by`` manifest (6.1) makes ``-w`` a usage error; ``--plan-out`` still works."""

    def _managed_manifest(self, folder: str, managed_by: Any = _LIGHTROOM) -> str:
        """Write a one-image manifest into *folder* carrying *managed_by* verbatim."""
        image = _write_bytes(os.path.join(folder, "box3_017.jpg"))
        manifest_path = _write_manifest(
            folder,
            [{"path": image}],
            name="lightroom-export.json",
        )
        with open(manifest_path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        document["managed_by"] = managed_by
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
        self.assertEqual(plan["managed_by"], _LIGHTROOM)
        self.assertEqual(len(plan["entries"]), 1)
        self.assertEqual(plan["entries"][0]["target"], "bw-001.jpg")
        self.assertIn("Rename plan for", stderr)

    def test_a_non_object_managed_by_still_refuses_dash_w(self) -> None:
        """The guard keys on presence, not shape (6.1).

        A manifest that marks its archive catalog-tracked with a string, a
        list, a bool or a number is catalog-tracked all the same -- and none
        of those shapes may crash the refusal on their way to the message.
        """
        for managed_by in _NON_OBJECT_MANAGED_BY:
            with self.subTest(managed_by=managed_by):
                folder = self.scratch()
                manifest_path = self._managed_manifest(folder, managed_by)
                before = _names(folder)

                code, stdout, stderr = self.run_cli([manifest_path, "--rename", "bw", "-w"])

                self.assertEqual(code, 2)
                self.assertEqual(_names(folder), before)
                self.assertIn("managed by a catalog application", stderr)
                self.assertIn("use --plan-out", stderr)
                self.assertEqual(stdout, "")

    def test_a_non_object_managed_by_reaches_the_plan_verbatim(self) -> None:
        """Whatever shape the wrapper wrote is the shape the plan hands back (6.1)."""
        for managed_by in _NON_OBJECT_MANAGED_BY:
            with self.subTest(managed_by=managed_by):
                folder = self.scratch()
                manifest_path = self._managed_manifest(folder, managed_by)
                plan_path = os.path.join(folder, "plan.json")

                code, _stdout, _stderr = self.run_cli(
                    [manifest_path, "--rename", "bw", "--plan-out", plan_path]
                )

                self.assertIsNone(code)
                with open(plan_path, "r", encoding="utf-8") as handle:
                    plan = json.load(handle)
                # Compared as JSON text rather than with assertEqual: `True ==
                # 1` in Python, and carrying a bool back as a bool is the point.
                self.assertEqual(json.dumps(plan["managed_by"]), json.dumps(managed_by))

    def test_an_explicit_null_managed_by_is_not_a_managed_manifest(self) -> None:
        """Presence means a value: an explicit ``null`` reads as no key at all."""
        folder = self.scratch()
        manifest_path = self._managed_manifest(folder, None)

        code, _stdout, _stderr = self.run_cli([manifest_path, "--rename", "bw", "-w"])

        self.assertIsNone(code)
        self.assertEqual(_non_journal_names(folder), ["bw-001.jpg", "lightroom-export.json"])


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


class TestExecutorCommandsRefuseDryRun(_CliTestCase):
    """``--dry-run`` is refused on the three executor commands (P1, round 2).

    ``--rename ... -w --dry-run`` already rehearses through
    ``rename_apply.apply_plan``'s own ``dry_run``. ``--rename-undo``,
    ``--rename-resume`` and ``--rename-finish`` have no such path -- each one
    starts writing (a fresh journal segment, then the actual moves) as soon
    as it runs -- so the global promise that ``--dry-run`` touches no
    destination is kept by refusing the combination outright, before either
    executor call is reached. Each case snapshots size and mtime, not just
    names, so a same-name overwrite would also be caught.
    """

    def test_rename_undo_dry_run_is_refused_and_touches_nothing(self) -> None:
        folder = self.make_folder("box3_017.jpg", "box3_017-b.tif")
        apply_code, _out, _err = self.run_cli([folder, "--rename", "bw", "-w"])
        self.assertIsNone(apply_code)
        before = _snapshot(folder)

        code, stdout, stderr = self.run_cli([folder, "--rename-undo", "--dry-run"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("cannot preview `--rename-undo`", stderr)
        self.assertIn("starts writing to disk", stderr)
        self.assertEqual(_snapshot(folder), before)

    def test_rename_resume_dry_run_is_refused_and_touches_nothing(self) -> None:
        folder = self.make_folder("box3_017.jpg")
        before = _snapshot(folder)

        code, stdout, stderr = self.run_cli([folder, "--rename-resume", "--dry-run"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("cannot preview `--rename-resume`", stderr)
        self.assertIn("starts writing to disk", stderr)
        self.assertEqual(_snapshot(folder), before)

    def test_rename_finish_dry_run_is_refused_and_touches_nothing(self) -> None:
        folder = self.scratch()
        image = _write_bytes(os.path.join(folder, "box3_017.jpg"))
        with open(os.path.join(folder, "box3_017.md"), "w", encoding="utf-8") as handle:
            handle.write("---\nsource_file: box3_017.jpg\n---\ntranscript\n")
        plan_path = os.path.join(folder, "plan.json")
        plan_code, _out, _err = self.run_cli(
            [folder, "--rename", "bw", "--plan-out", plan_path]
        )
        self.assertIsNone(plan_code)
        os.rename(image, os.path.join(folder, "bw-001.jpg"))
        before = _snapshot(folder)

        code, stdout, stderr = self.run_cli(
            ["--rename-finish", plan_path, "--dry-run"]
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("cannot preview `--rename-finish`", stderr)
        self.assertIn("starts writing to disk", stderr)
        self.assertEqual(_snapshot(folder), before)


class TestDryRunDoesNotWritePlanOut(_CliTestCase):
    """P2 round 4: ``--dry-run``'s global promise is that no destination is
    touched -- the plan file ``--plan-out`` writes is a destination exactly
    as much as the changeset already was, and must be skipped the same way."""

    def test_dry_run_with_plan_out_writes_nothing(self) -> None:
        folder = self.make_folder("box3_017.jpg")
        plan_path = os.path.join(folder, "plan.json")

        code, _stdout, stderr = self.run_cli(
            [folder, "--rename", "bw", "--plan-out", plan_path, "--dry-run"]
        )

        self.assertIsNone(code)
        self.assertFalse(os.path.exists(plan_path))
        self.assertIn("--dry-run", stderr)
        self.assertIn("nothing was written", stderr)
        # The preview itself is not a destination -- it still shows the plan.
        self.assertIn("bw-001.jpg", stderr)

    def test_without_dry_run_plan_out_still_writes(self) -> None:
        folder = self.make_folder("box3_017.jpg")
        plan_path = os.path.join(folder, "plan.json")

        code, _stdout, _stderr = self.run_cli(
            [folder, "--rename", "bw", "--plan-out", plan_path]
        )

        self.assertIsNone(code)
        self.assertTrue(os.path.isfile(plan_path))


class TestPlanOutAliasedWithChangesetIsRefused(_CliTestCase):
    """P2 round 4: ``--plan-out`` and the run's own changeset destination
    must not be allowed to name the same file -- whichever write ran second
    would silently overwrite the other's output."""

    def test_plan_out_matching_the_changeset_path_is_a_usage_error(self) -> None:
        folder = self.make_folder("box3_017.jpg")
        aliased_path = os.path.join(folder, f"{os.path.basename(folder)}_changeset.ndjson")

        code, stdout, stderr = self.run_cli(
            [folder, "--rename", "bw", "--changeset", "true", "--plan-out", aliased_path]
        )

        self.assertEqual(code, 2)
        self.assertIn("same file", stderr)
        self.assertFalse(os.path.exists(aliased_path))
        self.assertEqual(stdout, "")

    def test_plan_out_beside_the_changeset_still_works(self) -> None:
        folder = self.make_folder("box3_017.jpg")
        plan_path = os.path.join(folder, "plan.json")

        code, _stdout, _stderr = self.run_cli(
            [folder, "--rename", "bw", "--changeset", "true", "--plan-out", plan_path]
        )

        self.assertIsNone(code)
        self.assertTrue(os.path.isfile(plan_path))
        changeset_path = os.path.join(folder, f"{os.path.basename(folder)}_changeset.ndjson")
        self.assertTrue(os.path.isfile(changeset_path))


class TestPlanOutOntoARenameSourceIsRefused(_CliTestCase):
    """P1: the plan write is an ``os.replace`` over its destination, so a
    ``--plan-out`` that names one of the run's own images or companions
    replaces that file's contents with the plan JSON. It takes no ``-w`` --
    a bare preview run, documented as touching nothing, is enough -- and the
    ``-w`` run that follows then refuses on a stale plan without ever saying
    the file is gone.
    """

    _COMPANION_BYTES = b'{"caption": "the only copy of this text"}'

    def _folder_with_a_companion(self) -> tuple[str, str, str]:
        """Return ``(folder, image, companion)`` for a one-image, one-sidecar folder."""
        folder = self.make_folder("box3_017.jpg")
        image = os.path.join(folder, "box3_017.jpg")
        companion = _write_bytes(os.path.join(folder, "box3_017.json"), self._COMPANION_BYTES)
        return folder, image, companion

    def _read(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    def test_plan_out_onto_a_planned_companion_is_refused(self) -> None:
        folder, _image, companion = self._folder_with_a_companion()

        code, stdout, stderr = self.run_cli(
            [folder, "--rename", "bw", "--plan-out", companion]
        )

        self.assertEqual(code, 2)
        self.assertIn("would rename", stderr)
        self.assertIn(companion, stderr)
        self.assertEqual(self._read(companion), self._COMPANION_BYTES)
        self.assertEqual(stdout, "")

    def test_plan_out_onto_a_planned_image_is_refused(self) -> None:
        folder, image, _companion = self._folder_with_a_companion()
        before = self._read(image)

        code, stdout, stderr = self.run_cli([folder, "--rename", "bw", "--plan-out", image])

        self.assertEqual(code, 2)
        self.assertIn("would rename", stderr)
        self.assertEqual(self._read(image), before)
        self.assertEqual(stdout, "")

    def test_plan_out_spelled_through_a_subfolder_hop_is_refused(self) -> None:
        """The same file, reached by a path no string comparison would match."""
        folder, _image, companion = self._folder_with_a_companion()
        os.mkdir(os.path.join(folder, "sub"))
        detour = os.path.join(folder, "sub", os.pardir, "box3_017.json")

        code, _stdout, stderr = self.run_cli([folder, "--rename", "bw", "--plan-out", detour])

        self.assertEqual(code, 2)
        self.assertIn("would rename", stderr)
        # Both spellings are named: the one given, and the file it turned out to be.
        self.assertIn(detour, stderr)
        self.assertIn(companion, stderr)
        self.assertEqual(self._read(companion), self._COMPANION_BYTES)

    def test_plan_out_through_a_hard_link_to_a_source_is_refused(self) -> None:
        """Filesystem identity, not spelling: a link in another directory
        shares no part of its path with the companion it names."""
        folder, _image, companion = self._folder_with_a_companion()
        alias = os.path.join(self.scratch(), "plan.json")
        try:
            os.link(companion, alias)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"hard links unavailable here: {exc}")

        code, _stdout, stderr = self.run_cli([folder, "--rename", "bw", "--plan-out", alias])

        self.assertEqual(code, 2)
        self.assertIn("would rename", stderr)
        self.assertIn(companion, stderr)
        self.assertEqual(self._read(companion), self._COMPANION_BYTES)

    def test_plan_out_at_a_new_path_still_writes_the_plan(self) -> None:
        """The control: refusing a source must not refuse an ordinary
        destination, including one inside the folder being renamed."""
        folder, _image, companion = self._folder_with_a_companion()
        plan_path = os.path.join(folder, "rename-plan.json")

        code, _stdout, _stderr = self.run_cli(
            [folder, "--rename", "bw", "--plan-out", plan_path]
        )

        self.assertIsNone(code)
        with open(plan_path, encoding="utf-8") as handle:
            written = json.load(handle)
        self.assertEqual(written["entries"][0]["target"], "bw-001.jpg")
        self.assertEqual(self._read(companion), self._COMPANION_BYTES)


def _write_transcript(path: str, source_file: str) -> None:
    """Write a minimal ``.md`` transcript sidecar naming *source_file*."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f'---\nsource_file: "{source_file}"\n---\ntranscript\n')


class _PartialCatalogUndoCase(_CliTestCase):
    """Shared setup: a catalog (``--rename-finish``) renames two images, each
    with a transcript, then puts them back one at a time -- the shape a
    ``--rename-undo`` of a ``--rename-finish`` journal (5.5) can only
    partially finish until the second image comes home too."""

    def _finish_two_images(self, folder: str) -> tuple[str, str]:
        """Plan, then finish-rename two images a catalog has already moved.

        Returns:
            The two images' original basenames (``file102.tif``,
            ``file105.tif``), left as a record of what a full undo restores.
        """
        first = os.path.join(folder, "file102.tif")
        second = os.path.join(folder, "file105.tif")
        _write_bytes(first)
        _write_bytes(second)
        _write_transcript(os.path.join(folder, "file102.md"), "file102.tif")
        _write_transcript(os.path.join(folder, "file105.md"), "file105.tif")
        plan_path = os.path.join(folder, "plan.json")

        plan_code, _out, _err = self.run_cli(
            [folder, "--rename", "bw", "--plan-out", plan_path]
        )
        self.assertIsNone(plan_code)
        with open(plan_path, encoding="utf-8") as handle:
            plan = json.load(handle)
        targets = {entry["path"]: entry["target"] for entry in plan["entries"]}
        self.first_target = os.path.join(folder, targets[first])
        self.second_target = os.path.join(folder, targets[second])

        # The catalog application renames both images itself.
        os.rename(first, self.first_target)
        os.rename(second, self.second_target)

        finish_code, _out, _err = self.run_cli(["--rename-finish", plan_path])
        self.assertIsNone(finish_code)
        return "file102.tif", "file105.tif"

    def _leave_undo_partial(self, folder: str) -> None:
        """Run the setup, then put only the first image back and undo once.

        Leaves the journal open (``in_progress``) and marked partial: the
        first image's companion is home, the second image is still under its
        catalog-renamed name and so is its companion.
        """
        self._finish_two_images(folder)
        os.rename(self.first_target, os.path.join(folder, "file102.tif"))
        code, _out, _err = self.run_cli([folder, "--rename-undo"])
        self.assertIsNone(code)


class TestRenameUndoRetriesAPartialCatalogUndo(_PartialCatalogUndoCase):
    """P4 round: the folder form of ``--rename-undo`` must find a journal a
    previous partial catalog undo left open, not just a closed ``applied``
    one -- ``undo_run`` itself already knows how to retry it (5.5); only
    discovery by folder needed widening."""

    def test_folder_form_finds_and_finishes_the_partial_undo(self) -> None:
        folder = self.scratch()
        self._leave_undo_partial(folder)
        # The catalog has now put the second image back too.
        os.rename(self.second_target, os.path.join(folder, "file105.tif"))

        code, _out, _err = self.run_cli([folder, "--rename-undo"])

        self.assertIsNone(code)
        self.assertEqual(
            _non_journal_names(folder),
            sorted(["file102.tif", "file102.md", "file105.tif", "file105.md", "plan.json"]),
        )


class TestRenameResumeOnAPartialUndoIsANormalOutcome(_PartialCatalogUndoCase):
    """P4 round: ``--rename-resume`` on a partial-undo journal is a correct,
    expected refusal ("there is nothing to finish forward"), not a usage
    mistake -- it must report through the normal outcome path (exit 1), not
    the preflight usage-error path (exit 2)."""

    def test_resume_reports_exit_one_not_a_usage_error(self) -> None:
        folder = self.scratch()
        self._leave_undo_partial(folder)

        code, _out, stderr = self.run_cli([folder, "--rename-resume"])

        self.assertEqual(code, 1)
        self.assertIn("This undo stopped part way", stderr)
        self.assertIn("Run --rename-undo again", stderr)


class TestNoJournalFoundWording(unittest.TestCase):
    """``rename_no_journal_found`` must parse as English for both verbs.

    Regression coverage for a broken article: the message used to read "no an
    applied rename journal was found in:" and "no an in-progress or
    needs-attention rename journal was found in:", both of which stop a
    reader cold on the second word. Pinned directly on the message function
    rather than through the CLI, since this is wording, not wiring.
    """

    def test_undo_branch_reads_as_english(self) -> None:
        problem, _remedy = cli_messages.rename_no_journal_found("/scans", "undo")
        self.assertIn("no applied rename journal was found in:", problem)
        self.assertNotIn("no an applied", problem)

    def test_resume_branch_reads_as_english(self) -> None:
        problem, _remedy = cli_messages.rename_no_journal_found("/scans", "resume")
        self.assertIn(
            "no in-progress or needs-attention rename journal was found in:", problem
        )
        self.assertNotIn("no an in-progress", problem)



class TestNoDestinationLandsOnAFileTheRunNeeds(_CliTestCase):
    """One rule for every rename-mode destination: a file the run reads,
    renames, leaves behind or would recover from is never a legal place to
    write to.

    Six review rounds each patched one instance of the same defect -- a write
    that never asked what was already at its destination. These are the
    instances the per-flag patches did not cover; they belong to one guard,
    so they are asserted as one family.
    """

    _VICTIM = b"THE ONLY COPY OF THIS FILE"

    def _read(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    def test_plan_out_onto_a_left_behind_file_is_refused(self) -> None:
        """A file the run reports as left behind is still a file it depends on."""
        folder = self.make_folder("box3_017.jpg")
        victim = _write_bytes(os.path.join(folder, "box3_017.pdf"), self._VICTIM)

        code, stdout, stderr = self.run_cli(
            [folder, "--rename", "bw", "--plan-out", victim]
        )

        self.assertEqual(code, 2)
        self.assertIn("leaves behind", stderr)
        self.assertIn(victim, stderr)
        self.assertEqual(self._read(victim), self._VICTIM)
        self.assertEqual(stdout, "")

    def test_plan_out_onto_a_left_behind_file_is_refused_under_dry_run(self) -> None:
        """--dry-run answers "what would this command do", so it must say this."""
        folder = self.make_folder("box3_017.jpg")
        victim = _write_bytes(os.path.join(folder, "box3_017.pdf"), self._VICTIM)

        code, _stdout, stderr = self.run_cli(
            [folder, "--rename", "bw", "--plan-out", victim, "--dry-run"]
        )

        self.assertEqual(code, 2)
        self.assertIn("leaves behind", stderr)
        self.assertEqual(self._read(victim), self._VICTIM)

    def test_plan_out_onto_an_unplanned_photo_is_refused(self) -> None:
        """A photo the manifest never listed is not in the plan's entries, so
        only a check that looks at the folder can see it."""
        folder = self.make_folder("box3_017.jpg", "box3_099.jpg")
        bystander = _write_bytes(os.path.join(folder, "box3_099.jpg"), self._VICTIM)
        manifest = _write_manifest(folder, [{"path": os.path.join(folder, "box3_017.jpg")}])

        code, _stdout, stderr = self.run_cli(
            [manifest, "--rename", "bw", "--plan-out", bystander]
        )

        self.assertEqual(code, 2)
        self.assertIn("did not plan", stderr)
        self.assertEqual(self._read(bystander), self._VICTIM)

    def test_plan_out_onto_an_existing_journal_is_refused(self) -> None:
        """The journal is the undo record for the last rename of this folder."""
        folder = self.make_folder("box3_017.jpg")
        journal = _write_bytes(
            rename_apply.journal_path_for(folder, "2020-01-01T00:00:00Z_abcd1234"),
            self._VICTIM,
        )

        code, _stdout, stderr = self.run_cli(
            [folder, "--rename", "bw", "--plan-out", journal]
        )

        self.assertEqual(code, 2)
        self.assertIn("--rename-undo", stderr)
        self.assertEqual(self._read(journal), self._VICTIM)

    def test_plan_out_onto_the_input_manifest_is_refused(self) -> None:
        """The manifest is the run's own input; the plan write would replace it."""
        folder = self.make_folder("box3_017.jpg")
        manifest = _write_manifest(folder, [{"path": os.path.join(folder, "box3_017.jpg")}])
        before = self._read(manifest)

        code, _stdout, stderr = self.run_cli(
            [manifest, "--rename", "bw", "--plan-out", manifest]
        )

        self.assertEqual(code, 2)
        self.assertIn("reads", stderr)
        self.assertEqual(self._read(manifest), before)

    def test_plan_out_onto_a_name_the_run_renames_to_is_refused(self) -> None:
        """A target is a destination of this run too, and the apply would
        otherwise die on it long after the plan file was written."""
        folder = self.make_folder("box3_017.jpg")
        _write_bytes(os.path.join(folder, "box3_017.json"), self._VICTIM)
        target = os.path.join(folder, "bw-001.json")

        code, _stdout, stderr = self.run_cli(
            [folder, "--rename", "bw", "--plan-out", target]
        )

        self.assertEqual(code, 2)
        self.assertIn("would rename a file to", stderr)
        self.assertFalse(os.path.exists(target))

    def test_plan_out_spelled_differently_from_the_changeset_is_refused(self) -> None:
        """The dest-vs-dest check matched two strings, so a case-variant
        spelling of the same destination went through and the plan was
        overwritten by the changeset that followed it."""
        folder = self.make_folder("box3_017.jpg")
        changeset_name = f"{os.path.basename(folder)}_changeset.ndjson"
        shouted = os.path.join(folder, changeset_name.upper())

        code, stdout, stderr = self.run_cli(
            [folder, "--rename", "bw", "--changeset", "true", "--plan-out", shouted]
        )

        self.assertEqual(code, 2)
        self.assertIn("same file", stderr)
        self.assertFalse(os.path.exists(shouted))
        self.assertFalse(os.path.exists(os.path.join(folder, changeset_name)))
        self.assertEqual(stdout, "")

    def test_dry_run_still_refuses_a_plan_out_in_a_missing_directory(self) -> None:
        """The writability probe is not --dry-run exempt either: one seam,
        one behavior, and the other two modes already worked this way."""
        folder = self.make_folder("box3_017.jpg")
        missing = os.path.join(folder, "no-such-dir", "plan.json")

        code, _stdout, stderr = self.run_cli(
            [folder, "--rename", "bw", "--plan-out", missing, "--dry-run"]
        )

        self.assertEqual(code, 2)
        self.assertIn("--plan-out", stderr)

    def test_the_refusal_names_where_the_victim_actually_is(self) -> None:
        """The message has to name a location the user can act on.

        A companion's, a left-behind file's and a target's path are all built
        by joining the *folder as the user spelled it*, so a run given a
        relative folder carries relative paths through the plan. Printed raw
        beside a destination the user spelled some other way, the two lines
        name one file with two strings that match nothing and resolve against
        a working directory the message never states -- so the user is told a
        file is at risk without being told which one.
        """
        folder = self.make_folder("box3_017.jpg")
        companion = _write_bytes(os.path.join(folder, "box3_017.json"), self._VICTIM)
        os.mkdir(os.path.join(folder, "sub"))
        detour = os.path.join(folder, "sub", os.pardir, "box3_017.json")
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(os.path.dirname(folder))

        code, _stdout, stderr = self.run_cli(
            [os.path.basename(folder), "--rename", "bw", "--plan-out", detour]
        )

        self.assertEqual(code, 2)
        self.assertIn(companion, stderr)
        self.assertEqual(self._read(companion), self._VICTIM)

    def test_an_ordinary_destination_beside_the_run_still_writes(self) -> None:
        """The control: a new file, inside the folder being renamed, with a
        left-behind file and a journal both sitting next to it."""
        folder = self.make_folder("box3_017.jpg")
        _write_bytes(os.path.join(folder, "box3_017.pdf"), self._VICTIM)
        _write_bytes(
            rename_apply.journal_path_for(folder, "2020-01-01T00:00:00Z_abcd1234"),
            self._VICTIM,
        )
        plan_path = os.path.join(folder, "rename-plan.json")

        code, _stdout, _stderr = self.run_cli(
            [folder, "--rename", "bw", "--changeset", "true", "--plan-out", plan_path]
        )

        self.assertIsNone(code)
        with open(plan_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["entries"][0]["target"], "bw-001.jpg")
        self.assertTrue(os.path.isfile(_changeset_path(folder)))


if __name__ == "__main__":
    unittest.main()
