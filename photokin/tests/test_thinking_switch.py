import unittest

from photokin import api_claude


class _CapturingClient:
    """Stands in for anthropic.Anthropic; records the request payload.

    call_claude_model always streams (client.messages.stream(...) as a
    context manager, then .get_final_message()) rather than calling
    .messages.create() directly -- see api_claude.py's comment on why.
    """

    def __init__(self):
        self.captured = {}
        outer = self

        class _StreamContext:
            def __init__(self, kwargs):
                outer.captured.update(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def get_final_message(self):
                return "resp"

        class _Messages:
            def stream(self, **kwargs):
                return _StreamContext(kwargs)

        self.messages = _Messages()


class TestClaudeThinkingSwitch(unittest.TestCase):
    def _call(self, model: str, **kwargs) -> dict:
        client = _CapturingClient()
        api_claude.call_claude_model(
            client,
            model,
            [{"type": "input_text", "text": "prompt"}],
            [],
            **kwargs,
        )
        return client.captured

    def test_default_payload_unchanged(self):
        payload = self._call("claude-sonnet-4-6")
        self.assertNotIn("thinking", payload)
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(payload["max_tokens"], api_claude.MAX_TOKENS)

    def test_thinking_adaptive_for_sonnet(self):
        payload = self._call("claude-sonnet-4-6", thinking=True)
        self.assertEqual(payload["thinking"], {"type": "adaptive"})
        self.assertEqual(payload["max_tokens"], api_claude.THINKING_MAX_TOKENS)
        # The API rejects temperature overrides when thinking is enabled.
        self.assertNotIn("temperature", payload)

    def test_thinking_budget_for_haiku(self):
        payload = self._call("claude-haiku-4-5-20251001", thinking=True)
        self.assertEqual(payload["thinking"]["type"], "enabled")
        self.assertLess(payload["thinking"]["budget_tokens"], payload["max_tokens"])
        self.assertNotIn("temperature", payload)

    def test_dispatch_passes_thinking_through(self):
        from photokin import api

        client = _CapturingClient()
        api.call_model(
            client,
            "claude-sonnet-4-6",
            [{"type": "input_text", "text": "prompt"}],
            [],
            provider="anthropic",
            thinking=True,
        )
        self.assertEqual(client.captured["thinking"], {"type": "adaptive"})


if __name__ == "__main__":
    unittest.main()
