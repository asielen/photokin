"""Phase A coverage for :func:`photokin.core.analyze_folder`.

Folder mode used to skip any group without a primary front -- every album page
set and every negative-only set -- without a word, and then report a completion
count that only described what it had processed, so a lossy run read as a clean
one. It also had no per-group try/except, so one bad photo threw away every
result already gathered, and it wrote each sidecar twice.

These tests pin the replacement behavior: every dropped file is named in the
log, the completion line carries the skipped and unanalyzed counts at a level
that survives an INFO threshold, one failing group costs only itself while a
wholly failed batch still raises, a run-fatal provider error aborts immediately,
an interrupt is never swallowed, and the sidecar is written once with the
enriched record.

The model call is mocked out at ``analyze_photo``, so no provider client is ever
built. Fixtures are empty placeholder files -- the grouper only reads filenames.
The exception is ``TestFolderModeStdoutPurity``, which stubs only the provider
boundary so the real analysis path -- and every diagnostic inside it -- runs.
"""

import io
import json
import logging
import os
import tempfile
import unittest
from collections.abc import Iterable
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from photokin import core, utils
from photokin.errors import ProviderApiError

_CORE_LOGGER = "photokin.core"
_PACKAGE_LOGGER = "photokin"
_COMPLETION_PREFIX = "Batch completed"


def _fake_analysis(front_path: str) -> dict:
    """Build the minimal ``analyze_photo`` return shape ``analyze_folder`` reads.

    Args:
        front_path: Path the record is keyed by, exactly as ``analyze_folder``
            passes it in.

    Returns:
        A ``{"result": {front_path: record}}`` payload that survives a JSON
        round trip, so it can be compared against a written sidecar.
    """
    return {"result": {front_path: {"caption": "A caption", "keywords": ["family"]}}}


def _messages(records: list[logging.LogRecord], level: int) -> list[str]:
    """Return the formatted messages logged at exactly *level*."""
    return [record.getMessage() for record in records if record.levelno == level]


def _group_warnings(records: list[logging.LogRecord]) -> list[str]:
    """Return the per-group WARNING messages, excluding the run summary line.

    Args:
        records: Log records captured for the whole ``analyze_folder`` call.

    Returns:
        One message per group that reported dropped files.
    """
    return [m for m in _messages(records, logging.WARNING) if not m.startswith(_COMPLETION_PREFIX)]


def _completion_record(records: list[logging.LogRecord]) -> logging.LogRecord:
    """Return the run's single "Batch completed" summary record.

    The level is deliberately not filtered on: a lossy run summarizes itself at
    WARNING and a clean one at INFO, so which of the two it chose is a behavior
    to assert rather than a detail to search past.

    Args:
        records: Log records captured for the whole ``analyze_folder`` call.

    Returns:
        The completion record.

    Raises:
        AssertionError: If the run logged anything other than one such line.
    """
    found = [record for record in records if record.getMessage().startswith(_COMPLETION_PREFIX)]
    if len(found) != 1:
        raise AssertionError(
            f"expected exactly one completion line, got {[r.getMessage() for r in found]}"
        )
    return found[0]


class _FolderModeTestCase(unittest.TestCase):
    """Base giving each test an isolated scratch folder outside the repo tree."""

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.folder: str = scratch.name

    def _make_images(self, *names: str) -> None:
        """Create empty placeholder images named *names* in the scratch folder."""
        for name in names:
            with open(os.path.join(self.folder, name), "w", encoding="utf-8") as handle:
                handle.write("")

    def _basenames(self, paths: Iterable[str]) -> list[str]:
        """Return the sorted basenames of *paths*, for order-independent asserts."""
        return sorted(os.path.basename(p) for p in paths)


class TestUnanalyzedGroupFiles(unittest.TestCase):
    """The accounting helper that decides which files go unreported.

    Exercised directly because the crop and variant slots it branches on need a
    six-file fixture to reach through ``analyze_folder``, and the ordering of
    its output is what the "N file(s) not analyzed" listings read back.
    """

    def setUp(self) -> None:
        self.entry: dict[str, object] = {
            "primary": {
                "front": "f.jpg",
                "back": "f-back.jpg",
                "front_crop": "f-crop.jpg",
                "back_crop": None,
            },
            "variants": [
                {
                    "version": "b",
                    "front": "fb.jpg",
                    "back": None,
                    "front_crop": None,
                    "back_crop": None,
                }
            ],
            "pages": {1: "p1.jpg"},
            "page_crops": {},
            "negative": "neg.jpg",
            "negative_crop": None,
        }

    def test_an_analyzed_front_leaves_only_the_pages_negative_and_crops(self) -> None:
        dropped = core._unanalyzed_group_files(self.entry, front_analyzed=True)

        self.assertEqual(dropped, ["p1.jpg", "neg.jpg", "f-crop.jpg"])

    def test_an_unanalyzed_front_drags_every_front_and_back_in_with_it(self) -> None:
        dropped = core._unanalyzed_group_files(self.entry, front_analyzed=False)

        self.assertEqual(
            dropped, ["p1.jpg", "neg.jpg", "f.jpg", "f-back.jpg", "f-crop.jpg", "fb.jpg"]
        )


class TestFolderModeSkippedGroups(_FolderModeTestCase):
    """A group folder mode cannot analyze has to say so, by name and by file.

    The fixture is the one from the audit: an album page set and a
    negative-only set that folder mode has no path for, next to an ordinary
    photo that it does. Three of four files go unread; the bug was that the run
    said nothing about any of them and reported a plausible-looking count.
    """

    def _run_mixed_folder(self) -> tuple[dict, list[logging.LogRecord], Mock]:
        """Analyze album pages, a negative-only set and one ordinary photo."""
        self._make_images(
            "album-page1.jpg",
            "album-page2.jpg",
            "neg-negative.jpg",
            "box3_025.jpg",
        )
        analyze = Mock(side_effect=lambda front, *args, **kwargs: _fake_analysis(front))

        with patch("photokin.core.analyze_photo", analyze):
            with self.assertLogs(_CORE_LOGGER, level=logging.INFO) as captured:
                result = core.analyze_folder(self.folder, utils.Config())

        return result, captured.records, analyze

    def test_only_the_analyzable_group_reaches_the_model(self) -> None:
        result, _records, analyze = self._run_mixed_folder()

        self.assertEqual(analyze.call_count, 1)
        self.assertEqual(os.path.basename(analyze.call_args.args[0]), "box3_025.jpg")
        self.assertEqual(self._basenames(result["results"]), ["box3_025.jpg"])
        self.assertEqual(result["errors"], {})

    def test_each_skipped_group_is_warned_by_name_with_its_files(self) -> None:
        _result, records, _analyze = self._run_mixed_folder()

        skips = [m for m in _group_warnings(records) if m.startswith("Skipping group")]
        self.assertEqual(len(skips), 2, f"expected one warning per skipped group, got {skips}")

        album = next(m for m in skips if "'album'" in m)
        self.assertIn("multipage set has no primary front", album)
        self.assertIn("album-page1.jpg", album)
        self.assertIn("album-page2.jpg", album)

        negative = next(m for m in skips if "'neg'" in m)
        self.assertIn("negative-only set", negative)
        self.assertIn("neg-negative.jpg", negative)

    def test_completion_line_reports_the_skipped_and_unanalyzed_counts(self) -> None:
        _result, records, _analyze = self._run_mixed_folder()

        completion = _completion_record(records)
        self.assertEqual(
            completion.levelno,
            logging.WARNING,
            "a run that dropped files must not summarize itself at INFO",
        )
        message = completion.getMessage()
        self.assertIn("1 primary set(s)", message)
        self.assertIn("2 group(s) skipped", message)
        self.assertIn("0 group(s) failed", message)
        self.assertIn("3 file(s) not analyzed", message)

    def test_an_analyzed_group_still_reports_the_files_it_leaves_behind(self) -> None:
        self._make_images(
            "album.jpg", "album-page1.jpg", "album-page2.jpg", "album-negative.jpg"
        )

        with patch(
            "photokin.core.analyze_photo",
            side_effect=lambda front, *args, **kwargs: _fake_analysis(front),
        ):
            with self.assertLogs(_CORE_LOGGER, level=logging.INFO) as captured:
                result = core.analyze_folder(self.folder, utils.Config())

        self.assertEqual(self._basenames(result["results"]), ["album.jpg"])
        warnings = _group_warnings(captured.records)
        self.assertEqual(len(warnings), 1)
        self.assertIn("'album'", warnings[0])
        self.assertIn("3 file(s) are not analyzed", warnings[0])
        for dropped in ("album-page1.jpg", "album-page2.jpg", "album-negative.jpg"):
            self.assertIn(dropped, warnings[0])
        self.assertIn("3 file(s) not analyzed", _completion_record(captured.records).getMessage())


class TestFolderModeErrorIsolation(_FolderModeTestCase):
    """One bad photo costs one group; a total loss and an interrupt cost the batch.

    Isolating every failure was the fix for the first problem and the cause of
    two more: a run where every group failed would have returned an
    empty-but-valid result that the CLI exits 0 on, and a run-wide provider
    error would have been re-reported once per group instead of aborting.
    """

    def _run_with_failure_on(self, failing: str) -> tuple[dict, list[logging.LogRecord]]:
        """Analyze three photos with *failing* raising ``RuntimeError``."""
        self._make_images("box3_024.jpg", "box3_025.jpg", "box3_026.jpg")

        def analyze(front: str, *args: object, **kwargs: object) -> dict:
            if os.path.basename(front) == failing:
                raise RuntimeError("model returned no parsable JSON")
            return _fake_analysis(front)

        with patch("photokin.core.analyze_photo", side_effect=analyze):
            with self.assertLogs(_CORE_LOGGER, level=logging.INFO) as captured:
                result = core.analyze_folder(self.folder, utils.Config())

        return result, captured.records

    def test_the_groups_around_a_failure_still_complete(self) -> None:
        result, _records = self._run_with_failure_on("box3_025.jpg")

        self.assertEqual(self._basenames(result["results"]), ["box3_024.jpg", "box3_026.jpg"])
        self.assertEqual(self._basenames(result["errors"]), ["box3_025.jpg"])
        failed_path = next(iter(result["errors"]))
        self.assertEqual(result["errors"][failed_path]["type"], "RuntimeError")
        self.assertEqual(
            result["errors"][failed_path]["message"], "model returned no parsable JSON"
        )

    def test_the_failure_is_logged_and_counted(self) -> None:
        _result, records = self._run_with_failure_on("box3_025.jpg")

        errors = _messages(records, logging.ERROR)
        self.assertEqual(len(errors), 1)
        self.assertIn("'box3_025'", errors[0])
        self.assertIn("box3_025.jpg", errors[0])
        self.assertIn("RuntimeError", errors[0])
        self.assertIn("model returned no parsable JSON", errors[0])

        completion = _completion_record(records).getMessage()
        self.assertIn("2 primary set(s)", completion)
        self.assertIn("1 group(s) failed", completion)

    def test_a_wholly_failed_batch_raises_instead_of_returning_an_empty_result(self) -> None:
        self._make_images("box3_024.jpg", "box3_025.jpg")

        with patch(
            "photokin.core.analyze_photo",
            side_effect=RuntimeError("model returned no parsable JSON"),
        ):
            with self.assertLogs(_CORE_LOGGER, level=logging.ERROR):
                with self.assertRaises(RuntimeError) as ctx:
                    core.analyze_folder(self.folder, utils.Config())

        # Re-raising the first failure is what keeps the CLI from exiting 0 on a
        # run that produced nothing at all.
        self.assertEqual(str(ctx.exception), "model returned no parsable JSON")

    def test_a_run_fatal_provider_error_aborts_on_the_first_group(self) -> None:
        self._make_images("box3_024.jpg", "box3_025.jpg", "box3_026.jpg")
        error = ProviderApiError("missing_api_key", "OPENAI_API_KEY is not set.")

        mocked = Mock(side_effect=error)
        with patch("photokin.core.analyze_photo", mocked):
            with self.assertRaises(ProviderApiError) as ctx:
                core.analyze_folder(self.folder, utils.Config())

        self.assertEqual(ctx.exception.error_type, "missing_api_key")
        self.assertEqual(mocked.call_count, 1, "a missing key must not be retried per group")

    def test_keyboard_interrupt_is_not_swallowed_by_the_isolation_wrapper(self) -> None:
        self._make_images("box3_024.jpg", "box3_025.jpg", "box3_026.jpg")

        def analyze(front: str, *args: object, **kwargs: object) -> dict:
            if os.path.basename(front) == "box3_025.jpg":
                raise KeyboardInterrupt
            return _fake_analysis(front)

        mocked = Mock(side_effect=analyze)
        with patch("photokin.core.analyze_photo", mocked):
            with self.assertRaises(KeyboardInterrupt):
                core.analyze_folder(self.folder, utils.Config())

        self.assertEqual(mocked.call_count, 2, "the third group must never be attempted")

    def test_system_exit_is_not_swallowed_by_the_isolation_wrapper(self) -> None:
        self._make_images("box3_024.jpg", "box3_025.jpg", "box3_026.jpg")

        def analyze(front: str, *args: object, **kwargs: object) -> dict:
            if os.path.basename(front) == "box3_025.jpg":
                raise SystemExit(3)
            return _fake_analysis(front)

        mocked = Mock(side_effect=analyze)
        with patch("photokin.core.analyze_photo", mocked):
            with self.assertRaises(SystemExit) as ctx:
                core.analyze_folder(self.folder, utils.Config())

        self.assertEqual(ctx.exception.code, 3)
        self.assertEqual(mocked.call_count, 2, "the third group must never be attempted")


class TestFolderModeSidecarWrite(_FolderModeTestCase):
    """The sidecar is written once, by the only caller holding the variant list.

    Folder mode used to let ``analyze_photo`` write its own sidecar and then
    overwrite it with the enriched record, so every group paid for two writes
    and the file briefly held a record missing ``all_variant_files``.
    """

    def _analyze_two_groups(
        self, *, write_sidecars: bool
    ) -> tuple[dict, Mock, Mock, list[logging.LogRecord]]:
        """Analyze a two-group folder, recording every ``open`` core performs."""
        self._make_images(
            "box3_025.jpg",
            "box3_025-back.jpg",
            "box3_025b.jpg",
            "box3_026.jpg",
        )
        analyze = Mock(side_effect=lambda front, *args, **kwargs: _fake_analysis(front))
        opener = Mock(wraps=open)

        with patch("photokin.core.analyze_photo", analyze):
            with patch("photokin.core.open", opener, create=True):
                with self.assertLogs(_CORE_LOGGER, level=logging.INFO) as captured:
                    result = core.analyze_folder(
                        self.folder, utils.Config(), write_sidecars=write_sidecars
                    )

        return result, analyze, opener, captured.records

    def test_sidecar_is_written_once_per_group_with_the_enriched_record(self) -> None:
        result, analyze, opener, records = self._analyze_two_groups(write_sidecars=True)

        self.assertEqual(analyze.call_count, 2)
        for call in analyze.call_args_list:
            self.assertFalse(
                call.kwargs["write_sidecar"],
                "analyze_photo must not write its own sidecar; that was the double write",
            )

        written_paths = [call.args[0] for call in opener.call_args_list]
        self.assertEqual(
            self._basenames(written_paths), ["box3_025.json", "box3_026.json"]
        )

        sidecar = os.path.join(self.folder, "box3_025.json")
        with open(sidecar, "r", encoding="utf-8") as handle:
            payload = json.load(handle)

        front = os.path.join(self.folder, "box3_025.jpg")
        record = payload["result"][front]
        self.assertEqual(record, result["results"][front])
        self.assertEqual(
            self._basenames(record["all_variant_files"]["front"]),
            ["box3_025.jpg", "box3_025b.jpg"],
        )
        self.assertEqual(
            self._basenames(record["all_variant_files"]["back"]), ["box3_025-back.jpg"]
        )
        self.assertIn(sidecar, "\n".join(_messages(records, logging.INFO)))

    def test_no_sidecar_is_written_when_sidecars_are_off(self) -> None:
        result, _analyze, opener, records = self._analyze_two_groups(write_sidecars=False)

        opener.assert_not_called()
        self.assertEqual(
            [name for name in os.listdir(self.folder) if name.endswith(".json")], []
        )
        front = os.path.join(self.folder, "box3_025.jpg")
        self.assertIn("all_variant_files", result["results"][front])
        self.assertEqual(
            [m for m in _messages(records, logging.INFO) if m.startswith("Sidecar written")], []
        )


class TestFolderModeStdoutPurity(_FolderModeTestCase):
    """Folder mode's stdout carries the result JSON and nothing else.

    Every other test in this module replaces ``analyze_photo`` outright, which
    skips every diagnostic inside it -- and those are the ones that used to be
    ``print(..., file=sys.stderr)``. A single dropped ``file=`` kwarg put a
    diagnostic on stdout, in the middle of the document ``cli.main`` prints for
    the Lightroom plugin to parse, and no test could see it. This one stubs
    only the provider boundary (client construction and the model call), so the
    real path runs end to end, and then holds *both* streams to empty:

    * stdout empty -- nothing was printed into the result document;
    * stderr empty -- with ``assertLogs`` holding the package logger's only
      handler, a non-empty stderr means something wrote to it directly instead
      of going through the logger.

    The second assertion is what makes the fix structural rather than a spot
    check: it fails for any *new* direct-to-stream write anywhere under the
    call, not just the ones converted here.
    """

    # A forbidden keyword and no "proposed_new_keywords" key, so the two
    # converted warnings in analyze_photo both fire on the real path. The
    # single-key result is remapped onto the real front path by core.
    _MODEL_JSON = json.dumps(
        {
            "result": {
                "front.jpg": {
                    "caption": "Two people on a porch",
                    "keywords": ["porch", "family gathering"],
                }
            }
        }
    )
    _MODEL = "claude-sonnet-4-6"

    def _run_real_path(self) -> tuple[dict, list[logging.LogRecord], str, str]:
        """Analyze a real folder with only the provider calls stubbed out.

        Returns:
            ``(result, log records, stdout text, stderr text)``.
        """
        self._make_images("box3_025.jpg", "album-page1.jpg", "album-page2.jpg")
        response = SimpleNamespace(
            model=self._MODEL,
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=self._MODEL_JSON)],
        )
        # max_edge=None keeps the placeholder bytes out of Pillow; the vocab
        # file is the package's own, so updates stay off rather than writing to
        # it. Neither setting touches the message path under test.
        cfg = utils.Config(provider="anthropic", max_edge=None, no_update_vocab=True)
        stdout, stderr = io.StringIO(), io.StringIO()

        with (
            patch("photokin.core._build_provider_client", return_value=object()),
            patch("photokin.core.call_model", return_value=response) as call_model,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertLogs(_PACKAGE_LOGGER, level=logging.INFO) as captured,
        ):
            result = core.analyze_folder(self.folder, cfg)

        self.assertEqual(call_model.call_count, 1, "the real analysis path never ran")
        return result, captured.records, stdout.getvalue(), stderr.getvalue()

    def test_a_real_run_writes_nothing_to_stdout_or_stderr_directly(self) -> None:
        result, _records, stdout, stderr = self._run_real_path()

        front = os.path.join(self.folder, "box3_025.jpg")
        self.assertEqual(list(result["results"]), [front])
        self.assertEqual(
            stdout,
            "",
            "a diagnostic reached stdout, where the caller's result JSON lives",
        )
        self.assertEqual(
            stderr,
            "",
            "a diagnostic bypassed the logger and wrote to stderr directly",
        )

    def test_the_diagnostics_the_run_produced_are_on_the_logger(self) -> None:
        _result, records, _stdout, _stderr = self._run_real_path()

        warnings = _messages(records, logging.WARNING)
        self.assertIn(
            '"proposed_new_keywords" missing or invalid; skipping vocab updates.', warnings
        )
        self.assertTrue(
            any('"family gathering"' in message for message in warnings),
            f"the forbidden-keyword warning is missing from {warnings}",
        )
        self.assertTrue(
            any(message.startswith("Skipping group 'album'") for message in warnings),
            f"the skipped-group warning is missing from {warnings}",
        )
        self.assertIn(
            "Analysis completed for box3_025.jpg", _messages(records, logging.INFO)
        )


if __name__ == "__main__":
    unittest.main()
