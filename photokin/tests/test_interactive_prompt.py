"""``_interactive_prompt``: a blank answer, Ctrl+C, or closed stdin all mean
"nothing to run" and exit 0 quietly -- none should raise.

The regression this guards: ``normalize_path("")`` is ``"."`` (the current
directory, via ``os.path.normpath``), which is truthy. A blank front-image
answer used to fall straight through the emptiness check and become
``photokin .`` -- folder input over the current directory, silently, with no
confirmation. And ``input()`` raising ``EOFError`` on closed stdin was
unhandled, so a piped-empty or Ctrl+D session got a Python traceback instead
of the same graceful exit a typed blank line gets.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from photokin import cli


class TestInteractivePromptBlankInput(unittest.TestCase):
    def test_blank_front_exits_zero_not_dot(self):
        """The historical bug: blank input became '.', not a quiet exit."""
        with patch("builtins.input", return_value=""):
            with self.assertRaises(SystemExit) as ctx:
                cli._interactive_prompt()
        self.assertEqual(ctx.exception.code, 0)

    def test_whitespace_only_front_also_exits_zero(self):
        with patch("builtins.input", return_value="   "):
            with self.assertRaises(SystemExit) as ctx:
                cli._interactive_prompt()
        self.assertEqual(ctx.exception.code, 0)

    def test_eof_on_front_prompt_exits_zero_not_traceback(self):
        with patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(SystemExit) as ctx:
                cli._interactive_prompt()
        self.assertEqual(ctx.exception.code, 0)

    def test_ctrl_c_on_front_prompt_exits_zero_not_traceback(self):
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            with self.assertRaises(SystemExit) as ctx:
                cli._interactive_prompt()
        self.assertEqual(ctx.exception.code, 0)


class TestInteractivePromptRealPaths(unittest.TestCase):
    def test_front_only_returns_single_token(self):
        with patch("builtins.input", side_effect=["scan.jpg", ""]):
            extra = cli._interactive_prompt()
        self.assertEqual(len(extra), 1)
        self.assertTrue(extra[0].endswith("scan.jpg"))

    def test_front_and_back_returns_back_flag(self):
        with patch("builtins.input", side_effect=["scan.jpg", "scan-back.jpg"]):
            extra = cli._interactive_prompt()
        self.assertEqual(len(extra), 3)
        self.assertEqual(extra[1], "--back")
        self.assertTrue(extra[2].endswith("scan-back.jpg"))

    def test_blank_back_is_omitted_not_dot(self):
        """The back prompt has the same '.' hazard as the front prompt did."""
        with patch("builtins.input", side_effect=["scan.jpg", ""]):
            extra = cli._interactive_prompt()
        self.assertNotIn("--back", extra)

    def test_eof_on_back_prompt_treated_as_no_back(self):
        with patch("builtins.input", side_effect=["scan.jpg", EOFError()]):
            extra = cli._interactive_prompt()
        self.assertEqual(extra, ["scan.jpg"])

    def test_ctrl_c_on_back_prompt_aborts_entirely_not_front_only(self):
        """Unlike EOF on the same prompt, Ctrl+C means stop -- not "run with
        just the front image". Mirrors Ctrl+C on the front prompt."""
        with patch("builtins.input", side_effect=["scan.jpg", KeyboardInterrupt()]), self.assertRaises(
            SystemExit
        ) as ctx:
            cli._interactive_prompt()
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
