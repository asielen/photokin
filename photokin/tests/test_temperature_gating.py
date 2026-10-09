import unittest

import anthropic
import httpx

from photokin import api_claude
from photokin.errors import ProviderApiError


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
            "claude-haiku-5-5",
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


class _StreamContext:
    def __init__(self, kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get_final_message(self):
        return "resp"


class _AlwaysRaisesClient:
    """Every ``stream()`` call raises the same ``TypeError``, whatever the payload."""

    def __init__(self, message: str):
        self.calls: list = []
        self._message = message
        self.messages = self

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        raise TypeError(self._message)


class _RejectsTemperatureClient:
    """Stands in for an installed SDK whose generated ``messages.stream()``
    no longer accepts ``temperature`` in its signature at all -- a
    client-side ``TypeError`` raised before any request is sent, not the
    server-side 400 ``_model_supports_temperature``'s naming heuristic
    exists to avoid."""

    def __init__(self):
        self.calls: list = []
        self.messages = self

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        if "temperature" in kwargs:
            raise TypeError("Messages.stream() got an unexpected keyword argument 'temperature'")
        return _StreamContext(kwargs)


class _FakeMessage:
    def __init__(self, stop_reason):
        self.stop_reason = stop_reason


class _FinalMessageStreamContext:
    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get_final_message(self):
        return self._message


class _TruncatesThenSucceedsClient:
    """Truncates (``stop_reason="max_tokens"``) at whatever budget it is
    given, until that budget reaches ``THINKING_MAX_TOKENS`` -- standing in
    for a response that ran out of room mid-reasoning at the base budget but
    would have finished cleanly with more of it."""

    def __init__(self):
        self.calls: list = []
        self.messages = self

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        stop_reason = (
            "end_turn" if kwargs["max_tokens"] >= api_claude.THINKING_MAX_TOKENS else "max_tokens"
        )
        return _FinalMessageStreamContext(_FakeMessage(stop_reason))


class _AlwaysTruncatesClient:
    """Truncates at every budget offered, including the escalated retry."""

    def __init__(self):
        self.calls: list = []
        self.messages = self

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return _FinalMessageStreamContext(_FakeMessage("max_tokens"))


class TestMaxTokensRetry(unittest.TestCase):
    """A response that runs out of budget -- possibly mid-reasoning, before
    any answer text exists, per MAX_TOKENS' own comment -- gets one retry at
    THINKING_MAX_TOKENS before call_claude_model gives up."""

    def test_retries_once_at_thinking_ceiling_and_succeeds(self):
        client = _TruncatesThenSucceedsClient()
        result = api_claude.call_claude_model(
            client,
            "claude-sonnet-5",
            [{"type": "input_text", "text": "prompt"}],
            [],
        )
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0]["max_tokens"], api_claude.MAX_TOKENS)
        self.assertEqual(client.calls[1]["max_tokens"], api_claude.THINKING_MAX_TOKENS)

    def test_gives_up_after_one_retry_rather_than_looping(self):
        client = _AlwaysTruncatesClient()
        result = api_claude.call_claude_model(
            client,
            "claude-sonnet-5",
            [{"type": "input_text", "text": "prompt"}],
            [],
        )
        self.assertEqual(result.stop_reason, "max_tokens")
        self.assertEqual(len(client.calls), 2)

    def test_thinking_path_already_at_ceiling_does_not_retry(self):
        # thinking=True already requests THINKING_MAX_TOKENS -- nothing
        # higher to escalate to, so a truncation there must not loop.
        client = _AlwaysTruncatesClient()
        result = api_claude.call_claude_model(
            client,
            "claude-sonnet-5",
            [{"type": "input_text", "text": "prompt"}],
            [],
            thinking=True,
        )
        self.assertEqual(result.stop_reason, "max_tokens")
        self.assertEqual(len(client.calls), 1)


class TestUnsupportedTemperatureTypeErrorRetry(unittest.TestCase):
    """A ``temperature`` kwarg the installed SDK's ``stream()`` rejects
    outright -- unlike a server 400, this is a Python-level ``TypeError``
    the naming heuristic in ``_model_supports_temperature`` cannot predict,
    since it depends on the installed SDK version rather than the model."""

    def test_retries_once_without_temperature(self):
        client = _RejectsTemperatureClient()
        result = api_claude.call_claude_model(
            client,
            "claude-sonnet-4-6",
            [{"type": "input_text", "text": "prompt"}],
            [],
        )
        self.assertEqual(result, "resp")
        self.assertEqual(len(client.calls), 2)
        self.assertIn("temperature", client.calls[0])
        self.assertNotIn("temperature", client.calls[1])

    def test_unrelated_typeerror_is_not_swallowed(self):
        client = _AlwaysRaisesClient("boom")
        with self.assertRaises(TypeError):
            api_claude.call_claude_model(
                client,
                "claude-sonnet-4-6",
                [{"type": "input_text", "text": "prompt"}],
                [],
            )
        self.assertEqual(len(client.calls), 1)

    def test_no_retry_when_temperature_was_never_sent(self):
        # claude-opus-4-8 already omits temperature (see
        # TestModelSupportsTemperature.test_removed_on_newer_models); a
        # TypeError naming it anyway must be a real bug, not something to
        # paper over with a pointless retry.
        client = _AlwaysRaisesClient(
            "Messages.stream() got an unexpected keyword argument 'temperature'"
        )
        with self.assertRaises(TypeError):
            api_claude.call_claude_model(
                client,
                "claude-opus-4-8",
                [{"type": "input_text", "text": "prompt"}],
                [],
            )
        self.assertEqual(len(client.calls), 1)


def _bad_request(message: str) -> anthropic.BadRequestError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    return anthropic.BadRequestError(
        f"Error code: 400 - {body}", response=httpx.Response(400, request=request), body=body
    )


class _ServerRejectsClient:
    """Stands in for the API returning a 400 for any request ``rejects`` flags,
    the way Haiku 5.5 does for ``temperature`` and manual thinking budgets."""

    def __init__(self, rejects):
        self.calls: list = []
        self.messages = self
        self._rejects = rejects

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        message = self._rejects(kwargs)
        if message:
            raise _bad_request(message)
        return _FinalMessageStreamContext(_FakeMessage("end_turn"))


def _rejects_temperature(kwargs):
    return "`temperature` is deprecated for this model." if "temperature" in kwargs else None


def _rejects_manual_thinking(kwargs):
    if kwargs.get("thinking", {}).get("type") == "enabled":
        return '"thinking.type.enabled" is not supported for this model.'
    return None


class TestServerRejectedParamRetry(unittest.TestCase):
    """A 400 for a parameter the naming heuristics wrongly sent gets one
    retry without it, instead of failing every request for that model."""

    def _call(self, client, model, **kwargs):
        return api_claude.call_claude_model(
            client, model, [{"type": "input_text", "text": "prompt"}], [], **kwargs
        )

    def test_rejected_temperature_is_dropped(self):
        client = _ServerRejectsClient(_rejects_temperature)
        result = self._call(client, "claude-sonnet-4-6")
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(len(client.calls), 2)
        self.assertNotIn("temperature", client.calls[1])

    def test_rejected_manual_thinking_switches_to_adaptive(self):
        client = _ServerRejectsClient(_rejects_manual_thinking)
        result = self._call(client, "claude-haiku-4-5-20251001", thinking=True)
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[1]["thinking"], {"type": "adaptive"})

    def test_unrelated_400_is_raised_without_retry(self):
        client = _ServerRejectsClient(lambda kwargs: "messages: field required")
        with self.assertRaises(ProviderApiError) as ctx:
            self._call(client, "claude-sonnet-4-6")
        self.assertEqual(ctx.exception.error_type, "invalid_input")
        self.assertEqual(len(client.calls), 1)

    def test_repeated_rejection_is_raised_not_looped(self):
        # A 400 that names temperature even after it was dropped must surface.
        client = _ServerRejectsClient(lambda kwargs: "`temperature` is deprecated for this model.")
        with self.assertRaises(ProviderApiError):
            self._call(client, "claude-sonnet-4-6")
        self.assertEqual(len(client.calls), 2)

    def test_max_tokens_escalation_keeps_the_fix(self):
        calls: list = []

        class _Client:
            messages = None

            def stream(self, **kwargs):
                calls.append(kwargs)
                message = _rejects_temperature(kwargs)
                if message:
                    raise _bad_request(message)
                done = kwargs["max_tokens"] >= api_claude.THINKING_MAX_TOKENS
                return _FinalMessageStreamContext(_FakeMessage("end_turn" if done else "max_tokens"))

        client = _Client()
        client.messages = client
        result = self._call(client, "claude-sonnet-4-6")
        self.assertEqual(result.stop_reason, "end_turn")
        # rejected, truncated, then escalated without re-sending temperature
        self.assertEqual(len(calls), 3)
        self.assertNotIn("temperature", calls[2])


if __name__ == "__main__":
    unittest.main()
