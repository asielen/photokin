"""Holds every user-facing error message to a readable shape.

These messages grow. Each one is edited alone, by someone who has the whole
problem in their head and wants to be helpful, and a clause gets added; nothing
compares the result against its neighbours, so the corpus drifts one message at
a time until a Windows path is sitting in the middle of a subordinate clause.
That is what happened here: the median problem line was 77 characters while the
worst four had reached 160.

So the shape is asserted rather than agreed. Every rule below is a measurement
that was true of the good messages already, applied to all of them.

The messages are rendered with realistic arguments rather than read as source:
an f-string looks short in an editor and is long once a path is interpolated,
and the reader only ever sees the second one.
"""
from __future__ import annotations

import inspect
import unittest

from photokin import cli_messages

#: A long Windows path, because that is what makes a message unreadable in
#: practice and what a developer writing on a short sample never sees.
_SAMPLE_PATH = r"C:\Users\YourName\Pictures\Family Scans 1948\box3_017-back.jpg"

#: One realistic value per parameter name used across the module.
_SAMPLE_ARGS: dict[str, object] = {
    "path": _SAMPLE_PATH,
    "display": _SAMPLE_PATH,
    "out_path": _SAMPLE_PATH,
    "alias_value": _SAMPLE_PATH,
    "configured_path": r"C:\Users\YourName\.photokin\bin\exiftool.exe",
    "value": "XMP:dc:Description",
    "field": "XMP:dc:Description",
    "bad": "XMP:dc:Description",
    "good": "XMP-dc:Description",
    "flag": "--back",
    "alias_flag": "--folder",
    "role": "--output-file",
    "kind_label": "a folder",
    "reason": "it is a directory",
    "detail": "line 3 column 8: expecting ',' delimiter",
    "extension": ".txt",
    "first": "--folder",
    "second": "--manifest",
    "index": 3,
    "tokens": ["photokin", r"C:\Users\YourName\Pictures\Scans", "-rw"],
}

#: The longest a problem line may be before it stops being skimmable. Set from
#: the corpus as it stands rather than from taste: the median is 77 and the
#: longest well-shaped message is under 110, so 120 leaves room to write a
#: normal sentence and none to bury a path inside one.
_MAX_PROBLEM_LINE = 120

#: Messages whose problem legitimately spans more than one line, by name. A path
#: on its own indented line is the fix for a long message, not a violation of
#: it, but it has to be declared so a message does not sprawl by accident.
_MULTILINE_PROBLEMS = frozenset(
    {
        "exiftool_not_found",
        "exiftool_not_found_for_read",
        "output_destination_not_writable",
    }
)


def _rendered_messages() -> dict[str, tuple[str, str]]:
    """Render every ``(problem, remedy)`` builder with realistic arguments.

    Returns:
        The rendered pair for each public builder that takes arguments this
        module knows how to supply, keyed by function name.
    """
    out: dict[str, tuple[str, str]] = {}
    for name, fn in vars(cli_messages).items():
        if name.startswith("_") or not inspect.isfunction(fn):
            continue
        if fn.__module__ != cli_messages.__name__:
            continue
        try:
            kwargs = {}
            for param, spec in inspect.signature(fn).parameters.items():
                if param in _SAMPLE_ARGS:
                    kwargs[param] = _SAMPLE_ARGS[param]
                elif spec.default is inspect.Parameter.empty:
                    raise KeyError(param)
            result = fn(**kwargs)
        except (KeyError, TypeError, ValueError):
            continue
        if (
            isinstance(result, tuple)
            and len(result) == 2
            and all(isinstance(part, str) for part in result)
        ):
            out[name] = result
    return out


class TestEveryMessageIsRendered(unittest.TestCase):
    """The sweep below is worthless if it silently covers nothing."""

    def test_the_corpus_is_not_empty(self) -> None:
        """A signature change that defeats the renderer must not read as a pass."""
        rendered = _rendered_messages()
        self.assertGreaterEqual(
            len(rendered),
            25,
            "the renderer stopped being able to call most builders, so the style "
            "rules below are now asserting almost nothing; teach _SAMPLE_ARGS the "
            "new parameter rather than letting the sweep shrink",
        )

    def test_the_sample_path_really_is_interpolated_somewhere(self) -> None:
        """Non-vacuity: at least one message shows a real path, as a user sees it."""
        rendered = _rendered_messages()
        self.assertTrue(
            any(_SAMPLE_PATH in problem for problem, _ in rendered.values()),
            "no message interpolated the sample path, so the length rule is being "
            "checked against messages shorter than the ones users actually get",
        )


class TestProblemLinesStaySkimmable(unittest.TestCase):
    """One short sentence naming what is wrong."""

    def test_no_problem_line_runs_past_the_limit(self) -> None:
        """A message that has grown a second clause fails here."""
        for name, (problem, _remedy) in sorted(_rendered_messages().items()):
            for line in problem.splitlines():
                with self.subTest(message=name):
                    self.assertLessEqual(
                        len(line),
                        _MAX_PROBLEM_LINE,
                        f"{name} renders a {len(line)}-character line. Put a long "
                        f"path on its own indented line, and move the explanation "
                        f"of why it matters into the docstring:\n    {line}",
                    )

    def test_only_declared_messages_span_several_lines(self) -> None:
        """A problem grows a second line on purpose or not at all."""
        for name, (problem, _remedy) in sorted(_rendered_messages().items()):
            if len(problem.splitlines()) > 1:
                with self.subTest(message=name):
                    self.assertIn(
                        name,
                        _MULTILINE_PROBLEMS,
                        f"{name} now renders more than one line; if that is a long "
                        f"value on its own line, add it to _MULTILINE_PROBLEMS",
                    )

    # A rule about where in the sentence a path may sit was tried here and
    # removed. Measured across the corpus, the text after a path is the natural
    # predicate -- "is not valid JSON: ...", "has an empty `items` list" -- and
    # reads perfectly well; the tails run 0 to 54 characters with no bad case
    # among them. What actually made a message unreadable was a long lead-in and
    # a path and a trailing clause all on one line, which is just a long line,
    # and the rule above already catches it. A second rule would only have
    # forced rewrites of messages that were fine.


class TestRemediesAreActionable(unittest.TestCase):
    """The ``Try:`` line names what to do, in one line."""

    def test_every_remedy_is_a_single_line(self) -> None:
        for name, (_problem, remedy) in sorted(_rendered_messages().items()):
            with self.subTest(message=name):
                self.assertEqual(
                    len(remedy.splitlines()),
                    1,
                    f"{name}'s remedy spans several lines; one action per message",
                )

    def test_no_remedy_starts_with_a_capital_or_ends_with_a_period(self) -> None:
        """One house style, so the corpus reads as one voice.

        Rendered as ``Try: <remedy>``, so the remedy is a continuation and takes
        neither a leading capital nor a closing period. Both were drifting.
        """
        for name, (_problem, remedy) in sorted(_rendered_messages().items()):
            with self.subTest(message=name):
                self.assertFalse(
                    remedy.endswith("."),
                    f"{name}'s remedy ends with a period; the others do not",
                )
                first = remedy.lstrip("`")[:1]
                self.assertFalse(
                    first.isupper(),
                    f"{name}'s remedy starts with a capital; it continues 'Try: '",
                )


if __name__ == "__main__":
    unittest.main()
