"""Coverage for :func:`photokin.core.analyze_folder`, after Phase B2 routed it
through the manifest pipeline.

Folder mode used to group album pages and negative-only sets correctly and then
refuse to analyze them: no group without a plain front scan was ever sent to the
model. Phase A made that loss loud -- a warning per skipped group and a skipped
count -- and deliberately left it in place. Phase B2 removes it, so the tests
that pinned the skipping have been inverted rather than deleted: they now assert
that those groups are analyzed, that every file in the folder gets a record, and
that the completion line reports a clean run.

What is pinned here:

* every group reaches the model and every file gets a record, in both
  ``process_all_variants`` settings -- the album pages travel as ``Page 1`` /
  ``Page 2`` in one call once the flag is on, which is the flag working in
  folder mode for the first time;
* nothing is dropped in silence: a file that cannot be placed in its group's
  payload is warned about by name and recorded under ``all_variant_files``;
* one failing group costs one group, every file of it carries the same error
  payload, a wholly failed batch still raises, a run-fatal provider error still
  aborts on the first group, and an interrupt is never swallowed;
* the sidecar is written by the shared analysis call rather than by a
  folder-only second write, and a destination that cannot be written costs the
  sidecar alone -- never the paid-for analysis that produced it.

The model call is mocked out at ``analyze_photo`` / ``analyze_group_parts``, so
no provider client is ever built. Fixtures are empty placeholder files -- the
grouping only reads filenames. The exception is ``TestFolderModeStdoutPurity``,
which stubs only the provider boundary so the real analysis path -- and every
diagnostic inside it -- runs.
"""

import io
import json
import logging
import os
import stat
import tempfile
import unittest
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from photokin import core, utils
from photokin.errors import ProviderApiError

_CORE_LOGGER = "photokin.core"
_PACKAGE_LOGGER = "photokin"
_COMPLETION_PREFIX = "Batch completed"

#: A model reply the real parse path accepts, carrying a forbidden keyword and
#: no "proposed_new_keywords" key so the two vocabulary warnings inside
#: ``analyze_photo`` both fire. The single "front.jpg" key is remapped onto the
#: real front path by core, so one constant serves any fixture.
_MODEL_REPLY_JSON = json.dumps(
    {
        "result": {
            "front.jpg": {
                "caption": "Two people on a porch",
                "keywords": ["porch", "family gathering"],
            }
        }
    }
)
_MODEL_NAME = "claude-sonnet-4-6"


def _fake_analysis(front_path: str) -> dict:
    """Build the minimal ``analyze_photo`` return shape the stream reads.

    Args:
        front_path: Path the record is keyed by, exactly as the stream passes it
            in.

    Returns:
        A ``{"result": {front_path: record}}`` payload that survives a JSON
        round trip, so it can be compared against a written sidecar.
    """
    return {"result": {front_path: {"caption": "A caption", "keywords": ["family"]}}}


def _fake_group_analysis(parts: list[tuple[str, list[str]]]) -> dict:
    """Build the minimal ``analyze_group_parts`` return shape the stream reads.

    Args:
        parts: The ordered ``(label, paths)`` pairs the stream assembled.

    Returns:
        A payload keyed by the first path of the first part, which is the file
        the stream resolves as the group's primary.
    """
    return _fake_analysis(parts[0][1][0])


@contextmanager
def _only_the_provider_stubbed() -> Iterator[Mock]:
    """Replace the provider client and the model call, and nothing else.

    Every other test here replaces the analysis call outright, which skips the
    code inside it. Stubbing at the provider boundary instead lets the real
    ``analyze_photo`` run, so what it writes -- and what it does when it cannot
    write -- is observable.

    Yields:
        The patched ``call_model`` mock, for asserting on the call count.
    """
    response = SimpleNamespace(
        model=_MODEL_NAME,
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=_MODEL_REPLY_JSON)],
    )
    with patch("photokin.core._build_provider_client", return_value=object()), patch(
        "photokin.core.call_model", return_value=response
    ) as call_model:
        yield call_model


def _real_path_config() -> utils.Config:
    """Return the config a run that executes the real analysis path uses.

    ``max_edge=None`` keeps the placeholder bytes out of Pillow and the vocab
    file is the package's own, so updates stay off rather than writing to it.
    Neither setting touches what these tests assert.
    """
    return utils.Config(provider="anthropic", max_edge=None, no_update_vocab=True)


def _messages(records: list[logging.LogRecord], level: int) -> list[str]:
    """Return the formatted messages logged at exactly *level*."""
    return [record.getMessage() for record in records if record.levelno == level]


def _group_warnings(records: list[logging.LogRecord]) -> list[str]:
    """Return the per-group WARNING messages, excluding the run summary line.

    Args:
        records: Log records captured for the whole ``analyze_folder`` call.

    Returns:
        One message per group that reported a file it could not place.
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


class TestFolderModeGroupCoverage(_FolderModeTestCase):
    """Every group in the folder is analyzed, and every file gets a record.

    The fixture is the one from the audit: an album page set and a
    negative-only set, next to an ordinary photo. Folder mode grouped all three
    correctly and then analyzed only the third, because the other two have no
    plain front scan in them. Phase A reported the loss; this is the phase that
    removes it, so the three tests that pinned the skipping now pin its absence.
    """

    def _run_mixed_folder(
        self, *, process_all_variants: bool = False
    ) -> tuple[dict, list[logging.LogRecord], list]:
        """Analyze album pages, a negative-only set and one ordinary photo.

        Args:
            process_all_variants: When True the group-aware path is taken, so
                the mocked callee is ``analyze_group_parts`` rather than
                ``analyze_photo``.

        Returns:
            ``(result, log records, what each call sent)``, where a sent entry
            is the front's basename on the legacy path and the call's ordered
            ``(label, [basenames])`` parts on the group-aware one.
        """
        self._make_images(
            "album-page1.jpg",
            "album-page2.jpg",
            "neg-negative.jpg",
            "box3_025.jpg",
        )
        sent: list = []

        def analyze_photo(front: str, *args: object, **kwargs: object) -> dict:
            sent.append(os.path.basename(front))
            return _fake_analysis(front)

        def analyze_group_parts(parts: list, *args: object, **kwargs: object) -> dict:
            sent.append([(label, [os.path.basename(p) for p in paths]) for label, paths in parts])
            return _fake_group_analysis(parts)

        if process_all_variants:
            target, stub = "photokin.core.analyze_group_parts", analyze_group_parts
        else:
            target, stub = "photokin.core.analyze_photo", analyze_photo

        with patch(target, side_effect=stub):
            with self.assertLogs(_CORE_LOGGER, level=logging.INFO) as captured:
                result = core.analyze_folder(
                    self.folder, utils.Config(process_all_variants=process_all_variants)
                )

        return result, captured.records, sent

    def test_every_group_reaches_the_model_and_every_file_gets_a_record(self) -> None:
        result, _records, sent = self._run_mixed_folder()

        self.assertEqual(len(sent), 3)
        # One entry per FILE, not per group: the two album pages and the
        # negative used to have no entry at all.
        self.assertEqual(
            self._basenames(result["results"]),
            ["album-page1.jpg", "album-page2.jpg", "box3_025.jpg", "neg-negative.jpg"],
        )
        self.assertEqual(result["errors"], {})

    def test_album_pages_and_negative_only_sets_are_analyzed_not_skipped(self) -> None:
        _result, records, sent = self._run_mixed_folder()

        self.assertEqual(
            [m for m in _group_warnings(records) if m.startswith("Skipping group")],
            [],
            "no group is skipped any more; that limitation is what B2 removed",
        )
        self.assertEqual(sorted(sent), ["album-page1.jpg", "box3_025.jpg", "neg-negative.jpg"])

    def test_process_all_variants_sends_every_album_page_in_one_call(self) -> None:
        _result, records, sent = self._run_mixed_folder(process_all_variants=True)

        self.assertEqual(
            [m for m in _group_warnings(records) if m.startswith("Skipping group")], []
        )
        # The flag was dead in folder mode before B2: the group-aware path was
        # unreachable, so both settings analyzed the same single primary file.
        self.assertIn(
            [("Page 1", ["album-page1.jpg"]), ("Page 2", ["album-page2.jpg"])], sent
        )
        self.assertIn([("Negative", ["neg-negative.jpg"])], sent)
        self.assertIn([("Front", ["box3_025.jpg"])], sent)

    def test_completion_line_reports_a_clean_run_at_info(self) -> None:
        _result, records, _sent = self._run_mixed_folder()

        completion = _completion_record(records)
        self.assertEqual(
            completion.levelno,
            logging.INFO,
            "nothing was lost, so the run must not summarize itself at WARNING",
        )
        message = completion.getMessage()
        self.assertIn("3 group(s)", message)
        self.assertIn("4 file(s) recorded", message)
        self.assertIn("0 group(s) failed", message)
        self.assertIn("0 file(s) displaced or dropped", message)

    def test_a_group_holding_pages_and_a_negative_places_every_file_it_can(self) -> None:
        self._make_images(
            "album.jpg", "album-page1.jpg", "album-page2.jpg", "album-negative.jpg"
        )

        with patch(
            "photokin.core.analyze_photo",
            side_effect=lambda front, *args, **kwargs: _fake_analysis(front),
        ):
            with self.assertLogs(_CORE_LOGGER, level=logging.INFO) as captured:
                result = core.analyze_folder(self.folder, utils.Config())

        self.assertEqual(
            self._basenames(result["results"]),
            ["album-negative.jpg", "album-page1.jpg", "album-page2.jpg", "album.jpg"],
        )
        # The pages and the negative are analyzed and recorded. Only the
        # untagged album.jpg loses out, because page 1 is the front side of this
        # variant and album-page1.jpg holds it -- and it is named for that
        # reason rather than reported as "not analyzed".
        warnings = _group_warnings(captured.records)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("'album'", warnings[0])
        self.assertIn("album.jpg", warnings[0])
        self.assertIn("album-page1.jpg", warnings[0])
        self.assertIn("both claim the front side", warnings[0])

        album = os.path.join(self.folder, "album.jpg")
        self.assertEqual(
            result["results"][album]["all_variant_files"]["displaced"], {":none": [album]}
        )
        completion = _completion_record(captured.records)
        self.assertEqual(completion.levelno, logging.WARNING)
        self.assertIn("1 file(s) displaced or dropped", completion.getMessage())
        self.assertIn("4 file(s) recorded", completion.getMessage())


class TestFolderModeErrorIsolation(_FolderModeTestCase):
    """One bad photo costs one group; a total loss and an interrupt cost the batch.

    Isolating every failure was the fix for the first problem and the cause of
    two more: a run where every group failed would have returned an
    empty-but-valid result that the CLI exits 0 on, and a run-wide provider
    error would have been re-reported once per group instead of aborting. All
    three behaviors are what ``strict_run_failures=True`` carries into the
    shared stream, so none of them may be relaxed here.
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

    def test_every_file_of_a_failed_group_carries_the_same_error_payload(self) -> None:
        # The per-file result shape has a per-file error shape to match: a group
        # that fails owes an entry to each of its files, not just to the front
        # the model was called on.
        self._make_images("box3_025.jpg", "box3_025-back.jpg", "box3_026.jpg")

        def analyze(front: str, *args: object, **kwargs: object) -> dict:
            if os.path.basename(front) == "box3_025.jpg":
                raise RuntimeError("model returned no parsable JSON")
            return _fake_analysis(front)

        with patch("photokin.core.analyze_photo", side_effect=analyze):
            with self.assertLogs(_CORE_LOGGER, level=logging.INFO):
                result = core.analyze_folder(self.folder, utils.Config())

        self.assertEqual(
            self._basenames(result["errors"]), ["box3_025-back.jpg", "box3_025.jpg"]
        )
        payloads = list(result["errors"].values())
        self.assertEqual(payloads[0], payloads[1])
        self.assertEqual(payloads[0]["type"], "RuntimeError")
        self.assertEqual(self._basenames(result["results"]), ["box3_026.jpg"])

    def test_the_failure_is_logged_and_counted(self) -> None:
        _result, records = self._run_with_failure_on("box3_025.jpg")

        errors = _messages(records, logging.ERROR)
        self.assertEqual(len(errors), 1)
        self.assertIn("'box3_025'", errors[0])
        self.assertIn("box3_025.jpg", errors[0])
        self.assertIn("RuntimeError", errors[0])
        self.assertIn("model returned no parsable JSON", errors[0])

        completion = _completion_record(records).getMessage()
        self.assertIn("3 group(s)", completion)
        self.assertIn("2 file(s) recorded", completion)
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
    """There is exactly one sidecar writer, and folder mode is not it.

    Folder mode used to let ``analyze_photo`` write its own sidecar and then
    overwrite it with a variant-enriched record, so every group paid for two
    writes. Phase A fixed the double write by keeping ``write_sidecar`` off and
    doing the enriched write itself; B2 removes the folder-only writer
    altogether and forwards the flag instead, so folder and manifest input
    produce the same artifact. The enrichment has not gone anywhere -- it is on
    the returned record, which is where a caller should read it.
    """

    def _analyze_two_groups(self, *, write_sidecars: bool) -> tuple[dict, Mock, list[logging.LogRecord]]:
        """Analyze a two-group folder with the analysis call mocked out."""
        self._make_images(
            "box3_025.jpg",
            "box3_025-back.jpg",
            "box3_025b.jpg",
            "box3_026.jpg",
        )
        analyze = Mock(side_effect=lambda front, *args, **kwargs: _fake_analysis(front))

        with patch("photokin.core.analyze_photo", analyze):
            with self.assertLogs(_CORE_LOGGER, level=logging.INFO) as captured:
                result = core.analyze_folder(
                    self.folder, utils.Config(), write_sidecars=write_sidecars
                )

        return result, analyze, captured.records

    def test_sidecar_writing_is_delegated_to_the_shared_analysis_call(self) -> None:
        result, analyze, _records = self._analyze_two_groups(write_sidecars=True)

        self.assertEqual(analyze.call_count, 2)
        for call in analyze.call_args_list:
            self.assertTrue(
                call.kwargs["write_sidecar"],
                "the analysis call is now the only sidecar writer in any mode",
            )

        front = os.path.join(self.folder, "box3_025.jpg")
        variants = result["results"][front]["all_variant_files"]
        self.assertEqual(
            self._basenames(variants["front"]), ["box3_025.jpg", "box3_025b.jpg"]
        )
        self.assertEqual(self._basenames(variants["back"]), ["box3_025-back.jpg"])
        self.assertEqual(
            self._basenames(variants["all"]),
            ["box3_025-back.jpg", "box3_025.jpg", "box3_025b.jpg"],
        )

    def test_no_sidecar_is_written_when_sidecars_are_off(self) -> None:
        result, analyze, records = self._analyze_two_groups(write_sidecars=False)

        for call in analyze.call_args_list:
            self.assertFalse(call.kwargs["write_sidecar"])
        self.assertEqual(
            [name for name in os.listdir(self.folder) if name.endswith(".json")], []
        )
        front = os.path.join(self.folder, "box3_025.jpg")
        self.assertIn("all_variant_files", result["results"][front])
        self.assertEqual(
            [m for m in _messages(records, logging.INFO) if m.startswith("Sidecar written")], []
        )


@unittest.skipUnless(os.name == "nt", "read-only files only block writes on Windows")
class TestSidecarWriteFailureKeepsTheAnalysis(_FolderModeTestCase):
    """A sidecar that cannot be written must not take its record down with it.

    The analysis is already paid for by the time the sidecar is written, so the
    two failures are not the same failure. Phase A held that line with a folder-
    only writer that banked the record first and caught ``OSError``; routing
    folder mode through the shared stream moved the write inside the analysis
    call, where an escaping ``OSError`` reaches the stream's per-group handler
    -- which discards the model's output, writes an error payload typed
    ``PermissionError`` for every file of the group, and, once no group has
    succeeded, re-raises under ``strict_run_failures`` and loses the run.

    A read-only ``.json`` left by a previous run is enough to trigger it, as is
    a lock held by a sync client or a read-only share. These stub only the
    provider boundary, so the write really runs and really fails.
    """

    def _block_sidecars_for(self, *stems: str) -> None:
        """Leave an unwritable ``<stem>.json`` in the scratch folder for each stem."""
        for stem in stems:
            blocked = os.path.join(self.folder, f"{stem}.json")
            with open(blocked, "w", encoding="utf-8") as handle:
                handle.write("{}")
            os.chmod(blocked, stat.S_IREAD)
            self.addCleanup(os.chmod, blocked, stat.S_IWRITE | stat.S_IREAD)

    def _analyze_with_sidecars(self) -> tuple[dict, list[logging.LogRecord]]:
        """Run the real analysis path over two photos with sidecars requested."""
        with _only_the_provider_stubbed() as call_model:
            with self.assertLogs(_CORE_LOGGER, level=logging.INFO) as captured:
                result = core.analyze_folder(
                    self.folder, _real_path_config(), write_sidecars=True
                )

        self.assertEqual(call_model.call_count, 2, "the real analysis path never ran")
        return result, captured.records

    def test_one_unwritable_sidecar_costs_the_sidecar_and_nothing_else(self) -> None:
        self._make_images("box3_025.jpg", "box3_026.jpg")
        self._block_sidecars_for("box3_025")

        result, records = self._analyze_with_sidecars()

        self.assertEqual(
            self._basenames(result["results"]), ["box3_025.jpg", "box3_026.jpg"]
        )
        self.assertEqual(result["errors"], {})
        self.assertEqual(
            _messages(records, logging.ERROR),
            [],
            "a failed sidecar write was reported to the caller as a model failure",
        )
        self.assertTrue(
            any(
                m.startswith("Sidecar not written for box3_025.jpg")
                and m.endswith("the analysis is kept in the results.")
                for m in _messages(records, logging.WARNING)
            ),
            f"the failed write was not reported at all: {_group_warnings(records)}",
        )
        # The write that could succeed still did: one blocked destination must
        # not become a blanket skip.
        self.assertTrue(os.path.isfile(os.path.join(self.folder, "box3_026.json")))

    def test_a_wholly_unwritable_folder_still_returns_every_record(self) -> None:
        # The total-loss case: with every group failing, strict_run_failures
        # re-raises, so a read-only photo directory used to lose the whole batch
        # after paying for it -- the CLI exits 2 with nothing on stdout.
        self._make_images("box3_025.jpg", "box3_026.jpg")
        self._block_sidecars_for("box3_025", "box3_026")

        result, records = self._analyze_with_sidecars()

        self.assertEqual(
            self._basenames(result["results"]), ["box3_025.jpg", "box3_026.jpg"]
        )
        self.assertEqual(result["errors"], {})
        self.assertIn("0 group(s) failed", _completion_record(records).getMessage())


class TestFolderModeStdoutPurity(_FolderModeTestCase):
    """Folder mode's stdout carries the result JSON and nothing else.

    Every other test in this module replaces the analysis call outright, which
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

    def _run_real_path(self) -> tuple[dict, list[logging.LogRecord], str, str]:
        """Analyze a real folder with only the provider calls stubbed out.

        Returns:
            ``(result, log records, stdout text, stderr text)``.
        """
        self._make_images("box3_025.jpg", "album-page1.jpg", "album-page2.jpg")
        stdout, stderr = io.StringIO(), io.StringIO()

        with (
            _only_the_provider_stubbed() as call_model,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertLogs(_PACKAGE_LOGGER, level=logging.INFO) as captured,
        ):
            result = core.analyze_folder(self.folder, _real_path_config())

        # Two, not one: the album page set is its own group and its own call
        # now that folder mode no longer skips it.
        self.assertEqual(call_model.call_count, 2, "the real analysis path never ran")
        return result, captured.records, stdout.getvalue(), stderr.getvalue()

    def test_a_real_run_writes_nothing_to_stdout_or_stderr_directly(self) -> None:
        result, _records, stdout, stderr = self._run_real_path()

        self.assertEqual(
            self._basenames(result["results"]),
            ["album-page1.jpg", "album-page2.jpg", "box3_025.jpg"],
        )
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
        self.assertFalse(
            any(message.startswith("Skipping group") for message in warnings),
            f"no group is skipped any more, yet one was reported: {warnings}",
        )
        self.assertIn(
            "Analysis completed for box3_025.jpg", _messages(records, logging.INFO)
        )


if __name__ == "__main__":
    unittest.main()
