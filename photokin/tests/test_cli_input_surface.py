"""Phase C2: the input surface -- one token, one path through ``main``, no accidental write.

Four claims, none of which had coverage before this module:

* **detection is a rule, not a guess.** A directory is a folder even when it is
  called ``batch.json``, a ``.JSON`` in capitals is still a manifest, and the run
  says out loud which rule fired -- that INFO line is the only thing standing
  between a mis-detection and a confidently wrong run. The aliases assert the
  type where a positional infers it, so ``photokin X`` and ``photokin --folder X``
  must otherwise produce the same run;
* **every refusal is two lines the user can act on.** The matrix below asserts
  exit code 2 and the exact problem line for each one, plus the two properties
  the house style is really about: no argparse flag dump, and no traceback. A
  new case is one row;
* **nothing writes without an explicit opt-in.** Asserted against the bytes of
  real files rather than against the flags that were parsed, because the
  regression being guarded is precisely a flag that stopped meaning what it
  said. ``ExiftoolConfig.enabled`` used to default to true by a route that never
  reached the dataclass, so ``--changeset true`` alone rewrote the user's scans;
* **the plan is printed before the money is spent.** Asserted as an ordering
  against a marker the analysis stand-in logs on entry, not as mere presence.

Nothing here reaches a provider or an ExifTool binary. ``process_manifest_stream``
-- the only call in ``main`` that can build a provider client -- is replaced by
:class:`_StreamSpy`, and the ExifTool subprocess by :class:`_FakeExifTool`. The
fake binary is deliberately *not* inert: it appends to the file it is pointed at,
exactly as a real write would, so "the photo was not modified" is a claim about
the filesystem and can fail. Everything is created inside a
``TemporaryDirectory``; nothing here writes into the repository tree.

One divergence from the C2 brief is pinned here as shipped rather than as
written: ``-w --dry-run`` does not emit a changeset marked ``dry_run``. C2
resolved that contradiction in favour of stopping at the plan summary, so the
combination writes nothing at all -- see ``docs/unified-input-pipeline.md``
("``--dry-run`` stops after the summary").
"""

import io
import json
import logging
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from typing import Any, ClassVar
from unittest.mock import patch

from photokin import cli, cli_messages, utils
from photokin.exiftool.manifest import DEFAULT_EXIFTOOL_FIELDS

#: Blanked rather than removed: each of these is read through a falsy-default
#: lookup, so an empty value pins the documented default whatever the developer's
#: shell exports. ``EXIFTOOL_WRITE_ENABLED`` is the exception and is removed by
#: name wherever the flipped default is the thing under test -- ``_parse_bool_env``
#: reaches its ``default`` argument only when the variable is absent, so blanking
#: it pins "empty is falsy" rather than "the default is false".
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

#: The variable whose absence is the only way to observe the flipped default.
_WRITE_ENABLED = "EXIFTOOL_WRITE_ENABLED"

#: First line of the plan summary block.
_PLAN_HEADER = "Plan for this run:"

#: Placeholder image content. Real bytes rather than an empty file so a write
#: that happens anyway is visible as a change rather than as a file that was
#: empty before and after.
_IMAGE_BYTES = b"\xff\xd8PLACEHOLDER SCAN CONTENT\xff\xd9"

#: The extensions the folder-is-empty message names, in the order it names them.
_IMAGE_EXTENSIONS = ", ".join(sorted(utils.VALID_EXTS))


def _write_bytes(path: str, data: bytes = _IMAGE_BYTES) -> str:
    """Write *data* to *path* and return the path.

    Args:
        path: Destination file.
        data: Content to write.

    Returns:
        The path that was written.
    """
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def _write_manifest(folder: str, items: list[dict[str, Any]], name: str = "batch.json") -> str:
    """Write a manifest naming *items* into *folder* and return its path.

    Args:
        folder: Directory to write into.
        items: The ``items`` list, verbatim.
        name: The manifest's filename, so the capitalized-extension case can
            spell it differently.

    Returns:
        The manifest path.
    """
    manifest_path = os.path.join(folder, name)
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump({"items": items}, handle)
    return manifest_path


class _StreamSpy:
    """Stand in for ``process_manifest_stream``, the only call that can reach a provider.

    Records the keyword arguments of every call, so a test can ask what the run
    decided rather than re-deriving it, and drives both injected writers so the
    artifacts a real run would leave behind really exist by the time ``main``
    reaches the ExifTool apply step.

    Attributes:
        calls: One dict of keyword arguments per invocation.
    """

    #: What the stand-in proposes for every file. Two tags, so
    #: ``--exiftool-fields`` has something to select between and a test can tell
    #: selection from "everything was written".
    _PROPOSED: ClassVar[dict[str, str]] = {
        "EXIF:UserComment": "a barn in winter",
        "XMP:Description": "a barn in winter",
    }

    def __init__(self, *, marker: str | None = None) -> None:
        """Build the stand-in.

        Args:
            marker: Text logged on the ``photokin.core`` logger as the first
                thing the call does, standing in for the first model call. Its
                position in stderr is what makes "the plan comes first" an
                ordering assertion rather than a presence one.
        """
        self.calls: list[dict[str, Any]] = []
        self._marker = marker

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        """Record the call, drive the writers, and return a plausible aggregate.

        Args:
            kwargs: Whatever ``main`` passed; every argument is keyword-only at
                the call site.

        Returns:
            The ``{"results": ..., "errors": ...}`` aggregate the real stream
            returns, with one record per manifest item.
        """
        self.calls.append(kwargs)
        if self._marker is not None:
            logging.getLogger("photokin.core").info(self._marker)
        items = kwargs["manifest"]["items"]
        changeset_writer: Callable[[str], None] | None = kwargs.get("changeset_writer")
        ndjson_writer: Callable[[str], None] | None = kwargs.get("ndjson_writer")
        for item in items:
            if changeset_writer is not None:
                changeset_writer(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "path": item["path"],
                            "proposed_changes": {"set": dict(self._PROPOSED)},
                        }
                    )
                )
            if ndjson_writer is not None:
                ndjson_writer(json.dumps({"path": item["path"], "status": "ok"}))
        return {
            "results": {item["path"]: {"keywords": ["portrait"]} for item in items},
            "errors": {},
        }

    @property
    def called(self) -> bool:
        """Whether the analysis stream was entered at all."""
        return bool(self.calls)

    @property
    def manifest(self) -> dict[str, Any]:
        """The manifest document the first call was handed."""
        return self.calls[0]["manifest"]

    def item_paths(self) -> list[str]:
        """Return the ``path`` of every item in the first call's manifest."""
        return [item["path"] for item in self.manifest["items"]]

    def kwargs_without_hydrator(self) -> dict[str, Any]:
        """Return the first call's arguments minus the one that cannot compare.

        ``make_manifest_hydrator`` returns a fresh closure per run, so two runs
        that are otherwise identical differ on that object's identity alone.

        Returns:
            The keyword arguments, with ``metadata_hydrator`` removed.
        """
        return {k: v for k, v in self.calls[0].items() if k != "metadata_hydrator"}


class _FakeExifTool:
    """Stand in for the ExifTool subprocess, with the one effect that matters kept.

    A binary mocked away entirely would make "the photo was not modified" true by
    construction, which is the failure mode the write-default regression needs
    ruled out. This one appends a marker to the file ExifTool was pointed at --
    the last argument of the command it builds -- so an unwanted write shows up
    as changed bytes on disk.

    Attributes:
        commands: The argv of every invocation, in order.
    """

    _MARKER = b"EXIFTOOL WROTE HERE"

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess:
        """Record *cmd* and append the marker to the file it names.

        Args:
            cmd: The ExifTool argv; its last element is the target file.
            _kwargs: ``capture_output``/``text``/``check``, ignored.

        Returns:
            A successful :class:`subprocess.CompletedProcess`.
        """
        self.commands.append(list(cmd))
        with open(cmd[-1], "ab") as handle:
            handle.write(self._MARKER)
        return subprocess.CompletedProcess(list(cmd), 0, "", "")


class _CliTestCase(unittest.TestCase):
    """Base for tests that execute ``cli.main`` in-process.

    ``main`` installs a handler on a module-level logger, so process state is
    both an input and an output of every test here: the slate is cleared going in
    and the package logger restored coming out.
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
        the latter attached across tests holds an open file handle into a
        temp directory a later test may already have cleaned up.
        """
        for logger in (self.package_logger, self.root_logger):
            for handler in list(logger.handlers):
                if handler.get_name() in (cli._LOG_HANDLER_NAME, cli._LOG_FILE_HANDLER_NAME):
                    logger.removeHandler(handler)
                    handler.close()

    def scratch(self) -> str:
        """Return a fresh temporary directory, removed when the test ends."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return holder.name

    def make_folder(self, *names: str) -> str:
        """Create a scratch folder holding placeholder images.

        Args:
            names: Filenames to create inside it; defaults to one image.

        Returns:
            The folder path.
        """
        folder = self.scratch()
        for name in names or ("box3_025.jpg",):
            _write_bytes(os.path.join(folder, name))
        return folder

    def run_cli(
        self, argv: list[str], *, env: dict[str, str | None] | None = None
    ) -> tuple[int | None, str, str]:
        """Run ``cli.main`` with *argv*, returning its exit code, stdout and stderr.

        Args:
            argv: Arguments after the program name.
            env: Environment changes layered on top of ``_NEUTRAL_ENV``, where a
                value of None *removes* the variable rather than blanking it.
                Applied inside the ``patch.dict`` so both edits are restored.

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

    def usage_error(self, stderr: str) -> list[str]:
        """Return the two-line usage error a failed run ends with.

        Taken from the end rather than the start: a positional input logs what it
        was detected as before anything can go wrong with it, and that line is
        the point of the detection contract.

        Args:
            stderr: The whole stderr capture.

        Returns:
            The problem line and the ``Try:`` line.
        """
        return stderr.splitlines()[-2:]

    def plan_block(self, stderr: str) -> list[str]:
        """Return the plan summary as its own list of lines.

        The block is emitted as one log record, so the formatter prefixes only
        its header and every following line begins with the label indent.

        Args:
            stderr: The whole stderr capture.

        Returns:
            The header line followed by the label lines, or an empty list when
            no plan was printed.
        """
        lines = stderr.splitlines()
        start = next((i for i, line in enumerate(lines) if _PLAN_HEADER in line), None)
        if start is None:
            return []
        block = [lines[start]]
        for line in lines[start + 1:]:
            if not line.startswith("  "):
                break
            block.append(line)
        return block

    def assert_refused(self, argv: list[str], problem: str) -> str:
        """Run *argv*, assert it is refused with *problem*, and return stderr.

        Asserts the whole house style, not just the exit code: two lines, the
        second a remedy, no argparse flag dump, no traceback, nothing on stdout,
        and -- the load-bearing part -- neither the analysis stream nor the
        ExifTool apply step entered, since a regression that runs the batch and
        then fails also exits 2.

        Args:
            argv: Arguments after the program name.
            problem: The expected problem line, without the level prefix.

        Returns:
            The stderr capture, for any further assertion.
        """
        stream = _StreamSpy()
        exiftool = _FakeExifTool()
        with patch("photokin.cli.process_manifest_stream", stream), patch(
            "photokin.exiftool.apply.subprocess.run", exiftool
        ):
            code, stdout, stderr = self.run_cli(argv)

        self.assertFalse(stream.called, "the batch was analyzed before the run was refused")
        self.assertEqual(exiftool.commands, [])
        self.assertEqual(code, 2)
        reported, remedy = self.usage_error(stderr)
        self.assertEqual(reported, f"[ERROR] {problem}")
        self.assertTrue(remedy.startswith("Try: "), f"the remedy line is missing: {stderr!r}")
        self.assertNotIn("usage: ", stderr, "argparse boilerplate leaked into a usage error")
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(stdout, "")
        return stderr


class TestInputDetectionMatrix(_CliTestCase):
    """What a positional input is taken to be, and the line that says so.

    The rule is evaluated in one place and ordered so a directory wins over an
    extension. That ordering is invisible without the INFO line, which is why
    every case here asserts the inference as well as the outcome: a folder called
    ``batch.json`` analyzed as a folder is correct, and silently doing it is not.
    """

    def _run(self, argv: list[str]) -> tuple[int | None, str, _StreamSpy]:
        """Run *argv* with the analysis stream replaced.

        Args:
            argv: Arguments after the program name.

        Returns:
            ``(exit code, stderr, stream spy)``.
        """
        stream = _StreamSpy()
        with patch("photokin.cli.process_manifest_stream", stream):
            code, _stdout, stderr = self.run_cli(argv)
        return code, stderr, stream

    def test_a_directory_is_a_folder(self) -> None:
        folder = self.make_folder("box3_025.jpg", "box3_026.jpg")

        code, stderr, stream = self._run([folder])

        self.assertIsNone(code)
        self.assertIn(f"[INFO] Treating `{folder}` as a folder (it is a directory).", stderr)
        self.assertEqual(stream.manifest["source"]["type"], "folder")
        self.assertEqual(
            stream.item_paths(),
            [os.path.join(folder, "box3_025.jpg"), os.path.join(folder, "box3_026.jpg")],
        )

    def test_a_json_file_is_a_manifest(self) -> None:
        folder = self.make_folder()
        image = os.path.join(folder, "box3_025.jpg")
        manifest_path = _write_manifest(folder, [{"path": image, "is_back": False}])

        code, stderr, stream = self._run([manifest_path])

        self.assertIsNone(code)
        self.assertIn(
            f"[INFO] Treating `{manifest_path}` as a manifest (it is a .json file).", stderr
        )
        # The document reaches the stream verbatim: no ``source``, no
        # ``generated_by``, and the item's own keys intact. A manifest silently
        # rebuilt from its folder would lose exactly those.
        self.assertEqual(stream.manifest, {"items": [{"path": image, "is_back": False}]})

    def test_an_image_file_is_a_single_photo(self) -> None:
        folder = self.make_folder("box3_025.jpg", "box3_026.jpg")
        image = os.path.join(folder, "box3_025.jpg")

        code, stderr, stream = self._run([image])

        self.assertIsNone(code)
        self.assertIn(f"[INFO] Treating `{image}` as a single photo (it is a .jpg file).", stderr)
        self.assertEqual(stream.manifest["source"]["type"], "single")
        # One item, not the folder it sits in -- the neighbouring image is the
        # thing a folder/photo confusion would drag in.
        self.assertEqual(stream.item_paths(), [image])

    def test_a_directory_named_like_a_manifest_is_still_a_folder(self) -> None:
        # Directories are tested before extensions, so this is correct; the log
        # line is what keeps it from being a surprise.
        parent = self.scratch()
        folder = os.path.join(parent, "batch.json")
        os.mkdir(folder)
        _write_bytes(os.path.join(folder, "box3_025.jpg"))

        code, stderr, stream = self._run([folder])

        self.assertIsNone(code)
        self.assertIn(f"[INFO] Treating `{folder}` as a folder (it is a directory).", stderr)
        self.assertEqual(stream.manifest["source"]["type"], "folder")
        self.assertEqual(stream.item_paths(), [os.path.join(folder, "box3_025.jpg")])

    def test_a_capitalized_json_extension_is_still_a_manifest(self) -> None:
        folder = self.make_folder()
        image = os.path.join(folder, "box3_025.jpg")
        manifest_path = _write_manifest(folder, [{"path": image}], name="BATCH.JSON")

        code, stderr, stream = self._run([manifest_path])

        self.assertIsNone(code)
        # The reason names the rule, spelled the way the rule is written, rather
        # than echoing the extension the file happens to carry.
        self.assertIn(
            f"[INFO] Treating `{manifest_path}` as a manifest (it is a .json file).", stderr
        )
        self.assertEqual(stream.manifest, {"items": [{"path": image}]})

    def test_a_capitalized_image_extension_is_still_a_single_photo(self) -> None:
        folder = self.scratch()
        image = _write_bytes(os.path.join(folder, "BOX3_025.JPG"))

        code, stderr, stream = self._run([image])

        self.assertIsNone(code)
        self.assertIn(f"[INFO] Treating `{image}` as a single photo (it is a .jpg file).", stderr)
        self.assertEqual(stream.item_paths(), [image])

    def test_a_path_that_is_not_there_is_refused_before_anything_is_detected(self) -> None:
        missing = os.path.join(self.scratch(), "scanz")

        stderr = self.assert_refused([missing], f"`{missing}` not found.")

        self.assertEqual(
            self.usage_error(stderr)[1],
            "Try: check the spelling, or run from the folder that contains it",
        )
        # Nothing may be claimed about a path that does not exist.
        self.assertNotIn("Treating", stderr)

    def test_an_unrecognized_extension_names_the_three_invocations(self) -> None:
        notes = _write_bytes(os.path.join(self.scratch(), "notes.txt"), b"not a scan")

        stderr = self.assert_refused(
            [notes], f"`{notes}` isn't an image, folder, or .json manifest."
        )

        self.assertEqual(
            self.usage_error(stderr)[1],
            "Try: photokin ./scans/ (folder), photokin batch.json (manifest), "
            "or photokin scan_042.jpg (single photo)",
        )
        self.assertNotIn("Treating", stderr)


class TestAliasesAndPositionalsAgree(_CliTestCase):
    """``--folder``/``--manifest`` assert a type where a positional infers one.

    They are permanent aliases rather than a deprecation, so the contract is that
    the run either way is the same run. Exactly two lines may differ, and both
    differ for the same reason -- they quote the invocation rather than describe
    the run. The detection line is one: only a positional earns it, because an
    alias inferred nothing and so has nothing to report. The plan's advisory note
    is the other: it hands back the command that was typed with ``-rw`` added, so
    a caller who asserted the type keeps asserting it. Every row of the plan that
    describes the *run* is still compared line for line, as are the stream's
    arguments, which is where "the same run" is really pinned.
    """

    def _stream_kwargs(self, argv: list[str]) -> tuple[dict[str, Any], list[str], list[str]]:
        """Run *argv* and return the stream's arguments and the plan, split in two.

        Args:
            argv: Arguments after the program name.

        Returns:
            ``(keyword arguments minus the hydrator, the rows describing the run,
            the advisory note's lines)``.
        """
        stream = _StreamSpy()
        with patch("photokin.cli.process_manifest_stream", stream):
            code, _stdout, stderr = self.run_cli(argv)
        self.assertIsNone(code, stderr)
        block = self.plan_block(stderr)
        start = next((i for i, line in enumerate(block) if line.startswith("  note ")), len(block))
        return stream.kwargs_without_hydrator(), block[:start], block[start:]

    def test_a_folder_runs_identically_through_both_spellings(self) -> None:
        folder = self.make_folder("box3_025.jpg", "box3_025-back.jpg")

        positional, positional_plan, positional_note = self._stream_kwargs([folder])
        alias, alias_plan, alias_note = self._stream_kwargs(["--folder", folder])

        self.assertEqual(positional, alias)
        self.assertEqual(positional_plan, alias_plan)
        self.assertNotEqual(positional_plan, [])
        # The note is the one row that quotes the caller back, so it is the one
        # row that may differ -- by the alias itself, and by nothing else.
        self.assertEqual(positional_note[:-1], alias_note[:-1])
        self.assertEqual(
            alias_note[-1].strip(),
            positional_note[-1].strip().replace("photokin ", "photokin --folder ", 1),
        )

    def test_a_manifest_runs_identically_through_both_spellings(self) -> None:
        folder = self.make_folder()
        manifest_path = _write_manifest(folder, [{"path": os.path.join(folder, "box3_025.jpg")}])

        positional, positional_plan, positional_note = self._stream_kwargs([manifest_path])
        alias, alias_plan, alias_note = self._stream_kwargs(["--manifest", manifest_path])

        self.assertEqual(positional, alias)
        self.assertEqual(positional_plan, alias_plan)
        self.assertEqual(positional_note[:-1], alias_note[:-1])
        self.assertEqual(
            alias_note[-1].strip(),
            positional_note[-1].strip().replace("photokin ", "photokin --manifest ", 1),
        )
        # The hydrator is manifest-only, and it is the argument the comparison
        # above had to drop, so its presence is asserted separately for both.
        self.assertEqual(positional["strict_run_failures"], False)

    def test_only_the_positional_reports_what_it_inferred(self) -> None:
        folder = self.make_folder()
        stream = _StreamSpy()

        with patch("photokin.cli.process_manifest_stream", stream):
            _code, _stdout, alias_stderr = self.run_cli(["--folder", folder])
            _code, _stdout, positional_stderr = self.run_cli([folder])

        self.assertIn(f"Treating `{folder}` as a folder", positional_stderr)
        self.assertNotIn("Treating", alias_stderr)


class TestTheMessageMatrix(_CliTestCase):
    """Every refusal C2 owns, as one row each: exit code 2 and the first line.

    Wording is centralized in ``cli_messages`` precisely so it can be asserted
    without the pipeline, but what a user hits is the wording *as the CLI reaches
    it*, and reaching the wrong message is the failure this table catches -- a
    remedy that leads back to the error it came from, or an OS error reported as
    a spelling mistake. :meth:`assert_refused` carries the house-style half:
    two lines, a remedy, no argparse dump, no traceback, and nothing analyzed.
    """

    def _fixture(self) -> tuple[str, str, str]:
        """Build one valid input of each kind, so a row can vary one thing.

        Returns:
            ``(folder, image, manifest path)``.
        """
        folder = self.make_folder("box3_025.jpg")
        image = os.path.join(folder, "box3_025.jpg")
        return folder, image, _write_manifest(folder, [{"path": image}])

    def test_every_refusal_states_its_own_problem(self) -> None:
        folder, image, manifest_path = self._fixture()
        empty_folder = self.scratch()
        missing = os.path.join(folder, "scanz")
        notes = _write_bytes(os.path.join(folder, "notes.txt"), b"not a scan")
        not_json = os.path.join(folder, "broken.json")
        _write_bytes(not_json, b"{not json at all")
        not_manifest = os.path.join(folder, "settings.json")
        _write_bytes(not_manifest, b'{"provider": "openai"}')
        empty_items = _write_manifest(folder, [], name="empty.json")
        pathless = _write_manifest(folder, [{"is_back": True}], name="pathless.json")
        gone = os.path.join(folder, "box3_099.jpg")
        broken_item = _write_manifest(
            folder, [{"path": image}, {"path": gone}], name="broken2.json"
        )

        cases: tuple[tuple[str, list[str], str], ...] = (
            (
                "the input is not there",
                [missing],
                f"`{missing}` not found.",
            ),
            (
                "the folder holds nothing readable",
                [empty_folder],
                f"`{empty_folder}` holds no images; looked for {_IMAGE_EXTENSIONS}.",
            ),
            (
                "the extension is not one of the three",
                [notes],
                f"`{notes}` isn't an image, folder, or .json manifest.",
            ),
            (
                "the .json does not parse",
                [not_json],
                f"`{not_json}` is not valid JSON: Expecting property name enclosed in "
                "double quotes: line 1 column 2 (char 1).",
            ),
            (
                "the .json is not a manifest",
                [not_manifest],
                f"`{not_manifest}` is not a manifest; expected a top-level `items` list.",
            ),
            (
                "the manifest has no items",
                [empty_items],
                f"`{empty_items}` has an empty `items` list; there is nothing to analyze.",
            ),
            (
                "a manifest item has no path",
                [pathless],
                f"`{pathless}` items[0] has no `path` string.",
            ),
            (
                "a manifest item names a file that is gone",
                [broken_item],
                f"`{broken_item}` items[1] points at a file that does not exist: {gone}.",
            ),
            (
                "a positional and an alias both name an input",
                [image, "--folder", folder],
                f"`{image}` was given as the input and `--folder {folder}` names another; "
                "only one input is allowed.",
            ),
            (
                "both aliases name an input",
                ["--folder", folder, "--manifest", manifest_path],
                f"`--folder {folder}` and `--manifest {manifest_path}` both name an input; "
                "only one is allowed.",
            ),
            (
                "no input at all",
                ["--output-file", "results.json"],
                "no input was given.",
            ),
            (
                "--back against a folder",
                [folder, "--back", image],
                f"`--back {image}` only applies to a single photo, but `{folder}` was "
                "treated as a folder.",
            ),
            (
                "--back against a manifest",
                [manifest_path, "--back", image],
                f"`--back {image}` only applies to a single photo, but `{manifest_path}` "
                "was treated as a manifest.",
            ),
            (
                "--meta against a folder",
                [folder, "--meta", manifest_path],
                f"`--meta {manifest_path}` only applies to a single photo, but `{folder}` "
                "was treated as a folder.",
            ),
            (
                "--meta against a manifest",
                [manifest_path, "--meta", manifest_path],
                f"`--meta {manifest_path}` only applies to a single photo, but "
                f"`{manifest_path}` was treated as a manifest.",
            ),
            (
                "-w contradicted by --exiftool-write false",
                [folder, "-w", "--exiftool-write", "false"],
                "`-w` means --changeset true --exiftool-write true, but "
                "`--exiftool-write false` was also given.",
            ),
            (
                "-w contradicted by --changeset false",
                [folder, "-w", "--changeset", "false"],
                "`-w` means --changeset true --exiftool-write true, but "
                "`--changeset false` was also given.",
            ),
            (
                "-s contradicted by --sidecar-md off",
                [folder, "-s", "--sidecar-md", "off"],
                "`-s` means --sidecar-md auto, but `--sidecar-md off` was also given.",
            ),
            (
                "-s contradicted by --sidecar-md all",
                [folder, "-s", "--sidecar-md", "all"],
                "`-s` means --sidecar-md auto, but `--sidecar-md all` was also given.",
            ),
            (
                "a write with nothing to write from",
                [folder, "--exiftool-write", "true"],
                "`--exiftool-write true` needs a changeset to apply, but --changeset is false.",
            ),
            (
                "--output-file cannot hold results",
                [folder, "--output-file", os.path.join(folder, "results.txt")],
                f"`--output-file {os.path.join(folder, 'results.txt')}` must end with "
                ".ndjson or .json.",
            ),
        )

        for label, argv, problem in cases:
            with self.subTest(case=label):
                self.assert_refused(argv, problem)

    def test_the_meta_remedy_points_into_the_manifest_it_was_given(self) -> None:
        # The remedy is the half that differs by input kind: telling a manifest
        # user to "name the front image instead" would be advice to stop using
        # the manifest.
        folder, image, manifest_path = self._fixture()

        stderr = self.assert_refused(
            [manifest_path, "--meta", manifest_path],
            f"`--meta {manifest_path}` only applies to a single photo, but "
            f"`{manifest_path}` was treated as a manifest.",
        )
        folder_stderr = self.assert_refused(
            [folder, "--meta", manifest_path],
            f"`--meta {manifest_path}` only applies to a single photo, but `{folder}` "
            "was treated as a folder.",
        )

        self.assertEqual(
            self.usage_error(stderr)[1],
            "Try: carry it in the manifest item's `metadata` or `metadata_path` instead",
        )
        self.assertEqual(
            self.usage_error(folder_stderr)[1],
            f"Try: name the front image instead: photokin <front image> --meta {manifest_path}",
        )
        self.assertTrue(os.path.isfile(image))

    def test_a_positional_beside_an_alias_is_not_answered_by_argparse(self) -> None:
        # The mutually exclusive group argparse would use for this prints a
        # usage block and the flag list; removing it is what bought the message.
        folder, image, _manifest = self._fixture()

        stderr = self.assert_refused(
            [image, "--folder", folder],
            f"`{image}` was given as the input and `--folder {folder}` names another; "
            "only one input is allowed.",
        )

        self.assertEqual(
            self.usage_error(stderr)[1],
            f"Try: pass just one: photokin {image}, or photokin --folder {folder}",
        )
        self.assertNotIn("--openrouter-model", stderr)

    def test_exiftool_fields_with_nothing_to_write_is_a_note_not_an_error(self) -> None:
        # The flag is not wrong, it is inert, so the run continues. Refusing it
        # would break every caller that sets its tags once and toggles writing.
        folder = self.make_folder()
        stream = _StreamSpy()

        with patch("photokin.cli.process_manifest_stream", stream):
            code, _stdout, stderr = self.run_cli(
                [folder, "--exiftool-fields", "EXIF:UserComment"]
            )

        self.assertIsNone(code)
        self.assertTrue(stream.called, "an inert flag must not stop the run")
        self.assertIn(
            "[WARNING] `--exiftool-fields EXIF:UserComment` was given but nothing will be "
            "written; add -w (or --changeset true --exiftool-write true) to apply those tags.",
            stderr,
        )

    def test_an_unwritable_tag_spelling_is_refused_before_the_first_model_call(self) -> None:
        # The opposite of the inert case above: this flag is *wrong*, not merely
        # idle. Left to run, ExifTool answers "doesn't exist or isn't writable /
        # Nothing to do" once per file -- after the whole batch has been analysed
        # and paid for -- and the run still exited 0. So it is a usage error, and
        # the stream must not be entered.
        folder = self.make_folder()
        stream = _StreamSpy()

        with patch("photokin.cli.process_manifest_stream", stream):
            code, _stdout, stderr = self.run_cli(
                [folder, "-w", "--exiftool-fields", "XMP:dc:Description"]
            )

        self.assertEqual(code, 2)
        self.assertFalse(stream.called, "a run that cannot write must not call the model")
        self.assertEqual(
            self.usage_error(stderr),
            [
                "[ERROR] ExifTool cannot write `XMP:dc:Description`.",
                "Try: use `XMP-dc:Description` instead -- the same tag, spelled the way "
                "ExifTool wants it",
            ],
        )

    def test_the_refusal_names_the_tag_the_user_typed_not_a_canned_example(self) -> None:
        # Each rejected tag must be echoed back with its own correction, or the
        # message is a lecture rather than an answer.
        folder = self.make_folder()

        for bad, good in (
            ("XMP:dc:Subject", "XMP-dc:Subject"),
            ("XMP:dc:Title", "XMP-dc:Title"),
            ("XMP:photoshop:Headline", "XMP-photoshop:Headline"),
        ):
            with self.subTest(tag=bad):
                with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
                    code, _stdout, stderr = self.run_cli(
                        [folder, "-w", "--exiftool-fields", bad]
                    )
                self.assertEqual(code, 2)
                # The tag the user typed, echoed back verbatim, and its own
                # correction. Asserted on the tags rather than on the sentence
                # around them: the wording is free to get shorter, the two tags
                # are what make the message an answer instead of a lecture.
                self.assertIn(f"`{bad}`", stderr)
                self.assertIn(f"use `{good}` instead", stderr)

    def test_a_bad_spelling_is_caught_even_beside_valid_tags(self) -> None:
        # The flag takes a list, and one bad entry in it writes nothing for that
        # tag while the others succeed -- the partial failure hardest to notice.
        folder = self.make_folder()

        with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
            code, _stdout, stderr = self.run_cli(
                [folder, "-w", "--exiftool-fields", "EXIF:UserComment,XMP:dc:Description"]
            )

        self.assertEqual(code, 2)
        self.assertIn("`XMP:dc:Description`", stderr)

    def test_valid_multi_group_spellings_are_not_refused(self) -> None:
        """The bound: ``family0:family1:tag`` is legitimate ExifTool syntax.

        Measured against 13.10, ``EXIF:IFD0:Model`` writes and
        ``EXIF-IFD0:Model`` does not -- the exact inverse of the XMP case -- so
        a guard that keyed on "has a second colon" would refuse working input.
        Without this test the refusal could be widened to that rule and stay
        green.

        ``XMP:xmp:Rating`` is the same trap from the other direction: it is a
        two-colon *XMP* spelling, the exact shape the guard exists to catch,
        and it still writes -- because ``xmp`` collides with the family-0
        group name rather than naming a real family-1 group. A guard that
        keyed on "XMP, two colons" alone would refuse this one too.
        """
        folder = self.make_folder()

        for good in (
            "EXIF:IFD0:Model",
            "XMP-dc:Description",
            "XMP:Description",
            "XMP:xmp:Rating",
        ):
            with self.subTest(tag=good):
                with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
                    _code, _stdout, stderr = self.run_cli(
                        [folder, "-w", "--exiftool-fields", good, "--dry-run"]
                    )
                self.assertNotIn("ExifTool cannot write", stderr)

    def test_the_same_flag_is_silent_once_there_is_something_to_write(self) -> None:
        folder, image, _manifest = self._fixture()
        exiftool_path = _write_bytes(os.path.join(folder, "exiftool.exe"), b"")
        stream, fake = _StreamSpy(), _FakeExifTool()

        with patch("photokin.cli.process_manifest_stream", stream), patch(
            "photokin.exiftool.apply.subprocess.run", fake
        ):
            code, _stdout, stderr = self.run_cli(
                [
                    folder,
                    "-w",
                    "--exiftool-fields",
                    "XMP:Description",
                    "--exiftool-path",
                    exiftool_path,
                ]
            )

        self.assertIsNone(code)
        self.assertNotIn("nothing will be written", stderr)
        # The flag really selected the tag set, rather than being accepted and
        # dropped: only the tag asked for reaches the binary.
        self.assertEqual(len(fake.commands), 1)
        self.assertIn("-XMP:Description=a barn in winter", fake.commands[0])
        self.assertNotIn("-EXIF:UserComment=a barn in winter", fake.commands[0])
        self.assertEqual(fake.commands[0][0], exiftool_path)
        self.assertEqual(fake.commands[0][-1], image)


class _WriteFixtureTestCase(_CliTestCase):
    """Base for the classes that let a run reach a real ExifTool write.

    All of them need the same four things: one input of each kind over the same
    photo, a binary path that resolves, an ExifTool stand-in that really touches
    the file, and a way to ask whether the photos on disk changed.
    """

    def write_fixture(self) -> tuple[str, str, str, str]:
        """Build a folder holding a photo, a manifest naming it, and a fake binary.

        Returns:
            ``(folder, image, manifest path, exiftool path)``. The binary is a
            real empty file rather than a patched resolver, because the ExifTool
            pre-flight resolves the path itself and nothing here executes it.
        """
        folder = self.make_folder("box3_025.jpg")
        image = os.path.join(folder, "box3_025.jpg")
        manifest_path = _write_manifest(folder, [{"path": image}])
        exiftool_path = _write_bytes(os.path.join(folder, "exiftool.exe"), b"")
        return folder, image, manifest_path, exiftool_path

    def each_input(self, folder: str, image: str, manifest_path: str) -> tuple[list[str], ...]:
        """Return one argv prefix per input kind, in detection order."""
        return ([folder], [image], ["--manifest", manifest_path])

    def photo_bytes(self, folder: str) -> dict[str, bytes]:
        """Return the on-disk content of every image in *folder*.

        Args:
            folder: Directory to snapshot.

        Returns:
            A mapping of path to content, for the image extensions the pipeline
            recognizes.
        """
        snapshot: dict[str, bytes] = {}
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if os.path.isfile(path) and os.path.splitext(name)[1].lower() in utils.VALID_EXTS:
                with open(path, "rb") as handle:
                    snapshot[path] = handle.read()
        return snapshot

    def assert_photos_untouched(
        self, before: dict[str, bytes], folder: str, argv: list[str]
    ) -> None:
        """Assert every photo in *folder* is byte-identical to *before*.

        Args:
            before: The snapshot taken before the run.
            folder: The directory that was scanned.
            argv: The command that ran, quoted back in the failure message.
        """
        after = self.photo_bytes(folder)
        self.assertEqual(sorted(after), sorted(before))
        for path, content in before.items():
            self.assertEqual(
                after[path],
                content,
                "photokin modified the user's photos without being asked to: "
                f"{path} changed on disk after `photokin {' '.join(argv)}`. Writing to "
                "originals requires an explicit opt-in (-w, or --exiftool-write true "
                "beside --changeset true); no other combination may touch them.",
            )


class TestNoPhotoIsTouchedWithoutAnOptIn(_WriteFixtureTestCase):
    """The regression test for the flipped write default, asserted on the disk.

    ``ExiftoolConfig.enabled`` reads False on the dataclass and used to be
    unreachable from the CLI: ``from_env`` set it from ``EXIFTOOL_WRITE_ENABLED``
    with a ``True`` fallback and then discarded the None a missing flag produces.
    So ``--changeset true`` -- documented as *recording* the proposed writes --
    applied them, and anyone reading the dataclass would have concluded the
    opposite.

    Two things make these cases able to fail. ``EXIFTOOL_WRITE_ENABLED`` is
    removed rather than blanked, so the resolution really reaches the default
    literal; and the ExifTool stand-in really appends to the file, so the claim
    is about bytes rather than about which mock was called. The last case holds
    everything constant but the opt-in and asserts the photo *is* rewritten,
    which is what stops the rest from passing vacuously.
    """

    def test_a_plain_run_writes_to_no_file_in_any_input_mode(self) -> None:
        folder, image, manifest_path, exiftool_path = self.write_fixture()
        before = self.photo_bytes(folder)
        directory_before = sorted(os.listdir(folder))

        for argv in self.each_input(folder, image, manifest_path):
            with self.subTest(argv=argv):
                command = [*argv, "--exiftool-path", exiftool_path]
                stream, fake = _StreamSpy(), _FakeExifTool()
                with patch("photokin.cli.process_manifest_stream", stream), patch(
                    "photokin.exiftool.apply.subprocess.run", fake
                ):
                    code, _stdout, stderr = self.run_cli(command, env={_WRITE_ENABLED: None})

                self.assertIsNone(code, stderr)
                self.assertTrue(stream.called, "the fixture never got as far as a write")
                self.assert_photos_untouched(before, folder, command)
                self.assertEqual(fake.commands, [])
                # Nothing appeared either: a run asked for no artifact leaves a
                # changeset or a sidecar behind only by accident.
                self.assertEqual(sorted(os.listdir(folder)), directory_before)

    def test_recording_a_changeset_is_not_applying_it_in_any_input_mode(self) -> None:
        """``--changeset true`` is the one route that used to write on its own.

        This is the whole blast radius of the flipped default: a run that asks
        for the record of what *would* be written, and got the writes as well.
        """
        folder, image, manifest_path, exiftool_path = self.write_fixture()
        before = self.photo_bytes(folder)

        for argv in self.each_input(folder, image, manifest_path):
            with self.subTest(argv=argv):
                command = [*argv, "--changeset", "true", "--exiftool-path", exiftool_path]
                stream, fake = _StreamSpy(), _FakeExifTool()
                with patch("photokin.cli.process_manifest_stream", stream), patch(
                    "photokin.exiftool.apply.subprocess.run", fake
                ):
                    code, _stdout, stderr = self.run_cli(command, env={_WRITE_ENABLED: None})

                self.assertIsNone(code, stderr)
                self.assert_photos_untouched(before, folder, command)
                self.assertEqual(fake.commands, [])
                # The record was still produced -- the run did what was asked,
                # and only what was asked.
                changesets = [n for n in os.listdir(folder) if n.endswith("_changeset.ndjson")]
                self.assertEqual(len(changesets), 1, os.listdir(folder))
                with open(os.path.join(folder, changesets[0]), "r", encoding="utf-8") as handle:
                    self.assertEqual(json.loads(handle.readline())["path"], image)
                self.assertIn("write     : none (--exiftool-write defaults to false)", stderr)
                for stale in changesets:
                    os.remove(os.path.join(folder, stale))

    def test_the_same_fixture_does_rewrite_the_photo_once_it_is_asked_to(self) -> None:
        folder, image, manifest_path, exiftool_path = self.write_fixture()
        before = self.photo_bytes(folder)

        for argv in self.each_input(folder, image, manifest_path):
            with self.subTest(argv=argv):
                stream, fake = _StreamSpy(), _FakeExifTool()
                with patch("photokin.cli.process_manifest_stream", stream), patch(
                    "photokin.exiftool.apply.subprocess.run", fake
                ):
                    code, _stdout, stderr = self.run_cli(
                        [*argv, "-w", "--exiftool-path", exiftool_path],
                        env={_WRITE_ENABLED: None},
                    )

                self.assertIsNone(code, stderr)
                self.assertEqual([cmd[-1] for cmd in fake.commands], [image])
                with open(image, "rb") as handle:
                    self.assertNotEqual(handle.read(), before[image])
                _write_bytes(image, before[image])
                for stale in os.listdir(folder):
                    if stale.endswith("_changeset.ndjson"):
                        os.remove(os.path.join(folder, stale))


class TestTheWriteShorthand(_WriteFixtureTestCase):
    """``-w``: one definition, expanded once, overridable and refusable.

    The bundle lives in ``cli._WRITE_BUNDLE`` so the expansion and the
    contradiction check cannot disagree; what that buys is asserted here as
    behavior. The contradictions' *wording* is pinned in the message matrix
    above -- these cases pin the thing the wording is protecting, which is that
    the photos are still there afterwards.
    """

    def test_it_expands_to_both_halves_in_every_input_mode(self) -> None:
        folder, image, manifest_path, exiftool_path = self.write_fixture()

        for argv in self.each_input(folder, image, manifest_path):
            with self.subTest(argv=argv):
                original = self.photo_bytes(folder)[image]
                stream, fake = _StreamSpy(), _FakeExifTool()
                with patch("photokin.cli.process_manifest_stream", stream), patch(
                    "photokin.exiftool.apply.subprocess.run", fake
                ):
                    code, _stdout, stderr = self.run_cli(
                        [*argv, "-w", "--exiftool-path", exiftool_path],
                        env={_WRITE_ENABLED: None},
                    )

                self.assertIsNone(code, stderr)
                # Each half observed where only it shows: the changeset file
                # exists because --changeset true was set, and the photo changed
                # because --exiftool-write true was.
                changesets = [n for n in os.listdir(folder) if n.endswith("_changeset.ndjson")]
                self.assertEqual(len(changesets), 1, "-w did not expand to --changeset true")
                with open(image, "rb") as handle:
                    self.assertNotEqual(
                        handle.read(), original, "-w did not expand to --exiftool-write true"
                    )
                self.assertIn("write     : ExifTool EXIF:UserComment", stderr)
                _write_bytes(image, original)
                for stale in changesets:
                    os.remove(os.path.join(folder, stale))

    def test_an_explicit_flag_that_agrees_with_the_expansion_is_accepted(self) -> None:
        # Every member of the bundle expands to "true", so agreement and
        # contradiction are the only two outcomes an explicit flag can have.
        folder, image, _manifest, exiftool_path = self.write_fixture()
        original = self.photo_bytes(folder)[image]
        stream, fake = _StreamSpy(), _FakeExifTool()

        with patch("photokin.cli.process_manifest_stream", stream), patch(
            "photokin.exiftool.apply.subprocess.run", fake
        ):
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
                ],
                env={_WRITE_ENABLED: None},
            )

        self.assertIsNone(code, stderr)
        self.assertEqual([cmd[-1] for cmd in fake.commands], [image])
        with open(image, "rb") as handle:
            self.assertNotEqual(handle.read(), original)

    def test_a_contradicted_bundle_leaves_the_photos_alone(self) -> None:
        folder, image, manifest_path, exiftool_path = self.write_fixture()
        before = self.photo_bytes(folder)

        for argv in self.each_input(folder, image, manifest_path):
            for contradiction in (["--exiftool-write", "false"], ["--changeset", "false"]):
                with self.subTest(argv=argv, contradiction=contradiction):
                    command = [*argv, "-w", *contradiction, "--exiftool-path", exiftool_path]
                    self.assert_refused(
                        command,
                        "`-w` means --changeset true --exiftool-write true, but "
                        f"`{contradiction[0]} {contradiction[1]}` was also given.",
                    )
                    self.assert_photos_untouched(before, folder, command)
                    self.assertEqual(
                        [n for n in os.listdir(folder) if n.endswith("_changeset.ndjson")], []
                    )

    def test_a_dry_run_beside_it_writes_nothing_at_all(self) -> None:
        """``-w --dry-run`` stops at the plan, as C2 shipped it.

        The brief for this phase described the pair as emitting a changeset whose
        records carry ``dry_run: true``. C2 resolved that against the flag's own
        promise -- print the plan and stop, before the first model call -- so no
        analysis runs and there is nothing to record. Pinned as shipped, and as
        the safer of the two readings: the combination now cannot cost money.
        """
        folder, image, manifest_path, exiftool_path = self.write_fixture()
        before = self.photo_bytes(folder)

        for argv in self.each_input(folder, image, manifest_path):
            with self.subTest(argv=argv):
                command = [*argv, "-w", "--dry-run", "--exiftool-path", exiftool_path]
                stream, fake = _StreamSpy(), _FakeExifTool()
                with patch("photokin.cli.process_manifest_stream", stream), patch(
                    "photokin.exiftool.apply.subprocess.run", fake
                ):
                    code, stdout, stderr = self.run_cli(command, env={_WRITE_ENABLED: None})

                self.assertIsNone(code, stderr)
                self.assertFalse(stream.called, "--dry-run reached the model")
                self.assertEqual(fake.commands, [])
                self.assertEqual(stdout, "")
                self.assert_photos_untouched(before, folder, command)
                self.assertEqual(
                    [n for n in os.listdir(folder) if n.endswith("_changeset.ndjson")],
                    [],
                    "--dry-run created the changeset it promised not to write",
                )
                self.assertIn("  write     : none (--dry-run)", stderr)


class TestTheGatesAreLiftedForEveryInputType(_WriteFixtureTestCase):
    """``--output-file``, ``--changeset`` and the ``--exiftool-*`` flags, everywhere.

    Phase A made ``--output-file`` outside manifest mode an explicit error as a
    stopgap, and the other two were simply unreachable: folder and single-photo
    runs printed to stdout and never looked at them. C2 routed all three inputs
    through one path, so the flags are asserted here against folder and
    single-photo input specifically -- manifest input is the case that always
    worked and proves nothing.
    """

    def test_folder_input_writes_the_aggregate_json(self) -> None:
        folder = self.make_folder("box3_025.jpg", "box3_026.jpg")
        out_path = os.path.join(folder, "results.json")
        stream = _StreamSpy()

        with patch("photokin.cli.process_manifest_stream", stream):
            code, stdout, _stderr = self.run_cli([folder, "--output-file", out_path])

        self.assertIsNone(code)
        self.assertEqual(stdout, "", "a run with --output-file writes the file, not stdout")
        with open(out_path, "r", encoding="utf-8") as handle:
            self.assertEqual(sorted(json.load(handle)["results"]), sorted(stream.item_paths()))

    def test_single_photo_input_streams_ndjson(self) -> None:
        folder = self.make_folder("box3_025.jpg", "box3_025-back.jpg")
        image = os.path.join(folder, "box3_025.jpg")
        out_path = os.path.join(folder, "results.ndjson")

        with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
            code, stdout, _stderr = self.run_cli(
                [image, "--back", os.path.join(folder, "box3_025-back.jpg"),
                 "--output-file", out_path]
            )

        self.assertIsNone(code)
        self.assertEqual(stdout, "")
        with open(out_path, "r", encoding="utf-8") as handle:
            written = [json.loads(line) for line in handle if line.strip()]
        # Per-file records only: the file also carries the run envelope
        # (run: start/plan/complete), which has no "path" of its own.
        per_file = [record for record in written if "path" in record]
        self.assertEqual(
            [record["path"] for record in per_file],
            [image, os.path.join(folder, "box3_025-back.jpg")],
        )

    def test_the_changeset_path_follows_the_output_file_or_the_input(self) -> None:
        """Q1, generalized: ``dirname(--output-file or input)`` plus the stem.

        The no-output-file spellings are the ones that changed: both a ``.json``
        output and no output at all used to yield a bare ``changeset.ndjson``,
        which collides across runs sharing a directory.
        """
        folder, image, manifest_path, _exiftool = self.write_fixture()
        elsewhere = self.scratch()
        cases: tuple[tuple[str, list[str], str], ...] = (
            (
                "a folder is named by the folder",
                [folder],
                os.path.join(folder, f"{os.path.basename(folder)}_changeset.ndjson"),
            ),
            (
                "a photo is named by the photo",
                [image],
                os.path.join(folder, "box3_025_changeset.ndjson"),
            ),
            (
                "a manifest is named by the manifest",
                ["--manifest", manifest_path],
                os.path.join(folder, "batch_changeset.ndjson"),
            ),
            (
                "an output file wins, and its _results suffix is dropped",
                [folder, "--output-file", os.path.join(elsewhere, "box3_results.ndjson")],
                os.path.join(elsewhere, "box3_changeset.ndjson"),
            ),
            (
                "an aggregate .json output no longer yields a bare changeset",
                [folder, "--output-file", os.path.join(elsewhere, "run7.json")],
                os.path.join(elsewhere, "run7_changeset.ndjson"),
            ),
        )

        for label, argv, expected in cases:
            with self.subTest(case=label):
                stream = _StreamSpy()
                with patch("photokin.cli.process_manifest_stream", stream):
                    code, _stdout, stderr = self.run_cli(
                        [*argv, "--changeset", "true"], env={_WRITE_ENABLED: None}
                    )

                self.assertIsNone(code, stderr)
                self.assertTrue(os.path.isfile(expected), f"no changeset at {expected}")
                self.assertIn(f"  changeset : {expected}", stderr)
                os.remove(expected)

    def test_the_exiftool_flags_reach_the_binary_for_folder_and_photo_input(self) -> None:
        folder, image, _manifest, exiftool_path = self.write_fixture()

        for argv in ([folder], [image]):
            with self.subTest(argv=argv):
                original = self.photo_bytes(folder)[image]
                stream, fake = _StreamSpy(), _FakeExifTool()
                with patch("photokin.cli.process_manifest_stream", stream), patch(
                    "photokin.exiftool.apply.subprocess.run", fake
                ):
                    code, _stdout, stderr = self.run_cli(
                        [
                            *argv,
                            "--changeset",
                            "true",
                            "--exiftool-write",
                            "true",
                            "--exiftool-fields",
                            "EXIF:UserComment",
                            "--exiftool-path",
                            exiftool_path,
                        ],
                        env={_WRITE_ENABLED: None},
                    )

                self.assertIsNone(code, stderr)
                self.assertEqual(len(fake.commands), 1)
                self.assertEqual(fake.commands[0][0], exiftool_path)
                self.assertIn("-EXIF:UserComment=a barn in winter", fake.commands[0])
                self.assertEqual(fake.commands[0][-1], image)
                self.assertIn("[ExifTool] Apply result: files_seen=1 files_written=1", stderr)
                _write_bytes(image, original)
                for stale in os.listdir(folder):
                    if stale.endswith("_changeset.ndjson"):
                        os.remove(os.path.join(folder, stale))


class TestThePlanPrecedesTheFirstModelCall(_CliTestCase):
    """The cheapest guard against "wrong folder" and "I did not mean to write".

    Presence is not the claim -- a summary printed after the batch would still be
    in stderr. The analysis stand-in logs a marker as the first thing it does, so
    the assertion is that the plan appears earlier in the stream than the marker
    does.
    """

    _MARKER = "MODEL CALL WOULD HAPPEN HERE"

    def test_the_summary_is_printed_before_the_stream_is_entered(self) -> None:
        folder = self.make_folder("box3_025.jpg", "box3_025-back.jpg", "box3_040.jpg")
        stream = _StreamSpy(marker=self._MARKER)

        with patch("photokin.cli.process_manifest_stream", stream):
            code, _stdout, stderr = self.run_cli(
                [folder, "--provider", "anthropic", "--claude-model", "haiku"]
            )

        self.assertIsNone(code)
        self.assertTrue(stream.called)
        self.assertLess(
            stderr.index(_PLAN_HEADER),
            stderr.index(self._MARKER),
            "the plan summary must be printed before anything can cost money",
        )
        self.assertEqual(
            self.plan_block(stderr),
            [
                f"[INFO] {_PLAN_HEADER}",
                f"  input     : {os.path.abspath(folder)} (folder, 3 file(s) in 2 group(s), "
                "group-by object)",
                "  read      : none (-r not given)",
                "  output    : stdout",
                "  changeset : none (--changeset false)",
                "  write     : none",
                "  sidecars  : none (--sidecar-md off)",
                "  provider  : Claude",
                f"  model     : {utils.resolve_claude_model('haiku')}",
                # This run asked for nothing, so the block ends by naming the run
                # that does something. The provider and model the caller typed are
                # carried into it: a suggestion that dropped them would describe a
                # different run from the eight rows above it.
                "  note      : this run only prints results - your photos are not read or",
                "              changed. For the normal archival run:",
                "                  "
                + cli_messages.normal_run_command(
                    [folder, "--provider", "anthropic", "--claude-model", "haiku"]
                ),
            ],
        )

    def test_the_summary_names_what_the_input_was_detected_as(self) -> None:
        folder = self.make_folder()
        image = os.path.join(folder, "box3_025.jpg")
        manifest_path = _write_manifest(folder, [{"path": image}])
        expected = {
            "folder": (folder, "folder"),
            "photo": (image, "single photo"),
            "manifest": (manifest_path, "manifest"),
        }

        for kind, (token, label) in expected.items():
            with self.subTest(kind=kind):
                with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
                    _code, _stdout, stderr = self.run_cli([token, "--dry-run"])

                self.assertIn(
                    f"  input     : {os.path.abspath(token)} ({label}, 1 file(s) in "
                    "1 group(s), group-by object)",
                    stderr,
                )

    def test_a_dry_run_stops_after_the_summary(self) -> None:
        folder = self.make_folder()
        out_path = os.path.join(folder, "results.ndjson")
        _write_bytes(out_path, b"PREVIOUS RUN CONTENT\n")
        stream = _StreamSpy(marker=self._MARKER)

        with patch("photokin.cli.process_manifest_stream", stream):
            code, stdout, stderr = self.run_cli([folder, "--output-file", out_path, "--dry-run"])

        self.assertIsNone(code)
        self.assertFalse(stream.called)
        self.assertNotIn(self._MARKER, stderr)
        self.assertEqual(stdout, "")
        self.assertIn(
            "  --dry-run : stopping here; no model call, and nothing written.", stderr
        )
        # The previous run's artifact survives, which is what makes the flag
        # safe to point at a directory that already holds one: the streaming
        # path opens its destination with a truncating "w".
        with open(out_path, "rb") as handle:
            self.assertEqual(handle.read(), b"PREVIOUS RUN CONTENT\n")


class TestThePlanNamesWhatSidecarModeWillWrite(_CliTestCase):
    """The plan's ``sidecars`` row states what ``--sidecar-md`` will create.

    Before ``RunPlan`` gained a ``sidecars`` field, ``write`` spoke only for
    what ExifTool puts inside a file it already owns, so a ``--sidecar-md
    all`` run printed ``write: none`` immediately before creating one new
    ``.md`` file per photo -- and ``--dry-run``, documented as the check for
    what is about to be touched, could not preview it at all. The ``off`` row
    is already pinned by :class:`TestThePlanPrecedesTheFirstModelCall`; these
    cases cover ``all``, ``auto`` and the ``--dry-run`` preview.
    """

    def test_sidecar_md_all_is_named_in_the_plan(self) -> None:
        folder = self.make_folder("box3_025.jpg")

        with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
            code, _stdout, stderr = self.run_cli([folder, "--sidecar-md", "all"])

        self.assertIsNone(code, stderr)
        self.assertIn(
            "  sidecars  : <image stem>.md beside every analyzed image except "
            "crops (--sidecar-md all)",
            stderr,
        )

    def test_sidecar_md_auto_is_named_in_the_plan(self) -> None:
        folder = self.make_folder("box3_025.jpg")

        with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
            code, _stdout, stderr = self.run_cli([folder, "--sidecar-md", "auto"])

        self.assertIsNone(code, stderr)
        self.assertIn(
            "  sidecars  : <image stem>.md beside each image of a group the "
            "model calls",
            stderr,
        )
        self.assertIn("Document or Postcard (--sidecar-md auto)", stderr)

    def test_the_s_shorthand_expands_to_sidecar_md_auto(self) -> None:
        # -s is shorthand for --sidecar-md auto, the same relationship -w has
        # to its bundle: the plan clause is the auto clause, verbatim.
        folder = self.make_folder("box3_025.jpg")

        with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
            code, _stdout, stderr = self.run_cli([folder, "-s"])

        self.assertIsNone(code, stderr)
        self.assertIn("Document or Postcard (--sidecar-md auto)", stderr)

    def test_an_explicit_sidecar_md_auto_agrees_with_the_shorthand(self) -> None:
        # Agreement is not a contradiction: spelling both is redundant, legal,
        # and means what either alone means.
        folder = self.make_folder("box3_025.jpg")

        with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
            code, _stdout, stderr = self.run_cli([folder, "-s", "--sidecar-md", "auto"])

        self.assertIsNone(code, stderr)
        self.assertIn("Document or Postcard (--sidecar-md auto)", stderr)

    def test_dry_run_previews_the_sidecar_clause_before_anything_is_written(self) -> None:
        folder = self.make_folder("box3_025.jpg")
        stream = _StreamSpy()

        with patch("photokin.cli.process_manifest_stream", stream):
            code, _stdout, stderr = self.run_cli(
                [folder, "--sidecar-md", "all", "--dry-run"]
            )

        self.assertIsNone(code, stderr)
        self.assertFalse(stream.called, "--dry-run reached the model")
        self.assertIn(
            "  sidecars  : <image stem>.md beside every analyzed image except "
            "crops (--sidecar-md all)",
            stderr,
        )
        self.assertIn(
            "  --dry-run : stopping here; no model call, and nothing written.", stderr
        )


class TestThePlanAdvisesTheNormalRun(_CliTestCase):
    """The plan's last row tells a do-nothing run what the archival run is.

    ``photokin <input>`` with no other flag analyzes, prints JSON and leaves the
    files alone. That is the documented way to check the wiring and the first two
    cases here hold it to exactly that, because the note is an addition to that
    run and not a replacement for it.

    The rest is about the one line a reader will copy. A suggestion is only worth
    printing if it works, so these cases do not stop at asserting its text: they
    take the string back out of stderr, lex it the way a shell would, and run it,
    then assert the resulting plan reads and writes and names the same input. A
    hint that parses but describes a different run is the failure being guarded
    against, which is why the whole argv is carried into it rather than the input
    token alone.
    """

    #: Every flag that means "I have already said what I want". ``--changeset``
    #: and ``--exiftool-write`` appear at *both* values, not just ``true``:
    #: ``false`` is a decision too, and ``-w`` beside either is refused outright,
    #: which the last case in this class executes rather than asserts.
    _DECLARATIONS: ClassVar[dict[str, list[str]]] = {
        "-r": ["-r"],
        "-w": ["-w"],
        "--dry-run": ["--dry-run"],
        "--changeset true": ["--changeset", "true"],
        "--changeset false": ["--changeset", "false"],
        "--exiftool-write false": ["--exiftool-write", "false"],
        "--output-sidecars": ["--output-sidecars"],
    }

    def note_lines(self, stderr: str) -> list[str]:
        """Return the note's three lines, or an empty list when it was not printed.

        Args:
            stderr: The whole stderr capture.

        Returns:
            The ``note`` row and its two continuation lines.
        """
        block = self.plan_block(stderr)
        start = next((i for i, line in enumerate(block) if line.startswith("  note ")), None)
        return [] if start is None else block[start:]

    def suggested_command(self, stderr: str) -> str:
        """Return the command the note offers, stripped of its indent.

        Args:
            stderr: The whole stderr capture.

        Returns:
            The command line, starting with ``photokin``.
        """
        lines = self.note_lines(stderr)
        self.assertTrue(lines, f"no note was printed: {stderr!r}")
        self.assertEqual(len(lines), 3, f"the note is not three lines: {lines!r}")
        return lines[-1].strip()

    def paste(self, command: str, exiftool_path: str) -> tuple[int | None, str]:
        """Run *command* the way a shell would, and return its code and stderr.

        ``shlex`` in POSIX mode is the strictest of the three lexers the rendering
        has to satisfy -- it is the one that treats a bare backslash as an escape,
        so a Windows path that survives it round-trips through cmd.exe and
        PowerShell too.

        Args:
            command: The suggested command, program word included.
            exiftool_path: A binary for ``--exiftool-path``. Appended rather than
                expected on PATH: the suggestion is a real ``-rw`` run and the
                write half of it is refused up front when no ExifTool resolves,
                which is a property of the sandbox and not of the hint.

        Returns:
            The exit code and stderr of the pasted run.
        """
        program, *tokens = shlex.split(command, posix=True)
        self.assertEqual(program, "photokin")
        with patch("photokin.cli.process_manifest_stream", _StreamSpy()), patch(
            "photokin.cli._apply_exiftool_changeset", return_value=False
        ):
            code, _stdout, stderr = self.run_cli([*tokens, "--exiftool-path", exiftool_path])
        return code, stderr

    def contents(self, folder: str) -> dict[str, bytes]:
        """Return every file in *folder* by name, as bytes.

        Args:
            folder: The directory to read.

        Returns:
            A mapping of filename to content, for comparison across a run.
        """
        files = {}
        for name in os.listdir(folder):
            with open(os.path.join(folder, name), "rb") as handle:
                files[name] = handle.read()
        return files

    def test_a_bare_run_still_prints_json_and_changes_nothing(self) -> None:
        """The run the note is attached to is untouched by having a note."""
        folder = self.make_folder("box3_025.jpg", "box3_025-back.jpg")
        before = self.contents(folder)
        payload = {"results": {"box3_025.jpg": {"keywords": ["portrait"]}}, "errors": {}}

        with patch("photokin.cli.process_manifest_stream", lambda **_kw: payload):
            code, stdout, stderr = self.run_cli([folder])

        self.assertIsNone(code)
        self.assertEqual(json.loads(stdout), payload)
        # The advice is a diagnostic, so it goes where every diagnostic goes and
        # cannot corrupt the JSON the plugin parses off stdout.
        self.assertIn("For the normal archival run:", stderr)
        self.assertNotIn("For the normal archival run:", stdout)
        self.assertEqual(before, self.contents(folder))

    def test_every_input_mode_is_offered_the_command_it_would_paste(self) -> None:
        folder = self.make_folder("box3_025.jpg", "box3_025-back.jpg")
        image = os.path.join(folder, "box3_025.jpg")
        back = os.path.join(folder, "box3_025-back.jpg")
        manifest_path = _write_manifest(folder, [{"path": image}])
        # Expected commands are built with the real quoting rule rather than
        # hardcoded quotes: whether a token needs `"..."` around it depends on
        # what characters it holds (see cli_messages._quote_token), and a
        # temp-dir path on POSIX never does while the same path on Windows
        # always does (it contains a backslash). What this test asserts is the
        # *wiring* -- that each input mode's tokens reach the suggestion at
        # all, and in the right shape (e.g. --back survives as a flag, not as
        # a second input) -- which is orthogonal to the quoting rule itself;
        # quoting's own edge cases (spaces, `$`, a trailing backslash) are
        # covered directly in test_message_style.py.
        expected = {
            "folder": ([folder], cli_messages.normal_run_command([folder])),
            "folder alias": (
                ["--folder", folder],
                cli_messages.normal_run_command(["--folder", folder]),
            ),
            "single photo": ([image], cli_messages.normal_run_command([image])),
            # The one case a reconstruction from resolved values would get wrong:
            # the back is not the input, it is a flag on it.
            "single photo with a back": (
                [image, "--back", back],
                cli_messages.normal_run_command([image, "--back", back]),
            ),
            "manifest": ([manifest_path], cli_messages.normal_run_command([manifest_path])),
            "manifest alias": (
                ["--manifest", manifest_path],
                cli_messages.normal_run_command(["--manifest", manifest_path]),
            ),
        }

        for mode, (argv, command) in expected.items():
            with self.subTest(mode=mode):
                with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
                    _code, _stdout, stderr = self.run_cli(argv)

                self.assertEqual(
                    self.note_lines(stderr),
                    [
                        "  note      : this run only prints results - your photos are not "
                        "read or",
                        "              changed. For the normal archival run:",
                        f"                  {command}",
                    ],
                )

    def test_the_suggested_command_runs_and_does_what_it_says(self) -> None:
        """Paste it back: it must parse, read, write, and name the same input."""
        folder = self.make_folder("box3_025.jpg", "box3_025-back.jpg")
        image = os.path.join(folder, "box3_025.jpg")
        back = os.path.join(folder, "box3_025-back.jpg")
        manifest_path = _write_manifest(folder, [{"path": image}])
        exiftool_path = _write_bytes(os.path.join(self.scratch(), "exiftool.exe"), b"")
        invocations = {
            "folder": [folder],
            "folder alias": ["--folder", folder],
            "single photo": [image],
            "single photo with a back": [image, "--back", back],
            "manifest": [manifest_path],
            # Carried-over flags are the point: the pasted run must still be
            # Claude on haiku at pair granularity, not the defaults.
            "flags the caller chose": [
                folder, "--provider", "anthropic", "--claude-model", "haiku",
                "--group-by", "pair",
            ],
        }

        for mode, argv in invocations.items():
            with self.subTest(mode=mode):
                with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
                    _code, _stdout, stderr = self.run_cli(argv)
                planned = [line for line in self.plan_block(stderr) if line.startswith("  input")]

                code, pasted = self.paste(self.suggested_command(stderr), exiftool_path)

                self.assertIsNone(code, f"the suggested command was refused: {pasted!r}")
                self.assertIn(
                    "  read      : ExifTool " + ", ".join(DEFAULT_EXIFTOOL_FIELDS), pasted
                )
                self.assertIn("  write     : ExifTool EXIF:UserComment", pasted)
                # Same input, same grouping, same provider: everything the first
                # plan said, which is what makes it a next step and not a
                # different run.
                self.assertEqual(
                    planned,
                    [line for line in self.plan_block(pasted) if line.startswith("  input")],
                )
                self.assertEqual(self.note_lines(pasted), [], "the -rw run advised itself")

    def test_a_path_with_spaces_survives_the_round_trip(self) -> None:
        """The quoting is not decoration; an unquoted path would lex into two tokens."""
        root = self.scratch()
        folder = os.path.join(root, "Family Scans 1948")
        os.makedirs(folder)
        front = _write_bytes(os.path.join(folder, "card 009.jpg"))
        back = _write_bytes(os.path.join(folder, "card 009-back.jpg"))
        exiftool_path = _write_bytes(os.path.join(root, "exiftool.exe"), b"")

        for mode, argv in (
            ("folder", [folder]),
            ("single photo with a back", [front, "--back", back]),
        ):
            with self.subTest(mode=mode):
                with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
                    _code, _stdout, stderr = self.run_cli(argv)
                command = self.suggested_command(stderr)

                self.assertEqual(shlex.split(command, posix=True), ["photokin", *argv, "-rw"])
                code, pasted = self.paste(command, exiftool_path)
                self.assertIsNone(code, f"the suggested command was refused: {pasted!r}")
                self.assertIn("  write     : ExifTool EXIF:UserComment", pasted)

    def test_a_trailing_separator_survives_the_round_trip(self) -> None:
        """``C:\\Scans\\`` is how a path arrives from Explorer, and it is the shape
        the two quoting conventions disagree about: the closing quote pairs with
        that backslash on Windows and is escaped by it in POSIX. Doubling the run
        is read as one separator by both."""
        folder = self.make_folder() + os.sep
        exiftool_path = _write_bytes(os.path.join(self.scratch(), "exiftool.exe"), b"")

        with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
            _code, _stdout, stderr = self.run_cli([folder])
        command = self.suggested_command(stderr)

        self.assertEqual(shlex.split(command, posix=True), ["photokin", folder, "-rw"])
        code, pasted = self.paste(command, exiftool_path)
        self.assertIsNone(code, f"the suggested command was refused: {pasted!r}")
        self.assertIn("  write     : ExifTool EXIF:UserComment", pasted)

    def test_a_run_that_has_already_declared_itself_is_not_advised(self) -> None:
        folder = self.make_folder()
        exiftool_path = _write_bytes(os.path.join(self.scratch(), "exiftool.exe"), b"")
        cases = dict(
            self._DECLARATIONS,
            **{"--output-file": ["--output-file", os.path.join(self.scratch(), "results.json")]},
        )

        for label, flags in cases.items():
            with self.subTest(flag=label):
                with patch("photokin.cli.process_manifest_stream", _StreamSpy()), patch(
                    "photokin.cli._apply_exiftool_changeset", return_value=False
                ):
                    code, _stdout, stderr = self.run_cli(
                        [folder, *flags, "--exiftool-path", exiftool_path]
                    )

                self.assertIsNone(code)
                # The plan itself still prints; only its last row is withheld, so
                # a silent note cannot be mistaken for a silent run.
                self.assertIn(_PLAN_HEADER, stderr)
                self.assertEqual(self.note_lines(stderr), [])

    def test_generate_manifest_prints_no_plan_at_all_so_there_is_nothing_to_suppress(
        self,
    ) -> None:
        """Named here because the suppression list looks incomplete without it.

        ``--generate-manifest`` writes its file and returns above the plan, so the
        row a suppressor would remove is never built. This is what makes an entry
        for it in ``_suggest_the_normal_run`` dead code rather than defence.
        """
        folder = self.make_folder()
        out_path = os.path.join(self.scratch(), "scans-manifest.json")

        with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
            code, _stdout, stderr = self.run_cli([folder, "--generate-manifest", out_path])

        self.assertIsNone(code)
        self.assertEqual(self.plan_block(stderr), [])
        self.assertNotIn("For the normal archival run:", stderr)

    def test_the_note_does_not_depend_on_stdout_being_a_terminal(self) -> None:
        """A redirect moves the JSON, not the advice, so it must not move the advice.

        Pinned because it is a decision and not an accident: the note lives on
        stderr, and the tests below capture stdout into a ``StringIO`` that is
        never a terminal. Had the note been gated on ``isatty`` the entire suite
        would be exercising the silent branch while reading as coverage.
        """
        folder = self.make_folder()
        rendered = {}

        for label, isatty in (("piped", False), ("terminal", True)):
            # ``run_cli`` redirects stdout into a StringIO, so patching that
            # class is what a run actually asks when it asks about its terminal.
            with patch("photokin.cli.process_manifest_stream", _StreamSpy()), patch.object(
                sys.stdout.__class__, "isatty", lambda _self, answer=isatty: answer, create=True
            ):
                _code, _stdout, stderr = self.run_cli([folder])
            rendered[label] = self.note_lines(stderr)

        self.assertTrue(rendered["piped"])
        self.assertEqual(rendered["piped"], rendered["terminal"])

    def test_a_path_no_shell_can_carry_withholds_the_hint_entirely(self) -> None:
        """Silence beats a command that would run against the wrong folder.

        ``$`` expands inside the double quotes of a POSIX shell and of
        PowerShell, so ``"scans $HOME"`` would paste as a path that does not
        exist. There is no rendering that is right in cmd.exe and in both of the
        others, so no rendering is offered.
        """
        root = self.scratch()
        folder = os.path.join(root, "scans $HOME")
        os.makedirs(folder)
        _write_bytes(os.path.join(folder, "box3_025.jpg"))

        with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
            code, stdout, stderr = self.run_cli([folder])

        self.assertIsNone(code)
        self.assertIn(_PLAN_HEADER, stderr)
        self.assertEqual(self.note_lines(stderr), [])
        # Withholding the hint withholds nothing else: the run itself is normal.
        self.assertNotEqual(stdout, "")

    def test_the_two_declarations_that_would_have_produced_a_broken_command(self) -> None:
        """Why ``--changeset false`` suppresses even though it writes nothing.

        The suppression list treats ``--changeset``/``--exiftool-write`` as
        decisions at either value. This executes the alternative: appending
        ``-rw`` to the command those runs typed is not merely redundant, it is
        refused, so the hint would have been a command that exits 2.
        """
        folder = self.make_folder()
        exiftool_path = _write_bytes(os.path.join(self.scratch(), "exiftool.exe"), b"")

        for flag in ("--changeset", "--exiftool-write"):
            with self.subTest(flag=flag):
                with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
                    code, _stdout, stderr = self.run_cli(
                        [folder, flag, "false", "-rw", "--exiftool-path", exiftool_path]
                    )

                self.assertEqual(code, 2)
                self.assertIn(
                    "[ERROR] `-w` means --changeset true --exiftool-write true, but "
                    f"`{flag} false` was also given.",
                    stderr,
                )


class TestTheQuotingRuleWithholdsRatherThanCorrupts(unittest.TestCase):
    """Direct coverage of ``cli_messages.normal_run_command``'s quoting rule.

    The CLI-level tests above only exercise it through real, mostly
    special-character-free temp paths, so the rule's own edge cases -- the
    ones its docstring makes specific, measured claims about -- had no direct
    coverage of their own before this class.
    """

    def test_an_ordinary_path_is_not_quoted(self) -> None:
        self.assertEqual(
            cli_messages.normal_run_command(["/tmp/scans/box3_025.jpg"]),
            "photokin /tmp/scans/box3_025.jpg -rw",
        )

    def test_a_windows_path_survives_a_posix_shlex_round_trip(self) -> None:
        for raw in ("C:\\Scans\\Photos", "C:\\Scans\\"):
            with self.subTest(path=raw):
                command = cli_messages.normal_run_command([raw])
                self.assertIsNotNone(command)
                self.assertEqual(shlex.split(command)[1], raw)

    def test_a_unc_path_withholds_the_hint_rather_than_corrupting_it(self) -> None:
        """A leading double backslash is not the trailing case the doubling rule covers.

        Quoted and then parsed by a POSIX shell -- exactly what a WSL prompt or
        any POSIX ``paste()`` of a suggested command does -- ``\\\\`` inside
        double quotes is itself an escape for one literal backslash, so a UNC
        prefix would silently collapse from two backslashes to one and stop
        naming the same share. No suggestion is offered instead.
        """
        unc = "\\\\server\\share\\folder"
        self.assertIsNone(cli_messages.normal_run_command([unc]))


class TestTheCombinedShortFlagIsTheDocumentedNormalRun(_CliTestCase):
    """``-rw`` sets both halves, and keeps doing so.

    ``README.md`` names ``photokin <folder> -rw`` as the normal run, and nothing
    in the CLI implements it: argparse groups single-character options for free,
    so ``-rw`` is ``-r -w`` only for as long as BOTH stay ``store_true``. Give
    ``-r`` an argument one day -- ``-r TAGS`` for a custom read set is an easy
    thing to want -- and ``-rw`` silently becomes ``-r "w"``. The write half
    vanishes, no error is raised, and the documented command quietly stops
    writing anything. That failure is invisible without a case that asserts both
    halves of the grouped form, which is what this is.
    """

    def _plan_row(self, stderr: str, label: str) -> str:
        """Return the plan summary row named *label*, without its label."""
        for line in stderr.splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{label} ") or stripped.startswith(f"{label}:"):
                return stripped.split(":", 1)[1].strip()
        return ""

    def _rows_for(self, argv: list[str]) -> tuple[str, str]:
        """Run *argv* under --dry-run and return its ``read`` and ``changeset`` rows."""
        exiftool_path = _write_bytes(os.path.join(self.scratch(), "exiftool.exe"), b"")
        with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
            code, _stdout, stderr = self.run_cli(
                [*argv, "--exiftool-path", exiftool_path, "--dry-run"]
            )
        # None is this harness's clean return: main() fell off the end without
        # raising SystemExit.
        self.assertIsNone(code, stderr)
        return self._plan_row(stderr, "read"), self._plan_row(stderr, "changeset")

    def test_the_grouped_form_matches_the_two_flags_written_out(self) -> None:
        """``-rw`` is ``-r -w``, and ``-wr`` is too."""
        folder = self.make_folder()
        expected = self._rows_for([folder, "-r", "-w"])
        # Asserted against the spelled-out pair rather than against literals, so
        # the case survives any later rewording of the plan rows themselves.
        for grouped in ("-rw", "-wr"):
            with self.subTest(grouped=grouped):
                self.assertEqual(self._rows_for([folder, grouped]), expected)

    def test_each_half_of_the_grouped_form_really_took_effect(self) -> None:
        """Non-vacuity: the two rows differ from a run that asked for neither.

        Comparing ``-rw`` only against ``-r -w`` would still pass if both were
        parsed as nothing at all, so each row is also held against the run that
        genuinely asked for nothing.
        """
        folder = self.make_folder()
        read_row, changeset_row = self._rows_for([folder, "-rw"])
        idle_read, idle_changeset = self._rows_for([folder])

        self.assertIn("ExifTool", read_row, "the -r half of -rw did not take effect")
        self.assertNotEqual(read_row, idle_read)
        self.assertNotIn(
            "none", changeset_row, "the -w half of -rw did not take effect"
        )
        self.assertNotEqual(changeset_row, idle_changeset)



class TestNoDestinationLandsOnAFileTheRunNeeds(_CliTestCase):
    """Every destination is checked against what is already at it, in analysis
    mode too.

    ``--output-file`` was checked against nothing at all, and ``--log-file``
    never reached the destination pre-flight in the first place: it truncates
    on open, above every check below it, so it could empty an input photo
    under ``--dry-run`` and exit 0. Both are the same missing question, so
    both are asserted together.
    """

    _VICTIM = b"THE ONLY COPY OF THIS FILE"

    def _read(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    def test_log_file_onto_an_input_photo_is_refused(self) -> None:
        """The worst of the family: a photo, emptied by a --dry-run preview."""
        folder = self.make_folder("box3_017.jpg")
        photo = _write_bytes(os.path.join(folder, "box3_017.jpg"), self._VICTIM)

        code, _stdout, stderr = self.run_cli(
            [folder, "--dry-run", "--log-file", photo]
        )

        self.assertEqual(code, 2)
        self.assertIn("--log-file", stderr)
        self.assertEqual(self._read(photo), self._VICTIM)

    def test_log_file_onto_an_input_photo_spelled_differently_is_refused(self) -> None:
        """The same file, reached by a path no string comparison would match."""
        folder = self.make_folder("box3_017.jpg")
        photo = _write_bytes(os.path.join(folder, "box3_017.jpg"), self._VICTIM)
        os.mkdir(os.path.join(folder, "sub"))
        detour = os.path.join(folder, "sub", os.pardir, "box3_017.jpg")

        code, _stdout, stderr = self.run_cli(
            [folder, "--dry-run", "--log-file", detour]
        )

        self.assertEqual(code, 2)
        self.assertIn(photo, stderr)
        self.assertEqual(self._read(photo), self._VICTIM)

    def test_log_file_onto_a_transcript_the_run_would_write_is_refused(self) -> None:
        """--sidecar-md's own destination is a file this run depends on."""
        folder = self.make_folder("box3_017.jpg")
        transcript = _write_bytes(os.path.join(folder, "box3_017.md"), self._VICTIM)

        code, _stdout, stderr = self.run_cli(
            [folder, "--sidecar-md", "all", "--dry-run", "--log-file", transcript]
        )

        self.assertEqual(code, 2)
        self.assertIn("--sidecar-md", stderr)
        self.assertEqual(self._read(transcript), self._VICTIM)

    def test_log_file_and_output_file_at_the_same_path_are_refused(self) -> None:
        """Two destinations of one run, interleaving log lines into the results."""
        out_path = os.path.join(self.scratch(), "results.ndjson")
        folder = self.make_folder("box3_017.jpg")

        code, _stdout, stderr = self.run_cli(
            [folder, "--dry-run", "--output-file", out_path, "--log-file", out_path]
        )

        self.assertEqual(code, 2)
        self.assertIn("same file", stderr)

    def test_output_file_onto_the_input_manifest_is_refused(self) -> None:
        """A ``.json`` manifest in, the same ``.json`` out: the aggregate write
        replaces the very document the run was given."""
        folder = self.make_folder("box3_017.jpg")
        manifest = _write_manifest(folder, [{"path": os.path.join(folder, "box3_017.jpg")}])
        before = self._read(manifest)

        with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
            code, _stdout, stderr = self.run_cli(
                [manifest, "--output-file", manifest]
            )

        self.assertEqual(code, 2)
        self.assertIn("reads", stderr)
        self.assertEqual(self._read(manifest), before)

    def test_output_file_onto_a_sidecar_the_run_would_write_is_refused(self) -> None:
        """--output-sidecars derives ``<stem>.json`` beside the image, which is
        exactly the path --output-file is allowed to name."""
        folder = self.make_folder("box3_017.jpg")
        sidecar = _write_bytes(os.path.join(folder, "box3_017.json"), self._VICTIM)

        with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
            code, _stdout, stderr = self.run_cli(
                [folder, "--output-sidecars", "--output-file", sidecar]
            )

        self.assertEqual(code, 2)
        self.assertIn("--output-sidecars", stderr)
        self.assertEqual(self._read(sidecar), self._VICTIM)

    def test_generate_manifest_onto_the_meta_file_is_refused(self) -> None:
        """--generate-manifest overwrote the metadata document it was told to read."""
        folder = self.make_folder("box3_017.jpg")
        image = os.path.join(folder, "box3_017.jpg")
        meta = _write_bytes(os.path.join(folder, "meta.json"), b'{"title": "keep me"}')

        code, _stdout, stderr = self.run_cli(
            [image, "--generate-manifest", meta, "--meta", meta]
        )

        self.assertEqual(code, 2)
        self.assertIn("--meta", stderr)
        self.assertEqual(self._read(meta), b'{"title": "keep me"}')

    def test_output_file_onto_the_photo_context_file_is_refused(self) -> None:
        """Every read-only path the run was handed is protected, not just photos."""
        folder = self.make_folder("box3_017.jpg")
        context = _write_bytes(os.path.join(folder, "context.json"), self._VICTIM)

        with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
            code, _stdout, stderr = self.run_cli(
                [folder, "--photo-context-file", context, "--output-file", context]
            )

        self.assertEqual(code, 2)
        self.assertIn("--photo-context-file", stderr)
        self.assertEqual(self._read(context), self._VICTIM)

    def test_an_ordinary_destination_still_runs(self) -> None:
        """The control: a new results file beside the photos is still written."""
        folder = self.make_folder("box3_017.jpg")
        out_path = os.path.join(folder, "results.json")

        with patch("photokin.cli.process_manifest_stream", _StreamSpy()):
            code, _stdout, stderr = self.run_cli(
                [folder, "--sidecar-md", "all", "--output-file", out_path]
            )

        self.assertIsNone(code, stderr)
        self.assertTrue(os.path.isfile(out_path))


if __name__ == "__main__":
    unittest.main()
