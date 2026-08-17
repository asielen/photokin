"""Contracts ``cli.main`` makes to its callers, none of which had a test before.

Each one is load-bearing for the Lightroom plugin or for anyone piping the CLI:
the stderr log handler reaches the package logger and only the package logger, a
warning raised deep in ``exiftool.hydrate`` actually becomes visible, a flag that
would be silently ignored is refused instead, stdout carries the result JSON and
nothing else, and every cost gate stops a run before a single model call has been
paid for -- covering both destinations a run writes, and exempting the dry run
that needs no binary at all.

Every provider, ExifTool and analysis dependency is mocked; nothing here opens a
socket, launches a subprocess, or writes outside a temporary directory.
"""

import io
import json
import logging
import os
import stat
import sys
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

from photokin import cli

# Blanked rather than removed: every one of these is read through a falsy-default
# lookup, so an empty value pins the documented default (INFO logging, no
# configured ExifTool, writes off) whatever the developer's shell exports.
_NEUTRAL_ENV: dict[str, str] = {
    "MEL_VERBOSE": "",
    "MEL_DEBUG": "",
    "EXIFTOOL_PATH": "",
    "EXIFTOOL_WRITE_ENABLED": "",
    "EXIFTOOL_FIELDS": "",
}


def _write_manifest(folder: str) -> str:
    """Write a one-item manifest into *folder* and return its path."""
    manifest_path = os.path.join(folder, "batch.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump({"items": [{"path": os.path.join(folder, "box3_025.jpg")}]}, handle)
    return manifest_path


def _analyzer_logging(message: str, payload: dict[str, object]) -> Callable[..., dict[str, object]]:
    """Return an analyzer stand-in that logs *message* and returns *payload*.

    Args:
        message: Diagnostic text emitted on the ``photokin.core`` logger, standing
            in for the batch-completion and skipped-group lines the real analyzers
            produce.
        payload: The analysis result the stand-in returns.

    Returns:
        A callable accepting the real analyzer's arguments and ignoring them.
    """

    def _analyze(*_args: object, **_kwargs: object) -> dict[str, object]:
        logging.getLogger("photokin.core").warning(message)
        return payload

    return _analyze


def _stream_running_the_hydrator(
    *,
    metadata_hydrator: Callable[[list[dict[str, object]]], None],
    **_ignored: object,
) -> dict[str, object]:
    """Stand in for ``process_manifest_stream``, invoking only the injected hydrator."""
    metadata_hydrator([{"path": "C:/scans/box3_025.jpg", "metadata": {"userComment": ""}}])
    return {"results": {}}


def _stream_filling_the_changeset(
    message: str, payload: dict[str, object]
) -> Callable[..., dict[str, object]]:
    """Return a ``process_manifest_stream`` stand-in that produces a changeset record.

    Args:
        message: Diagnostic text emitted on the ``photokin.core`` logger, standing
            in for the per-group lines the real stream logs while analyzing.
        payload: The aggregate result the stand-in returns.

    Returns:
        A callable accepting the real stream's keyword arguments and using only
        the injected ``changeset_writer``, so the changeset file the CLI opened
        holds a real record by the time the ExifTool apply step reads it.
    """

    def _stream(
        *, changeset_writer: Callable[[str], None] | None = None, **_ignored: object
    ) -> dict[str, object]:
        logging.getLogger("photokin.core").warning(message)
        if changeset_writer is not None:
            changeset_writer(
                json.dumps(
                    {
                        "path": "C:/scans/box3_025.jpg",
                        "fields": {"EXIF:UserComment": "portrait"},
                    }
                )
            )
        return payload

    return _stream


class _CliTestCase(unittest.TestCase):
    """Base for tests that execute ``cli.main`` in-process.

    ``main`` installs a handler on a module-level logger, so the surrounding
    process state is both an input and an output of every test here: the slate is
    cleared going in and the package logger is restored coming out.
    """

    def setUp(self) -> None:
        self.package_logger = logging.getLogger("photokin")
        self.root_logger = logging.getLogger()
        self._original_level = self.package_logger.level
        self._remove_cli_handlers()

    def tearDown(self) -> None:
        self._remove_cli_handlers()
        self.package_logger.setLevel(self._original_level)

    def _remove_cli_handlers(self) -> None:
        """Detach every handler this CLI installs, from both logger scopes."""
        for logger in (self.package_logger, self.root_logger):
            for handler in list(logger.handlers):
                if handler.get_name() == cli._LOG_HANDLER_NAME:
                    logger.removeHandler(handler)
                    handler.close()

    def cli_handlers(self) -> list[logging.Handler]:
        """Return the handlers ``main`` installed on the ``photokin`` logger."""
        return [h for h in self.package_logger.handlers if h.get_name() == cli._LOG_HANDLER_NAME]

    def run_cli(self, argv: list[str]) -> tuple[int | None, str, str]:
        """Run ``cli.main`` with *argv*, returning its exit code, stdout and stderr.

        Args:
            argv: Arguments after the program name.

        Returns:
            ``(exit_code, stdout, stderr)``, where the exit code is None when
            ``main`` returned without raising ``SystemExit``.
        """
        stdout, stderr = io.StringIO(), io.StringIO()
        code: int | None = None
        with patch.dict(os.environ, _NEUTRAL_ENV), patch.object(sys, "argv", ["photokin", *argv]):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    cli.main()
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
        return code, stdout.getvalue(), stderr.getvalue()


class TestCliLoggingHandler(_CliTestCase):
    """One handler, on the package logger, surviving repeated in-process runs.

    The package logger is the only scope that catches every module's
    ``getLogger(__name__)``; the root logger belongs to whatever process embeds
    this code, so claiming it would duplicate or reformat an application's own
    output. Reuse matters just as much: a handler binds the stream it was built
    with, so a stacked second copy both double-prints and writes into a stream
    the previous call has already abandoned.

    The fixture is a logged diagnostic from a stubbed analyzer rather than a usage
    error, so these stay tests of the handler alone: a regression in one of the
    pre-flight guards belongs to that guard's own tests and should not turn these
    red as well.
    """

    _DIAGNOSTIC = "Skipping group 'album': multipage set has no primary front"

    def test_handler_lands_on_the_package_logger_and_not_the_root(self) -> None:
        root_handlers_before = list(self.root_logger.handlers)
        analyzer = _analyzer_logging(self._DIAGNOSTIC, {"results": {}})

        with patch("photokin.cli.analyze_folder", analyzer):
            code, _, stderr = self.run_cli(["--folder", "./scans/"])

        self.assertIsNone(code)
        self.assertEqual(len(self.cli_handlers()), 1)
        self.assertEqual(self.root_logger.handlers, root_handlers_before)
        self.assertEqual(self.package_logger.level, logging.INFO)
        # Matched whole rather than searched: the "[WARNING] " prefix is this
        # CLI's own formatter, and an exact match also rules out a second copy
        # of the record arriving through some other handler.
        self.assertEqual(stderr, f"[WARNING] {self._DIAGNOSTIC}\n")

    def test_repeated_runs_reuse_the_handler_and_follow_the_current_stderr(self) -> None:
        analyzer = _analyzer_logging(self._DIAGNOSTIC, {"results": {}})
        argv = ["--folder", "./scans/"]

        with patch("photokin.cli.analyze_folder", analyzer):
            first_code, _, first_stderr = self.run_cli(argv)
            second_code, _, second_stderr = self.run_cli(argv)
            third_code, _, third_stderr = self.run_cli(argv)

        self.assertEqual([first_code, second_code, third_code], [None, None, None])
        self.assertEqual(len(self.cli_handlers()), 1)
        # Each run captures a fresh stream, so a handler still bound to the first
        # run's stderr leaves the second and third captures empty.
        for stderr in (first_stderr, second_stderr, third_stderr):
            self.assertEqual(stderr, f"[WARNING] {self._DIAGNOSTIC}\n")


class TestHydrationWarningVisibility(_CliTestCase):
    """The invisible-warning bug: ``exiftool.hydrate`` logs, and nobody listened.

    ``hydrate_user_comments`` degrades to a warning when no ExifTool binary
    resolves, which silently costs the run every ``EXIF:UserComment`` it would
    have recovered. With no handler anywhere in the ``photokin`` hierarchy that
    warning went nowhere, so the run looked complete.
    """

    def test_hydration_warning_reaches_stderr_through_the_cli_handler(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            manifest_path = _write_manifest(folder)

            with patch(
                "photokin.exiftool.hydrate.resolve_exiftool_path",
                side_effect=FileNotFoundError("ExifTool not found."),
            ), patch("photokin.cli.process_manifest_stream", _stream_running_the_hydrator):
                code, stdout, stderr = self.run_cli(["--manifest", manifest_path])

        self.assertIsNone(code)
        # The "[WARNING] " prefix is this CLI's own formatter, so its presence
        # proves the record travelled through the installed handler rather than
        # logging's last-resort fallback or a test-runner handler.
        self.assertIn("[WARNING] Skipping EXIF:UserComment hydration: ExifTool not found.", stderr)
        self.assertNotIn("Skipping EXIF:UserComment hydration", stdout)


class TestOutputFileOutsideManifestMode(_CliTestCase):
    """``--output-file`` used to be accepted, ignored, and exited 0.

    Folder and single-photo runs print to stdout and never read the flag, so the
    old behavior produced no file, no error, and a success code -- indistinguishable
    from a run whose results were written.
    """

    def test_folder_input_with_output_file_exits_two_with_its_message(self) -> None:
        analyze_folder = Mock()

        with patch("photokin.cli.analyze_folder", analyze_folder):
            code, stdout, stderr = self.run_cli(
                ["--folder", "./scans/", "--output-file", "results.ndjson"]
            )

        self.assertEqual(code, 2)
        lines = stderr.splitlines()
        self.assertEqual(
            lines[0],
            "[ERROR] --output-file is only supported in --manifest mode; saw "
            "--folder ./scans/ with --output-file results.ndjson, which would be ignored.",
        )
        self.assertEqual(
            lines[1],
            "Try: redirect stdout instead: photokin --folder ./scans/ > results.ndjson",
        )
        analyze_folder.assert_not_called()
        self.assertEqual(stdout, "")

    def test_single_photo_input_with_output_file_exits_two_with_its_message(self) -> None:
        analyze_photo = Mock()

        with patch("photokin.cli.analyze_photo", analyze_photo):
            code, stdout, stderr = self.run_cli(["box3_025.jpg", "--output-file", "results.json"])

        self.assertEqual(code, 2)
        lines = stderr.splitlines()
        self.assertEqual(
            lines[0],
            "[ERROR] --output-file is only supported in --manifest mode; saw "
            "box3_025.jpg with --output-file results.json, which would be ignored.",
        )
        self.assertEqual(
            lines[1],
            "Try: redirect stdout instead: photokin box3_025.jpg > results.json",
        )
        analyze_photo.assert_not_called()
        self.assertEqual(stdout, "")

    def test_output_file_with_no_input_at_all_names_the_defaulted_mode(self) -> None:
        with patch("photokin.cli.analyze_photo") as analyze_photo:
            code, _, stderr = self.run_cli(["--output-file", "results.json"])

        # Only the problem line is pinned here: with no path to name, the
        # remedy interpolates the placeholder into the suggested command.
        self.assertEqual(code, 2)
        self.assertEqual(
            stderr.splitlines()[0],
            "[ERROR] --output-file is only supported in --manifest mode; saw "
            "single-photo input with --output-file results.json, which would be ignored.",
        )
        analyze_photo.assert_not_called()


class TestStdoutIsJsonOnly(_CliTestCase):
    """Callers parse stdout, so a diagnostic printed there corrupts the result.

    Moving user-facing messages onto the logger is what makes this hold: the
    handler writes to stderr, leaving stdout as a single parseable document.
    Parsing the whole capture is the assertion -- one stray line and
    ``json.loads`` fails.

    The two analyzer cases can only speak for the analyzer they replace, so the
    manifest case runs the post-analysis code for real: that is the stretch of
    ``main`` that still emits diagnostics of its own after the last mock has
    returned, and it ends in the very ``print`` the plugin parses.
    """

    def test_folder_run_emits_only_json_on_stdout(self) -> None:
        payload: dict[str, object] = {
            "results": {"C:/scans/box3_025.jpg": {"keywords": ["portrait"]}},
            "errors": {},
        }
        diagnostic = "Skipping group 'album': multipage set has no primary front"

        with patch(
            "photokin.cli.analyze_folder", _analyzer_logging(diagnostic, payload)
        ):
            code, stdout, stderr = self.run_cli(["--folder", "./scans/"])

        self.assertIsNone(code)
        self.assertEqual(json.loads(stdout), payload)
        self.assertIn(diagnostic, stderr)
        self.assertNotIn(diagnostic, stdout)

    def test_single_photo_run_emits_only_json_on_stdout(self) -> None:
        payload: dict[str, object] = {
            "result": {"C:/scans/box3_025.jpg": {"keywords": ["portrait"]}}
        }
        diagnostic = "Skipping archival upload for provider anthropic"

        with patch(
            "photokin.cli.analyze_photo", _analyzer_logging(diagnostic, payload)
        ):
            code, stdout, stderr = self.run_cli(["box3_025.jpg", "--provider", "anthropic"])

        self.assertIsNone(code)
        self.assertEqual(json.loads(stdout), payload)
        self.assertIn(diagnostic, stderr)
        self.assertNotIn(diagnostic, stdout)

    def test_manifest_changeset_run_keeps_the_exiftool_status_off_stdout(self) -> None:
        """The apply step reports into the same stdout the result JSON is printed on.

        With ``--changeset true`` and no ``--output-file`` there is no NDJSON file
        to append the ExifTool status record to, so it goes to the logger instead
        -- and ``main`` then prints the aggregate result to stdout. Nothing but the
        provider layer is replaced here: ``_apply_exiftool_changeset`` really runs,
        so a status record written to stdout lands inside the plugin's JSON.
        """
        payload: dict[str, object] = {
            "results": {"C:/scans/box3_025.jpg": {"keywords": ["portrait"]}}
        }
        diagnostic = "Skipping group 'album': multipage set has no primary front"
        summary = {
            "files_seen": 1,
            "files_written": 1,
            "tags_written": 1,
            "errors": [],
            "warnings": [],
        }

        with tempfile.TemporaryDirectory() as folder:
            manifest_path = _write_manifest(folder)
            # A real file rather than a patched resolver: the ExifTool pre-flight
            # resolves this path itself, and nothing here ever executes it.
            exiftool_path = os.path.join(folder, "exiftool.exe")
            with open(exiftool_path, "w", encoding="utf-8"):
                pass

            with patch(
                "photokin.cli.process_manifest_stream",
                _stream_filling_the_changeset(diagnostic, payload),
            ), patch("photokin.cli.apply_changeset", return_value=summary) as apply:
                code, stdout, stderr = self.run_cli(
                    [
                        "--manifest",
                        manifest_path,
                        "--changeset",
                        "true",
                        "--exiftool-write",
                        "true",
                        "--exiftool-path",
                        exiftool_path,
                    ]
                )

        self.assertIsNone(code)
        apply.assert_called_once()
        self.assertEqual(json.loads(stdout), payload)
        self.assertIn(diagnostic, stderr)
        self.assertIn('"run": "exiftool_apply"', stderr)
        self.assertNotIn("[ExifTool]", stdout)


class TestExiftoolPreflight(_CliTestCase):
    """A write the run cannot perform has to be caught before the run is paid for.

    ``apply_changeset`` looks for the binary only after the whole batch has been
    analyzed, so the failure used to arrive after every model call had been
    billed. The exit code alone would not prove the ordering: the load-bearing
    claim is that the analysis stream -- the only thing here that spends money --
    is never entered. A dry run is the one exemption: it reports what it would
    write without invoking the binary, so demanding one would block a preview
    that needs nothing installed.
    """

    def test_unresolvable_exiftool_stops_before_the_batch_is_analyzed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            manifest_path = _write_manifest(folder)
            missing_exiftool = os.path.join(folder, "nosuchdir", "exiftool.exe")
            stream = Mock(return_value={"results": {}})

            with patch("photokin.cli.process_manifest_stream", stream):
                code, stdout, stderr = self.run_cli(
                    [
                        "--manifest",
                        manifest_path,
                        "--changeset",
                        "true",
                        "--exiftool-write",
                        "true",
                        "--exiftool-path",
                        missing_exiftool,
                    ]
                )

            # Asserted ahead of the exit code: a regression that lets the batch
            # run and then fails still exits 2, so the exit code would report
            # success at the very moment the money has already been spent.
            self.assertFalse(
                stream.called,
                "analysis must not start when ExifTool cannot be resolved - the run "
                "would cost money and then fail on the write it was asked for",
            )
            self.assertEqual(code, 2)
            lines = stderr.splitlines()
            self.assertEqual(
                lines[0],
                "[ERROR] --changeset true needs ExifTool to write the results, but no "
                f"ExifTool binary was found (configured path: {missing_exiftool}).",
            )
            self.assertTrue(lines[1].startswith("Try: "))
            self.assertEqual(stdout, "")
            self.assertFalse(os.path.exists(os.path.join(folder, "changeset.ndjson")))

    def test_a_dry_run_proceeds_with_no_exiftool_binary_at_all(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            manifest_path = _write_manifest(folder)
            stream = Mock(return_value={"results": {}})

            with patch(
                "photokin.cli.resolve_exiftool_path",
                side_effect=FileNotFoundError("ExifTool not found."),
            ), patch("photokin.cli.process_manifest_stream", stream), patch(
                "photokin.cli._apply_exiftool_changeset"
            ):
                code, _, stderr = self.run_cli(
                    [
                        "--manifest",
                        manifest_path,
                        "--changeset",
                        "true",
                        "--exiftool-write",
                        "true",
                        "--dry-run",
                    ]
                )

            self.assertIsNone(code)
            self.assertNotIn("needs ExifTool", stderr)
            stream.assert_called_once()


class TestOutputFilePreflight(_CliTestCase):
    """The aggregate ``.json`` destination is only opened once the batch is done.

    Unlike the streaming ``.ndjson`` path, which fails on its truncate-on-open
    before the first model call, an unwritable ``.json`` destination used to
    discard a completed batch at the moment of writing it out.
    """

    def test_missing_json_output_directory_fails_before_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            manifest_path = _write_manifest(folder)
            missing_dir = os.path.join(folder, "nosuchdir")
            stream = Mock(return_value={"results": {}})

            with patch("photokin.cli.process_manifest_stream", stream):
                code, stdout, stderr = self.run_cli(
                    [
                        "--manifest",
                        manifest_path,
                        "--output-file",
                        os.path.join(missing_dir, "results.json"),
                    ]
                )

            self.assertFalse(
                stream.called,
                "analysis must not start when the --output-file directory is missing - "
                "the run would cost money and then have nowhere to put its results",
            )
            self.assertEqual(code, 2)
            lines = stderr.splitlines()
            self.assertEqual(
                lines[0], f"[ERROR] --output-file directory does not exist: {missing_dir}"
            )
            self.assertEqual(
                lines[1],
                "Try: create the directory first, or point --output-file at an existing one",
            )
            self.assertEqual(stdout, "")

    @unittest.skipUnless(os.name == "nt", "read-only files only block writes on Windows")
    def test_read_only_json_output_fails_before_analysis_and_keeps_the_old_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            manifest_path = _write_manifest(folder)
            out_path = os.path.join(folder, "results.json")
            with open(out_path, "w", encoding="utf-8") as handle:
                handle.write('{"previous": "run"}')
            os.chmod(out_path, stat.S_IREAD)
            stream = Mock(return_value={"results": {}})

            try:
                with patch("photokin.cli.process_manifest_stream", stream):
                    code, _, stderr = self.run_cli(
                        ["--manifest", manifest_path, "--output-file", out_path]
                    )

                self.assertFalse(
                    stream.called,
                    "analysis must not start when --output-file is read-only - the run "
                    "would cost money and then be discarded at the moment of writing",
                )
                self.assertEqual(code, 2)
                lines = stderr.splitlines()
                self.assertEqual(
                    lines[0],
                    f"[ERROR] --output-file already exists and is not writable: {out_path}",
                )
                self.assertTrue(lines[1].startswith("Try: "))
                with open(out_path, "r", encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), '{"previous": "run"}')
            finally:
                os.chmod(out_path, stat.S_IWRITE | stat.S_IREAD)

    def test_writable_json_output_survives_the_probe_and_leaves_no_debris(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            manifest_path = _write_manifest(folder)
            out_path = os.path.join(folder, "results.json")
            payload = {"results": {"C:/scans/box3_025.jpg": {"keywords": ["portrait"]}}}

            with patch("photokin.cli.process_manifest_stream", return_value=payload):
                code, stdout, _ = self.run_cli(
                    ["--manifest", manifest_path, "--output-file", out_path]
                )

            self.assertIsNone(code)
            self.assertEqual(stdout, "")
            with open(out_path, "r", encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), payload)
            # Listing the directory rather than probing one name catches debris
            # from either temp file: the atomic write's "<out>.tmp" sibling and
            # the pre-flight's own uniquely named probe.
            self.assertEqual(sorted(os.listdir(folder)), ["batch.json", "results.json"])

    def test_the_probe_leaves_a_pre_existing_tmp_sibling_untouched(self) -> None:
        # "<out>.tmp" belongs to the atomic write, so the pre-flight must not
        # probe with it: opening that name "w" would truncate a real file
        # sitting there and then delete it outright.
        with tempfile.TemporaryDirectory() as folder:
            out_path = os.path.join(folder, "results.json")
            tmp_sibling = out_path + ".tmp"
            with open(tmp_sibling, "w", encoding="utf-8") as handle:
                handle.write("PREVIOUS RUN TEMP CONTENT")

            cli._preflight_output_file(out_path)

            with open(tmp_sibling, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "PREVIOUS RUN TEMP CONTENT")
            self.assertEqual(os.listdir(folder), ["results.json.tmp"])


class TestChangesetPreflight(_CliTestCase):
    """The changeset NDJSON is a second destination, held to the same gate.

    ``--changeset true`` derives its own path from ``--output-file``, and both
    files are created with a truncating ``open(..., "w")`` before the first
    model call. Validating only the output file would let a run wipe the
    previous changeset on its way to failing on the path it never checked.
    """

    def test_both_the_output_and_the_derived_changeset_path_are_preflighted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            manifest_path = _write_manifest(folder)
            out_path = os.path.join(folder, "results.ndjson")
            preflight = Mock()

            with patch("photokin.cli._preflight_output_file", preflight), patch(
                "photokin.cli.process_manifest_stream", return_value={"results": {}}
            ), patch("photokin.cli._apply_exiftool_changeset"):
                code, _, _ = self.run_cli(
                    [
                        "--manifest",
                        manifest_path,
                        "--output-file",
                        out_path,
                        "--changeset",
                        "true",
                        "--exiftool-write",
                        "false",
                    ]
                )

            self.assertIsNone(code)
            self.assertEqual(
                [call.args[0] for call in preflight.call_args_list],
                [out_path, os.path.join(folder, "results_changeset.ndjson")],
            )

    def test_an_unusable_output_extension_aborts_with_the_old_changeset_intact(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            manifest_path = _write_manifest(folder)
            changeset_path = os.path.join(folder, "changeset.ndjson")
            with open(changeset_path, "w", encoding="utf-8") as handle:
                handle.write("PREVIOUS RUN CONTENT\n")
            out_path = os.path.join(folder, "results.txt")
            stream = Mock(return_value={"results": {}})

            with patch("photokin.cli.process_manifest_stream", stream):
                code, stdout, stderr = self.run_cli(
                    [
                        "--manifest",
                        manifest_path,
                        "--output-file",
                        out_path,
                        "--changeset",
                        "true",
                        "--exiftool-write",
                        "false",
                    ]
                )

            self.assertFalse(
                stream.called,
                "analysis must not start when --output-file has an extension the run "
                "cannot write - the run would cost money and then be discarded",
            )
            self.assertEqual(code, 2)
            self.assertEqual(
                stderr.splitlines()[0],
                f"[ERROR] --output-file must end with .ndjson or .json; got {out_path}.",
            )
            self.assertEqual(stdout, "")
            with open(changeset_path, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "PREVIOUS RUN CONTENT\n")


if __name__ == "__main__":
    unittest.main()
