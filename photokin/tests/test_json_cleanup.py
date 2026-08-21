"""Tests for JSON cleanup and parse_with_retry utilities."""

import json
import os
import sys
import unittest

# Allow running this file directly (python test_json_cleanup.py), where the
# repo root is not on sys.path. This used to bypass the package __init__ via a
# spec_from_file_location copy registered as "photokin.utils" -- but executing
# utils.py imports the photokin package anyway, and every provider SDK import
# is lazy, so the bypass bought nothing. What it cost was real: the duplicate
# module replaced sys.modules["photokin.utils"] for the rest of the pytest
# process, so a later patch("photokin.utils.X") patched the copy while cli and
# core kept calling the original.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PY = os.path.dirname(os.path.dirname(_HERE))
if _PY not in sys.path:
    sys.path.insert(0, _PY)

from photokin.utils import _cleanup_model_json, _extract_json_payload, parse_with_retry


class TestCleanupModelJson(unittest.TestCase):
    """Tests for _cleanup_model_json."""

    def test_valid_json_unchanged(self):
        raw = '{"keywords": ["a", "b"], "title": "test"}'
        self.assertEqual(_cleanup_model_json(raw), raw)

    def test_literal_newline_in_string(self):
        raw = '{"caption": "line1\nline2"}'
        cleaned = _cleanup_model_json(raw)
        self.assertEqual(cleaned, '{"caption": "line1\\nline2"}')
        data = json.loads(cleaned)
        self.assertEqual(data["caption"], "line1\nline2")

    def test_mismatched_brace_closes_array(self):
        # Gemini quirk: } used to close array, with outer } still present
        raw = '{"keywords": ["a", "b"}}'
        cleaned = _cleanup_model_json(raw)
        self.assertIn("]", cleaned)
        data = json.loads(cleaned)
        self.assertEqual(data["keywords"], ["a", "b"])

    def test_mismatched_bracket_closes_object(self):
        raw = '{"key": "value"]'
        cleaned = _cleanup_model_json(raw)
        self.assertIn("}", cleaned)
        data = json.loads(cleaned)
        self.assertEqual(data["key"], "value")

    def test_colon_after_array_close(self):
        """Gemini quirk: `]:` instead of `],`."""
        raw = '{"keywords": ["a", "b"]: "caption": "test"}'
        cleaned = _cleanup_model_json(raw)
        data = json.loads(cleaned)
        self.assertEqual(data["keywords"], ["a", "b"])
        self.assertEqual(data["caption"], "test")

    def test_trailing_comma_in_array(self):
        raw = '{"keywords": ["a", "b",]}'
        cleaned = _cleanup_model_json(raw)
        data = json.loads(cleaned)
        self.assertEqual(data["keywords"], ["a", "b"])

    def test_trailing_comma_in_object(self):
        raw = '{"key": "value", "key2": "value2",}'
        cleaned = _cleanup_model_json(raw)
        data = json.loads(cleaned)
        self.assertEqual(data["key"], "value")
        self.assertEqual(data["key2"], "value2")

    def test_trailing_comma_with_whitespace(self):
        raw = '{"keywords": ["a", "b" ,\n  ]}'
        cleaned = _cleanup_model_json(raw)
        data = json.loads(cleaned)
        self.assertEqual(data["keywords"], ["a", "b"])

    def test_control_chars_in_strings_escaped(self):
        raw = '{"title": "test\x01value"}'
        cleaned = _cleanup_model_json(raw)
        data = json.loads(cleaned)
        self.assertEqual(data["title"], "test\x01value")

    def test_empty_input(self):
        self.assertEqual(_cleanup_model_json(""), "")
        self.assertEqual(_cleanup_model_json(None), None)

    def test_nested_structure_preserved(self):
        raw = json.dumps({
            "result": {
                "file.tif": {
                    "keywords": ["Army", "Gemini Analyzed", "DATE: Y!M!D!"],
                    "caption": "test",
                    "location_guess": {"country": "US", "state": None},
                }
            }
        })
        cleaned = _cleanup_model_json(raw)
        data = json.loads(cleaned)
        self.assertEqual(data["result"]["file.tif"]["keywords"][2], "DATE: Y!M!D!")

    def test_gemini_realistic_malformed(self):
        """Simulate a realistic Gemini output with } closing an array and ]: quirk."""
        raw = (
            '{"result": {"file.tif": {"keywords": ["Army", "Document", '
            '"Gemini gemini-2.5-flash Analyzed", "DATE: Y!M!D!"}: '
            '"caption": "text", "ai_caption": "desc"}}}'
        )
        cleaned = _cleanup_model_json(raw)
        data = json.loads(cleaned)
        self.assertIn("DATE: Y!M!D!", data["result"]["file.tif"]["keywords"])


class TestExtractJsonPayload(unittest.TestCase):
    """Tests for _extract_json_payload."""

    def test_fenced_json_block(self):
        raw = 'Here is the result:\n```json\n{"key": "value"}\n```\nDone.'
        payload = _extract_json_payload(raw)
        self.assertEqual(payload, '{"key": "value"}')

    def test_generic_fenced_block(self):
        raw = '```\n{"key": "value"}\n```'
        payload = _extract_json_payload(raw)
        self.assertEqual(payload, '{"key": "value"}')

    def test_unfenced_with_surrounding_text(self):
        raw = 'Here is the JSON: {"key": "value"} hope this helps!'
        payload = _extract_json_payload(raw)
        self.assertEqual(payload, '{"key": "value"}')

    def test_plain_json(self):
        raw = '{"key": "value"}'
        payload = _extract_json_payload(raw)
        self.assertEqual(payload, '{"key": "value"}')


class TestParseWithRetry(unittest.TestCase):
    """Tests for parse_with_retry."""

    def test_valid_json_parses_immediately(self):
        raw = '{"key": "value"}'
        data, used = parse_with_retry(raw, lambda: "")
        self.assertEqual(data, {"key": "value"})

    def test_cleanup_fixes_issues(self):
        raw = '{"keywords": ["a", "b"}}' # inner } instead of ]
        data, used = parse_with_retry(raw, lambda: "")
        self.assertEqual(data["keywords"], ["a", "b"])

    def test_retries_on_parse_failure(self):
        """If cleanup can't fix the JSON, retry with the model."""
        bad_json = '{"broken": [invalid'
        good_json = '{"fixed": true}'
        retry_called = []

        def retry_fn():
            retry_called.append(True)
            return good_json

        data, used = parse_with_retry(bad_json, retry_fn)
        self.assertTrue(retry_called, "retry_fn should have been called")
        self.assertEqual(data, {"fixed": True})

    def test_retry_on_empty_input(self):
        good_json = '{"key": "value"}'
        data, used = parse_with_retry("", lambda: good_json)
        self.assertEqual(data, {"key": "value"})

    def test_raises_on_all_failures(self):
        bad_json = '{"broken": [invalid'
        with self.assertRaises(json.JSONDecodeError):
            parse_with_retry(bad_json, lambda: '{"also": broken}')

    def test_trailing_comma_fixed(self):
        raw = '{"keywords": ["a", "b",], "title": "test",}'
        data, used = parse_with_retry(raw, lambda: "")
        self.assertEqual(data["keywords"], ["a", "b"])
        self.assertEqual(data["title"], "test")


if __name__ == "__main__":
    unittest.main()
