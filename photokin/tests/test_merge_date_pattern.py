import os
import subprocess
import sys
import unittest

from photokin.merge import _valid_pattern


class TestValidPattern(unittest.TestCase):
    """Regression tests for a bug where _valid_pattern rejected the "~"
    (best guess) marker even though the prompt spec defines it and real model
    output uses it constantly (e.g. "DATE: Y~" for decade guesses) -- so the
    merge step's fallback DATE: keyword injection silently never fired for
    best-guess dates.
    """

    def test_accepts_best_guess_marker(self):
        for pattern in ("Y~", "Y!M~", "Y~M~", "Y!M!D~", "y~"):
            self.assertTrue(_valid_pattern(pattern), pattern)

    def test_accepts_confident_unknown_and_legacy_markers(self):
        for pattern in ("Y!", "Y?", "Y@", "Y!M!D!", "Y?M!D!", "Y!M@"):
            self.assertTrue(_valid_pattern(pattern), pattern)

    def test_rejects_bogus_strings(self):
        for pattern in ("", "banana", "Y", "M!", "Y!D!M!", "Y!!", "DATE: Y~", None, 7):
            self.assertFalse(_valid_pattern(pattern), repr(pattern))


class TestParseInputLoggingDefault(unittest.TestCase):
    """Regression test for a bug where the MEL_LOG_PARSE_WITH_RETRY env gate
    was commented out and the flag hardcoded True, dumping per-photo parse
    logs into ./debug/ for every user unconditionally.
    """

    def test_defaults_off_without_env_var(self):
        # A fresh interpreter avoids reload/order interactions with other tests.
        env = {k: v for k, v in os.environ.items() if k != "MEL_LOG_PARSE_WITH_RETRY"}
        out = subprocess.check_output(
            [sys.executable, "-c",
             "from photokin import utils; print(utils._LOG_PARSE_INPUTS_ENABLED)"],
            env=env, text=True,
        )
        self.assertEqual(out.strip(), "False")


if __name__ == "__main__":
    unittest.main()
