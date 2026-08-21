"""Contracts ``cli.main`` makes to its callers, none of which had a test before.

Each one is load-bearing for the Lightroom plugin or for anyone piping the CLI:
the stderr log handler reaches the package logger and only the package logger, a
warning raised deep in ``exiftool.hydrate`` actually becomes visible, stdout
carries the result JSON and nothing else, and every cost gate stops a run before
a single model call has been paid for -- covering both destinations a run writes.

Phase C2 turned two of these on their head. ``--output-file`` outside manifest
mode was a Phase A stopgap error and is now real support, so the class that
pinned the refusal pins the writes instead; and ``--dry-run`` stops after the
plan summary rather than running, so the pre-flight it used to be exempt from
now applies to it.

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
#
# ``EXIFTOOL_WRITE_ENABLED`` is the exception, and the reason ``run_cli`` takes
# ``unset_env``. ``_parse_bool_env`` returns its ``default`` argument only when
# ``os.environ.get`` answers None, so blanking that variable pins false by a
# route that never reaches the default -- which is the very literal the flipped
# write default lives in. A test meaning to exercise that default has to remove
# the variable, not blank it.
_NEUTRAL_ENV: dict[str, str] = {
    "MEL_VERBOSE": "",
    "MEL_DEBUG": "",
    "EXIFTOOL_PATH": "",
    "EXIFTOOL_WRITE_ENABLED": "",
    "EXIFTOOL_FIELDS": "",
    # Pinned rather than blanked: with no provider chosen the CLI reads the
    # installed SDKs, which differ between machines.
    "LLM_PROVIDER": "openai",
}

#: The first line of the plan summary, which every run now prints before the
#: first model call.
_PLAN_HEADER = "Plan for this run:"


def _write_manifest(folder: str) -> str:
    """Write a one-item manifest into *folder*, with the file it names, and return it.

    The image is created as well as listed: since C2 the CLI refuses a manifest
    whose items point at files that are not there, before anything is analyzed.

    Args:
        folder: Directory to write into.

    Returns:
        The manifest path.
    """
    image_path = os.path.join(folder, "box3_025.jpg")
    with open(image_path, "w", encoding="utf-8"):
        pass
    manifest_path = os.path.join(folder, "batch.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump({"items": [{"path": image_path}]}, handle)
    return manifest_path


def _analyzer_logging(message: str, payload: dict[str, object]) -> Callable[..., dict[str, object]]:
    """Return an analyzer stand-in that logs *message* and returns *payload*.

    Args:
        message: Diagnostic text emitted on the ``photokin.core`` logger, standing
            in for the per-group and batch-completion lines the real analyzers
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
        """Detach every handler this CLI installs, from both logger scopes.

        Both the stderr handler and the optional --log-file/-v one: leaving
        the latter attached across tests holds an open file handle into
        whatever temp directory the test that created it already cleaned
        up, which is a Windows PermissionError waiting for the next test
        that touches the same path, not just a descriptor leak.
        """
        for logger in (self.package_logger, self.root_logger):
            for handler in list(logger.handlers):
                if handler.get_name() in (cli._LOG_HANDLER_NAME, cli._LOG_FILE_HANDLER_NAME):
                    logger.removeHandler(handler)
                    handler.close()

    def cli_handlers(self) -> list[logging.Handler]:
        """Return the handlers ``main`` installed on the ``photokin`` logger."""
        return [h for h in self.package_logger.handlers if h.get_name() == cli._LOG_HANDLER_NAME]

    def make_folder(self, *names: str) -> str:
        """Create a scratch folder holding empty placeholder images.

        Detection now refuses an input that is not there, so a folder run needs a
        real directory holding at least one file the listing recognizes.

        Args:
            names: Image filenames to create inside it.

        Returns:
            The folder path, cleaned up when the test ends.
        """
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        for name in names or ("box3_025.jpg",):
            with open(os.path.join(scratch.name, name), "w", encoding="utf-8"):
                pass
        return scratch.name

    def enter_folder(self, folder: str) -> None:
        """Run the rest of the test from inside *folder*.

        Needed by the cases about tokens that resolve to the working directory:
        what makes them dangerous is which directory that is. Registered after
        the scratch folder's own cleanup so it unwinds first -- Windows refuses
        to remove a directory that is some process's cwd.

        Args:
            folder: Directory to change into.
        """
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(folder)

    def run_cli(
        self, argv: list[str], *, env: dict[str, str | None] | None = None
    ) -> tuple[int | None, str, str]:
        """Run ``cli.main`` with *argv*, returning its exit code, stdout and stderr.

        Args:
            argv: Arguments after the program name.
            env: Environment changes layered on top of ``_NEUTRAL_ENV``, where a
                value of None *removes* the variable. Applied here rather than
                by the caller because ``_NEUTRAL_ENV`` is patched inside this
                method and would override an outer ``patch.dict``. Removal is a
                distinct case from blanking: anything read through
                ``_parse_bool_env`` only reaches its documented default when the
                variable is absent. ``patch.dict`` restores the whole mapping on
                exit, so both edits are contained.

        Returns:
            ``(exit_code, stdout, stderr)``, where the exit code is None when
            ``main`` returned without raising ``SystemExit``.
        """
        stdout, stderr = io.StringIO(), io.StringIO()
        code: int | None = None
        with patch.dict(os.environ, _NEUTRAL_ENV), patch.object(sys, "argv", ["photokin", *argv]):
            for name, value in (env or {}).items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    cli.main()
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
        return code, stdout.getvalue(), stderr.getvalue()

    def lines_naming(self, stderr: str, fragment: str) -> list[str]:
        """Return the stderr lines containing *fragment*, formatter prefix included."""
        return [line for line in stderr.splitlines() if fragment in line]

    def usage_error(self, stderr: str) -> list[str]:
        """Return the usage error a failed run ends with, however many lines.

        Taken from the end rather than the start: a positional input logs what
        it was detected as before anything can go wrong with it, and that line
        is the point of the detection contract.

        Sliced from the ``[ERROR]`` line rather than by a fixed count. A problem
        line may carry continuation lines -- a long configured path is put on
        one of its own instead of into a parenthetical -- and a fixed ``[-2:]``
        silently drops the problem itself when that happens, leaving the
        continuation to be compared against the problem and failing in a way
        that points at the message rather than at this slice.

        Args:
            stderr: The whole stderr capture.

        Returns:
            Every line of the usage error, problem first, ``Try:`` last.
        """
        lines = stderr.splitlines()
        for index in range(len(lines) - 1, -1, -1):
            if lines[index].startswith("[ERROR]"):
                return lines[index:]
        return lines[-2:]


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
    red as well. The diagnostic is matched as the run's only copy rather than as
    the whole capture, because every run now also prints its plan summary.
    """

    # A line the pipeline really does emit. It used to be a skipped-group
    # warning, which stopped being true when Phase B2 routed folder input
    # through the manifest grouper and nothing is skipped any more; a fixture
    # quoting a message the system cannot produce reads as evidence that it can.
    _DIAGNOSTIC = "Group 'album': 1 crop file(s) are recorded but not analyzed: album-crop.jpg"

    def test_handler_lands_on_the_package_logger_and_not_the_root(self) -> None:
        folder = self.make_folder()
        root_handlers_before = list(self.root_logger.handlers)
        analyzer = _analyzer_logging(self._DIAGNOSTIC, {"results": {}})

        with patch("photokin.cli.process_manifest_stream", analyzer):
            code, _, stderr = self.run_cli([folder])

        self.assertIsNone(code)
        self.assertEqual(len(self.cli_handlers()), 1)
        self.assertEqual(self.root_logger.handlers, root_handlers_before)
        self.assertEqual(self.package_logger.level, logging.INFO)
        # Matched whole rather than searched: the "[WARNING] " prefix is this
        # CLI's own formatter, and a single-element list also rules out a second
        # copy of the record arriving through some other handler.
        self.assertEqual(
            self.lines_naming(stderr, self._DIAGNOSTIC), [f"[WARNING] {self._DIAGNOSTIC}"]
        )

    def test_repeated_runs_reuse_the_handler_and_follow_the_current_stderr(self) -> None:
        folder = self.make_folder()
        analyzer = _analyzer_logging(self._DIAGNOSTIC, {"results": {}})

        with patch("photokin.cli.process_manifest_stream", analyzer):
            first_code, _, first_stderr = self.run_cli([folder])
            second_code, _, second_stderr = self.run_cli([folder])
            third_code, _, third_stderr = self.run_cli([folder])

        self.assertEqual([first_code, second_code, third_code], [None, None, None])
        self.assertEqual(len(self.cli_handlers()), 1)
        # Each run captures a fresh stream, so a handler still bound to the first
        # run's stderr leaves the second and third captures empty.
        for stderr in (first_stderr, second_stderr, third_stderr):
            self.assertEqual(
                self.lines_naming(stderr, self._DIAGNOSTIC), [f"[WARNING] {self._DIAGNOSTIC}"]
            )


class TestHydrationWarningVisibility(_CliTestCase):
    """The invisible-warning bug: ``exiftool.hydrate`` logs, and nobody listened.

    ``hydrate_item_metadata`` degrades to a warning when the binary disappears
    mid-run, which silently costs the run every tag it would have recovered.
    With no handler anywhere in the ``photokin`` hierarchy that warning went
    nowhere, so the run looked complete.

    Since C3 the request itself is pre-flighted, so reaching the warning takes a
    binary that resolves for ``main`` and then fails for the hydrator -- which is
    the reachable shape it is kept for, and exactly what the two patches below
    arrange.
    """

    def test_hydration_warning_reaches_stderr_through_the_cli_handler(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            manifest_path = _write_manifest(folder)

            with patch(
                "photokin.cli.resolve_exiftool_path", return_value="/fake/exiftool"
            ), patch(
                "photokin.exiftool.hydrate.resolve_exiftool_path",
                side_effect=FileNotFoundError("ExifTool not found."),
            ), patch("photokin.cli.process_manifest_stream", _stream_running_the_hydrator):
                code, stdout, stderr = self.run_cli(["-r", "--manifest", manifest_path])

        self.assertIsNone(code)
        # The "[WARNING] " prefix is this CLI's own formatter, so its presence
        # proves the record travelled through the installed handler rather than
        # logging's last-resort fallback or a test-runner handler.
        self.assertIn("[WARNING] Skipping metadata hydration: ExifTool not found.", stderr)
        self.assertNotIn("Skipping metadata hydration", stdout)


class TestOutputFileForEveryInputType(_CliTestCase):
    """``--output-file`` was manifest-only; C2 lifted the gate.

    Phase A made the flag an explicit error outside manifest mode, because folder
    and single-photo runs printed to stdout and never read it -- no file, no
    error, exit 0. C2 replaces the stopgap with the real thing: every input type
    streams or aggregates to the named file, and prints nothing to stdout while
    doing it. What is still refused is naming no input at all.
    """

    _PAYLOAD: dict[str, object] = {
        "results": {"C:/scans/box3_025.jpg": {"keywords": ["portrait"]}},
        "errors": {},
    }

    def test_folder_input_streams_to_an_ndjson_output_file(self) -> None:
        folder = self.make_folder()
        out_path = os.path.join(folder, "results.ndjson")

        def _stream(*, ndjson_writer: Callable[[str], None] | None = None, **_ignored: object):
            ndjson_writer(json.dumps({"path": "box3_025.jpg", "status": "ok"}))
            return self._PAYLOAD

        with patch("photokin.cli.process_manifest_stream", _stream):
            code, stdout, _stderr = self.run_cli([folder, "--output-file", out_path])

        self.assertIsNone(code)
        self.assertEqual(stdout, "", "a run with --output-file writes the file, not stdout")
        with open(out_path, "r", encoding="utf-8") as handle:
            lines = [json.loads(line) for line in handle if line.strip()]
        # Per-file records only: the file also carries the run envelope
        # (run: start/plan/complete), which has no "path" of its own.
        per_file = [record for record in lines if "path" in record]
        self.assertEqual(
            per_file, [{"path": "box3_025.jpg", "status": "ok", "schema_version": 2}]
        )

    def test_single_photo_input_writes_an_aggregate_json_output_file(self) -> None:
        folder = self.make_folder()
        image = os.path.join(folder, "box3_025.jpg")
        out_path = os.path.join(folder, "results.json")
        stream = Mock(return_value=self._PAYLOAD)

        with patch("photokin.cli.process_manifest_stream", stream):
            code, stdout, _stderr = self.run_cli([image, "--output-file", out_path])

        self.assertIsNone(code)
        self.assertEqual(stdout, "")
        stream.assert_called_once()
        with open(out_path, "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), self._PAYLOAD)

    def test_output_file_with_no_input_at_all_is_refused(self) -> None:
        with patch("photokin.cli.process_manifest_stream") as stream:
            code, stdout, stderr = self.run_cli(["--output-file", "results.json"])

        # Argv carrying flags but no input used to fall into single-photo mode
        # with an empty front path and produce nonsense.
        self.assertEqual(code, 2)
        lines = stderr.splitlines()
        self.assertEqual(lines[0], "[ERROR] no input was given.")
        self.assertTrue(lines[1].startswith("Try: "))
        self.assertEqual(len(lines), 2)
        self.assertEqual(stdout, "")
        stream.assert_not_called()

    def test_an_unusable_extension_is_refused_for_folder_input_too(self) -> None:
        folder = self.make_folder()
        out_path = os.path.join(folder, "results.txt")
        stream = Mock(return_value=self._PAYLOAD)

        with patch("photokin.cli.process_manifest_stream", stream):
            code, _stdout, stderr = self.run_cli([folder, "--output-file", out_path])

        self.assertEqual(code, 2)
        self.assertEqual(
            self.usage_error(stderr)[0],
            f"[ERROR] `--output-file {out_path}` must end with .ndjson or .json.",
        )
        stream.assert_not_called()


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
        folder = self.make_folder()
        payload: dict[str, object] = {
            "results": {"C:/scans/box3_025.jpg": {"keywords": ["portrait"]}},
            "errors": {},
        }
        diagnostic = "Group 'album': 1 crop file(s) are recorded but not analyzed: album-crop.jpg"

        with patch(
            "photokin.cli.process_manifest_stream", _analyzer_logging(diagnostic, payload)
        ):
            code, stdout, stderr = self.run_cli([folder])

        self.assertIsNone(code)
        self.assertEqual(json.loads(stdout), payload)
        self.assertIn(diagnostic, stderr)
        self.assertNotIn(diagnostic, stdout)
        # The plan summary is the newest thing on stderr, and the likeliest to
        # have been printed on the wrong stream.
        self.assertIn(_PLAN_HEADER, stderr)
        self.assertNotIn(_PLAN_HEADER, stdout)

    def test_single_photo_run_emits_only_json_on_stdout(self) -> None:
        # Patched at the stream, not at ``analyze_photo``: since B2 the
        # single-photo branch synthesizes a one- or two-item manifest and routes
        # it through the same batch path every other mode uses, so its stdout is
        # the stream's aggregate rather than one photo's ``{"result": ...}``.
        image = os.path.join(self.make_folder(), "box3_025.jpg")
        payload: dict[str, object] = {
            "results": {"C:/scans/box3_025.jpg": {"keywords": ["portrait"]}},
            "errors": {},
        }
        diagnostic = "Skipping archival upload for provider anthropic"

        with patch(
            "photokin.cli.process_manifest_stream", _analyzer_logging(diagnostic, payload)
        ):
            code, stdout, stderr = self.run_cli([image, "--provider", "anthropic"])

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
        diagnostic = "Group 'album': 1 crop file(s) are recorded but not analyzed: album-crop.jpg"
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
    is never entered.
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
                "[ERROR] no ExifTool found, and writing needs it.",
            )
            self.assertEqual(lines[1], f"  looked for it at: {missing_exiftool}")
            self.assertTrue(lines[2].startswith("Try: "))
            self.assertEqual(stdout, "")
            self.assertFalse(os.path.exists(os.path.join(folder, "batch_changeset.ndjson")))

    def test_a_dry_run_still_fails_when_exiftool_cannot_be_resolved(self) -> None:
        """``--dry-run`` is a pre-flight now, so it cannot skip one.

        It used to run the analysis and merely refrain from applying, which made
        it exempt from this gate. Since C2 it prints the plan and stops, and a
        plan reporting a write set while no binary exists would be a lie -- so
        the gate fires first and the summary is never printed.
        """
        with tempfile.TemporaryDirectory() as folder:
            manifest_path = _write_manifest(folder)
            stream = Mock(return_value={"results": {}})

            with patch(
                "photokin.cli.resolve_exiftool_path",
                side_effect=FileNotFoundError("ExifTool not found."),
            ), patch("photokin.cli.process_manifest_stream", stream), patch(
                "photokin.cli._apply_exiftool_changeset", return_value=False
            ):
                code, stdout, stderr = self.run_cli(
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

            self.assertEqual(code, 2)
            self.assertEqual(
                stderr.splitlines()[0],
                "[ERROR] no ExifTool found, and writing needs it.",
            )
            self.assertNotIn(_PLAN_HEADER, stderr)
            self.assertEqual(stdout, "")
            stream.assert_not_called()


class TestDryRunStopsAtThePlan(_CliTestCase):
    """``--dry-run`` prints the plan and stops, before the first model call.

    This is the cheapest guard against "wrong folder" and "I did not mean to
    write", so what matters is that nothing downstream of the summary happens:
    no model call, and no destination truncated, which is what makes it safe to
    point at a directory holding a real previous run.
    """

    def test_the_plan_prints_and_the_run_stops_without_touching_the_old_output(self) -> None:
        folder = self.make_folder()
        out_path = os.path.join(folder, "results.ndjson")
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write("PREVIOUS RUN CONTENT\n")
        stream = Mock(return_value={"results": {}})
        # A resolvable binary, same as every other write-flag test in this
        # module: -w is not exempt from the ExifTool pre-flight under
        # --dry-run (see the module docstring), so without one this would
        # exit 2 on a real ExifTool-less machine rather than exercise the
        # thing under test.
        exiftool_path = os.path.join(folder, "exiftool.exe")
        with open(exiftool_path, "w", encoding="utf-8"):
            pass

        with patch("photokin.cli.process_manifest_stream", stream):
            code, stdout, stderr = self.run_cli(
                [
                    folder,
                    "--output-file",
                    out_path,
                    "-w",
                    "--dry-run",
                    "--exiftool-path",
                    exiftool_path,
                ]
            )

        self.assertIsNone(code)
        stream.assert_not_called()
        self.assertEqual(stdout, "")
        self.assertIn(_PLAN_HEADER, stderr)
        # -w with --dry-run is not a contradiction: the plan names the changeset
        # it would have written and says nothing will be.
        self.assertIn(os.path.join(folder, "results_changeset.ndjson"), stderr)
        self.assertIn("write     : none (--dry-run)", stderr)
        with open(out_path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "PREVIOUS RUN CONTENT\n")
        self.assertFalse(os.path.exists(os.path.join(folder, "results_changeset.ndjson")))


class _WriteFixtureTestCase(_CliTestCase):
    """Base for the two classes that let a run reach ``apply_changeset``.

    Both need the same three things: a resolvable ExifTool binary, a stream that
    really fills the changeset the apply step reads, and one input of each kind,
    since the whole point of C2's lifted gates is that the write flags behave
    identically for all three.
    """

    def write_fixture(self) -> tuple[str, str, str, str]:
        """Build a folder holding an image, a manifest naming it and a fake binary.

        Returns:
            ``(folder, image, manifest_path, exiftool_path)``. The binary is a
            real empty file rather than a patched resolver, because the ExifTool
            pre-flight resolves the path itself and nothing here executes it.
        """
        folder = self.make_folder()
        exiftool_path = os.path.join(folder, "exiftool.exe")
        with open(exiftool_path, "w", encoding="utf-8"):
            pass
        return folder, os.path.join(folder, "box3_025.jpg"), _write_manifest(folder), exiftool_path

    def each_input(self, folder: str, image: str, manifest_path: str) -> tuple[list[str], ...]:
        """Return one argv prefix per input kind, in detection order."""
        return ([folder], [image], ["--manifest", manifest_path])


class TestWhichMissingExiftoolMessageAReadOrWriteRunGets(_WriteFixtureTestCase):
    """Three requests, two messages, and one branch deciding between them.

    ``-r`` and ``-w`` both need a binary that does not exist here, and C3 gave
    the read its own wording because the write message's remedy -- "re-run with
    --exiftool-write false" -- is useless advice to someone who asked to read.
    When *both* were requested the phase chose to report the write, because the
    other two remedies both messages offer (fetch a binary, install one) fix the
    read at the same time, so the write message is the one that ends the whole
    problem in a single step.

    That choice is one ``if writes_requested:`` inside
    ``cli._preflight_exiftool``, sitting above an unguarded fall-through to the
    read message. Nothing passed the two flags together, so swapping the two
    calls -- reporting the read and stranding the write -- shipped green. All
    three states are pinned here, in every input mode, and the two-flag case is
    pinned in both typing orders, since argparse makes the order invisible to
    the branch and a reader should not have to know that.

    The fixture's real ExifTool binary is deliberately left unused: every run
    below points ``--exiftool-path`` at a path that is not there, which is also
    what puts the configured path in the message and so pins that the run named
    the thing it could not find.
    """

    #: Both messages, each as the exact two lines a failed run ends with. Stated
    #: as literals rather than rendered from ``cli_messages``: a test that builds
    #: its expectation from the function under test pins nothing about it. The
    #: ``{configured}`` slot is the only part that varies per run.
    #: The configured path sits on its own indented line rather than inside a
    #: parenthetical, which is the whole point of the shape: a Windows path runs
    #: past 80 characters routinely and used to swallow the sentence around it.
    _READ_MESSAGE = (
        (
            "[ERROR] no ExifTool found, and -r needs it to read your files.\n"
            "  looked for it at: {configured}"
        ),
        (
            "Try: run `python -m photokin.exiftool.fetch` to install one, "
            "or drop -r to analyze without reading"
        ),
    )
    _WRITE_MESSAGE = (
        (
            "[ERROR] no ExifTool found, and writing needs it.\n"
            "  looked for it at: {configured}"
        ),
        (
            "Try: run `python -m photokin.exiftool.fetch` to install one, "
            "or drop -w to analyze without writing"
        ),
    )

    def _missing_binary(self, folder: str) -> str:
        """Return a path inside *folder* that no binary will ever resolve to."""
        return os.path.join(folder, "nosuchdir", "exiftool.exe")

    def _assert_reports(
        self, argv: list[str], missing: str, expected: tuple[str, str]
    ) -> None:
        """Run *argv* and assert it exits 2 with *expected*, having analyzed nothing.

        Args:
            argv: Arguments after the program name, ``--exiftool-path`` included.
            missing: The configured path, interpolated into the expected lines.
            expected: The two-line message this state is owed.
        """
        stream = Mock(return_value={"results": {}})
        with patch("photokin.cli.process_manifest_stream", stream), patch(
            "photokin.cli.apply_changeset"
        ) as apply:
            code, stdout, stderr = self.run_cli(argv)

        # Asserted ahead of the exit code, as in TestExiftoolPreflight: a
        # regression that runs the batch and only then fails also exits 2, so
        # the code alone would report success at the point the money is spent.
        stream.assert_not_called()
        apply.assert_not_called()
        self.assertEqual(code, 2)
        # Joined rather than compared line-by-line, so a problem that spans two
        # lines is one expectation instead of an index the reader has to align.
        self.assertEqual(
            "\n".join(self.usage_error(stderr)),
            "\n".join(line.format(configured=missing) for line in expected),
        )
        self.assertEqual(stdout, "")

    def test_each_request_is_answered_by_the_message_written_for_it(self) -> None:
        folder, image, manifest_path, _exiftool = self.write_fixture()
        missing = self._missing_binary(folder)
        states = (
            ("read only", ["-r"], self._READ_MESSAGE),
            ("write only", ["-w"], self._WRITE_MESSAGE),
            # Both: the write wins, because fetching or installing a binary --
            # the first two remedies of either message -- clears the read too,
            # while the read message's own remedy ("re-run without -r") would
            # leave the user facing the write failure on the next run.
            ("both", ["-r", "-w"], self._WRITE_MESSAGE),
        )

        for argv in self.each_input(folder, image, manifest_path):
            for label, flags, expected in states:
                with self.subTest(argv=argv, state=label):
                    self._assert_reports(
                        [*argv, *flags, "--exiftool-path", missing], missing, expected
                    )

    def test_the_two_flags_answer_the_same_way_in_either_order(self) -> None:
        # ``-w -r`` and ``-r -w`` are the same request; argparse hands the branch
        # a Namespace either way, so an answer that depended on typing order
        # could only come from something reading argv directly. Cheap to state,
        # and it is the spelling a user actually varies.
        folder, _image, _manifest, _exiftool = self.write_fixture()
        missing = self._missing_binary(folder)

        for flags in (["-r", "-w"], ["-w", "-r"]):
            with self.subTest(flags=flags):
                self._assert_reports(
                    [folder, *flags, "--exiftool-path", missing], missing, self._WRITE_MESSAGE
                )

    def test_a_run_asking_for_neither_does_not_need_a_binary_at_all(self) -> None:
        """Non-vacuity, and the bound: ExifTool stays optional.

        The same unresolvable path, in every input mode, with neither flag. If
        this failed, the three states above would be asserting that a broken
        ``--exiftool-path`` stops any run whatsoever, which is a different and
        much weaker claim than the one this class is making.
        """
        folder, image, manifest_path, _exiftool = self.write_fixture()
        missing = self._missing_binary(folder)

        for argv in self.each_input(folder, image, manifest_path):
            with self.subTest(argv=argv):
                stream = Mock(return_value={"results": {}})
                with patch("photokin.cli.process_manifest_stream", stream):
                    code, _stdout, stderr = self.run_cli(
                        [*argv, "--exiftool-path", missing]
                    )

                self.assertIsNone(code, stderr)
                stream.assert_called_once()


class TestNothingIsWrittenWithoutAnOptIn(_WriteFixtureTestCase):
    """The flipped default: ``--exiftool-write`` resolves to false everywhere.

    ``--changeset true`` used to be enough on its own, because ``from_env``
    defaulted the flag to true and then discarded the unset sentinel. Recording
    the proposed writes and applying them are now separate requests.

    ``EXIFTOOL_WRITE_ENABLED`` is *removed* rather than blanked here, which is
    the difference between pinning the flipped default and merely pinning that
    an empty string is falsy. With it blanked -- the suite-wide neutral value --
    ``_parse_bool_env`` short-circuits before its ``default`` argument, so this
    class passed unchanged against a tree with the pre-C2 ``True`` restored.
    """

    _WRITE_ENABLED = "EXIFTOOL_WRITE_ENABLED"

    def test_a_changeset_run_applies_nothing_unless_the_flag_says_so(self) -> None:
        folder, image, manifest_path, exiftool_path = self.write_fixture()

        for argv in self.each_input(folder, image, manifest_path):
            with self.subTest(argv=argv):
                with patch(
                    "photokin.cli.process_manifest_stream",
                    _stream_filling_the_changeset("analyzing", {"results": {}}),
                ), patch("photokin.cli.apply_changeset") as apply:
                    code, _stdout, stderr = self.run_cli(
                        [*argv, "--changeset", "true", "--exiftool-path", exiftool_path],
                        env={self._WRITE_ENABLED: None},
                    )

                self.assertIsNone(code)
                apply.assert_not_called()
                self.assertIn("write     : none (--exiftool-write defaults to false)", stderr)

    def test_the_same_fixture_does_write_once_the_environment_asks_for_it(self) -> None:
        """Proves the assertion above is a guard rather than an always-false claim.

        Everything is held constant except the one variable whose default is
        under test, so a fixture that could never reach ``apply_changeset`` --
        an unresolvable binary, a changeset the stream forgot to fill -- fails
        here instead of passing quietly there.
        """
        folder, image, manifest_path, exiftool_path = self.write_fixture()

        for argv in self.each_input(folder, image, manifest_path):
            with self.subTest(argv=argv):
                with patch(
                    "photokin.cli.process_manifest_stream",
                    _stream_filling_the_changeset("analyzing", {"results": {}}),
                ), patch("photokin.cli.apply_changeset", return_value={}) as apply:
                    code, _stdout, stderr = self.run_cli(
                        [*argv, "--changeset", "true", "--exiftool-path", exiftool_path],
                        env={self._WRITE_ENABLED: "true"},
                    )

                self.assertIsNone(code)
                apply.assert_called_once()
                self.assertIn("write     : ExifTool EXIF:UserComment", stderr)


class TestWriteBundleGuards(_WriteFixtureTestCase):
    """``-w``, the flags that contradict it, and the one that needs it.

    These two guards are the whole distance between a user's "do not write" and
    ExifTool rewriting their scans, and neither had a test: collapsing
    ``_resolve_write_bundle``'s contradiction branch to an unconditional
    assignment, or deleting the needs-a-changeset guard outright, both left the
    suite green while ``-w --exiftool-write false`` silently became a write.

    "Explicit flags override the expansion" has only two observable outcomes,
    because every member of ``cli._WRITE_BUNDLE`` expands to ``"true"``: an
    explicit ``true`` agrees and is used, and an explicit ``false`` contradicts
    and is refused. Both are pinned below.
    """

    def _assert_refused(self, argv: list[str], first_line: str) -> None:
        """Run *argv* and assert it exits 2 with *first_line*, having written nothing.

        Args:
            argv: Arguments after the program name.
            first_line: The expected problem line, without the level prefix.
        """
        stream = Mock(return_value={"results": {}})
        with patch("photokin.cli.process_manifest_stream", stream), patch(
            "photokin.cli.apply_changeset"
        ) as apply:
            code, stdout, stderr = self.run_cli(argv)

        self.assertEqual(code, 2)
        self.assertEqual(self.usage_error(stderr)[0], f"[ERROR] {first_line}")
        self.assertTrue(self.usage_error(stderr)[1].startswith("Try: "))
        self.assertEqual(stdout, "")
        # The exit code alone would not distinguish "refused the combination"
        # from "ran the batch and then failed", which is the regression shape
        # that matters: by then the writes have already happened.
        stream.assert_not_called()
        apply.assert_not_called()

    def test_w_beside_an_explicit_exiftool_write_false_is_refused(self) -> None:
        folder, image, manifest_path, _exiftool = self.write_fixture()

        for argv in self.each_input(folder, image, manifest_path):
            with self.subTest(argv=argv):
                self._assert_refused(
                    [*argv, "-w", "--exiftool-write", "false"],
                    "`-w` means --changeset true --exiftool-write true, but "
                    "`--exiftool-write false` was also given.",
                )

    def test_w_beside_an_explicit_changeset_false_is_refused(self) -> None:
        folder, image, manifest_path, _exiftool = self.write_fixture()

        for argv in self.each_input(folder, image, manifest_path):
            with self.subTest(argv=argv):
                self._assert_refused(
                    [*argv, "-w", "--changeset", "false"],
                    "`-w` means --changeset true --exiftool-write true, but "
                    "`--changeset false` was also given.",
                )

    def test_writing_without_a_changeset_to_write_from_is_refused(self) -> None:
        folder, image, manifest_path, _exiftool = self.write_fixture()

        for argv in self.each_input(folder, image, manifest_path):
            with self.subTest(argv=argv):
                self._assert_refused(
                    [*argv, "--exiftool-write", "true"],
                    "`--exiftool-write true` needs a changeset to apply, but "
                    "--changeset is false.",
                )
        # Spelling the changeset out is the remedy the message offers, so it has
        # to be the one thing that clears the error rather than a second one.
        with self.subTest(argv="the remedy"):
            self._assert_refused(
                [folder, "--exiftool-write", "true", "--changeset", "false"],
                "`--exiftool-write true` needs a changeset to apply, but "
                "--changeset is false.",
            )

    def test_w_expands_to_both_flags_for_every_input_type(self) -> None:
        folder, image, manifest_path, exiftool_path = self.write_fixture()

        for argv in self.each_input(folder, image, manifest_path):
            with self.subTest(argv=argv):
                with patch(
                    "photokin.cli.process_manifest_stream",
                    _stream_filling_the_changeset("analyzing", {"results": {}}),
                ), patch("photokin.cli.apply_changeset", return_value={}) as apply:
                    code, _stdout, stderr = self.run_cli(
                        [*argv, "-w", "--exiftool-path", exiftool_path]
                    )

                self.assertIsNone(code)
                # Both halves of the bundle, each observed where only it shows:
                # the changeset file exists because --changeset true was set,
                # and apply ran because --exiftool-write true was.
                apply.assert_called_once()
                self.assertIn("write     : ExifTool EXIF:UserComment", stderr)
                # Named by the input's own stem, so the three inputs produce
                # three different filenames; the suffix is the invariant.
                self.assertTrue(
                    [n for n in os.listdir(folder) if n.endswith("_changeset.ndjson")],
                    "-w did not expand to --changeset true",
                )
                for stale in os.listdir(folder):
                    if stale.endswith("_changeset.ndjson"):
                        os.remove(os.path.join(folder, stale))

    def test_an_explicit_flag_agreeing_with_the_expansion_is_accepted(self) -> None:
        folder, _image, _manifest, exiftool_path = self.write_fixture()

        with patch(
            "photokin.cli.process_manifest_stream",
            _stream_filling_the_changeset("analyzing", {"results": {}}),
        ), patch("photokin.cli.apply_changeset", return_value={}) as apply:
            code, _stdout, stderr = self.run_cli(
                [
                    folder,
                    "-w",
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
        self.assertIn("write     : ExifTool EXIF:UserComment", stderr)


class TestABlankInputTokenIsRefused(_CliTestCase):
    """A token that names nothing must not resolve to the current directory.

    ``utils.normalize_path`` strips whitespace and one surrounding quote pair
    and then calls ``os.path.normpath``, which answers ``"."`` for what is left.
    A caller interpolating an unset variable -- ``photokin "$SEL" -w`` where
    ``$SEL`` is a space -- therefore analyzed every image in the working
    directory and, under ``-w``, applied ExifTool to all of them. An empty
    string was already refused, so blanks looked as though they were caught --
    though it was refused by the wrong branch, and said "no input was given."
    to a caller who had given one. Both halves are pinned here now: every
    spelling is refused, and every spelling is refused as a blank.
    """

    #: Every spelling of "this names nothing", the bare empty string included:
    #: argparse stores it like any other token, so it is a blank input rather
    #: than an absent one and answers the same way the rest do.
    _BLANK_TOKENS = ("", " ", "\t", "  ", '""', "''")

    def test_no_blank_positional_reaches_the_pipeline(self) -> None:
        folder = self.make_folder()
        self.enter_folder(folder)

        for token in self._BLANK_TOKENS:
            with self.subTest(token=repr(token)):
                stream = Mock(return_value={"results": {}})
                with patch("photokin.cli.process_manifest_stream", stream):
                    code, stdout, stderr = self.run_cli([token, "-w"])

                self.assertEqual(code, 2)
                self.assertEqual(
                    self.usage_error(stderr)[0],
                    f"[ERROR] `{token}` is blank, so it names no input.",
                )
                self.assertEqual(stdout, "")
                stream.assert_not_called()

    def test_neither_alias_accepts_one_either(self) -> None:
        # Both alias resolvers carried the same ``normalize_path(value) or ""``
        # line, and neither logs a detection line, so ``--folder " "`` reached
        # the working directory without printing anything about it at all.
        # The empty spelling is included because the aliases shared the
        # positional's truthiness filter and so shared its wrong answer too.
        folder = self.make_folder()
        self.enter_folder(folder)

        for flag in ("--folder", "--manifest"):
            for token in ("", " "):
                with self.subTest(flag=flag, token=repr(token)):
                    stream = Mock(return_value={"results": {}})
                    with patch("photokin.cli.process_manifest_stream", stream):
                        code, _stdout, stderr = self.run_cli([flag, token])

                    self.assertEqual(code, 2)
                    self.assertEqual(
                        self.usage_error(stderr)[0],
                        f"[ERROR] `{token}` is blank, so it names no input.",
                    )
                    stream.assert_not_called()

    def test_a_genuinely_empty_token_takes_the_same_path(self) -> None:
        """``photokin ""`` is a blank token, not an absent one.

        argparse stores the empty string, and the source list used to filter on
        truthiness, so the one spelling a wrapper produces most easily -- an
        unset variable interpolated bare -- was reported as "no input was
        given.", sending the reader off to add an argument they had already
        typed. The quoted spellings answered correctly, which is what made the
        gap invisible.
        """
        folder = self.make_folder()
        self.enter_folder(folder)

        code, stdout, stderr = self.run_cli([""])

        self.assertEqual(code, 2)
        self.assertEqual(self.usage_error(stderr)[0], "[ERROR] `` is blank, so it names no input.")
        self.assertEqual(stdout, "")

    def test_no_input_at_all_still_says_so(self) -> None:
        # The counterpart: with the filter widened to "was it given", a run that
        # really passed no input must not be described as passing a blank one.
        folder = self.make_folder()
        self.enter_folder(folder)

        code, _stdout, stderr = self.run_cli(["--provider", "openai"])

        self.assertEqual(code, 2)
        self.assertEqual(self.usage_error(stderr)[0], "[ERROR] no input was given.")

    def test_an_explicit_dot_is_still_a_real_request(self) -> None:
        """The guard must refuse blanks without refusing the working directory.

        ``photokin .`` normalizes to the same ``"."`` a blank token does; the
        difference is that the user typed it. A guard keyed on the normalized
        result rather than on the token would break this.
        """
        folder = self.make_folder()
        self.enter_folder(folder)
        stream = Mock(return_value={"results": {}})

        with patch("photokin.cli.process_manifest_stream", stream):
            code, _stdout, stderr = self.run_cli(["."])

        self.assertIsNone(code)
        stream.assert_called_once()
        self.assertIn("[INFO] Treating `.` as a folder (it is a directory).", stderr)


class TestThePlanNamesTheResolvedInput(_CliTestCase):
    """The summary answers "which folder", which a relative token cannot.

    The block exists as the guard against running against the wrong directory,
    and its ``output`` and ``changeset`` lines are already absolute. Echoing the
    raw token on the ``input`` line left the one value the guard is about as the
    only unresolved thing in it.
    """

    def test_the_input_line_carries_the_absolute_path_not_the_token(self) -> None:
        folder = self.make_folder()
        self.enter_folder(folder)
        stream = Mock(return_value={"results": {}})

        with patch("photokin.cli.process_manifest_stream", stream):
            code, _stdout, stderr = self.run_cli([".", "--dry-run"])

        self.assertIsNone(code)
        self.assertIn(
            f"  input     : {os.path.abspath(folder)} (folder, 1 file(s) in 1 group(s), "
            "group-by object)",
            stderr,
        )
        # The typed token still appears, on the detection line above it, so
        # nothing is lost by resolving the plan's copy.
        self.assertIn("Treating `.` as a folder", stderr)


class TestGenerateManifestHonorsDryRun(_CliTestCase):
    """``--dry-run`` promises no destination is touched; this branch touched one.

    ``main`` dispatched to ``_generate_manifest`` and returned above the
    ``--dry-run`` check, so previewing the flag over a hand-edited manifest
    replaced it with the generated document and printed nothing.
    """

    _HAND_WRITTEN = '{"MY": "HAND WRITTEN MANIFEST"}'

    def test_an_existing_manifest_survives_the_preview(self) -> None:
        folder = self.make_folder()
        out_path = os.path.join(folder, "important.json")
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(self._HAND_WRITTEN)
        stream = Mock()

        with patch("photokin.cli.process_manifest_stream", stream):
            code, stdout, stderr = self.run_cli(
                [folder, "--generate-manifest", out_path, "--dry-run"]
            )

        self.assertIsNone(code)
        self.assertEqual(stdout, "")
        stream.assert_not_called()
        with open(out_path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), self._HAND_WRITTEN)
        self.assertIn(
            f"[INFO] --dry-run: would write a manifest for 1 file(s) in 1 group(s) to "
            f"{out_path}; nothing was written.",
            stderr,
        )
        # The analysis plan describes a run that is not happening here: this
        # branch reaches no provider and writes no results.
        self.assertNotIn(_PLAN_HEADER, stderr)

    def test_without_the_flag_the_same_command_still_writes(self) -> None:
        folder = self.make_folder()
        out_path = os.path.join(folder, "important.json")
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(self._HAND_WRITTEN)

        with patch("photokin.cli.process_manifest_stream", Mock()):
            code, _stdout, stderr = self.run_cli([folder, "--generate-manifest", out_path])

        self.assertIsNone(code)
        self.assertIn("Wrote manifest for 1 file(s) in 1 group(s)", stderr)
        with open(out_path, "r", encoding="utf-8") as handle:
            self.assertIn("generated_by", handle.read())


class TestGenerateManifestRemediesTerminate(_CliTestCase):
    """Following a message's ``Try:`` line must not lead back to the first error.

    The write-bundle guards ran first, so ``--generate-manifest
    --exiftool-write true`` was answered with "add --changeset true" -- whose
    own refusal then said to drop it, returning the user to the start. The
    branch written for this case could never execute at all.
    """

    def test_the_write_flag_is_answered_by_the_message_written_for_it(self) -> None:
        folder = self.make_folder()
        out_path = os.path.join(folder, "out.json")

        with patch("photokin.cli.process_manifest_stream", Mock()):
            code, _stdout, stderr = self.run_cli(
                [folder, "--generate-manifest", out_path, "--exiftool-write", "true"]
            )

        self.assertEqual(code, 2)
        self.assertEqual(
            self.usage_error(stderr),
            [
                (
                    "[ERROR] `--generate-manifest` makes no model call, so "
                    "`--exiftool-write true` has nothing to write."
                ),
                "Try: drop it; generate first, then: photokin <manifest> -w",
            ],
        )
        self.assertFalse(os.path.exists(out_path))

    def test_following_that_remedy_completes(self) -> None:
        # The loop is the defect, so the assertion is that the remedy ends it.
        folder = self.make_folder()
        out_path = os.path.join(folder, "out.json")

        with patch("photokin.cli.process_manifest_stream", Mock()) as stream:
            code, _stdout, stderr = self.run_cli([folder, "--generate-manifest", out_path])

        self.assertIsNone(code)
        self.assertIn("Wrote manifest for 1 file(s) in 1 group(s)", stderr)
        stream.assert_not_called()

    def test_each_write_flag_keeps_its_own_wording(self) -> None:
        # Reordering the guards must not collapse four distinct answers into
        # whichever one happens to be checked first.
        folder = self.make_folder()
        out_path = os.path.join(folder, "out.json")
        cases = (
            (["-w"], "`--generate-manifest` makes no model call, so `-w` has nothing to write."),
            (
                ["--changeset", "true"],
                (
                    "`--generate-manifest` makes no model call, so `--changeset true` has "
                    "nothing to record."
                ),
            ),
            (
                ["--output-file", "r.ndjson"],
                (
                    "`--generate-manifest` writes a manifest and stops, so `--output-file "
                    "r.ndjson` would never be written."
                ),
            ),
        )

        for flags, expected in cases:
            with self.subTest(flags=flags):
                with patch("photokin.cli.process_manifest_stream", Mock()):
                    code, _stdout, stderr = self.run_cli(
                        [folder, "--generate-manifest", out_path, *flags]
                    )

                self.assertEqual(code, 2)
                self.assertEqual(self.usage_error(stderr)[0], f"[ERROR] {expected}")
                self.assertFalse(os.path.exists(out_path))


class TestTheWriteBundleIsDefinedOnce(_CliTestCase):
    """Every flag ``-w`` expands to is refused beside ``--generate-manifest``.

    ``cli._WRITE_BUNDLE`` was already the one definition of what the shorthand
    means, and the expansion and the contradiction check both read it -- but the
    ``--generate-manifest`` refusal listed the same flags again by hand. That is
    a definition in two places, and the two could disagree in exactly one
    direction: a member added to the bundle would be expanded by ``-w`` and then
    silently permitted beside the one flag that can never honor it, since
    ``--generate-manifest`` stops before any model call and so has nothing to
    record or write.

    These cases are written against the bundle rather than against its current
    contents, so a third member is covered the day it is added rather than the
    day someone remembers this file exists.
    """

    def test_every_member_of_the_bundle_is_refused(self) -> None:
        folder = self.make_folder()
        out_path = os.path.join(folder, "out.json")

        for dest, member in cli._WRITE_BUNDLE.items():
            with self.subTest(dest=dest):
                flag = cli._flag_spelling(dest)
                stream = Mock()
                with patch("photokin.cli.process_manifest_stream", stream):
                    code, stdout, stderr = self.run_cli(
                        [folder, "--generate-manifest", out_path, flag, member.value]
                    )

                self.assertEqual(code, 2)
                self.assertEqual(
                    self.usage_error(stderr),
                    [
                        (
                            "[ERROR] `--generate-manifest` makes no model call, so "
                            f"`{flag} {member.value}` has nothing to {member.verb}."
                        ),
                        (
                            "Try: drop it; generate first, then: photokin <manifest> "
                            f"{member.replay}"
                        ),
                    ],
                )
                self.assertEqual(stdout, "")
                self.assertFalse(os.path.exists(out_path))
                stream.assert_not_called()

    def test_a_member_added_to_the_bundle_is_refused_with_no_second_edit(self) -> None:
        """The divergence guard, run against a bundle this CLI does not ship.

        ``--debug-dump-llm-request`` stands in for a future member: it is a real
        argparse destination taking the same ``true``/``false`` values, so the
        run reaches the refusal exactly as a real member would, and it is not a
        member today -- which is the whole point. Against a refusal that
        restates the membership list this run exits 0 and writes the manifest.
        """
        folder = self.make_folder()
        out_path = os.path.join(folder, "out.json")
        added = cli._WriteBundleMember("true", "write", "-w")
        stream = Mock()

        with patch.dict(cli._WRITE_BUNDLE, {"debug_dump_llm_request": added}), patch(
            "photokin.cli.process_manifest_stream", stream
        ):
            code, stdout, stderr = self.run_cli(
                [folder, "--generate-manifest", out_path, "--debug-dump-llm-request", "true"]
            )

        self.assertEqual(code, 2)
        self.assertEqual(
            self.usage_error(stderr)[0],
            "[ERROR] `--generate-manifest` makes no model call, so "
            "`--debug-dump-llm-request true` has nothing to write.",
        )
        self.assertEqual(stdout, "")
        self.assertFalse(os.path.exists(out_path))
        stream.assert_not_called()

    def test_the_contradiction_check_covers_an_added_member_too(self) -> None:
        # The bundle's other reader, held to the same standard: ``-w`` beside an
        # explicit value that disagrees with the added member's is refused by
        # the same loop, so the expansion cannot quietly win an argument the
        # user thought they had made.
        folder = self.make_folder()
        added = cli._WriteBundleMember("true", "write", "-w")
        stream = Mock()

        with patch.dict(cli._WRITE_BUNDLE, {"debug_dump_llm_request": added}), patch(
            "photokin.cli.process_manifest_stream", stream
        ):
            code, _stdout, stderr = self.run_cli(
                [folder, "-w", "--debug-dump-llm-request", "false"]
            )

        self.assertEqual(code, 2)
        self.assertEqual(
            self.usage_error(stderr)[0],
            "[ERROR] `-w` means --changeset true --exiftool-write true, but "
            "`--debug-dump-llm-request false` was also given.",
        )
        stream.assert_not_called()

    def test_the_shorthand_itself_is_still_named_before_its_members(self) -> None:
        # ``-w`` is the bundle's trigger, not a member of it, so it keeps its own
        # branch. A user who typed the shorthand should be answered about the
        # shorthand rather than about whichever member the loop reaches first.
        folder = self.make_folder()
        out_path = os.path.join(folder, "out.json")

        with patch("photokin.cli.process_manifest_stream", Mock()):
            code, _stdout, stderr = self.run_cli([folder, "--generate-manifest", out_path, "-w"])

        self.assertEqual(code, 2)
        self.assertEqual(
            self.usage_error(stderr)[0],
            "[ERROR] `--generate-manifest` makes no model call, so `-w` has nothing to write.",
        )
        self.assertFalse(os.path.exists(out_path))


class TestAnUnreadableManifestIsNotReportedAsMissing(_CliTestCase):
    """Detection proved the file is there, so "not found" contradicts the line above.

    Every ``OSError`` from the read was mapped to the not-found message, whose
    remedy -- check the spelling, run from the folder that contains it -- is
    actively wrong for the reachable case: a denied ACL, a lock held by a sync
    client, a handle another process opened exclusively. The folder branch has
    named the OS reason all along.
    """

    def test_a_permission_error_names_the_reason_the_way_a_folder_does(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            manifest_path = _write_manifest(folder)
            stream = Mock()

            with patch(
                "photokin.cli.load_json",
                side_effect=PermissionError(13, "Permission denied"),
            ), patch("photokin.cli.process_manifest_stream", stream):
                code, stdout, stderr = self.run_cli([manifest_path])

            self.assertEqual(code, 2)
            self.assertEqual(
                self.usage_error(stderr),
                [
                    f"[ERROR] `{manifest_path}` cannot be read: Permission denied.",
                    "Try: check the file's permissions, or close whatever is holding it open",
                ],
            )
            self.assertEqual(stdout, "")
            stream.assert_not_called()

    def test_a_file_removed_after_detection_is_still_reported_as_missing(self) -> None:
        # The one OSError for which "not found" is the true answer, so the split
        # has to keep it rather than route everything to the new message.
        with tempfile.TemporaryDirectory() as folder:
            manifest_path = _write_manifest(folder)

            with patch(
                "photokin.cli.load_json", side_effect=FileNotFoundError(2, "No such file")
            ), patch("photokin.cli.process_manifest_stream", Mock()):
                code, _stdout, stderr = self.run_cli([manifest_path])

            self.assertEqual(code, 2)
            self.assertEqual(
                self.usage_error(stderr)[0], f"[ERROR] `{manifest_path}` not found."
            )


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
            self.assertEqual(
                sorted(os.listdir(folder)), ["batch.json", "box3_025.jpg", "results.json"]
            )

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
            ), patch(
                "photokin.cli._apply_exiftool_changeset", return_value=False
            ):
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
                f"[ERROR] `--output-file {out_path}` must end with .ndjson or .json.",
            )
            self.assertEqual(stdout, "")
            with open(changeset_path, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "PREVIOUS RUN CONTENT\n")


class TestGenerateManifestInputExists(_CliTestCase):
    """``--generate-manifest`` describes a run, so it must refuse one that cannot happen.

    The folder form already fails loudly on a directory that is not there,
    because building the manifest has to list it. The single-photo form built
    from the argument alone, so a typo used to write a manifest for a file that
    does not exist and exit 0 -- while the identical input without the flag
    exits 2 -- and the file it produced only failed later, fed back through
    ``--manifest``.

    Since C2 both answers come out of the same detection step, which is what
    makes "one input, one answer" structural rather than a coincidence of two
    guards agreeing.
    """

    def _run_generate(self, argv: list[str]) -> tuple[int | None, str, str, Mock]:
        """Run ``--generate-manifest`` with the analysis entry point mocked.

        Args:
            argv: Arguments after the program name.

        Returns:
            ``(exit code, stdout, stderr, stream mock)``.
        """
        stream = Mock()
        with patch("photokin.cli.process_manifest_stream", stream):
            code, stdout, stderr = self.run_cli(argv)
        return code, stdout, stderr, stream

    def test_a_missing_image_is_refused_and_nothing_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            missing = os.path.join(folder, "nope.jpg")
            out_path = os.path.join(folder, "out.json")

            code, stdout, stderr, _stream = self._run_generate(
                [missing, "--generate-manifest", out_path]
            )

            self.assertEqual(code, 2)
            self.assertEqual(self.usage_error(stderr)[0], f"[ERROR] `{missing}` not found.")
            self.assertFalse(
                os.path.exists(out_path),
                "a manifest was written describing a run that cannot happen",
            )
            self.assertEqual(stdout, "")

    def test_a_missing_back_is_refused_too(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            image = os.path.join(folder, "box3_025.jpg")
            with open(image, "w", encoding="utf-8") as handle:
                handle.write("")
            missing_back = os.path.join(folder, "nope.jpg")
            out_path = os.path.join(folder, "out.json")

            code, _stdout, stderr, _stream = self._run_generate(
                [image, "--back", missing_back, "--generate-manifest", out_path]
            )

            self.assertEqual(code, 2)
            self.assertEqual(
                self.usage_error(stderr)[0], f"[ERROR] `--back {missing_back}` not found."
            )
            self.assertFalse(os.path.exists(out_path))

    def test_the_error_matches_the_one_the_same_input_gets_without_the_flag(self) -> None:
        # The point of the guard: one input, one answer. The flag describing a
        # grouping for input the analysis path refuses is the mismatch.
        with tempfile.TemporaryDirectory() as folder:
            missing = os.path.join(folder, "nope.jpg")

            _code, _stdout, generated, _stream = self._run_generate(
                [missing, "--generate-manifest", os.path.join(folder, "out.json")]
            )
            analyzed_code, _stdout, analyzed = self.run_cli([missing, "--no-update-vocab"])

            self.assertEqual(analyzed_code, 2)
            self.assertEqual(self.usage_error(generated), self.usage_error(analyzed))

    def test_an_existing_image_still_writes_its_manifest_and_calls_no_model(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            image = os.path.join(folder, "box3_025.jpg")
            with open(image, "w", encoding="utf-8") as handle:
                handle.write("")
            out_path = os.path.join(folder, "out.json")

            code, stdout, stderr, stream = self._run_generate(
                [image, "--generate-manifest", out_path]
            )

            self.assertIsNone(code)
            self.assertEqual(stdout, "")
            self.assertIn("Wrote manifest for 1 file(s) in 1 group(s)", stderr)
            with open(out_path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            self.assertEqual(manifest["items"], [{"path": image, "group": "box3_025"}])
            self.assertFalse(stream.called)


class TestARunThatWroteNothingSaysSo(_CliTestCase):
    """Writes requested, files seen, none written: the run failed.

    The apply step used to report ``files_written=0 errors=3`` and exit 0. The
    analysis had run, the results had printed and the changeset had been
    written, so the run looked successful while the one thing asked of it
    silently did not happen -- and a script checking the exit status would carry
    on to the next box of scans.

    The line is drawn at *total* failure only. One locked file among fifty is
    ordinary and belongs in the records; zero of fifty is a setting that is
    wrong for all of them, which no amount of reading per-file errors will make
    the caller notice if the process said it succeeded.

    ``apply_changeset`` is stubbed rather than driven through real ExifTool: the
    behaviour under test is what the CLI does with the summary, and the summary
    is the seam. The real path was confirmed separately against read-only files.
    """

    def _run_with_summary(self, summary: dict, argv_extra: list[str] | None = None):
        """Run a -w folder batch whose apply step reports *summary*."""
        folder = self.make_folder()
        # A real file rather than a patched resolver, matching the idiom above:
        # the pre-flight resolves this path itself and nothing executes it.
        exiftool_path = os.path.join(folder, "exiftool.exe")
        with open(exiftool_path, "w", encoding="utf-8"):
            pass
        with patch(
            "photokin.cli.process_manifest_stream", return_value={"results": {}}
        ), patch("photokin.cli.apply_changeset", return_value=summary):
            return self.run_cli(
                [folder, "-w", "--exiftool-path", exiftool_path, *(argv_extra or [])]
            )

    def test_zero_written_out_of_several_seen_fails_the_run(self) -> None:
        code, _stdout, stderr = self._run_with_summary(
            {"files_seen": 3, "files_written": 0, "tags_written": 0,
             "errors": [{"path": "a.jpg", "error": "read-only"}], "warnings": []}
        )
        self.assertEqual(code, 2)
        self.assertIn("[ERROR] nothing was written", stderr)

    def test_the_per_file_errors_survive_the_failure(self) -> None:
        """The summary is logged before the exit, not replaced by it.

        Failing the run is only an improvement if it still says which files
        failed and why; a bare exit code would be less informative than the
        exit 0 it replaces.
        """
        code, _stdout, stderr = self._run_with_summary(
            {"files_seen": 2, "files_written": 0, "tags_written": 0,
             "errors": [{"path": "a.jpg", "error": "Error renaming temporary file"}],
             "warnings": []}
        )
        self.assertEqual(code, 2)
        self.assertIn("[ExifTool] Errors:", stderr)
        self.assertIn("Error renaming temporary file", stderr)

    def test_a_partial_failure_is_not_a_failed_run(self) -> None:
        """The bound. Some files were written, so the settings were right."""
        code, _stdout, stderr = self._run_with_summary(
            {"files_seen": 3, "files_written": 2, "tags_written": 2,
             "errors": [{"path": "c.jpg", "error": "locked"}], "warnings": []}
        )
        self.assertIsNone(code)
        self.assertNotIn("nothing was written", stderr)

    def test_seeing_no_files_at_all_is_not_a_failed_run(self) -> None:
        """An empty changeset proposes nothing, so nothing failing is correct."""
        code, _stdout, stderr = self._run_with_summary(
            {"files_seen": 0, "files_written": 0, "tags_written": 0,
             "errors": [], "warnings": []}
        )
        self.assertIsNone(code)
        self.assertNotIn("nothing was written", stderr)

    def test_manifest_input_keeps_the_plugin_contract(self) -> None:
        """Manifest mode reports per item and exits 0, here as everywhere else.

        photokin/README.md records this asymmetry deliberately: the plug-in
        reads the per-item records, so failing the batch tells it less than the
        records already do. The same total failure that fails a folder run must
        leave a manifest run's exit status alone.
        """
        folder = self.make_folder()
        manifest_path = _write_manifest(folder)
        exiftool_path = os.path.join(folder, "exiftool.exe")
        with open(exiftool_path, "w", encoding="utf-8"):
            pass
        summary = {"files_seen": 3, "files_written": 0, "tags_written": 0,
                   "errors": [{"path": "a.jpg", "error": "read-only"}], "warnings": []}
        with patch(
            "photokin.cli.process_manifest_stream", return_value={"results": {}}
        ), patch("photokin.cli.apply_changeset", return_value=summary):
            code, _stdout, stderr = self.run_cli(
                ["--manifest", manifest_path, "-w", "--exiftool-path", exiftool_path]
            )
        self.assertIsNone(code)
        self.assertNotIn("nothing was written", stderr)
        # And the records the plug-in reads are still there.
        self.assertIn("[ExifTool] Errors:", stderr)


class TestProviderResolution(_CliTestCase):
    """--provider resolution: flag, then LLM_PROVIDER, then the installed SDK.

    There is no hardcoded default provider. ``_NEUTRAL_ENV`` pins
    ``LLM_PROVIDER`` for every other test in this file, so each case here
    removes it and states its own detection result.
    """

    def test_the_one_installed_sdk_is_the_default(self):
        folder = self.make_folder()
        with patch("photokin.utils.installed_provider_sdks", return_value=["anthropic"]):
            code, _stdout, stderr = self.run_cli(
                [folder, "--dry-run"], env={"LLM_PROVIDER": None}
            )
        self.assertIsNone(code)
        self.assertIn("provider  : Claude", stderr)

    def test_multiple_installed_sdks_require_a_choice(self):
        folder = self.make_folder()
        with patch(
            "photokin.utils.installed_provider_sdks", return_value=["openai", "anthropic"]
        ):
            code, _stdout, stderr = self.run_cli(
                [folder, "--dry-run"], env={"LLM_PROVIDER": None}
            )
        self.assertEqual(code, 2)
        self.assertIn("more than one provider SDK is installed (openai, anthropic)", stderr)
        # The remedy is a real, pasteable choice, not a placeholder.
        self.assertIn("--provider openai", stderr)
        self.assertIn("LLM_PROVIDER", stderr)

    def test_no_installed_sdk_names_the_install(self):
        folder = self.make_folder()
        with patch("photokin.utils.installed_provider_sdks", return_value=[]):
            code, _stdout, stderr = self.run_cli(
                [folder, "--dry-run"], env={"LLM_PROVIDER": None}
            )
        self.assertEqual(code, 2)
        self.assertIn('pip install "photokin[openai]"', stderr)

    def test_env_choice_quiets_the_requirement(self):
        folder = self.make_folder()
        with patch(
            "photokin.utils.installed_provider_sdks", return_value=["openai", "anthropic"]
        ):
            code, _stdout, stderr = self.run_cli(
                [folder, "--dry-run"], env={"LLM_PROVIDER": "anthropic"}
            )
        self.assertIsNone(code)
        self.assertIn("provider  : Claude", stderr)

    def test_flag_beats_env(self):
        folder = self.make_folder()
        code, _stdout, stderr = self.run_cli(
            [folder, "--dry-run", "--provider", "openai"], env={"LLM_PROVIDER": "anthropic"}
        )
        self.assertIsNone(code)
        self.assertIn("provider  : ChatGPT", stderr)

    def test_env_choice_is_case_insensitive(self):
        folder = self.make_folder()
        code, _stdout, stderr = self.run_cli(
            [folder, "--dry-run"], env={"LLM_PROVIDER": "Anthropic"}
        )
        self.assertIsNone(code)
        self.assertIn("provider  : Claude", stderr)

    def test_unrecognized_env_value_is_a_usage_error(self):
        """A typo'd LLM_PROVIDER must not fall through to a silent OpenAI guess --
        the exact guess the whole flag/env/installed-SDK order exists to avoid."""
        folder = self.make_folder()
        code, _stdout, stderr = self.run_cli(
            [folder, "--dry-run"], env={"LLM_PROVIDER": "chatgpt-4"}
        )
        self.assertEqual(code, 2)
        self.assertIn("LLM_PROVIDER=`chatgpt-4`", stderr)
        self.assertIn("openai, anthropic, gemini, openrouter", stderr)

    def test_generate_manifest_needs_no_provider(self):
        """--generate-manifest never calls a model, so it must not demand one."""
        folder = self.make_folder()
        dest = os.path.join(folder, "scans-manifest.json")
        with patch("photokin.utils.installed_provider_sdks", return_value=[]):
            code, _stdout, _stderr = self.run_cli(
                [folder, "--generate-manifest", dest], env={"LLM_PROVIDER": None}
            )
        self.assertIsNone(code)
        self.assertTrue(os.path.exists(dest))


class TestEmptyArgvRouting(_CliTestCase):
    """Empty argv goes to the interactive prompt only when stdin is a terminal.

    A headless launcher -- a plugin's subprocess call, a script, a scheduled
    task -- whose argument list came out empty (a quoting bug that ate every
    token, for one) is not a human at a keyboard. Routing it into a stdin
    read it can never answer trades one silent hang for another; it should
    get the same usage error any other malformed invocation gets.
    """

    def test_non_tty_stdin_is_a_usage_error_not_a_prompt(self):
        with patch("sys.stdin.isatty", return_value=False), patch(
            "photokin.cli._interactive_prompt"
        ) as prompt:
            code, _stdout, stderr = self.run_cli([])
        prompt.assert_not_called()
        self.assertEqual(code, 2)
        self.assertIn("no terminal to prompt on", stderr)

    def test_tty_stdin_still_prompts(self):
        with patch("sys.stdin.isatty", return_value=True), patch(
            "photokin.cli._interactive_prompt", side_effect=SystemExit(0)
        ) as prompt:
            code, _stdout, _stderr = self.run_cli([])
        prompt.assert_called_once()
        self.assertEqual(code, 0)


def _ndjson_lines(path: str) -> list[dict]:
    """Parse every line of an NDJSON file, in order."""
    with open(path, "r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _envelope_records(lines: list[dict]) -> list[dict]:
    """The ``run: ...`` records among *lines*, in order."""
    return [rec for rec in lines if "run" in rec]


class TestRunEnvelope(_CliTestCase):
    """Every pre-flight refusal, and every real run, is now visible in the
    results NDJSON to a caller that never sees stderr -- a fire-and-forget
    subprocess launch with its output discarded, which cannot tell "still
    running" from "already failed" any other way.
    """

    def test_a_fresh_destination_gets_start_before_any_preflight_runs(self):
        """Even a refusal that fires before the model would ever be reached
        (an unwritable ExifTool tag, here) leaves run: start + run: fatal
        behind -- not a file that simply never appeared."""
        folder = self.make_folder()
        out_path = os.path.join(folder, "results.ndjson")
        code, _stdout, _stderr = self.run_cli(
            [folder, "--output-file", out_path, "--exiftool-fields", "XMP:dc:Description"]
        )
        self.assertEqual(code, 2)
        records = _envelope_records(_ndjson_lines(out_path))
        self.assertEqual(records[0]["run"], "start")
        self.assertEqual(records[-1]["run"], "fatal")
        self.assertIn("XMP:dc:Description", records[-1]["error"]["message"])

    def test_a_successful_run_ends_with_plan_then_complete(self):
        folder = self.make_folder()
        out_path = os.path.join(folder, "results.ndjson")
        stream = Mock(
            return_value={"results": {}, "errors": {}, "groups_failed": 0, "files_unsent": 0, "cancelled": False}
        )
        with patch("photokin.cli.process_manifest_stream", stream):
            code, _stdout, _stderr = self.run_cli([folder, "--output-file", out_path])
        self.assertIsNone(code)
        records = _envelope_records(_ndjson_lines(out_path))
        self.assertEqual([r["run"] for r in records], ["start", "plan", "complete"])
        self.assertEqual(records[-1]["files_recorded"], 0)
        self.assertEqual(records[-1]["groups_failed"], 0)

    def test_a_cancelled_run_ends_with_cancelled_not_complete(self):
        folder = self.make_folder()
        out_path = os.path.join(folder, "results.ndjson")
        stream = Mock(
            return_value={"results": {}, "errors": {}, "groups_failed": 0, "files_unsent": 0, "cancelled": True}
        )
        with patch("photokin.cli.process_manifest_stream", stream):
            code, _stdout, _stderr = self.run_cli([folder, "--output-file", out_path])
        self.assertIsNone(code)
        records = _envelope_records(_ndjson_lines(out_path))
        self.assertEqual(records[-1]["run"], "cancelled")

    def test_every_record_is_schema_stamped(self):
        folder = self.make_folder()
        out_path = os.path.join(folder, "results.ndjson")

        def _stream(*, ndjson_writer=None, **_ignored):
            ndjson_writer(json.dumps({"path": "a.jpg", "status": "ok"}))
            return {"results": {}, "errors": {}, "groups_failed": 0, "files_unsent": 0, "cancelled": False}

        with patch("photokin.cli.process_manifest_stream", _stream):
            self.run_cli([folder, "--output-file", out_path])
        lines = _ndjson_lines(out_path)
        self.assertTrue(lines, "expected at least one record")
        self.assertTrue(all(rec.get("schema_version") == cli._NDJSON_SCHEMA_VERSION for rec in lines))

    def test_batch_id_is_stamped_on_envelope_records_too(self):
        """Not just per-file records -- --batch-id used to only reach those."""
        folder = self.make_folder()
        out_path = os.path.join(folder, "results.ndjson")
        stream = Mock(
            return_value={"results": {}, "errors": {}, "groups_failed": 0, "files_unsent": 0, "cancelled": False}
        )
        with patch("photokin.cli.process_manifest_stream", stream):
            self.run_cli([folder, "--output-file", out_path, "--batch-id", "b17"])
        records = _envelope_records(_ndjson_lines(out_path))
        self.assertTrue(all(rec.get("batch_id") == "b17" for rec in records))

    def test_dry_run_never_touches_the_destination(self):
        """--dry-run's existing promise: this includes the envelope, which is
        a destination like any other -- not an exception to the rule."""
        folder = self.make_folder()
        out_path = os.path.join(folder, "results.ndjson")
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write("PREVIOUS RUN\n")
        stream = Mock()
        with patch("photokin.cli.process_manifest_stream", stream):
            code, _stdout, _stderr = self.run_cli([folder, "--output-file", out_path, "--dry-run"])
        self.assertIsNone(code)
        stream.assert_not_called()
        with open(out_path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "PREVIOUS RUN\n")

    def test_generate_manifest_beside_output_file_never_opens_an_envelope(self):
        """--output-file is refused outright beside --generate-manifest --
        the combination must not create the file it names before refusing it."""
        folder = self.make_folder()
        out_path = os.path.join(folder, "results.ndjson")
        manifest_path = os.path.join(folder, "manifest.json")
        code, _stdout, stderr = self.run_cli(
            [folder, "--generate-manifest", manifest_path, "--output-file", out_path]
        )
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(out_path))

    def test_a_preexisting_destination_survives_a_refusal_untouched(self):
        """The pinned contract from before the envelope existed, still true:
        a refusal must not destroy what a previous run left behind."""
        folder = self.make_folder()
        out_path = os.path.join(folder, "results.ndjson")
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write("PREVIOUS RUN\n")
        code, _stdout, _stderr = self.run_cli(
            [folder, "--output-file", out_path, "--exiftool-fields", "XMP:dc:Description"]
        )
        self.assertEqual(code, 2)
        with open(out_path, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "PREVIOUS RUN\n")

    def test_a_preexisting_destination_gets_the_envelope_once_the_run_commits(self):
        """The other half of the deferred-open design: once every check has
        passed, the run is about to overwrite this file anyway, so it gets
        the same run: start a fresh destination got immediately."""
        folder = self.make_folder()
        out_path = os.path.join(folder, "results.ndjson")
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write("PREVIOUS RUN\n")
        stream = Mock(
            return_value={"results": {}, "errors": {}, "groups_failed": 0, "files_unsent": 0, "cancelled": False}
        )
        with patch("photokin.cli.process_manifest_stream", stream):
            code, _stdout, _stderr = self.run_cli([folder, "--output-file", out_path])
        self.assertIsNone(code)
        records = _envelope_records(_ndjson_lines(out_path))
        self.assertEqual([r["run"] for r in records], ["start", "plan", "complete"])


class TestVerboseBundle(_CliTestCase):
    """``-v`` bundles the two debug-dump flags, mirroring how ``-w`` bundles
    ``--changeset``/``--exiftool-write``."""

    def test_expands_to_both_dump_flags(self):
        folder = self.make_folder()
        # -v's own default --log-file lands under the cwd-relative
        # --debug-dump-dir default for folder input; without isolating cwd
        # this would leak a real debug/ directory into the repository.
        self.enter_folder(folder)
        stream = Mock(return_value={"results": {}, "errors": {}})
        with patch("photokin.cli.process_manifest_stream", stream):
            self.run_cli([folder, "-v"])
        cfg = stream.call_args.kwargs["cfg"]
        self.assertTrue(cfg.debug_dump_llm_request)
        self.assertTrue(cfg.debug_dump_hydration)

    def test_explicit_false_contradicts_v(self):
        folder = self.make_folder()
        code, _stdout, stderr = self.run_cli([folder, "-v", "--debug-dump-hydration", "false"])
        self.assertEqual(code, 2)
        self.assertIn("-v", stderr)
        self.assertIn("--debug-dump-hydration false", stderr)

    def test_refused_beside_generate_manifest(self):
        folder = self.make_folder()
        manifest_path = os.path.join(folder, "manifest.json")
        code, _stdout, stderr = self.run_cli([folder, "--generate-manifest", manifest_path, "-v"])
        self.assertEqual(code, 2)
        self.assertIn("-v", stderr)

    def test_v_defaults_the_log_file_beside_the_other_dumps(self):
        """Folder input's own --debug-dump-dir default is cwd-relative (a
        pre-existing behavior, not something -v changes) -- enter_folder
        makes that cwd the scratch folder instead of wherever pytest runs
        from, which would otherwise leak a real debug/ directory into the
        repository on every test run."""
        folder = self.make_folder()
        self.enter_folder(folder)
        stream = Mock(return_value={"results": {}, "errors": {}})
        with patch("photokin.cli.process_manifest_stream", stream):
            self.run_cli([folder, "-v", "--batch-id", "b1"])
        log_path = os.path.join(folder, "debug", "b1.log")
        self.assertTrue(os.path.isfile(log_path), f"expected a log file at {log_path}")

    def test_explicit_log_file_is_not_overridden_by_v(self):
        folder = self.make_folder()
        custom_log = os.path.join(folder, "mine.log")
        stream = Mock(return_value={"results": {}, "errors": {}})
        with patch("photokin.cli.process_manifest_stream", stream):
            self.run_cli([folder, "-v", "--log-file", custom_log])
        self.assertTrue(os.path.isfile(custom_log))

    def test_explicit_log_file_works_without_v_too(self):
        folder = self.make_folder()
        custom_log = os.path.join(folder, "mine.log")
        stream = Mock(return_value={"results": {}, "errors": {}})
        with patch("photokin.cli.process_manifest_stream", stream):
            self.run_cli([folder, "--log-file", custom_log])
        self.assertTrue(os.path.isfile(custom_log))

    def test_explicit_log_file_creates_its_parent_directory(self):
        """Regression: logging.FileHandler does not do this on its own."""
        folder = self.make_folder()
        nested_log = os.path.join(folder, "nested", "deep", "mine.log")
        stream = Mock(return_value={"results": {}, "errors": {}})
        with patch("photokin.cli.process_manifest_stream", stream):
            code, _stdout, _stderr = self.run_cli([folder, "--log-file", nested_log])
        self.assertIsNone(code)
        self.assertTrue(os.path.isfile(nested_log))


class TestCancelFile(_CliTestCase):
    """``--cancel-file`` is threaded through to ``process_manifest_stream`` as
    ``should_cancel``, and its file existence is the only thing that trips it."""

    def test_cancel_file_becomes_a_should_cancel_callable(self):
        folder = self.make_folder()
        cancel_path = os.path.join(folder, "CANCEL")
        stream = Mock(return_value={"results": {}, "errors": {}})
        with patch("photokin.cli.process_manifest_stream", stream):
            self.run_cli([folder, "--cancel-file", cancel_path])
        should_cancel = stream.call_args.kwargs["should_cancel"]
        self.assertFalse(should_cancel())
        with open(cancel_path, "w", encoding="utf-8") as handle:
            handle.write("")
        self.assertTrue(should_cancel())

    def test_no_flag_means_no_cancellation_support(self):
        folder = self.make_folder()
        stream = Mock(return_value={"results": {}, "errors": {}})
        with patch("photokin.cli.process_manifest_stream", stream):
            self.run_cli([folder])
        self.assertIsNone(stream.call_args.kwargs["should_cancel"])


class TestCapabilities(_CliTestCase):
    """``--capabilities`` answers a compatibility check before any input is
    required, the same way ``--help`` does."""

    def test_prints_json_and_exits_without_requiring_input(self):
        code, stdout, _stderr = self.run_cli(["--capabilities"])
        self.assertIsNone(code)
        payload = json.loads(stdout)
        self.assertIn("version", payload)
        self.assertEqual(payload["ndjson_schema_version"], cli._NDJSON_SCHEMA_VERSION)
        self.assertIn("--verbose", payload["flags"])
        self.assertIn("--cancel-file", payload["flags"])
        self.assertEqual(payload["canonical_tags"]["caption"], "XMP-dc:Description")
        self.assertIn("anthropic", payload["providers"])

    def test_does_not_call_the_model(self):
        stream = Mock()
        with patch("photokin.cli.process_manifest_stream", stream):
            self.run_cli(["--capabilities"])
        stream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
