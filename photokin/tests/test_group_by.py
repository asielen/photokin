"""Phase C1: ``--group-by`` is the one grouping axis, and its default is a no-op.

C1 replaced two orthogonal knobs -- ``--process-all-variants`` (how many images
go in the call) and ``--update-policy`` (which files get written) -- with a
single granularity axis. The existing grouping modules were re-parameterized onto
that axis in place, which pins the axis but leaves four claims resting on nothing:

* **the default costs the ordinary user nothing.** ``object`` is meant to be
  byte-identical to commit f4153ae on input a single front/back pair fully
  describes, which is nearly all real input. The literals below were captured by
  running f4153ae's ``analyze_folder`` over this fixture; a diff here is a
  regression on the path every existing user is already on;
* **each value groups the plan's own table.** ``docs/unified-input-pipeline.md``
  states 1 / 3 / 5 calls over ``box3_025{,-back,b,b-back,c}.jpg``. That table is
  the specification of the axis, so it is asserted with exact files and labels
  rather than as three counts that merely differ;
* **``none`` splits things it is not obvious anyone wants split** -- a front from
  its own back, a multipage document into unrelated pages. Both are stated
  consequences of "every file alone" and are written down here so that a later
  reader who finds them surprising cannot quietly repair them;
* **the retired flags still parse.** The Lightroom plug-in launches
  ``python -m photokin.cli`` and may pass either, and argparse exits 2 on an
  unrecognized argument -- a hard crash rather than a behavior change. The plan
  lists this seam, and ``--group-by`` reaching ``Config`` from argparse, as the
  last thing C1 owed.

Plus the two rules C1 added rather than moved: the ``negative`` part marker,
which had no implementation at all before this phase, and the per-value crop
rule.

Every model entry point is mocked, so no provider client is built and nothing
opens a socket. Image fixtures are empty placeholder files -- grouping reads
filenames only -- and every one of them is created inside a
``TemporaryDirectory``, so no test here writes into the repository tree.
"""

import io
import json
import logging
import os
import sys
import tempfile
import unittest
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from typing import ClassVar, cast
from unittest.mock import patch

from photokin import cli, core, utils

_CORE_LOGGER = "photokin.core"

#: The plan's own table fixture: one print, a rescan of it, a second rescan with
#: no back, and a back for each of the first two.
_TABLE_FIXTURE: tuple[str, ...] = (
    "box3_025.jpg",
    "box3_025-back.jpg",
    "box3_025b.jpg",
    "box3_025b-back.jpg",
    "box3_025c.jpg",
)

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


class _RecordingAnalyzers:
    """Stand-in for the three model entry points, recording what each was sent.

    A call is recorded as ``(callee, ...)`` naming the files and, for the group
    forms, the part label each file travelled under -- which is the whole of what
    the grouping axis decides. ``write_sidecar`` is deliberately not part of the
    tuple: it is orthogonal to granularity and already pinned in
    ``test_folder_routing.py``, and carrying it here would put a constant
    ``False`` in every literal of the table.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def photo(
        self,
        front_path: str,
        back_path: str | None = None,
        config: utils.Config | None = None,
        *,
        original_meta: dict | None = None,
        write_sidecar: bool = False,
    ) -> dict:
        """Record an ``analyze_photo`` call and return a minimal valid result."""
        self.calls.append(("photo", front_path, back_path))
        return _reply(front_path)

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
        self.calls.append(("front_back", tuple(fronts), tuple(backs)))
        return _reply((fronts + backs)[0])

    def parts(
        self,
        parts: list[tuple[str, list[str]]],
        config: utils.Config | None = None,
        *,
        original_meta: dict | None = None,
        write_sidecar: bool = False,
    ) -> dict:
        """Record an ``analyze_group_parts`` call."""
        self.calls.append(("parts", tuple((label, tuple(paths)) for label, paths in parts)))
        return _reply(next(path for _label, paths in parts for path in paths))


def _reply(front_path: str) -> dict:
    """Build the ``{"result": {path: record}}`` shape every analyzer returns.

    The keywords are chosen so the record is worth comparing rather than merely
    present: ``Back`` exercises the part-marker strip, ``PC-R-123`` the code
    union that is shared across the whole group, and ``family`` an ordinary
    keyword that must survive both.

    Args:
        front_path: The path the analyzer files its result under.

    Returns:
        One analyzer reply.
    """
    return {
        "result": {
            front_path: {
                "caption": "A caption",
                "keywords": ["family", "Back", "PC-R-123"],
                "location_guess": {"value": "Springfield", "confidence": 0.9},
                "date_guess": {"value": "1954", "confidence": 0.9},
                "analysis_notes": "notes",
            }
        }
    }


@contextmanager
def _recording() -> Iterator[_RecordingAnalyzers]:
    """Replace the three model entry points with recorders for the block."""
    rec = _RecordingAnalyzers()
    with (
        patch("photokin.core.analyze_photo", rec.photo),
        patch("photokin.core.analyze_group_front_back", rec.front_back),
        patch("photokin.core.analyze_group_parts", rec.parts),
    ):
        yield rec


def _shorten(value: object) -> object:
    """Rewrite every path inside *value* to its basename, recursively.

    Args:
        value: Any nesting of dicts, lists, tuples and scalars.

    Returns:
        The same structure with directory components dropped, so a run in one
        temporary directory compares against a literal or against a run in
        another. Strings holding no separator are left exactly as they are, which
        is what keeps captions and keywords intact.
    """
    if isinstance(value, str):
        return os.path.basename(value) if ("/" in value or os.sep in value) else value
    if isinstance(value, tuple):
        return tuple(_shorten(item) for item in value)
    if isinstance(value, list):
        return [_shorten(item) for item in value]
    if isinstance(value, dict):
        return {_shorten(key): _shorten(item) for key, item in value.items()}
    return value


def _sent(calls: Sequence[tuple]) -> list[tuple]:
    """Rewrite a recorder's call log to basenames, so two runs compare."""
    return [tuple(_shorten(field) for field in call) for call in calls]


def _shorten_records(results: dict[str, dict]) -> dict[str, dict]:
    """Rewrite a run's per-file record map to basenames, keys and values alike.

    Args:
        results: The ``results`` half of a stream's aggregate return.

    Returns:
        The same map addressed by basename, with every path stored inside a
        record shortened as well, so it compares against a checked-in literal.
    """
    return {
        os.path.basename(path): cast(dict, _shorten(record))
        for path, record in results.items()
    }


class _GroupByTestCase(unittest.TestCase):
    """Base giving each test scratch space and one way to run a folder."""

    #: Whole record sets are compared here; a truncated diff would say the
    #: default path moved without saying which field moved.
    maxDiff = None

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.work: str = scratch.name

    def make_folder(self, *names: str) -> str:
        """Create a scratch folder holding empty placeholder files named *names*."""
        folder = os.path.join(self.work, "scans")
        os.makedirs(folder, exist_ok=True)
        for name in names:
            with open(os.path.join(folder, name), "w", encoding="utf-8"):
                pass
        return folder

    def run_folder(
        self, folder: str, *, group_by: str = utils.GROUP_BY_OBJECT
    ) -> tuple[list[tuple], dict, list[logging.LogRecord]]:
        """Analyze *folder* with every model entry point recorded.

        Args:
            folder: Directory to analyze.
            group_by: Grouping granularity, one of ``utils.GROUP_BY_VALUES``.

        Returns:
            ``(model calls, aggregate result, log records)``.
        """
        config = utils.Config(group_by=group_by)
        with _recording() as rec, self.assertLogs(_CORE_LOGGER, level=logging.INFO) as captured:
            result = core.analyze_folder(folder, config)
        return rec.calls, result, captured.records

    def run_items(
        self, items: list[dict], *, group_by: str = utils.GROUP_BY_OBJECT
    ) -> tuple[list[tuple], dict]:
        """Run *items* through the stream the way every input mode reaches it."""
        with _recording() as rec:
            result = core.process_manifest_stream(
                manifest={"items": items}, cfg=utils.Config(group_by=group_by)
            )
        return rec.calls, result

    def keywords_by_file(self, result: dict) -> dict[str, list[str]]:
        """Return ``{basename: keywords}`` for every file the run recorded."""
        return {
            os.path.basename(path): record.get("keywords") or []
            for path, record in result["results"].items()
        }

    def basenames(self, paths: Iterable[str]) -> list[str]:
        """Return the sorted basenames of *paths*, for order-independent asserts."""
        return sorted(os.path.basename(path) for path in paths)

    def messages(
        self, records: Sequence[logging.LogRecord], level: int, folder: str
    ) -> list[str]:
        """Return the messages logged at exactly *level*, with *folder* stripped.

        The directory prefix is removed rather than each path being reduced to
        its basename, because a message holds prose as well as paths and under
        ``--group-by none`` the group's own name is a path too.

        Args:
            records: The run's log records.
            level: The exact level to keep, so a WARNING assertion cannot be
                satisfied by an INFO line of the same wording.
            folder: The scratch directory the run was given.

        Returns:
            The matching messages, comparable against a literal.
        """
        prefix = folder + os.sep
        return [
            record.getMessage().replace(prefix, "")
            for record in records
            if record.levelno == level
        ]


class _CliTestCase(_GroupByTestCase):
    """Runs ``cli.main`` in-process and cleans up the handler it installs.

    ``main`` attaches a stderr handler to the ``photokin`` logger, so process
    state is both an input and an output of every CLI test: a handler left behind
    binds an abandoned stream and double-prints into the next run's capture. This
    duplicates ``test_folder_routing._CliTestCase`` for the reason recorded
    there -- ``photokin/tests`` is not a package, so a cross-module test import
    would depend on how the runner happened to set up ``sys.path``.
    """

    def setUp(self) -> None:
        super().setUp()
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

    def run_cli(self, argv: list[str]) -> tuple[int | None, str, str, list[tuple]]:
        """Run ``cli.main`` with *argv* and every model entry point recorded.

        Args:
            argv: Arguments after the program name.

        Returns:
            ``(exit code, stdout, stderr, model calls)``, the exit code being
            ``None`` when ``main`` returned without raising ``SystemExit``.
        """
        stdout, stderr = io.StringIO(), io.StringIO()
        code: int | None = None
        # Hydration is stubbed so the run does not go looking for an ExifTool
        # binary whose presence differs between machines; it does nothing to
        # either input here, since neither a folder item nor a manifest item
        # written below carries the ``metadata`` dict it acts on.
        with (
            patch.dict(os.environ, _NEUTRAL_ENV),
            patch.object(sys, "argv", ["photokin", *argv]),
            patch("photokin.cli.make_manifest_hydrator", return_value=lambda items: None),
            _recording() as rec,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        return code, stdout.getvalue(), stderr.getvalue(), rec.calls

    def warnings_naming(self, stderr: str, flag: str) -> list[str]:
        """Return the ``[WARNING]`` lines in *stderr* that mention *flag*."""
        return [
            line
            for line in stderr.splitlines()
            if line.startswith("[WARNING]") and flag in line
        ]


class TestTheDefaultIsANoOp(_GroupByTestCase):
    """``--group-by object`` on ordinary input is exactly what f4153ae produced.

    "Ordinary" is the precise thing the axis promises to leave alone: a group a
    single front/back pair fully describes. C1 deliberately changed four shapes
    -- a second front-side scan, a second back, a page, a negative -- and this
    fixture holds none of them, so every byte of it has to survive.

    The literals were captured by extracting commit f4153ae into a scratch tree
    and running its ``analyze_folder`` over this fixture with the same recorders
    and the same canned reply used here. The capture was repeated to confirm it
    is deterministic before it was written down.

    Two differences are out of scope and stated here rather than hidden.

    A *model-emitted* ``Negative`` keyword now leaves the group-wide pool, where
    f4153ae kept it on every file. That is C1's marker rule
    (``_split_keywords_for_merge``) and is intended, so this fixture's reply does
    not contain one.

    The ``caption`` is the other, and it is the whole subject of C3's caption
    block: a group's captions are now merged into one labelled block written to
    every file of it. f4153ae wrote "[Front] A caption" onto the lone scan and
    "[Back] A caption" onto BOTH files of the pair -- including the front, which
    is the mislabelling the block replaces. What every file gets here instead is
    the bare "A caption", because these placeholders hold no captions of their
    own: the model's transcription is the only content there is, and with
    nothing else in the group to tell it apart from it earns no section label
    of its own, the same as any other lone scan's caption. The caption is
    therefore the one field of this golden that is *expected* to differ from
    f4153ae; every other byte still has to survive.

    Per-page captions did not move it again, and that is the point of leaving
    this golden alone: the fixture is a lone scan and a front/back pair, so
    nothing in it is a document, the group block is still what every file gets,
    and no ``caption_scope`` key appears. A run of this file that suddenly
    disagrees is the alarm for a per-page rule that leaked out of documents.
    """

    _FIXTURE: ClassVar[tuple[str, ...]] = (
        "box3_025.jpg",
        "box3_025-back.jpg",
        "box3_040.jpg",
    )
    _REGRESSED: ClassVar[str] = (
        "REGRESSION -- the default path moved. --group-by object is required to "
        "reproduce commit f4153ae exactly on input a single front/back pair "
        "describes, which is nearly all real input; the shapes C1 meant to change "
        "(a second front-side scan, a second back, a page, a negative) are all "
        "absent from this fixture. See the C1 section of "
        "docs/unified-input-pipeline.md."
    )

    #: Captured from f4153ae. The lone front is its own group and its own call;
    #: the pair travels through ``analyze_photo``, not the group analyzer, which
    #: is what keeps the prompt, the dump tag and the caption block unchanged.
    _F4153AE_CALLS: ClassVar[list[tuple]] = [
        ("photo", "box3_025.jpg", "box3_025-back.jpg"),
        ("photo", "box3_040.jpg", None),
    ]

    # Named once and referenced from every record they appear in, so the golden
    # below reads as the three records it is rather than as one wall of nulls.
    _NO_USAGE: ClassVar[dict[str, None]] = {
        "prompt_tokens": None,
        "completion_tokens": None,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "model": None,
    }
    _NO_MERGE: ClassVar[dict[str, list]] = {"overrides": [], "unions": []}
    _LOCATION: ClassVar[dict[str, object]] = {
        "city": None,
        "state": None,
        "country": None,
        "sublocation": None,
        "confidence": 0.9,
    }
    _DATE: ClassVar[dict[str, object]] = {"value": "1954", "confidence": 0.9}
    _PAIR_FILES: ClassVar[dict[str, object]] = {
        "front": ["box3_025.jpg"],
        "back": ["box3_025-back.jpg"],
        "variants": [
            {"path": "box3_025-back.jpg", "version": None, "is_back": True, "preferred": False},
            {"path": "box3_025.jpg", "version": None, "is_back": False, "preferred": False},
        ],
        "all": ["box3_025-back.jpg", "box3_025.jpg"],
    }

    #: Captured from f4153ae: the complete per-file record set, paths shortened
    #: to basenames -- with the ``caption`` field updated to C3's block, for the
    #: reason the class docstring gives. One caption still lands on every file of
    #: the pair, as it did in f4153ae; what changed is that it no longer claims
    #: the front is a back.
    _F4153AE_RECORDS: ClassVar[dict[str, dict[str, object]]] = {
        "box3_025-back.jpg": {
            "caption": "A caption",
            "keywords": ["family", "PC-R-123", "back"],
            "location_guess": _LOCATION,
            "date_guess": _DATE,
            "analysis_notes": "notes",
            "_usage": _NO_USAGE,
            "all_variant_files": _PAIR_FILES,
            "_merge": _NO_MERGE,
        },
        "box3_025.jpg": {
            "caption": "A caption",
            "keywords": ["family", "PC-R-123"],
            "location_guess": _LOCATION,
            "date_guess": _DATE,
            "analysis_notes": "notes",
            "_usage": _NO_USAGE,
            "all_variant_files": _PAIR_FILES,
            "_merge": _NO_MERGE,
        },
        "box3_040.jpg": {
            "caption": "A caption",
            "keywords": ["family", "PC-R-123"],
            "location_guess": _LOCATION,
            "date_guess": _DATE,
            "analysis_notes": "notes",
            "_usage": _NO_USAGE,
            "all_variant_files": {
                "front": ["box3_040.jpg"],
                "back": [],
                "variants": [
                    {
                        "path": "box3_040.jpg",
                        "version": None,
                        "is_back": False,
                        "preferred": False,
                    }
                ],
                "all": ["box3_040.jpg"],
            },
            "_merge": _NO_MERGE,
        },
    }

    def test_the_model_is_sent_what_f4153ae_sent(self) -> None:
        calls, _result, _records = self.run_folder(self.make_folder(*self._FIXTURE))

        self.assertEqual(_sent(calls), self._F4153AE_CALLS, self._REGRESSED)

    def test_every_record_is_the_one_f4153ae_produced(self) -> None:
        _calls, result, _records = self.run_folder(self.make_folder(*self._FIXTURE))

        self.assertEqual(
            _shorten_records(result["results"]), self._F4153AE_RECORDS, self._REGRESSED
        )
        self.assertEqual(result["errors"], {})

    def test_the_default_is_reached_without_naming_it(self) -> None:
        # The no-op claim is about the value a user who passes nothing gets, so
        # the comparison above is worth nothing if the dataclass default is not
        # that value.
        self.assertEqual(utils.Config().group_by, utils.GROUP_BY_OBJECT)

        folder = self.make_folder(*self._FIXTURE)
        with _recording() as rec, self.assertLogs(_CORE_LOGGER, level=logging.INFO):
            core.analyze_folder(folder, utils.Config())

        self.assertEqual(_sent(rec.calls), self._F4153AE_CALLS, self._REGRESSED)

    def test_an_ordinary_run_reports_no_loss(self) -> None:
        _calls, _result, records = self.run_folder(self.make_folder(*self._FIXTURE))

        completion = [
            record for record in records if record.getMessage().startswith("Batch completed")
        ]
        self.assertEqual(len(completion), 1)
        self.assertEqual(completion[0].levelno, logging.INFO)
        self.assertIn("0 file(s) recorded without being sent", completion[0].getMessage())


class TestEachValueGroupsThePlansTable(_GroupByTestCase):
    """The plan's own table, asserted with the files and labels, not just counts.

    ``docs/unified-input-pipeline.md`` specifies the axis as 1 / 3 / 5 calls over
    ``box3_025{,-back,b,b-back,c}.jpg``. Counts alone would pass for three
    implementations that split on the wrong thing, so each value's exact payload
    is pinned beside its count.
    """

    _EXPECTED: ClassVar[dict[str, list[tuple]]] = {
        utils.GROUP_BY_OBJECT: [
            # One object, one call. The variant order is ``variant_list_sorted``,
            # which sorts the unversioned scan last; that predates C1 and is
            # pinned as observed so a later decision to change it surfaces here.
            (
                "front_back",
                ("box3_025b.jpg", "box3_025c.jpg", "box3_025.jpg"),
                ("box3_025b-back.jpg", "box3_025-back.jpg"),
            ),
        ],
        utils.GROUP_BY_PAIR: [
            # One call per rescan, each with its own back. ``box3_025c.jpg`` has
            # none, so it goes alone -- judged on its own merits is the point.
            ("photo", "box3_025.jpg", "box3_025-back.jpg"),
            ("photo", "box3_025b.jpg", "box3_025b-back.jpg"),
            ("photo", "box3_025c.jpg", None),
        ],
        utils.GROUP_BY_NONE: [
            # Five files, five calls, every back detached from its front. The
            # order is ``list_folder_images``' ``(name.lower(), name)``.
            ("photo", "box3_025-back.jpg", None),
            ("photo", "box3_025.jpg", None),
            ("photo", "box3_025b-back.jpg", None),
            ("photo", "box3_025b.jpg", None),
            ("photo", "box3_025c.jpg", None),
        ],
    }

    def test_the_call_count_is_the_one_the_plan_states(self) -> None:
        folder = self.make_folder(*_TABLE_FIXTURE)

        for group_by, expected in self._EXPECTED.items():
            with self.subTest(group_by=group_by):
                calls, _result, _records = self.run_folder(folder, group_by=group_by)

                self.assertEqual(
                    len(calls),
                    len(expected),
                    f"--group-by {group_by} made {len(calls)} call(s) over "
                    f"{len(_TABLE_FIXTURE)} files; the plan's table says "
                    f"{len(expected)}",
                )

    def test_each_value_sends_exactly_these_files_under_these_labels(self) -> None:
        folder = self.make_folder(*_TABLE_FIXTURE)

        for group_by, expected in self._EXPECTED.items():
            with self.subTest(group_by=group_by):
                calls, _result, _records = self.run_folder(folder, group_by=group_by)

                self.assertEqual(_sent(calls), expected)

    def test_the_three_values_really_do_differ(self) -> None:
        # A guard against the whole table passing because every value collapsed
        # onto one behavior and the literals were written to match.
        folder = self.make_folder(*_TABLE_FIXTURE)

        payloads = {
            group_by: repr(_sent(self.run_folder(folder, group_by=group_by)[0]))
            for group_by in utils.GROUP_BY_VALUES
        }

        self.assertEqual(
            len(set(payloads.values())),
            len(utils.GROUP_BY_VALUES),
            f"--group-by is not changing what is sent: {payloads}",
        )

    def test_every_file_is_recorded_exactly_once_at_every_value(self) -> None:
        # Granularity may change what the model sees; it may never change
        # whether a file is accounted for.
        folder = self.make_folder(*_TABLE_FIXTURE)

        for group_by in utils.GROUP_BY_VALUES:
            with self.subTest(group_by=group_by):
                _calls, result, _records = self.run_folder(folder, group_by=group_by)

                self.assertEqual(self.basenames(result["results"]), sorted(_TABLE_FIXTURE))
                self.assertEqual(result["errors"], {})

    def test_an_unknown_value_is_refused_before_any_model_call(self) -> None:
        # argparse guards the CLI; this is the library caller who sets
        # ``cfg.group_by`` by hand and would otherwise get a silent ``object``.
        with _recording() as rec, self.assertRaises(ValueError):
            core.build_manifest_buckets([{"path": "box3_025.jpg"}], group_by="variant")

        self.assertEqual(rec.calls, [])


class TestNoneSplitsWhatTheOtherValuesKeepWhole(_GroupByTestCase):
    """The stated, accepted costs of the escape hatch, written down on purpose.

    ``none`` exists for when filenames lie and the grammar mis-groups. Both
    consequences below look like defects out of context, and the plan says so in
    those words: a back analyzed alone is handwriting with no photo attached, and
    page 2 without page 1 is meaningless. They are asserted here so that nobody
    later "fixes" either one by accident, and so the fix, if it is ever wanted,
    has to be a deliberate change to this file.
    """

    def test_a_front_is_split_from_its_own_back(self) -> None:
        folder = self.make_folder("box3_025.jpg", "box3_025-back.jpg")

        calls, _result, _records = self.run_folder(folder, group_by=utils.GROUP_BY_NONE)

        self.assertEqual(
            _sent(calls),
            [("photo", "box3_025-back.jpg", None), ("photo", "box3_025.jpg", None)],
            "--group-by none must separate a back from its front. This is the "
            "escape hatch's stated cost, not a bug: caption, date and location "
            "inference all lean on seeing the front",
        )

    def test_the_split_reaches_the_records_and_not_only_the_calls(self) -> None:
        # The back is not merely sent alone -- it is a different object, so its
        # record no longer names the front at all. A split that showed up in the
        # calls but not here would leave the plug-in fanning group metadata
        # across files the model never saw together.
        folder = self.make_folder("box3_025.jpg", "box3_025-back.jpg")

        _calls, result, _records = self.run_folder(folder, group_by=utils.GROUP_BY_NONE)

        files = {
            name: record["all_variant_files"]
            for name, record in _shorten_records(result["results"]).items()
        }
        self.assertEqual(files["box3_025.jpg"]["front"], ["box3_025.jpg"])
        self.assertEqual(files["box3_025.jpg"]["back"], [])
        self.assertEqual(files["box3_025-back.jpg"]["front"], [])
        self.assertEqual(files["box3_025-back.jpg"]["back"], ["box3_025-back.jpg"])

    def test_a_lone_back_is_still_never_sent_as_its_own_front(self) -> None:
        # Splitting the pair puts a back in a group of one, which is the shape
        # that used to resolve both payload roles to the same file. Paying twice
        # for one upload and calling it its own reverse would make the escape
        # hatch unsafe rather than merely expensive.
        folder = self.make_folder("box3_025.jpg", "box3_025-back.jpg")

        calls, _result, records = self.run_folder(folder, group_by=utils.GROUP_BY_NONE)

        self.assertNotIn(
            ("photo", "box3_025-back.jpg", "box3_025-back.jpg"), _sent(calls)
        )
        self.assertIn(
            "Group 'box3_025-back.jpg': box3_025-back.jpg is the only file standing "
            "for both sides; sending it once rather than as its own back.",
            self.messages(records, logging.INFO, folder),
        )

    def test_a_multipage_set_becomes_unrelated_pages(self) -> None:
        folder = self.make_folder("alb-page1.jpg", "alb-page2.jpg", "alb-page3.jpg")

        calls, _result, _records = self.run_folder(folder, group_by=utils.GROUP_BY_NONE)

        self.assertEqual(
            _sent(calls),
            [
                ("parts", (("Page 1", ("alb-page1.jpg",)),)),
                ("parts", (("Page 2", ("alb-page2.jpg",)),)),
                ("parts", (("Page 3", ("alb-page3.jpg",)),)),
            ],
            "--group-by none must split a multipage document into unrelated "
            "pages. The CLI help states it plainly and the plan calls it an "
            "accepted consequence of 'split every file', not a carve-out -- so "
            "if this is being repaired, repair the help text and the plan first",
        )

    def test_object_and_pair_keep_the_multipage_set_whole(self) -> None:
        # The other half of the same guard: the split has to be ``none``'s alone.
        # Pages carry no variant letter, so ``pair`` has nothing to split on.
        folder = self.make_folder("alb-page1.jpg", "alb-page2.jpg", "alb-page3.jpg")

        for group_by in (utils.GROUP_BY_OBJECT, utils.GROUP_BY_PAIR):
            with self.subTest(group_by=group_by):
                calls, _result, _records = self.run_folder(folder, group_by=group_by)

                self.assertEqual(
                    _sent(calls),
                    [
                        (
                            "parts",
                            (
                                ("Page 1", ("alb-page1.jpg",)),
                                ("Page 2", ("alb-page2.jpg",)),
                                ("Page 3", ("alb-page3.jpg",)),
                            ),
                        )
                    ],
                )

    def test_no_page_is_lost_by_being_split(self) -> None:
        folder = self.make_folder("alb-page1.jpg", "alb-page2.jpg", "alb-page3.jpg")

        _calls, result, _records = self.run_folder(folder, group_by=utils.GROUP_BY_NONE)

        self.assertEqual(
            self.basenames(result["results"]),
            ["alb-page1.jpg", "alb-page2.jpg", "alb-page3.jpg"],
        )
        self.assertEqual(result["errors"], {})


class TestTheRetiredFlagsStillParse(_CliTestCase):
    """The one piece of compatibility C1 kept, and the reason it is narrow.

    ``--process-all-variants`` and ``--update-policy`` are accepted, suppressed
    out of ``--help``, ignored, and warned about once each. Deleting them would
    make argparse exit 2 on a plug-in that still passes them, which is a hard
    crash rather than a behavior change; keeping them costs about six lines and
    no deprecation machinery.

    The folder here holds three groups so "warned once" is a real assertion: a
    warning emitted per group or per file would appear three or four times.
    """

    _FOLDER: ClassVar[tuple[str, ...]] = (
        "box3_025.jpg",
        "box3_025-back.jpg",
        "box3_040.jpg",
        "box3_041.jpg",
    )

    def test_process_all_variants_is_accepted_and_warns_exactly_once(self) -> None:
        folder = self.make_folder(*self._FOLDER)

        code, _out, err, calls = self.run_cli(["--folder", folder, "--process-all-variants"])

        self.assertIsNone(
            code,
            "--process-all-variants must stay accepted: the Lightroom plug-in "
            "launches 'python -m photokin.cli' and argparse exits 2 on an "
            "unrecognized flag",
        )
        self.assertNotEqual(calls, [])
        warnings = self.warnings_naming(err, "--process-all-variants")
        self.assertEqual(len(warnings), 1, err)
        self.assertIn("no longer does anything", warnings[0])
        self.assertIn("--group-by", warnings[0])

    def test_update_policy_is_accepted_and_warns_exactly_once(self) -> None:
        folder = self.make_folder(*self._FOLDER)

        for value in ("master_exact", "merge_per_variant"):
            with self.subTest(value=value):
                code, _out, err, calls = self.run_cli(
                    ["--folder", folder, "--update-policy", value]
                )

                self.assertIsNone(code)
                self.assertNotEqual(calls, [])
                warnings = self.warnings_naming(err, "--update-policy")
                self.assertEqual(len(warnings), 1, err)
                self.assertIn("no longer does anything", warnings[0])
                self.assertIn("--group-by", warnings[0])

    def test_neither_flag_is_warned_about_when_it_is_not_passed(self) -> None:
        # ``--update-policy`` used to carry a real default, so a warning keyed on
        # the default rather than on the argument being supplied would fire on
        # every ordinary run.
        folder = self.make_folder(*self._FOLDER)

        _code, _out, err, _calls = self.run_cli(["--folder", folder])

        self.assertEqual(self.warnings_naming(err, "--process-all-variants"), [])
        self.assertEqual(self.warnings_naming(err, "--update-policy"), [])

    def test_the_retired_flags_change_nothing_about_the_run(self) -> None:
        folder = self.make_folder(*self._FOLDER)

        _code, _out, _err, plain = self.run_cli(["--folder", folder])
        _code, _out, _err, flagged = self.run_cli(
            [
                "--folder",
                folder,
                "--process-all-variants",
                "--update-policy",
                "master_exact",
            ]
        )

        self.assertEqual(
            _sent(flagged),
            _sent(plain),
            "a retired flag still changed the payload; it is meant to be "
            "accepted and inert",
        )

    def test_group_by_reaches_the_config_from_argparse(self) -> None:
        folder = self.make_folder(*self._FOLDER)
        seen: list[str] = []

        # C2 collapsed the mode branches, so every input type reaches the model
        # through ``process_manifest_stream``; ``cli.analyze_folder`` is gone.
        def _spy(*, cfg: utils.Config, **kwargs: object) -> dict:
            seen.append(cfg.group_by)
            return {"results": {}, "errors": {}}

        for argv, expected in (
            ([], utils.GROUP_BY_OBJECT),
            (["--group-by", "object"], utils.GROUP_BY_OBJECT),
            (["--group-by", "pair"], utils.GROUP_BY_PAIR),
            (["--group-by", "none"], utils.GROUP_BY_NONE),
        ):
            with self.subTest(argv=argv):
                seen.clear()
                with patch("photokin.cli.process_manifest_stream", _spy):
                    code, _out, _err, _calls = self.run_cli(["--folder", folder, *argv])

                self.assertIsNone(code)
                self.assertEqual(seen, [expected])

    def test_an_unknown_group_by_value_exits_two(self) -> None:
        folder = self.make_folder(*self._FOLDER)

        code, _out, err, calls = self.run_cli(["--folder", folder, "--group-by", "variant"])

        self.assertEqual(code, 2)
        self.assertEqual(calls, [])
        self.assertIn("invalid choice", err)

    def test_a_manifest_carrying_preferred_runs_without_raising(self) -> None:
        # ``preferred`` was expected to become a no-op once every file of a group
        # is analyzed. It did not -- it still ranks two files contesting one slot
        # -- but either way the plug-in already sends it, so the contract that
        # matters is that it never fails a run.
        folder = self.make_folder(*self._FOLDER)
        manifest_path = os.path.join(self.work, "batch.json")
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "items": [
                        {"path": os.path.join(folder, "box3_025.jpg"), "preferred": True},
                        {
                            "path": os.path.join(folder, "box3_025-back.jpg"),
                            "preferred": "true",
                        },
                        {"path": os.path.join(folder, "box3_040.jpg"), "preferred": False},
                    ]
                },
                handle,
            )

        code, out, _err, calls = self.run_cli(["--manifest", manifest_path])

        self.assertIsNone(code)
        self.assertNotEqual(calls, [])
        self.assertEqual(
            self.basenames(json.loads(out)["results"]),
            ["box3_025-back.jpg", "box3_025.jpg", "box3_040.jpg"],
        )

    def test_preferred_cannot_raise_on_any_json_value(self) -> None:
        # ``_resolve_manifest_entry`` reads it as ``bool(raw.get("preferred"))``,
        # so no value a JSON document can hold reaches a comparison. Pinned
        # because the plan reasoned about it rather than testing it.
        folder = self.make_folder("box3_025.jpg", "box3_025-back.jpg")
        values: tuple[object, ...] = (
            True,
            False,
            None,
            0,
            1,
            3.5,
            "",
            "yes",
            "no",
            [],
            [1, 2],
            {},
            {"a": 1},
        )

        for value in values:
            for group_by in utils.GROUP_BY_VALUES:
                with self.subTest(preferred=value, group_by=group_by):
                    items: list[dict] = [
                        {"path": os.path.join(folder, "box3_025.jpg"), "preferred": value},
                        {"path": os.path.join(folder, "box3_025-back.jpg")},
                    ]

                    _calls, result = self.run_items(items, group_by=group_by)

                    self.assertEqual(
                        self.basenames(result["results"]),
                        ["box3_025-back.jpg", "box3_025.jpg"],
                    )


class TestPartMarkers(_GroupByTestCase):
    """Every file of a group shares one analysis except for its own part marker.

    Backs have carried ``back`` since long before the plan; negatives carried
    nothing, so the rule the axis promises was half implemented and C1 added the
    other half. The marker is a property of the file, not of the object, so the
    test that matters is not "the negative got one" but "nothing else did".
    """

    def test_each_marker_lands_on_its_own_file_and_on_no_other(self) -> None:
        folder = self.make_folder(
            "box3_026.jpg", "box3_026-back.jpg", "box3_026-negative.jpg"
        )

        for group_by in utils.GROUP_BY_VALUES:
            with self.subTest(group_by=group_by):
                _calls, result, _records = self.run_folder(folder, group_by=group_by)

                self.assertEqual(
                    self.keywords_by_file(result),
                    {
                        "box3_026.jpg": ["family", "PC-R-123"],
                        "box3_026-back.jpg": ["family", "PC-R-123", "back"],
                        "box3_026-negative.jpg": ["family", "PC-R-123", "negative"],
                    },
                    "a part marker describes one file. The print is neither a "
                    "back nor a negative and must carry neither keyword",
                )

    def test_a_negative_alone_still_gets_its_marker(self) -> None:
        # The commonest shape a negative has in an archive is a group of one, and
        # the marker is the only thing distinguishing that record from a print.
        folder = self.make_folder("box3_026-negative.jpg")

        for group_by in utils.GROUP_BY_VALUES:
            with self.subTest(group_by=group_by):
                _calls, result, _records = self.run_folder(folder, group_by=group_by)

                self.assertEqual(
                    self.keywords_by_file(result),
                    {"box3_026-negative.jpg": ["family", "PC-R-123", "negative"]},
                )

    def test_neither_marker_is_duplicated_when_the_file_already_carries_it(self) -> None:
        # The keyword arrives from the file's own metadata, which is merged into
        # the record after the analysis; appending a second copy would propose a
        # redundant catalog write on every re-run.
        cases = (
            ("box3_026-back.jpg", "back"),
            ("box3_026-negative.jpg", "negative"),
        )
        for name, marker in cases:
            for spelling in (marker, marker.title(), marker.upper(), f" {marker} "):
                with self.subTest(name=name, spelling=spelling):
                    folder = self.make_folder(name)
                    items = [
                        {
                            "path": os.path.join(folder, name),
                            "metadata": {"keywords": [spelling, "seaside"]},
                        }
                    ]

                    _calls, result = self.run_items(items)

                    keywords = self.keywords_by_file(result)[name]
                    self.assertEqual(
                        [kw for kw in keywords if kw.strip().lower() == marker],
                        [spelling.strip()],
                        f"{marker!r} was added beside the caller's own spelling",
                    )
                    self.assertIn("seaside", keywords)

    def test_a_hand_tagged_marker_survives_a_group_that_applies_none(self) -> None:
        # A print scanned from a negative and tagged "Negative" in Lightroom,
        # with no negative file in its group. Nothing can have leaked the keyword
        # onto it, so removing it would be proposing the deletion of user data.
        folder = self.make_folder("box3_026.jpg")
        items = [
            {
                "path": os.path.join(folder, "box3_026.jpg"),
                "metadata": {"keywords": ["Negative", "seaside"]},
            }
        ]

        for group_by in utils.GROUP_BY_VALUES:
            with self.subTest(group_by=group_by):
                _calls, result = self.run_items(items, group_by=group_by)

                self.assertIn("Negative", self.keywords_by_file(result)["box3_026.jpg"])


class TestCropsPerValue(_GroupByTestCase):
    """A crop is a supporting view of its parent -- except when it has no parent.

    ``object`` and ``pair`` are unchanged by C1: a crop yields its parent's slot,
    is recorded, warned about and never analyzed. Under ``none`` a crop is its
    own object and must be analyzed, because "recorded but not analyzed" in a
    group of one would mean no record at all, and the stream owes every listed
    file one.
    """

    _CROPS: ClassVar[tuple[str, ...]] = (
        "box3_025.jpg",
        "box3_025-crop.jpg",
        "box3_090-crop.jpg",
    )

    def test_object_and_pair_record_a_crop_without_analyzing_it(self) -> None:
        folder = self.make_folder(*self._CROPS)

        for group_by in (utils.GROUP_BY_OBJECT, utils.GROUP_BY_PAIR):
            with self.subTest(group_by=group_by):
                calls, result, records = self.run_folder(folder, group_by=group_by)

                self.assertEqual(
                    _sent(calls),
                    [("photo", "box3_025.jpg", None), ("photo", "box3_090-crop.jpg", None)],
                    "a crop must yield its parent's slot; the parent is the "
                    "object and the crop is a supporting view of it",
                )
                self.assertIn("box3_025-crop.jpg", self.basenames(result["results"]))
                self.assertIn(
                    "Group 'box3_025': 1 crop file(s) are recorded but not analyzed: "
                    "box3_025-crop.jpg",
                    self.messages(records, logging.WARNING, folder),
                )

    def test_an_orphan_crop_is_analyzed_in_its_missing_parents_place(self) -> None:
        folder = self.make_folder(*self._CROPS)

        for group_by in (utils.GROUP_BY_OBJECT, utils.GROUP_BY_PAIR):
            with self.subTest(group_by=group_by):
                _calls, _result, records = self.run_folder(folder, group_by=group_by)

                self.assertIn(
                    "Group 'box3_090': box3_090-crop.jpg has no uncropped original in "
                    "the manifest; analyzing the crop as the object itself.",
                    self.messages(records, logging.WARNING, folder),
                )

    def test_none_analyzes_every_crop_as_its_own_object(self) -> None:
        folder = self.make_folder(*self._CROPS)

        calls, result, _records = self.run_folder(folder, group_by=utils.GROUP_BY_NONE)

        self.assertEqual(
            _sent(calls),
            [
                ("photo", "box3_025-crop.jpg", None),
                ("photo", "box3_025.jpg", None),
                ("photo", "box3_090-crop.jpg", None),
            ],
            "under --group-by none a crop is its own group, so declining to "
            "analyze it would leave it with no record at all",
        )
        self.assertEqual(self.basenames(result["results"]), sorted(self._CROPS))

    def test_none_does_not_warn_about_crops_it_analyzes_by_design(self) -> None:
        # Both warnings state a surprise. Under ``none`` every crop is an orphan
        # by construction and every crop is analyzed, so both conditions hold on
        # every run and the warnings would be noise rather than information.
        folder = self.make_folder(*self._CROPS)

        _calls, _result, records = self.run_folder(folder, group_by=utils.GROUP_BY_NONE)

        self.assertEqual(
            [
                line
                for line in self.messages(records, logging.WARNING, folder)
                if "crop" in line.lower()
            ],
            [],
        )

    def test_a_crop_of_a_back_yields_the_back_slot_and_keeps_the_back_marker(self) -> None:
        # The crop rule is decided per slot, not per group: this group has an
        # uncropped back, so the crop of it is recorded rather than sent, and it
        # is still a back.
        folder = self.make_folder(
            "box3_025.jpg", "box3_025-back.jpg", "box3_025-back-crop.jpg"
        )

        calls, result, _records = self.run_folder(folder)

        self.assertEqual(_sent(calls), [("photo", "box3_025.jpg", "box3_025-back.jpg")])
        keywords = self.keywords_by_file(result)
        self.assertIn("back", keywords["box3_025-back-crop.jpg"])
        self.assertNotIn("back", keywords["box3_025.jpg"])


if __name__ == "__main__":
    unittest.main()
