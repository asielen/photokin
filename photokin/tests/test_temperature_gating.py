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


class TestModelSupportsTemperature(unittest.TestCase):
    def test_still_supported(self):
        for model in (
            "claude-opus-4-6",
            "claude-opus-4-5",
            "claude-opus-4-1",
            "claude-opus-4-0",
            "claude-sonnet-4-6",
            "claude-sonnet-4-5",
            "claude-sonnet-4-0",
            "claude-haiku-4-5-20251001",
            "claude-3-opus-20240229",
            "claude-3-5-sonnet-20241022",
        ):
            with self.subTest(model=model):
                self.assertTrue(api_claude._model_supports_temperature(model))

    def test_removed_on_newer_models(self):
        for model in (
            "claude-opus-4-7",
            "claude-opus-4-8",
            "claude-sonnet-5",
            "claude-fable-5",
            "claude-mythos-5",
        ):
            with self.subTest(model=model):
                self.assertFalse(api_claude._model_supports_temperature(model))

    def test_unknown_future_model_defaults_to_unsupported(self):
        # A model this tool doesn't recognize yet -- default to omitting
        # temperature rather than risk a 400 (the API trend is removal).
        self.assertFalse(api_claude._model_supports_temperature("claude-opus-5-0"))
        self.assertFalse(api_claude._model_supports_temperature("claude-sonnet-6"))


class TestTemperatureInRequestPayload(unittest.TestCase):
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

    def test_older_opus_gets_temperature(self):
        self.assertEqual(self._call("claude-opus-4-6")["temperature"], 0)

    def test_newer_opus_omits_temperature(self):
        self.assertNotIn("temperature", self._call("claude-opus-4-8"))
        self.assertNotIn("temperature", self._call("claude-opus-4-7"))

    def test_sonnet_5_omits_temperature(self):
        self.assertNotIn("temperature", self._call("claude-sonnet-5"))

    def test_sonnet_4_6_gets_temperature(self):
        self.assertEqual(self._call("claude-sonnet-4-6")["temperature"], 0)

    def test_fable_5_omits_temperature(self):
        self.assertNotIn("temperature", self._call("claude-fable-5"))

    def test_newer_model_with_thinking_still_omits_temperature(self):
        # Belt-and-suspenders: thinking=True already strips temperature, but
        # a newer model should never have had it in the first place.
        payload = self._call("claude-opus-4-8", thinking=True)
        self.assertNotIn("temperature", payload)
        self.assertEqual(payload["thinking"], {"type": "adaptive"})


if __name__ == "__main__":
    unittest.main()
