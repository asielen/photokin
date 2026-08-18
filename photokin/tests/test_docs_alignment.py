"""Guards that the documented CLI surface matches the one argparse actually defines.

Flag documentation drifts silently: adding an argument is easy to remember,
deleting a row from a table is easy to forget, and neither shows up in a normal
test run because nothing executes the README. These cases fail instead.

The parser is read with :mod:`ast` rather than by importing and invoking
``cli.main``: the parser is built inside ``main`` alongside side effects such as
installing a log handler and reading ``sys.argv``, so constructing it for real
would mean running the CLI. Parsing the source needs neither.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE = _REPO_ROOT / "photokin"

#: photokin ships several command lines, not one: the main ``photokin`` CLI plus
#: the ExifTool wrapper's own applier, fetcher and manifest tools. Scanning only
#: ``cli.py`` would report the wrapper's flags as documentation for something
#: that does not exist.
_ENTRY_POINTS = (
    _PACKAGE / "cli.py",
    _PACKAGE / "exiftool" / "apply.py",
    _PACKAGE / "exiftool" / "fetch.py",
    _PACKAGE / "exiftool" / "manifest.py",
)

#: Correspondingly, the flags are spread across three READMEs. A flag counts as
#: documented if any of them mentions it; which file is the right home is an
#: editorial question these cases deliberately do not litigate.
_DOCS = (
    _REPO_ROOT / "README.md",
    _PACKAGE / "README.md",
    _PACKAGE / "exiftool" / "README.md",
)

#: Flags the documentation mentions that belong to other programs. The Quick
#: Start tells a new user to run ``python --version``; that is not a photokin
#: flag and must not be read as one.
_FOREIGN_FLAGS = frozenset({"--version"})

#: Flags accepted purely so an external caller does not hard-crash on them.
#: argparse exits 2 on an unrecognized argument, so a retired flag stays
#: registered while doing nothing. They are exempt from requiring a table row,
#: but :meth:`TestDocumentedFlagsExist` still holds them to existing if the
#: README does describe them.
_RETIRED_BUT_ACCEPTED = frozenset({"--process-all-variants", "--update-policy"})


def _defined_flags() -> set[str]:
    """Return every long option any photokin entry point registers with argparse.

    Returns:
        Long-form flag strings, each including its leading ``--``.
    """
    flags: set[str] = set()
    for source in _ENTRY_POINTS:
        if not source.is_file():
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if arg.value.startswith("--"):
                        flags.add(arg.value)
    return flags


def _documented_flags() -> set[str]:
    """Return every long option the READMEs mention, in prose or in a code block.

    Matching is on the bare token rather than on inline code spans: several flags
    are only ever shown inside fenced example commands, and an inline-span-only
    scan reported those as undocumented.

    The trailing ``\\*?`` deliberately swallows the prose wildcard so that
    ``--exiftool-*`` is discarded whole. Excluding the asterisk instead would
    let the pattern backtrack off the hyphen and yield ``--exiftool``, a flag
    that does not exist, which is precisely the false positive being avoided.

    Returns:
        Long-form flag strings found anywhere in the documentation, excluding
        prose wildcards and flags belonging to other programs.
    """
    flags: set[str] = set()
    for doc in _DOCS:
        if not doc.is_file():
            continue
        text = doc.read_text(encoding="utf-8")
        flags.update(
            token
            for token in re.findall(r"(?<![\w-])(--[a-z0-9][a-z0-9-]*\*?)", text)
            if not token.endswith(("*", "-"))
        )
    return flags - _FOREIGN_FLAGS


class TestEveryFlagIsDocumented(unittest.TestCase):
    """Every argparse flag must appear in the README."""

    def test_no_flag_ships_undocumented(self) -> None:
        """A flag added without a README entry fails here."""
        undocumented = sorted(_defined_flags() - _documented_flags() - _RETIRED_BUT_ACCEPTED)
        self.assertEqual(
            undocumented,
            [],
            "these flags exist in an entry point but no README mentions them, so a user "
            "cannot discover them; add a row to the flag table",
        )


class TestDocumentedFlagsExist(unittest.TestCase):
    """The README must not describe flags the CLI no longer accepts."""

    def test_no_documented_flag_has_been_removed(self) -> None:
        """A README row left behind after a flag was deleted fails here."""
        stale = sorted(_documented_flags() - _defined_flags())
        self.assertEqual(
            stale,
            [],
            "README.md documents these flags but argparse no longer defines them, so the "
            "documented invocation would exit 2; remove or correct the entry",
        )


if __name__ == "__main__":
    unittest.main()
