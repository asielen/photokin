"""CLI rough edges from issue #15."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from photokin import cli_messages, rename_apply
from photokin.tests.test_cli_input_surface import _CliTestCase, _StreamSpy
from photokin.tests.test_cli_preflight import _envelope_records, _ndjson_lines


class TestModeOnlyFlags(_CliTestCase):
    """A flag only one mode reads is refused without that mode."""

    def test_each_rename_or_revert_flag_is_refused_without_its_mode(self) -> None:
        folder = self.make_folder()
        cases = [
            (["--plan-out", "plan.json"], "--plan-out", "--rename"),
            (["--digits", "5"], "--digits", "--rename"),
            (["--order", "natural"], "--order", "--rename"),
            (["--undated", "nodate"], "--undated", "--rename"),
            (["--today", "2026-01-01"], "--today", "--rename"),
            (["--companions", "pdf"], "--companions", "--rename"),
            (["--revert-paths", "a.jpg"], "--revert-paths", "--revert-changeset"),
            (["--revert-match", "*"], "--revert-match", "--revert-changeset"),
            (["--force"], "--force", "--revert-changeset"),
        ]
        for extra, flag, mode in cases:
            with self.subTest(flag=flag):
                # Arrange
                stream = _StreamSpy()

                # Act
                with patch("photokin.cli.process_manifest_stream", stream):
                    code, _stdout, stderr = self.run_cli([folder, "--dry-run", *extra])

                # Assert
                self.assertEqual(code, 2)
                self.assertFalse(stream.called)
                problem, _remedy = self.usage_error(stderr)
                self.assertEqual(problem, f"[ERROR] {cli_messages.flag_needs_mode(flag, mode)[0]}")


class TestRenameUndoTakesAFolderAfterTheFlag(_CliTestCase):
    """``--rename-undo ./scans`` means the folder, not a journal named ./scans."""

    def test_a_folder_given_as_the_journal_is_used_as_the_folder(self) -> None:
        # Arrange
        folder = self.make_folder()

        # Act
        with patch("photokin.cli.rename_apply.latest_journal", return_value=None) as latest:
            code, _stdout, stderr = self.run_cli(["--rename-undo", folder])

        # Assert
        self.assertEqual(code, 2)
        self.assertEqual(os.path.normcase(latest.call_args.args[0]), os.path.normcase(folder))
        self.assertNotIn("permissions", stderr)


class TestVersion(_CliTestCase):
    """``--version`` prints the version and exits 0."""

    def test_version(self) -> None:
        # Act
        code, stdout, _stderr = self.run_cli(["--version"])

        # Assert
        self.assertIn(code, (0, None))
        self.assertTrue(stdout.startswith("photokin "))


def test_a_missing_journal_says_so(tmp_path: os.PathLike) -> None:
    # Act / Assert
    with pytest.raises(rename_apply.RenamePreflightError, match="does not exist"):
        rename_apply.read_journal(os.path.join(tmp_path, "nope.ndjson"))


def test_a_folder_is_not_a_journal(tmp_path: os.PathLike) -> None:
    # Act / Assert
    with pytest.raises(rename_apply.RenamePreflightError, match="folder, not a rename journal"):
        rename_apply.read_journal(str(tmp_path))


def test_destination_remedies_name_the_flag_that_failed() -> None:
    # Act
    remedies = [
        cli_messages.output_dir_missing("--plan-out", "C:/x")[1],
        cli_messages.output_not_writable("--plan-out", "C:/x/p.json")[1],
        cli_messages.output_destination_not_writable("--plan-out", "C:/x/p.json", "denied")[1],
    ]

    # Assert
    assert all("--plan-out" in r and "--output-file" not in r for r in remedies)


@pytest.mark.parametrize(
    ("role", "flag"), [("--changeset output", "--output-file"), ("-v's log file", "--log-file")]
)
def test_a_derived_destinations_remedy_names_the_flag_that_moves_it(role: str, flag: str) -> None:
    # Act
    remedy = cli_messages.output_not_writable(role, "C:/x/y")[1]

    # Assert
    assert flag in remedy and role not in remedy


class TestModeOnlyRefusalReachesTheEnvelope(_CliTestCase):
    """A launcher watching the results file sees the refusal."""

    def test_the_refusal_is_recorded_as_fatal(self) -> None:
        # Arrange
        folder = self.make_folder()
        out = os.path.join(folder, "out.ndjson")

        # Act
        with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
            code, _stdout, _stderr = self.run_cli([folder, "--output-file", out, "--digits", "4"])

        # Assert
        self.assertEqual(code, 2)
        records = _envelope_records(_ndjson_lines(out))
        self.assertEqual([r["run"] for r in records], ["start", "fatal"])
