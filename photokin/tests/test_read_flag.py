"""Phase C3: ``-r`` / ``--read``, and the date it makes reachable.

Reading a file's own metadata is now an explicit opt-in that works in every
input mode, mirroring ``-w`` / ``--write``. Two things make that worth pinning
rather than trusting: the flag decides whether photokin shells out to ExifTool
against the user's photo library at all, and turning the read on brings a
dormant date-correction heuristic to life in folder mode for the first time.
``photokin/tests/test_read_flag_hazards.py`` covers the guards that keep the
read safe once it happens -- the ``DATE:`` interlock, batching, the caption
join, title provenance, which file a group's metadata comes from. This module
covers the flag itself:

* nothing is read without it, in any input mode, measured at the subprocess;
* the worked example: an EXIF scan date is *evidence*, not truth -- it feeds the
  gap heuristic and can rewrite the file's date, but it no longer overwrites the
  model's inference at confidence 1.0;
* ``dateTimeOriginal`` actually reaches the prompt, which the key rename between
  ``combine_group_metadata`` and the forward allowlist used to prevent;
* ``-r`` hydrates folder, single-photo and manifest input alike, and tells the
  merge that the titles it filled in may be the files' own rather than a human's;
* ``-r --generate-manifest`` writes what it read, and feeding that document back
  reproduces the run without reading anything;
* grouping stays permutation-invariant now that every item carries metadata;
* a read that cannot happen fails loudly up front, and one that half-happens
  costs the run nothing.

The provider is never built and ExifTool is never launched: the three analyzer
entry points are replaced, and so are binary discovery and ``subprocess.run``.
Image fixtures are empty placeholder files, since grouping reads filenames only,
and every path lives under a ``TemporaryDirectory``.
"""

import io
import itertools
import json
import logging
import os
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from typing import Any, ClassVar, NamedTuple
from unittest.mock import patch

from photokin import cli, core, utils
from photokin.canonical import build_canonical_patch
from photokin.exiftool import ExiftoolConfig
from photokin.exiftool.hydrate import hydrate_item_metadata
from photokin.exiftool.manifest import DEFAULT_EXIFTOOL_FIELDS

# Blanked rather than removed: each is read through a falsy-default lookup, so an
# empty value pins the documented default whatever the developer's shell exports.
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

#: The plan summary's read row, stated on every run. The "off" spelling is
#: pinned as a literal because it is how a caller who never passes ``-r`` -- the
#: Lightroom plug-in, which used to hydrate unasked -- learns that nothing is
#: being read any more; the "on" one is built from the read set, since naming
#: the tags is the whole content of that row.
_READ_ROW_OFF = "read      : none (-r not given)"
_READ_ROW_ON = "read      : ExifTool " + ", ".join(DEFAULT_EXIFTOOL_FIELDS)

#: The checked-in document ``-r --generate-manifest`` has to reproduce byte for
#: byte, beside the un-hydrated golden ``test_folder_routing.py`` already keeps.
_GOLDEN_READ_MANIFEST = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "manifests",
    "read_flag_manifest.json",
)
#: Stands in for the run's temporary directory in that golden, which has to
#: compare equal from any checkout on any machine.
_FOLDER_TOKEN = "<FOLDER>"


def _tokenize(text: str, folder: str) -> str:
    """Make a written JSON document comparable against the checked-in golden.

    A copy of ``test_folder_routing._tokenize`` rather than an import of it:
    ``photokin/tests`` is not a package, so a cross-module import would depend on
    how the runner happened to set up ``sys.path`` -- the same reason this module
    keeps its own ``_CliTestCase`` rather than borrowing one.

    Args:
        text: The document as written, holding absolute paths.
        folder: The directory the run was given.

    Returns:
        The document with *folder* replaced by ``<FOLDER>`` and Windows path
        separators rewritten as forward slashes, so one golden serves every
        platform.
    """
    return text.replace(json.dumps(folder)[1:-1], _FOLDER_TOKEN).replace("\\\\", "/")


def _touch(directory: str, name: str) -> str:
    """Create an empty placeholder image and return its path."""
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8"):
        pass
    return path


def _batch_paths(cmd: Sequence[str]) -> list[str]:
    """Return the file paths from one prepared ExifTool command line.

    Everything up to the last switch is the invariant prefix: the binary,
    ``-json`` and ``-G1``, two ``-charset`` pairs and one ``-TAG`` selector per
    requested field. The charset *values* are bare words, so "does not start
    with a dash" would count them as files; the tail after the final switch is
    the batch and nothing else.

    Args:
        cmd: The argument list handed to ``subprocess.run``.

    Returns:
        The paths this invocation was asked to read, in order.
    """
    last_switch = max(index for index, arg in enumerate(cmd) if arg.startswith("-"))
    return list(cmd[last_switch + 1 :])


class _CompletedProcess(NamedTuple):
    """The three fields ``run_exiftool_json`` reads off a finished process."""

    returncode: int
    stdout: str
    stderr: str


class _ExifToolStub:
    """Stands in for the ExifTool binary, recording every invocation.

    The recording is the point. Whether a run read the user's files is not
    visible in its output -- an un-hydrated run produces records that look
    complete -- so the only honest place to assert it is the subprocess boundary.

    Attributes:
        tags: ExifTool tag values per normalized path; a path absent from it is
            a file holding nothing readable.
        invocations: One entry per launch, holding the paths that batch carried.
        binary: Whether the binary resolves at all. False makes both resolution
            sites raise, which is what a machine with no ExifTool looks like.
        failure: Raised instead of running, standing in for a mid-run failure
            that gets past the pre-flight -- a lock, a device error, a timeout.
        raw_stdout: Returned verbatim instead of a JSON document, for the case
            where ExifTool answers with something unparseable.
    """

    def __init__(
        self,
        tags: dict[str, dict[str, Any]] | None = None,
        *,
        binary: bool = True,
        failure: Exception | None = None,
        raw_stdout: str | None = None,
    ) -> None:
        self.tags = {
            utils.normalize_path(path): value for path, value in (tags or {}).items()
        }
        self.invocations: list[list[str]] = []
        self.binary = binary
        self.failure = failure
        self.raw_stdout = raw_stdout

    def run(self, cmd: Sequence[str], **_kwargs: object) -> _CompletedProcess:
        """Record one invocation and answer it from :attr:`tags`."""
        paths = _batch_paths(cmd)
        self.invocations.append(paths)
        if self.failure is not None:
            raise self.failure
        if self.raw_stdout is not None:
            return _CompletedProcess(0, self.raw_stdout, "")
        # ExifTool reports SourceFile with forward slashes even on Windows,
        # which is the mismatch the hydrator normalizes away.
        records = [
            {
                "SourceFile": path.replace("\\", "/"),
                **self.tags.get(utils.normalize_path(path), {}),
            }
            for path in paths
        ]
        return _CompletedProcess(0, json.dumps(records), "")

    def _resolve(self, _cfg: object = None) -> str:
        """Stand in for ``resolve_exiftool_path``."""
        if not self.binary:
            raise FileNotFoundError("ExifTool not found.")
        return "/fake/exiftool"

    @contextmanager
    def installed(self) -> Iterator["_ExifToolStub"]:
        """Replace binary discovery and the subprocess for the block.

        Both resolution sites are patched, and deliberately: without that, "did
        this run shell out" would depend on whether the developer happens to
        have ExifTool installed, and a gate regression would pass unnoticed on
        any machine without it.
        """
        with patch("photokin.cli.resolve_exiftool_path", self._resolve), patch(
            "photokin.exiftool.hydrate.resolve_exiftool_path", self._resolve
        ), patch("photokin.exiftool.manifest.subprocess.run", self.run):
            yield self


def _hydrator_for(stub: _ExifToolStub) -> Callable[[list[dict]], None]:
    """Return the ``metadata_hydrator`` the CLI would build, reading *stub*."""

    def _hydrate(items: list[dict]) -> None:
        with stub.installed():
            hydrate_item_metadata(items, ExiftoolConfig())

    return _hydrate


class _Call(NamedTuple):
    """One model call: which analyzer, what it carried, what it was told."""

    callee: str
    #: ``((part label, (basename, ...)), ...)`` -- the same shape for all three
    #: analyzers, so two runs compare directly whichever one they reached.
    payload: tuple[tuple[str, tuple[str, ...]], ...]
    forwarded: dict


class _Run(NamedTuple):
    """One CLI run: its exit code, its streams, its calls and its ExifTool use."""

    code: int | None
    stdout: str
    stderr: str
    calls: list[_Call]
    invocations: list[list[str]]


def _reply(**fields: Any) -> dict:
    """Return a model record, defaulted to the minimum the pipeline accepts."""
    return {"caption": "A caption", "keywords": ["family"], **fields}


class _RecordingAnalyzers:
    """Stand-in for the three model entry points, recording their arguments.

    ``original_meta`` is recorded alongside the file set because it is the whole
    subject here: it is the forwarded snapshot the prompt is built from, and
    under ``-r`` it is where a file's own tags surface.
    """

    def __init__(self, reply: dict | None = None) -> None:
        self.calls: list[_Call] = []
        self.reply = reply or _reply()

    def _record(
        self,
        callee: str,
        payload: tuple[tuple[str, tuple[str, ...]], ...],
        original_meta: dict | None,
        first_path: str,
    ) -> dict:
        """Bank one call and return the ``{"result": {path: record}}`` shape."""
        self.calls.append(
            _Call(
                callee,
                tuple(
                    (label, tuple(os.path.basename(p) for p in paths))
                    for label, paths in payload
                ),
                json.loads(json.dumps(original_meta or {})),
            )
        )
        return {"result": {first_path: json.loads(json.dumps(self.reply))}}

    def photo(
        self,
        front_path: str,
        back_path: str | None = None,
        config: utils.Config | None = None,
        *,
        original_meta: dict | None = None,
        write_sidecar: bool = False,
    ) -> dict:
        """Record an ``analyze_photo`` call."""
        payload: list[tuple[str, tuple[str, ...]]] = [("Front", (front_path,))]
        if back_path:
            payload.append(("Back", (back_path,)))
        return self._record("photo", tuple(payload), original_meta, front_path)

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
        payload = tuple(
            (label, tuple(paths))
            for label, paths in (("Front", fronts), ("Back", backs))
            if paths
        )
        return self._record("front_back", payload, original_meta, (fronts + backs)[0])

    def parts(
        self,
        parts: list[tuple[str, list[str]]],
        config: utils.Config | None = None,
        *,
        original_meta: dict | None = None,
        write_sidecar: bool = False,
    ) -> dict:
        """Record an ``analyze_group_parts`` call."""
        payload = tuple((label, tuple(paths)) for label, paths in parts)
        first = next(path for _label, paths in parts for path in paths)
        return self._record("parts", payload, original_meta, first)


@contextmanager
def _recording(reply: dict | None = None) -> Iterator[_RecordingAnalyzers]:
    """Replace the three model entry points with recorders for the block."""
    rec = _RecordingAnalyzers(reply)
    with patch("photokin.core.analyze_photo", rec.photo), patch(
        "photokin.core.analyze_group_front_back", rec.front_back
    ), patch("photokin.core.analyze_group_parts", rec.parts):
        yield rec


class _ReadFlagTestCase(unittest.TestCase):
    """Scratch space, a CLI runner, and one way to run the stream directly.

    ``cli.main`` attaches a stderr handler to the ``photokin`` logger, so process
    state is both an input and an output of every CLI test: a handler left behind
    binds an abandoned stream and double-prints into the next run's capture.
    Kept local rather than imported from another test module -- ``photokin/tests``
    is not a package, so a cross-module import would depend on how the runner
    happened to set up ``sys.path``.
    """

    #: Whole records and whole manifests are compared here; a truncated diff
    #: would say two runs differed without saying where.
    maxDiff = None

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.work: str = scratch.name
        package_logger = logging.getLogger("photokin")
        self.addCleanup(package_logger.setLevel, package_logger.level)
        self.addCleanup(self._remove_cli_handlers)
        self._remove_cli_handlers()

    def _remove_cli_handlers(self) -> None:
        """Detach every handler ``cli.main`` installed, from both logger scopes.

        Both the stderr handler and the optional --log-file/-v one: leaving
        the latter attached across tests holds an open file handle into a
        temp directory a later test may already have cleaned up.
        """
        for logger in (logging.getLogger("photokin"), logging.getLogger()):
            for handler in list(logger.handlers):
                if handler.get_name() in (cli._LOG_HANDLER_NAME, cli._LOG_FILE_HANDLER_NAME):
                    logger.removeHandler(handler)
                    handler.close()

    def make_folder(self, *names: str, into: str = "scans") -> str:
        """Create a scratch folder holding empty placeholder files named *names*."""
        folder = os.path.join(self.work, into)
        os.makedirs(folder, exist_ok=True)
        for name in names:
            _touch(folder, name)
        return folder

    def write_manifest(self, items: list[dict], name: str = "batch.json") -> str:
        """Write a manifest document into scratch space and return its path."""
        path = os.path.join(self.work, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"items": items}, handle)
        return path

    def run_cli(
        self, argv: list[str], stub: _ExifToolStub, *, reply: dict | None = None
    ) -> _Run:
        """Run ``cli.main`` against *stub*, with every model entry point recorded.

        Args:
            argv: Arguments after the program name.
            stub: The ExifTool stand-in; its invocation log is returned with the
                run and is what the no-read assertions read.
            reply: The record every analyzer returns, for the few cases that turn
                on what the model said rather than on what it was told.

        Returns:
            The exit code (``None`` when ``main`` returned normally), both
            streams, the model calls and the ExifTool invocations.
        """
        stdout, stderr = io.StringIO(), io.StringIO()
        code: int | None = None
        with patch.dict(os.environ, _NEUTRAL_ENV), patch.object(
            sys, "argv", ["photokin", *argv]
        ), stub.installed(), _recording(reply) as rec, redirect_stdout(
            stdout
        ), redirect_stderr(stderr):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return _Run(code, stdout.getvalue(), stderr.getvalue(), rec.calls, stub.invocations)

    def stream(
        self, items: list[dict], stub: _ExifToolStub | None, *, reply: dict | None = None
    ) -> dict:
        """Run *items* through the stream, hydrating from *stub* when given.

        Args:
            items: Manifest items, folder-shaped (``{path}`` only) unless a test
                is specifically about caller-supplied metadata.
            stub: The ExifTool stand-in, or None for a run with no ``-r``.
            reply: The record every analyzer returns.

        Returns:
            The aggregate ``{"results": ..., "errors": ...}``.
        """
        with _recording(reply):
            return core.process_manifest_stream(
                manifest={"items": items},
                cfg=utils.Config(),
                metadata_hydrator=_hydrator_for(stub) if stub else None,
                # A stub stands in for photokin's own read, so this mirrors what
                # the CLI does under -r. The core cannot infer it from the
                # hydrator -- an embedder's own reads a database, not a file --
                # so a test standing in for the CLI has to say it as the CLI does.
                titles_may_be_from_files=stub is not None,
            )


class TestNothingIsReadWithoutTheFlag(_ReadFlagTestCase):
    """The reason the read is a flag rather than a default.

    A folder run must not silently shell out to ExifTool against a photo
    library and change what the model is sent. Manifest input used to hydrate
    unconditionally, so this is a real narrowing rather than a property that
    always held, and the plug-in's prompt quality depends on it being announced.

    Every assertion here is on the subprocess, because that is the only place
    the question is decided: an un-hydrated run's records look exactly like a
    hydrated one's over a file that happened to hold nothing.
    """

    _UNASKED = (
        "a run read the user's files unasked: ExifTool was launched against the "
        "input by a run that never passed -r. Reading is an explicit opt-in in "
        "every input mode -- a folder run must not shell out to ExifTool and "
        "change what the model is sent because it was pointed at a directory. "
        "See the C3 section of docs/unified-input-pipeline.md."
    )

    def _fixture(self) -> tuple[dict[str, list[str]], _ExifToolStub]:
        """Return one argv per input mode over one file, and a stub answering for it.

        Returns:
            The four argv forms, keyed by what the CLI will detect them as, and
            a stub that would answer richly if anything asked it to. The stub is
            fresh per call, so its invocation log belongs to one run.
        """
        folder = self.make_folder("box3_025.jpg")
        front = os.path.join(folder, "box3_025.jpg")
        inputs = {
            "folder": [folder],
            "single photo": [front],
            "manifest": [self.write_manifest([{"path": front}])],
            "generate-manifest": [
                folder,
                "--generate-manifest",
                os.path.join(self.work, "generated.json"),
            ],
        }
        stub = _ExifToolStub(
            {
                front: {
                    "EXIF:DateTimeOriginal": "2019:04:03 11:22:33",
                    "EXIF:UserComment": "box 3, envelope 12",
                    "XMP:Description": "Grandma on the porch",
                    "XMP:Title": "Scanned Image",
                    "XMP:Subject": ["family", "ohio"],
                }
            }
        )
        return inputs, stub

    def test_no_input_mode_reads_the_files_unasked(self) -> None:
        for label in self._fixture()[0]:
            with self.subTest(input=label):
                inputs, stub = self._fixture()

                run = self.run_cli(inputs[label], stub)

                self.assertIsNone(run.code)
                self.assertEqual(run.invocations, [], self._UNASKED)

    def test_the_model_is_told_nothing_the_files_hold(self) -> None:
        # The consequence, stated separately from the mechanism: what makes an
        # unasked read a problem is not the subprocess but that it silently
        # changes the prompt -- and the bill -- for a run that did not ask.
        inputs, stub = self._fixture()

        run = self.run_cli(inputs["folder"], stub)

        self.assertEqual([call.forwarded for call in run.calls], [{}], self._UNASKED)

    def test_the_stub_would_have_answered(self) -> None:
        """Non-vacuity: the same input under ``-r`` reads, so the stub works."""
        inputs, stub = self._fixture()

        run = self.run_cli([*inputs["folder"], "-r"], stub)

        self.assertEqual(len(run.invocations), 1)
        self.assertEqual(
            [os.path.basename(p) for p in run.invocations[0]], ["box3_025.jpg"]
        )
        self.assertEqual(run.calls[0].forwarded["userComment"], "box 3, envelope 12")

    def test_the_plan_summary_says_the_run_reads_nothing(self) -> None:
        # How the loss is announced to a plug-in that used to hydrate for free:
        # one row in the block every run prints before its first model call.
        inputs, stub = self._fixture()

        run = self.run_cli(inputs["folder"], stub)

        self.assertIn(_READ_ROW_OFF, run.stderr)
        self.assertNotIn(_READ_ROW_ON, run.stderr)


class TestTheScanDateIsEvidenceNotTruth(_ReadFlagTestCase):
    """The worked example, and the regression it exists to prevent.

    ``-r`` reads ``EXIF:DateTimeOriginal``. On a flatbed scan that is the day
    the print was *scanned*, not the day the photograph was taken, and nothing
    in the file distinguishes the two. The old merge stamped it over
    ``date_guess`` at confidence 1.0 whenever it was present, so reading the
    date would have asserted the scan date as the capture date and thrown away
    "circa 1952, confidence 0.7" -- the inference the model was paid for.

    It is now evidence: it drives the gap heuristic, which decides what reaches
    the FILE, and it is recorded as ``date_original``, but it never touches the
    model's guess. The heuristic itself is untouched by C3; what ``-r`` changes
    is that in folder mode it can finally fire at all, since a folder item used
    to carry no date to compare against.
    """

    #: "circa 1952", the model's own words for a print it cannot date exactly.
    #: 0.7 meets date_override_confidence_threshold (0.7, compared with ``>=``);
    #: the ``Y~`` pattern
    #: is not precise, so every row below is judged against the wide
    #: date_override_year_gap (20) rather than the precise one.
    MODEL_DATE: ClassVar[dict[str, Any]] = {
        "iso": "1952",
        "import_date": "1952-01-01",
        "confidence": 0.7,
        "pattern": "Y~",
    }

    #: The archivist's own dating, in the tag ``-r`` reads keywords out of.
    REVIEWED: ClassVar[tuple[str, ...]] = ("family", "DATE: Y!")

    class _Row(NamedTuple):
        """One row of the table: a file's date and what the run should do with it."""

        label: str
        exif: str
        keywords: tuple[str, ...] | None
        #: What ends up proposed for the file, which is what ``-w`` would write.
        written: str
        #: Whether the gap heuristic reported itself as having overridden.
        overridden: bool

    #: The gap is measured against 1952. 20 is the threshold and the comparison
    #: is strict, so 1972 is the last year that keeps its date and 1973 the
    #: first that loses it -- both are in the table, because a boundary stated
    #: only on one side is not stated.
    TABLE: ClassVar[tuple["TestTheScanDateIsEvidenceNotTruth._Row", ...]] = (
        _Row("the worked example", "2019:04:03 11:22:33", None, "1952-01-01", True),
        _Row("an older scanner run", "1990:01:01 00:00:00", None, "1952-01-01", True),
        _Row("one year outside the gap", "1973:01:01 00:00:00", None, "1952-01-01", True),
        _Row("exactly at the gap", "1972:01:01 00:00:00", None, "1972:01:01 00:00:00", False),
        _Row("a modern photo", "1955:06:01 09:00:00", None, "1955:06:01 09:00:00", False),
        _Row("hand reviewed", "2019:04:03 11:22:33", REVIEWED, "2019:04:03 11:22:33", False),
    )

    _CLOBBERED = (
        "the scan date overwrote the model's inference. An original date is "
        "evidence, not truth: on a flatbed scan EXIF:DateTimeOriginal is the day "
        "the print was scanned, so stamping it over date_guess at confidence 1.0 "
        "asserts the scan date as the capture date and discards the dating the "
        "model was paid for. It may drive the gap heuristic and fill "
        "dateTimeOriginal; it may not touch date_guess."
    )

    def _merged(self, row: "TestTheScanDateIsEvidenceNotTruth._Row") -> dict:
        """Run one row through the pipeline as a folder item and return its record.

        One placeholder file serves every row: the stream is handed a fresh item
        list and a fresh stub each time, so nothing carries between them and the
        difference under test is entirely in the tags the stub answers with.
        """
        folder = self.make_folder("box3_014.jpg")
        path = os.path.join(folder, "box3_014.jpg")
        tags: dict[str, Any] = {"EXIF:DateTimeOriginal": row.exif}
        if row.keywords is not None:
            tags["XMP:Subject"] = list(row.keywords)
        reply = _reply(
            caption="Two men beside a car",
            keywords=["automobile"],
            date_guess=dict(self.MODEL_DATE),
        )
        out = self.stream([{"path": path}], _ExifToolStub({path: tags}), reply=reply)
        return out["results"][path]

    def test_the_model_inference_survives_every_row(self) -> None:
        for row in self.TABLE:
            with self.subTest(row=row.label):
                merged = self._merged(row)

                self.assertEqual(merged["date_guess"], self.MODEL_DATE, self._CLOBBERED)

    def test_the_gap_rule_alone_decides_what_reaches_the_file(self) -> None:
        for row in self.TABLE:
            with self.subTest(row=row.label):
                merged = self._merged(row)

                self.assertEqual(
                    merged["dateTimeOriginal"],
                    row.written,
                    "the date proposed for the file is not the one the gap "
                    "heuristic decided on. Reading the date must change which "
                    "files the heuristic reaches, never the rule it applies.",
                )
                self.assertEqual(
                    "dateTimeOriginal" in merged["_merge"]["overrides"], row.overridden
                )

    def test_the_file_date_is_kept_as_evidence_rather_than_discarded(self) -> None:
        # Nothing is lost by refusing to call it the capture date: the value the
        # file held is on the record under its own key, so a consumer that
        # really wants the file's date still has it.
        for row in self.TABLE:
            with self.subTest(row=row.label):
                merged = self._merged(row)

                self.assertEqual(merged["date_original"], row.exif)

    def test_the_reviewed_marker_is_the_only_veto_in_its_row(self) -> None:
        """Non-vacuity: the last row differs from the first only by the marker."""
        with_marker = self.TABLE[-1]

        merged = self._merged(with_marker._replace(keywords=("family",)))

        self.assertEqual(merged["dateTimeOriginal"], "1952-01-01")
        self.assertIn("dateTimeOriginal", merged["_merge"]["overrides"])

    def test_the_worked_example_reaches_the_file_as_1952(self) -> None:
        # The end of the chain: what ``-w`` would hand ExifTool. A record whose
        # dateTimeOriginal was right but whose patch was not would still write
        # the wrong year into the photograph.
        cfg = utils.Config()

        rewritten = self._merged(self.TABLE[0])
        untouched = self._merged(self.TABLE[4])

        rewritten_patch, _meta = build_canonical_patch(rewritten, cfg)
        untouched_patch, _meta = build_canonical_patch(untouched, cfg)
        self.assertEqual(rewritten_patch["EXIF:DateTimeOriginal"]["value"], "1952-01-01")
        self.assertEqual(
            untouched_patch["EXIF:DateTimeOriginal"]["value"],
            "1955:06:01 09:00:00",
            "a modern photo inside the gap had its date rewritten from the "
            "model's guess; the heuristic declined, so the file's own date is "
            "what must be proposed",
        )

    def test_without_the_flag_the_heuristic_has_nothing_to_compare(self) -> None:
        # Why reading the date is the point of the flag rather than a side
        # effect: a folder item carries a path and nothing else, so before C3
        # this heuristic never fired in folder mode at all.
        folder = self.make_folder("box3_014.jpg", into="unread")
        path = os.path.join(folder, "box3_014.jpg")
        reply = _reply(date_guess=dict(self.MODEL_DATE))

        merged = self.stream([{"path": path}], None, reply=reply)["results"][path]

        self.assertNotIn("date_original", merged)
        self.assertNotIn("dateTimeOriginal", merged)
        self.assertEqual(merged["date_guess"], self.MODEL_DATE)


class TestTheForwardedDateReachesThePrompt(_ReadFlagTestCase):
    """The field the config forwards, which a key rename used to lose.

    ``metadata_forward.toml`` lists ``dateTimeOriginal`` and the prompt is
    written to receive it, but ``combine_group_metadata`` renamed the key to
    ``date`` on the way out and ``date`` is not in the allowlist, so it was
    dropped at the last step -- the config asked for the field, the prompt
    expected it, and nothing arrived::

        combine_group_metadata emits : caption, date, keywords, title, userComment
        ACTUALLY reaches the prompt  : caption, keywords, title, userComment
        dropped at the allowlist     : date

    Both spellings are now emitted. ``date`` cannot simply be renamed back:
    ``merge`` reads ``original["date"]``, and that reader is the gap heuristic
    which is the whole justification for reading the date in the first place.
    """

    FILE_DATE = "1948:05:01 10:23:00"

    def _forwarded(self) -> dict:
        """Return the metadata snapshot a hydrated folder run sends the model."""
        folder = self.make_folder("box3_025.jpg")
        path = os.path.join(folder, "box3_025.jpg")
        stub = _ExifToolStub({path: {"EXIF:DateTimeOriginal": self.FILE_DATE}})
        run = self.run_cli([folder, "-r"], stub)
        self.assertIsNone(run.code)
        return run.calls[0].forwarded

    def test_the_allowlist_names_the_forwarded_spelling_and_not_the_other(self) -> None:
        # The mechanism of the loss, pinned so the fix cannot be undone by
        # "just add date to the allowlist" -- which would leave two spellings of
        # one field in a contract the plug-in reads.
        self.assertIn("dateTimeOriginal", utils.DEFAULT_METADATA_FORWARD_FIELDS)
        self.assertNotIn("date", utils.DEFAULT_METADATA_FORWARD_FIELDS)

    def test_the_group_snapshot_carries_both_spellings(self) -> None:
        forwarded = self._forwarded()

        self.assertEqual(forwarded["dateTimeOriginal"], self.FILE_DATE)
        self.assertEqual(
            forwarded["date"],
            self.FILE_DATE,
            "the `date` spelling is gone. merge.py reads original['date'] -- "
            "that reader is the gap heuristic -- so the fix adds a spelling "
            "rather than renaming one",
        )

    def test_the_date_survives_the_allowlist_into_the_prompt(self) -> None:
        # The step that used to drop it. Composed exactly as ``analyze_photo``
        # composes it: the snapshot recorded above, and ``forward_fields=None``,
        # which is what every run resolves to today -- ``core`` opens the TOML
        # with ``json.load``, so the extension list is always empty and the
        # effective allowlist is always DEFAULT_METADATA_FORWARD_FIELDS.
        forwarded = self._forwarded()

        bundle = utils.build_prompt_bundle(
            "gpt-4o",
            "2026-01-01",
            forwarded_meta=forwarded,
            forward_fields=None,
            cfg=utils.Config(),
        )

        line = next(
            item["text"]
            for item in bundle
            if item.get("text", "").startswith("Forwarded metadata: ")
        )
        payload = json.loads(line[len("Forwarded metadata: ") :])
        self.assertEqual(
            payload.get("dateTimeOriginal"),
            self.FILE_DATE,
            "the date read out of the file never reached the prompt. "
            "combine_group_metadata renames dateTimeOriginal to date and only "
            "dateTimeOriginal is in the forward allowlist, so emitting one "
            "spelling drops the field at the last step",
        )
        self.assertNotIn("date", payload)


class TestReadHydratesEveryInputMode(_ReadFlagTestCase):
    """``-r`` works for folder, manifest and single-photo input alike.

    The gate is one expression, which is the point: the three inputs are one
    pipeline since B2, so the flag either reaches all of them or the phase did
    not happen. Hydration was manifest-only before C3, and folder items -- the
    ones with everything to gain, carrying a path and nothing else -- were
    skipped outright by a guard that required a ``metadata`` dict to already be
    there.
    """

    TAGS: ClassVar[dict[str, Any]] = {
        "EXIF:DateTimeOriginal": "1961:06:11 00:00:00",
        "EXIF:UserComment": "box 3, envelope 12",
        "XMP:Description": "Grandma on the porch",
        "XMP:Title": "Scanned Image",
        "XMP:Subject": ["family", "ohio"],
    }
    EXPECTED: ClassVar[dict[str, Any]] = {
        "keywords": ["family", "ohio"],
        "title": "Scanned Image",
        "caption": "Grandma on the porch",
        "date": "1961:06:11 00:00:00",
        "dateTimeOriginal": "1961:06:11 00:00:00",
        "userComment": "box 3, envelope 12",
    }

    def test_every_input_mode_sends_the_files_own_metadata(self) -> None:
        folder = self.make_folder("box3_025.jpg")
        front = os.path.join(folder, "box3_025.jpg")
        inputs = {
            "folder": [folder],
            "single photo": [front],
            "manifest": [self.write_manifest([{"path": front}])],
        }

        for label, argv in inputs.items():
            with self.subTest(input=label):
                stub = _ExifToolStub({front: self.TAGS})

                run = self.run_cli([*argv, "-r"], stub)

                self.assertIsNone(run.code)
                self.assertEqual(
                    run.calls[0].forwarded,
                    self.EXPECTED,
                    f"{label} input did not send the model what the file holds; "
                    "-r reads the whole set in every input mode",
                )

    def test_a_folder_item_carrying_only_a_path_is_no_longer_skipped(self) -> None:
        # The guard fix, at the level it was broken: hydration required an
        # existing ``metadata`` dict, and a folder item has none.
        folder = self.make_folder("box3_025.jpg", into="guard")
        path = os.path.join(folder, "box3_025.jpg")
        items: list[dict[str, Any]] = [{"path": path}]

        _hydrator_for(_ExifToolStub({path: self.TAGS}))(items)

        self.assertEqual(items[0]["metadata"]["userComment"], "box 3, envelope 12")

    def test_the_input_still_beats_the_file(self) -> None:
        # ``-r`` fills what the input does not carry and nothing more, so a
        # value a human supplied is never replaced by one out of a file.
        folder = self.make_folder("box3_025.jpg", into="meta")
        front = os.path.join(folder, "box3_025.jpg")
        meta_path = os.path.join(self.work, "meta.json")
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump({"userComment": "typed by a human"}, handle)
        stub = _ExifToolStub({front: self.TAGS})

        run = self.run_cli([front, "-r", "--meta", meta_path], stub)

        self.assertEqual(run.calls[0].forwarded["userComment"], "typed by a human")
        # And the rest of the set still arrives: --meta wins the field it names,
        # not the whole read.
        self.assertEqual(run.calls[0].forwarded["title"], "Scanned Image")

    def test_a_whole_folder_is_read_in_one_invocation(self) -> None:
        # The union of paths needing anything, in one launch. A per-file
        # subprocess would make -r cost a process per photo.
        folder = self.make_folder(
            "box3_025.jpg", "box3_025-back.jpg", "box3_026.jpg", "box3_027.jpg"
        )
        stub = _ExifToolStub(
            {os.path.join(folder, name): self.TAGS for name in os.listdir(folder)}
        )

        run = self.run_cli([folder, "-r"], stub)

        self.assertEqual(len(run.invocations), 1)
        self.assertEqual(
            sorted(os.path.basename(p) for p in run.invocations[0]),
            ["box3_025-back.jpg", "box3_025.jpg", "box3_026.jpg", "box3_027.jpg"],
        )

    def test_the_plan_summary_names_the_read_set(self) -> None:
        folder = self.make_folder("box3_025.jpg", into="plan")

        run = self.run_cli([folder, "-r"], _ExifToolStub())

        self.assertIn(_READ_ROW_ON, run.stderr)

    def test_the_flag_also_tells_the_merge_where_the_titles_came_from(self) -> None:
        """``-r`` wires up two things, and the second one is easy to drop.

        Reading ``XMP:Title`` is only half of it: the merge has to be told that
        the title in the item may be that read rather than a human's, or scanner
        boilerplate outranks the transcription and ``-r`` makes the record worse.
        The core cannot work that out for itself -- an embedder's hydrator reads
        a database, and its titles must keep winning -- so the CLI states it, and
        nothing but this run would notice the statement going missing.
        """
        folder = self.make_folder("box3_025.jpg", into="provenance")
        front = os.path.join(folder, "box3_025.jpg")
        stub = _ExifToolStub({front: self.TAGS})

        run = self.run_cli(
            [front, "-r"], stub, reply=_reply(title="Wedding Day 1952")
        )

        self.assertIsNone(run.code)
        # Sent the boilerplate, as every other test in this class expects...
        self.assertEqual(run.calls[0].forwarded["title"], "Scanned Image")
        # ...and did not let it back out over the transcription.
        record = json.loads(run.stdout)["results"][front]
        self.assertEqual(record["title"], "Wedding Day 1952")
        self.assertNotIn("title", record["_merge"]["overrides"])

    def test_the_same_read_without_the_flag_keeps_the_input_title(self) -> None:
        # The bound: the narrowing is the read's, not the merge's. With no -r
        # the same title is a caller's and still outranks the model.
        folder = self.make_folder("box3_025.jpg", into="provenance-off")
        front = os.path.join(folder, "box3_025.jpg")
        manifest = self.write_manifest(
            [{"path": front, "metadata": {"title": "Aunt Edith's wedding, St Marys"}}],
            name="titled.json",
        )

        run = self.run_cli(
            [manifest], _ExifToolStub(), reply=_reply(title="Wedding Day 1952")
        )

        self.assertIsNone(run.code)
        record = json.loads(run.stdout)["results"][front]
        self.assertEqual(record["title"], "Aunt Edith's wedding, St Marys")


class TestGeneratedManifestRoundTripsWhatWasRead(_ReadFlagTestCase):
    """``-r --generate-manifest`` writes the read down, and the write replays.

    The combination is the reason ``-r`` is allowed beside a flag that makes no
    model call: the generated document is how a read is captured once and reused
    without touching the files again. That only means something if feeding it
    back reproduces the run -- same grouping, same metadata in front of the
    model -- and if the replay itself reads nothing.
    """

    TAGS: ClassVar[dict[str, Any]] = {
        "EXIF:DateTimeOriginal": "2019:04:03 11:22:33",
        "EXIF:UserComment": "box 3, envelope 12",
        "XMP:Description": "Grandma on the porch",
        "XMP:Title": "Scanned Image",
        "XMP:Subject": ["family", "ohio"],
    }

    def _folder(self) -> tuple[str, dict[str, dict[str, Any]]]:
        """Return a folder of four scans, three of which hold metadata."""
        folder = self.make_folder(
            "box3_025.jpg", "box3_025-back.jpg", "box3_025b.jpg", "box3_026.jpg"
        )
        tags = {
            os.path.join(folder, "box3_025.jpg"): self.TAGS,
            os.path.join(folder, "box3_025-back.jpg"): {
                "EXIF:UserComment": "Ruth and Edith, back porch"
            },
            os.path.join(folder, "box3_026.jpg"): {"XMP:Title": "Untitled Scan"},
        }
        return folder, tags

    def _generate(self, folder: str, tags: dict[str, dict[str, Any]], name: str) -> str:
        """Write a generated manifest for *folder* under ``-r`` and return its path."""
        out_path = os.path.join(self.work, name)
        run = self.run_cli(
            [folder, "-r", "--generate-manifest", out_path], _ExifToolStub(tags)
        )
        self.assertIsNone(run.code)
        self.assertEqual(run.calls, [], "--generate-manifest called the model")
        return out_path

    def test_the_document_carries_what_was_read(self) -> None:
        folder, tags = self._folder()

        out_path = self._generate(folder, tags, "generated.json")

        with open(out_path, "r", encoding="utf-8") as handle:
            items = json.load(handle)["items"]
        written = {os.path.basename(item["path"]): item.get("metadata") for item in items}
        self.assertEqual(
            written,
            {
                "box3_025-back.jpg": {"userComment": "Ruth and Edith, back porch"},
                "box3_025.jpg": {
                    "dateTimeOriginal": "2019:04:03 11:22:33",
                    "userComment": "box 3, envelope 12",
                    "caption": "Grandma on the porch",
                    "title": "Scanned Image",
                    "keywords": ["family", "ohio"],
                },
                "box3_025b.jpg": None,
                "box3_026.jpg": {"title": "Untitled Scan"},
            },
            "the generated manifest does not describe what was read. A file "
            "holding nothing keeps its item exactly as it arrived; every other "
            "item carries the mapped keys and nothing else, since the document "
            "has to round-trip",
        )

    def test_the_replay_reproduces_the_grouping_and_the_metadata(self) -> None:
        folder, tags = self._folder()
        out_path = self._generate(folder, tags, "generated.json")

        original = self.run_cli([folder, "-r"], _ExifToolStub(tags))
        replay = self.run_cli([out_path], _ExifToolStub(tags))

        self.assertEqual([original.code, replay.code], [None, None])
        self.assertEqual(
            replay.calls,
            original.calls,
            "the generated manifest does not reproduce the folder run it "
            "describes: either the grouping moved or the metadata -r read did "
            "not survive the write",
        )
        # Whole-document equality, which holds here and is narrower than it
        # looks: the title rule is scoped by provenance -- a title read out of a
        # file yields to the model's, a title a caller supplied does not -- and
        # the document records no provenance, so the replay reads every hydrated
        # title as a caller's. The model returns no title in this fixture, so the
        # two sides cannot diverge on it.
        self.assertEqual(json.loads(replay.stdout), json.loads(original.stdout))

    def test_the_replay_reads_nothing(self) -> None:
        # The saving, and the contract: a captured read is not re-taken. The
        # replay carries no ``-r``, and the document deliberately records no
        # ``read: true`` marker -- it describes the input, never the run.
        folder, tags = self._folder()
        out_path = self._generate(folder, tags, "generated.json")

        replay = self.run_cli([out_path], _ExifToolStub(tags))

        self.assertEqual(
            replay.invocations,
            [],
            "replaying a generated manifest launched ExifTool; the read it "
            "carries is exactly what makes the replay unnecessary",
        )
        self.assertNotEqual(replay.calls, [])

    def test_the_document_is_the_same_twice(self) -> None:
        # Byte-identical, not merely equivalent: this is a file a user diffs
        # and hand edits, and the hydrated key order is fixed by the read set.
        folder, tags = self._folder()

        first = self._generate(folder, tags, "first.json")
        second = self._generate(folder, tags, "second.json")

        with open(first, "r", encoding="utf-8") as handle:
            first_text = handle.read()
        with open(second, "r", encoding="utf-8") as handle:
            second_text = handle.read()
        self.assertEqual(first_text, second_text)

    def test_the_document_matches_the_checked_in_golden(self) -> None:
        """The one shape claim the two tests above cannot make between them.

        ``test_the_document_carries_what_was_read`` compares parsed dicts, so it
        is blind to order; ``test_the_document_is_the_same_twice`` compares two
        outputs of the same code, so any reordering moves both sides together.
        Yet ``hydrate.py`` states the order as a property -- the hydrated keys
        follow ``DEFAULT_EXIFTOOL_FIELDS``, which is what makes repeat generation
        byte-identical -- and that property is what a user diffing two runs of
        this file depends on. Sorting the write-back loop by manifest key, a
        one-line tidy-up, leaves the whole suite green without this.

        A frozen file rather than an inline literal, matching
        ``test_folder_routing.py``'s golden for the un-hydrated document: it
        makes the indent, the trailing newline and the ``metadata`` block's key
        order all one comparison, and it is the only version of them that
        survives a change to the code that produced it. The file is generated by
        this very code path, never hand-written, so regenerating it is how a
        deliberate change is recorded.
        """
        folder, tags = self._folder()

        out_path = self._generate(folder, tags, "generated.json")

        with open(out_path, "r", encoding="utf-8") as handle:
            written = _tokenize(handle.read(), folder)
        with open(_GOLDEN_READ_MANIFEST, "r", encoding="utf-8") as handle:
            golden = handle.read()
        self.assertEqual(
            written,
            golden,
            "the document -r --generate-manifest writes no longer matches "
            f"{_GOLDEN_READ_MANIFEST}. If the change is intended, regenerate the "
            "golden from this same code path rather than editing it by hand",
        )


class TestTheChangesetDiffIsNeutralExceptWhereTheReadLands(_ReadFlagTestCase):
    """What ``-r`` changes about the writes a run proposes, and what it must not.

    C3's central claim is that reading a file changes the *evidence* and not the
    *proposal*: the same writes come out the other end, except on the keys the
    read is about. That claim is made in changeset space -- ``core.py`` builds a
    before snapshot from the file's own metadata, an after snapshot from the
    patch, and hands both to ``diff_canonical_metadata`` -- and it was the one
    part of the phase nothing executed. ``build_canonical_patch`` is called all
    over these two modules; ``diff_canonical_metadata`` was called by neither, so
    the ``proposed_changes`` block rested on a differential that was run once by
    hand and no longer exists.

    ``photokin/tests/test_changeset.py`` unit-tests the diff function on
    hand-built dicts, which is a different claim: it pins the arithmetic, not
    what a real ``-r`` run feeds it. Everything here goes through ``cli.main``
    with ``--changeset true``, so the whole chain -- hydrate, group, merge,
    ``build_canonical_patch``, ``canonical_values_from_*``, the diff -- runs
    untouched, and the assertions are read back off the NDJSON a user would.

    Kept here rather than in ``test_manifest_grouping.py``, whose
    ``run_manifest_*`` helpers already take a ``changeset_writer``: those run the
    stream directly and take no hydrator, so they cannot express "the same run
    with and without ``-r``", which is half of what is being pinned.
    """

    #: The model's answer, held fixed across every row and both runs, so any
    #: difference in what is proposed comes from the file rather than from it.
    #: It spans all four canonical surfaces on purpose: a date guess that clears
    #: ``date_confidence_threshold`` (0.6, compared with ``>=``), a location
    #: guess, a transcribed title, a caption and analysis notes.
    REPLY: ClassVar[dict[str, Any]] = {
        "caption": "Two men beside a car",
        "keywords": ["family"],
        "title": "Wedding Day 1952",
        "analysis_notes": "print, deckled edge",
        "date_guess": {
            "iso": "1952",
            "import_date": "1952-01-01",
            "confidence": 0.7,
            "pattern": "Y~",
        },
        "location_guess": {"country": "United States", "state": "Ohio", "confidence": 0.9},
    }

    #: What a previous photokin run left in ``XMP:Description``: the file's own
    #: caption with this run's matching transcription appended beneath it.
    #: ``REPLY["caption"]`` is the same text on every call, so re-reading it
    #: reproduces it exactly -- the fresh line matches the one already there and
    #: is deduplicated rather than added again -- which is what makes the
    #: description propose nothing below. This file is a lone scan with no
    #: back, so its own caption earns no section label -- there is nothing in
    #: the group to tell it apart from.
    JOINED_CAPTION = "Grandma on the porch\nTwo men beside a car"

    #: The model's whole answer as canonical writes, which is what every row
    #: proposes before the file gets a say. Spelled once so each row below reads
    #: as "the model's answer, minus what the file already settles".
    _MODEL_PROPOSES: ClassVar[dict[str, Any]] = {
        "EXIF:DateTimeOriginal": "1952-01-01",
        "EXIF:UserComment": "print, deckled edge",
        "IPTC:Country-PrimaryLocationName": "United States",
        "IPTC:Province-State": "Ohio",
        "XMP-dc:Description": "Two men beside a car",
        "XMP-dc:Title": "Wedding Day 1952",
    }

    #: The canonical tags a read can move, and the ExifTool tag each comes from:
    #:
    #:   EXIF:DateTimeOriginal <- EXIF:DateTimeOriginal (feeds the gap rule)
    #:   XMP-dc:Title          <- XMP:Title
    #:   XMP-dc:Description    <- XMP:Description       (the caption join)
    #:   XMP-dc:Subject        <- XMP:Subject           (as keywords_add/remove)
    #:
    #: Named rather than derived by diffing the two runs, because a loose diff
    #: would absorb a regression instead of reporting it. ``EXIF:UserComment`` is
    #: deliberately *not* here even though ``-r`` reads it: the before snapshot
    #: is built from ``merge_original_sources`` output, which does not forward
    #: ``userComment``, so the value never reaches the diff. That is a documented
    #: pre-existing gap (see the C3 open risks in docs/unified-input-pipeline.md)
    #: and it makes ``EXIF:UserComment`` one of the keys neutrality is measured
    #: on below -- if the read ever starts moving it, this class says so.
    READ_REACHES: ClassVar[frozenset[str]] = frozenset(
        {
            "EXIF:DateTimeOriginal",
            "XMP-dc:Title",
            "XMP-dc:Description",
            "XMP-dc:Subject",
        }
    )

    class _Shape(NamedTuple):
        """One file, what it holds, and the whole diff its record must carry."""

        label: str
        name: str
        tags: dict[str, Any]
        proposed: dict[str, Any]

    #: One file per C3-relevant shape, each its own group so nothing a sibling
    #: holds can reach it. Every expected diff is written as the model's whole
    #: answer minus the keys that file settles for itself, so a row states the
    #: read's effect rather than restating six literals; and each is compared
    #: whole, because a per-key assertion cannot see a key that appeared.
    SHAPES: ClassVar[tuple["TestTheChangesetDiffIsNeutralExceptWhereTheReadLands._Shape", ...]] = (
        _Shape(
            "the gap rule rewrites the date",
            "box3_020.jpg",
            {"EXIF:DateTimeOriginal": "2019:04:03 11:22:33"},
            # The 2019 scan date is 67 years from the model's 1952, well past
            # date_override_year_gap, so the model's year is what gets written
            # -- and the rewrite says so in a DATE: marker.
            {
                "set": dict(_MODEL_PROPOSES),
                "keywords_add": ["family", "DATE: Y~"],
                "keywords_remove": [],
            },
        ),
        _Shape(
            "the gap rule declines",
            "box3_021.jpg",
            {"EXIF:DateTimeOriginal": "1955:06:01 09:00:00"},
            # Three years from 1952, inside the gap, so the file keeps its own
            # date -- and the date key leaves the diff entirely rather than
            # being proposed back at the value already in the file.
            {
                "set": {
                    key: value
                    for key, value in _MODEL_PROPOSES.items()
                    if key != "EXIF:DateTimeOriginal"
                },
                "keywords_add": ["family"],
                "keywords_remove": [],
            },
        ),
        _Shape(
            "scanner boilerplate against a transcription",
            "box3_022.jpg",
            {"XMP:Title": "Scanned Image"},
            # The whole model answer, title included: had the boilerplate won
            # the merge, before and after would both read "Scanned Image" and
            # the title would vanish from the diff instead of being written.
            {"set": dict(_MODEL_PROPOSES), "keywords_add": ["family"], "keywords_remove": []},
        ),
        _Shape(
            "the file already holds the answer",
            "box3_023.jpg",
            {
                "XMP:Title": "Wedding Day 1952",
                "XMP:Subject": ["family"],
                "XMP:Description": JOINED_CAPTION,
            },
            # Three keys settled by the file drop out; the two the file says
            # nothing about stay, which is what keeps this from being an
            # assertion about an empty record.
            {
                "set": {
                    key: value
                    for key, value in _MODEL_PROPOSES.items()
                    if key not in ("XMP-dc:Title", "XMP-dc:Description")
                },
                "keywords_add": [],
                "keywords_remove": [],
            },
        ),
        _Shape(
            "the file holds nothing",
            "box3_024.jpg",
            {},
            {"set": dict(_MODEL_PROPOSES), "keywords_add": ["family"], "keywords_remove": []},
        ),
    )

    def _run_changeset(self, *, read: bool) -> dict[str, dict]:
        """Run the whole table once and return each file's changeset record.

        Args:
            read: Whether to pass ``-r``.

        Returns:
            The emitted changeset records, keyed by basename.

        The changeset is routed through ``--output-file`` rather than left to
        default beside the input, so it is not written into the folder being
        scanned -- a second run would otherwise list it.
        """
        stem = "read" if read else "unread"
        folder = self.make_folder(*(shape.name for shape in self.SHAPES), into=stem)
        stub = _ExifToolStub(
            {
                os.path.join(folder, shape.name): shape.tags
                for shape in self.SHAPES
                if shape.tags
            }
        )
        out_path = os.path.join(self.work, f"{stem}.ndjson")
        argv = [folder, "--changeset", "true", "--output-file", out_path]
        if read:
            argv.append("-r")

        run = self.run_cli(argv, stub, reply=dict(self.REPLY))

        self.assertIsNone(run.code, run.stderr)
        changeset_path = os.path.join(self.work, f"{stem}_changeset.ndjson")
        with open(changeset_path, "r", encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
        by_name = {os.path.basename(rec["path"]): rec for rec in records}
        self.assertEqual(
            sorted(by_name), sorted(shape.name for shape in self.SHAPES), run.stderr
        )
        return by_name

    def test_every_shape_proposes_exactly_this(self) -> None:
        records = self._run_changeset(read=True)

        for shape in self.SHAPES:
            with self.subTest(shape=shape.label):
                self.assertEqual(
                    records[shape.name]["proposed_changes"],
                    shape.proposed,
                    "the writes -r makes a run propose are not the documented "
                    "ones. This is the block the apply step consumes, so a key "
                    "that appeared here is a write into the user's photograph "
                    "that nothing else in the suite would notice",
                )

    def test_the_before_snapshot_carries_what_the_file_held(self) -> None:
        # The diff's left-hand side, asserted separately: a before snapshot that
        # silently came back empty would make every row above propose the whole
        # model answer and still look like a pass on four of the five.
        records = self._run_changeset(read=True)

        self.assertEqual(
            records["box3_023.jpg"]["original_data"]["file_metadata"],
            {
                "XMP-dc:Title": "Wedding Day 1952",
                "XMP-dc:Subject": ["family"],
                "XMP-dc:Description": self.JOINED_CAPTION,
            },
        )
        self.assertEqual(records["box3_024.jpg"]["original_data"]["file_metadata"], {})

    def test_the_gap_rules_two_answers_differ_only_in_the_date_key(self) -> None:
        """The rewrite and the refusal, stated as a contrast rather than as literals.

        Two files whose only difference is the year their date carries. One is
        outside ``date_override_year_gap`` and one inside, and the entire
        observable consequence has to be the date key: if declining also dropped
        the caption or the title, the heuristic would be deciding more than the
        date.
        """
        records = self._run_changeset(read=True)

        rewritten = records["box3_020.jpg"]["proposed_changes"]
        declined = records["box3_021.jpg"]["proposed_changes"]
        self.assertEqual(rewritten["set"]["EXIF:DateTimeOriginal"], "1952-01-01")
        self.assertNotIn(
            "EXIF:DateTimeOriginal",
            declined["set"],
            "a date the heuristic declined to rewrite was still proposed as a "
            "write. before and after both hold the file's own date, so the "
            "correct diff names no date at all",
        )
        self.assertEqual(
            {k: v for k, v in rewritten["set"].items() if k != "EXIF:DateTimeOriginal"},
            {k: v for k, v in declined["set"].items() if k != "EXIF:DateTimeOriginal"},
        )
        # The rewrite is the only one of the two that marks itself.
        self.assertEqual(rewritten["keywords_add"], ["family", "DATE: Y~"])
        self.assertEqual(declined["keywords_add"], ["family"])

    def test_a_value_the_file_already_holds_is_not_proposed_back_to_it(self) -> None:
        # The property that makes a changeset worth reading: it lists writes,
        # not restatements. A run that proposed every value it computed -- even
        # the ones already in the file -- would rewrite the whole archive on
        # every pass and bury the real changes in the noise.
        proposed = self._run_changeset(read=True)["box3_023.jpg"]["proposed_changes"]

        for key in ("XMP-dc:Title", "XMP-dc:Description"):
            self.assertNotIn(key, proposed["set"], f"{key} was proposed at its current value")
        self.assertEqual(proposed["keywords_add"], [])
        self.assertEqual(
            proposed["keywords_remove"],
            [],
            "the keyword the file already carried was proposed for deletion",
        )

    def test_the_read_moves_only_the_keys_it_reads(self) -> None:
        """Diff neutrality itself: the same run without ``-r`` proposes the same writes.

        Reading a file is meant to change which writes are *needed*, never which
        writes the model's answer amounts to. So every canonical key outside
        :data:`READ_REACHES` has to carry the identical value in both runs, for
        every file -- including the location tags and the analysis notes, which
        no read touches and which are what keep this comparison from being made
        between two empty dicts.
        """
        read = self._run_changeset(read=True)
        unread = self._run_changeset(read=False)

        for shape in self.SHAPES:
            with self.subTest(shape=shape.label):
                residual = {
                    label: {
                        key: value
                        for key, value in side[shape.name]["proposed_changes"]["set"].items()
                        if key not in self.READ_REACHES
                    }
                    for label, side in (("read", read), ("unread", unread))
                }
                self.assertEqual(
                    residual["read"],
                    residual["unread"],
                    "-r changed a write it has no business changing. The read "
                    "supplies evidence about the keys it reads; every other "
                    "proposal is the model's answer and must survive the flag "
                    "untouched",
                )
                self.assertNotEqual(residual["read"], {}, "nothing was compared")

    def test_a_file_holding_nothing_gets_the_identical_diff(self) -> None:
        # Neutrality with no exclusions at all, which is the honest form of the
        # claim wherever it can be made: a read that finds nothing must leave a
        # run indistinguishable from one that never read.
        read = self._run_changeset(read=True)
        unread = self._run_changeset(read=False)

        self.assertEqual(
            read["box3_024.jpg"]["proposed_changes"],
            unread["box3_024.jpg"]["proposed_changes"],
        )

    def test_the_flag_really_did_move_the_keys_it_is_excused_on(self) -> None:
        """Non-vacuity: :data:`READ_REACHES` is an excuse list, so it must be earned.

        Every key named there is excluded from the comparison above, so a stale
        entry silently narrows the neutrality claim. Each one is asserted to
        actually differ somewhere in the table, which is what stops the
        exclusion list from growing into a way of passing.
        """
        read = self._run_changeset(read=True)
        unread = self._run_changeset(read=False)

        moved: set[str] = set()
        for shape in self.SHAPES:
            before = unread[shape.name]["proposed_changes"]
            after = read[shape.name]["proposed_changes"]
            moved |= {
                key
                for key in set(before["set"]) | set(after["set"])
                if before["set"].get(key) != after["set"].get(key)
            }
            if before["keywords_add"] != after["keywords_add"]:
                moved.add("XMP-dc:Subject")
            if before["keywords_remove"] != after["keywords_remove"]:
                moved.add("XMP-dc:Subject")

        self.assertEqual(
            moved,
            set(self.READ_REACHES),
            "the keys -r actually moves are not the keys it is excused for. A "
            "key here that the table never moves is an exclusion doing nothing "
            "but weakening the neutrality assertion",
        )


class TestGroupingSurvivesEveryItemCarryingMetadata(_ReadFlagTestCase):
    """One answer per folder, whatever order the files arrive in.

    Before ``-r``, folder items carried no metadata, so the group-wide snapshot
    was ``{}`` and permutation-independence was trivially true. With every file
    contributing a title, a caption, a date and keywords, first-non-empty needs
    a defined order to be a defined answer -- otherwise the same folder yields a
    different prompt, a different merge input and a different record depending
    on how the filesystem listed it.
    """

    def _folder(self) -> tuple[str, _ExifToolStub]:
        """Return a folder spanning pages, a variant pair and a back, all tagged."""
        names = (
            "album-page1.jpg",
            "album-page2.jpg",
            "box3_025-back.jpg",
            "box3_025.jpg",
            "box3_025b.jpg",
        )
        folder = self.make_folder(*names)
        tags = {
            os.path.join(folder, name): {
                "XMP:Title": f"title {index}",
                "XMP:Description": f"caption {index}",
                "EXIF:DateTimeOriginal": f"20{index:02d}:01:01 00:00:00",
                "EXIF:UserComment": f"note {index}",
                "XMP:Subject": [f"kw{index}", "shared"],
            }
            for index, name in enumerate(names)
        }
        return folder, _ExifToolStub(tags)

    def _answer(self, paths: Sequence[str], stub: _ExifToolStub) -> str:
        """Run *paths* in the given order and return a comparable digest.

        ``all_variant_files`` is excluded and only that: B1 keeps those lists
        input-ordered on purpose, since canonicalizing them would reorder the
        NDJSON of every manifest the plug-in already sends. Everything else --
        the model calls, the forwarded snapshot, every merged field -- has to be
        the same answer.
        """
        with _recording() as rec:
            out = core.process_manifest_stream(
                manifest={"items": [{"path": path} for path in paths]},
                cfg=utils.Config(),
                metadata_hydrator=_hydrator_for(stub),
            )
        results = {
            os.path.basename(path): {
                key: value
                for key, value in record.items()
                if key != "all_variant_files"
            }
            for path, record in out["results"].items()
        }
        return json.dumps(
            {"calls": rec.calls, "results": results, "errors": out["errors"]},
            sort_keys=True,
            default=str,
        )

    def test_one_answer_across_every_permutation(self) -> None:
        folder, stub = self._folder()
        paths = [os.path.join(folder, name) for name in sorted(os.listdir(folder))]

        answers = {
            self._answer(order, stub) for order in itertools.permutations(paths)
        }

        self.assertEqual(
            len(answers),
            1,
            "the folder's answer depends on the order its files were listed. "
            "With -r every item carries metadata, so first-non-empty over an "
            "arrival-ordered scan makes the prompt, the merge input and the "
            "record all depend on the filesystem",
        )

    def test_the_one_answer_is_not_an_empty_one(self) -> None:
        """Non-vacuity: an invariant of nothing would satisfy the test above."""
        folder, stub = self._folder()
        paths = [os.path.join(folder, name) for name in sorted(os.listdir(folder))]

        answer = json.loads(self._answer(paths, stub))

        forwarded = [call[2] for call in answer["calls"]]
        self.assertEqual(len(forwarded), 2)
        for snapshot in forwarded:
            self.assertTrue(snapshot["title"])
            self.assertTrue(snapshot["caption"])
            self.assertTrue(snapshot["dateTimeOriginal"])
            self.assertIn("shared", snapshot["keywords"])


class TestAReadThatCannotRunOrReturnsNothing(_ReadFlagTestCase):
    """The two halves of the documented failure split.

    A read that cannot happen at all is fatal before the first model call, like
    a requested write: the failure is otherwise silent and expensive, since the
    run proceeds to pay for every call with a strictly worse prompt and produces
    records nothing distinguishes from "there was nothing to read". Once that
    pre-flight passes, a mid-run failure warns and the batch carries on -- the
    analysis is the expensive part and it is unaffected.
    """

    def _inputs(self) -> dict[str, list[str]]:
        """Return one argv per input mode, including the two no-model-call ones."""
        folder = self.make_folder("box3_025.jpg")
        front = os.path.join(folder, "box3_025.jpg")
        return {
            "folder": [folder],
            "single photo": [front],
            "manifest": [self.write_manifest([{"path": front}])],
            "generate-manifest": [
                folder,
                "--generate-manifest",
                os.path.join(self.work, "generated.json"),
            ],
            "dry-run": [folder, "--dry-run"],
        }

    def test_a_missing_binary_stops_the_run_before_the_model(self) -> None:
        for label, argv in self._inputs().items():
            with self.subTest(input=label):
                stub = _ExifToolStub(binary=False)

                run = self.run_cli([*argv, "-r"], stub)

                self.assertEqual(run.code, 2)
                self.assertEqual(
                    run.calls,
                    [],
                    f"{label} input with -r and no ExifTool paid for the batch "
                    "and only then discovered it could not read any of it",
                )
                self.assertIn("-r", run.stderr)

    def test_the_same_input_without_the_flag_is_unaffected(self) -> None:
        # The bound on that refusal: ExifTool stays optional. A run that never
        # asked to read does not care whether one exists.
        argv = self._inputs()["folder"]

        run = self.run_cli(argv, _ExifToolStub(binary=False))

        self.assertIsNone(run.code)
        self.assertEqual(len(run.calls), 1)

    def test_a_mid_run_failure_warns_and_keeps_the_batch(self) -> None:
        argv = self._inputs()["folder"]
        stub = _ExifToolStub(failure=OSError(5, "Input/output error"))

        run = self.run_cli([*argv, "-r"], stub)

        self.assertIsNone(run.code)
        self.assertEqual(len(json.loads(run.stdout)["results"]), 1)
        self.assertIn("Skipping metadata hydration", run.stderr)

    def test_an_unparseable_answer_costs_the_run_nothing_but_the_read(self) -> None:
        # ExifTool answering with something that is not JSON -- a truncated
        # write, a warning banner on stdout -- is the same class of failure and
        # must not be the one that escapes as an exception.
        argv = self._inputs()["folder"]
        stub = _ExifToolStub(raw_stdout="Warning: unsupported file type\n")

        run = self.run_cli([*argv, "-r"], stub)

        self.assertIsNone(run.code)
        self.assertEqual(len(json.loads(run.stdout)["results"]), 1)
        self.assertIn("Skipping metadata hydration", run.stderr)

    def test_a_file_holding_nothing_is_left_exactly_as_it_arrived(self) -> None:
        # No ``metadata`` key is attached for a file with nothing in it, which
        # is what keeps ``load_item_metadata`` answering None rather than {} and
        # keeps a generated manifest identical to an unread one for that file.
        folder = self.make_folder("box3_025.jpg", into="empty")
        items = [{"path": os.path.join(folder, "box3_025.jpg")}]

        _hydrator_for(_ExifToolStub())(items)

        self.assertEqual(items, [{"path": os.path.join(folder, "box3_025.jpg")}])

    def test_an_item_naming_a_sidecar_is_not_shadowed_by_the_read(self) -> None:
        # ``load_item_metadata`` prefers an inline dict over a ``metadata_path``,
        # so seeding one would silently replace the sidecar the caller named --
        # even when that sidecar is missing, which is an absent read rather than
        # a failed run.
        folder = self.make_folder("box3_025.jpg", into="sidecar")
        path = os.path.join(folder, "box3_025.jpg")
        items = [{"path": path, "metadata_path": os.path.join(folder, "absent.json")}]
        stub = _ExifToolStub({path: {"XMP:Title": "Scanned Image"}})

        _hydrator_for(stub)(items)

        self.assertNotIn("metadata", items[0])
        self.assertEqual(stub.invocations, [])

    def test_an_item_whose_metadata_is_not_a_dict_is_left_alone(self) -> None:
        # The items are the caller's objects and the read mutates them in place,
        # so anything already sitting at ``metadata`` that photokin does not
        # understand is not photokin's to replace. ``load_item_metadata`` answers
        # None for it either way, which is exactly why swapping it for a dict of
        # tags would be a silent change to someone else's data for no gain.
        folder = self.make_folder("box3_025.jpg", into="odd-metadata")
        path = os.path.join(folder, "box3_025.jpg")
        stub = _ExifToolStub({path: {"XMP:Title": "Scanned Image"}})
        items = [{"path": path, "metadata": "handled by our own loader"}]

        _hydrator_for(stub)(items)

        self.assertEqual(
            items, [{"path": path, "metadata": "handled by our own loader"}]
        )
        self.assertEqual(stub.invocations, [])

    def test_a_group_whose_metadata_cannot_be_read_still_produces_records(self) -> None:
        # End to end on the failure path: one photo's tags are unreachable and
        # the whole folder still comes back, because the read is context and the
        # analysis is the product.
        folder = self.make_folder("box3_025.jpg", "box3_026.jpg", into="partial")
        stub = _ExifToolStub(
            {os.path.join(folder, "box3_026.jpg"): {"XMP:Title": "Untitled Scan"}}
        )

        run = self.run_cli([folder, "-r"], stub)

        self.assertIsNone(run.code)
        self.assertEqual(
            sorted(os.path.basename(p) for p in json.loads(run.stdout)["results"]),
            ["box3_025.jpg", "box3_026.jpg"],
        )
        self.assertEqual(json.loads(run.stdout)["errors"], {})


class TestOverwritingADateCostsMoreThanFillingOne(unittest.TestCase):
    """The two date gates must stay in the right order relative to each other.

    Filling a date a file does not have is cheap: if the guess is poor the field
    was empty anyway, so nothing was lost. Overwriting a date the file already
    holds destroys something that may well have been right. The confidence
    needed for the second must therefore be at least the confidence needed for
    the first.

    They were inverted for most of this project's life -- 0.7 to fill a blank,
    0.6 to overwrite -- so a 0.65 inference was too weak to fill an empty date
    and strong enough to replace a correct one. That was not a typo but a
    consequence of the two living in different modules, set at different times,
    for different reasons, with nothing anywhere comparing them. This case is
    that comparison, so the next person to tune either one has to look at both.
    """

    def test_the_override_gate_is_not_below_the_write_gate(self) -> None:
        defaults = utils.Config()
        self.assertGreaterEqual(
            defaults.date_override_confidence_threshold,
            defaults.date_confidence_threshold,
            "replacing a date the file already holds now takes LESS confidence "
            "than filling an empty one, so a guess too weak to fill a blank can "
            "overwrite a date that was correct",
        )

    def test_a_tighter_year_gap_costs_more_confidence(self) -> None:
        """The precise variant buys less evidence, so it pays more for it.

        A wide year gap is itself evidence the file's date is wrong; a narrow
        one is not, so the precise rule has to make that up in confidence. If
        this inverts, the rule that fires on the flimsier signal becomes the
        easier one to trigger.
        """
        defaults = utils.Config()
        self.assertLess(
            defaults.date_override_precise_year_gap,
            defaults.date_override_year_gap,
        )
        self.assertGreaterEqual(
            defaults.date_override_precise_confidence_threshold,
            defaults.date_override_confidence_threshold,
            "the precise override fires on a narrower year gap but demands no "
            "more confidence, so it is strictly easier to trigger than the wide "
            "rule it is supposed to be the careful version of",
        )


if __name__ == "__main__":
    unittest.main()
