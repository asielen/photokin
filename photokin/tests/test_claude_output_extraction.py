import unittest
from types import SimpleNamespace

from photokin import api_claude
from photokin.errors import ProviderApiError


def _block(block_type: str, text: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(type=block_type, text=text)


def _response(blocks: list, stop_reason: str | None) -> SimpleNamespace:
    return SimpleNamespace(content=blocks, stop_reason=stop_reason)


class TestExtractClaudeOutputText(unittest.TestCase):
    def test_normal_response_returns_joined_text(self):
        resp = _response([_block("text", "hello world")], stop_reason="end_turn")
        self.assertEqual(api_claude.extract_claude_output_text(resp), "hello world")

    def test_thinking_block_is_skipped(self):
        resp = _response(
            [_block("thinking", "internal reasoning"), _block("text", "the answer")],
            stop_reason="end_turn",
        )
        self.assertEqual(api_claude.extract_claude_output_text(resp), "the answer")

    def test_truncated_with_partial_text_raises_length(self):
        resp = _response([_block("text", "partial ans")], stop_reason="max_tokens")
        with self.assertRaises(ProviderApiError) as ctx:
            api_claude.extract_claude_output_text(resp)
        self.assertEqual(ctx.exception.error_type, "length")

    def test_truncated_while_still_thinking_raises_length_not_json_garbage(self):
        # The bug this guards: budget ran out before any text block existed
        # at all (content is thinking-only) -- must still be reported as a
        # clean truncation, not the response's own repr handed downstream to
        # a JSON parser as if it were the model's answer.
        resp = _response([_block("thinking", "still reasoning...")], stop_reason="max_tokens")
        with self.assertRaises(ProviderApiError) as ctx:
            api_claude.extract_claude_output_text(resp)
        self.assertEqual(ctx.exception.error_type, "length")

    def test_empty_content_not_truncated_falls_back_to_repr(self):
        # Genuinely unexpected shape (no text, no truncation) -- preserves
        # the pre-existing fallback rather than raising something new.
        resp = _response([], stop_reason="end_turn")
        self.assertEqual(api_claude.extract_claude_output_text(resp), str(resp))


if __name__ == "__main__":
    unittest.main()
