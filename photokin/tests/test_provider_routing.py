import os
import sys
import types
import unittest
from unittest.mock import patch

from photokin import core, utils
from photokin.api_claude import _data_url_to_image_block
from photokin.api_openai import ProviderApiError
from photokin.api_openai_compat import extract_openai_compat_output_text
from photokin.errors import SELF_EXPLANATORY_ERROR_TYPES


class TestOpenRouterKeyIsolation(unittest.TestCase):
    """OpenRouter uses the OpenAI SDK; passing api_key=None would let it read
    OPENAI_API_KEY and send the wrong provider's secret to OpenRouter."""

    def _fake_openai_module(self):
        calls: dict = {}

        class _OpenAI:
            def __init__(self, **kwargs):
                calls["kwargs"] = kwargs

        mod = types.ModuleType("openai")
        mod.OpenAI = _OpenAI
        return mod, calls

    def test_missing_openrouter_key_raises_without_using_openai_key(self):
        mod, calls = self._fake_openai_module()
        cfg = types.SimpleNamespace(provider="openrouter")
        with patch.dict(sys.modules, {"openai": mod}), patch.dict(
            os.environ, {"OPENAI_API_KEY": "sk-openai"}, clear=False
        ):
            os.environ.pop("OPENROUTER_API_KEY", None)
            with self.assertRaises(RuntimeError):
                core._build_provider_client(cfg)
        # Must not have constructed a client (which would carry the OpenAI key).
        self.assertNotIn("kwargs", calls)

    def test_uses_openrouter_key_not_openai_key(self):
        mod, calls = self._fake_openai_module()
        cfg = types.SimpleNamespace(provider="openrouter")
        env = {"OPENAI_API_KEY": "sk-openai", "OPENROUTER_API_KEY": "or-secret"}
        with patch.dict(sys.modules, {"openai": mod}), patch.dict(os.environ, env, clear=False):
            core._build_provider_client(cfg)
        self.assertEqual(calls["kwargs"]["api_key"], "or-secret")
        self.assertIn("openrouter.ai", calls["kwargs"]["base_url"])


class TestMissingApiKeyErrors(unittest.TestCase):
    """A missing key must fail fast with a clear, actionable ProviderApiError
    naming the exact env var -- not a bare SDK auth error surfacing deep
    inside the first request (what used to happen: client construction
    accepted api_key=None and let the SDK raise its own opaque error)."""

    def test_missing_anthropic_key(self):
        mod = types.ModuleType("anthropic")
        mod.Anthropic = lambda **kw: None
        cfg = types.SimpleNamespace(provider="anthropic")
        with patch.dict(sys.modules, {"anthropic": mod}), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            with self.assertRaises(ProviderApiError) as ctx:
                core._build_provider_client(cfg)
        self.assertEqual(ctx.exception.error_type, "missing_api_key")
        self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))

    def test_missing_gemini_key(self):
        genai_mod = types.ModuleType("google.genai")
        genai_mod.Client = lambda **kw: None
        types_mod = types.ModuleType("google.genai.types")
        types_mod.HttpOptions = lambda **kw: None
        google_mod = types.ModuleType("google")
        google_mod.genai = genai_mod
        cfg = types.SimpleNamespace(provider="gemini")
        with patch.dict(
            sys.modules, {"google": google_mod, "google.genai": genai_mod, "google.genai.types": types_mod}
        ), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_API_KEY", None)
            with self.assertRaises(ProviderApiError) as ctx:
                core._build_provider_client(cfg)
        self.assertEqual(ctx.exception.error_type, "missing_api_key")
        self.assertIn("GEMINI_API_KEY", str(ctx.exception))

    def test_missing_openai_key_default_provider(self):
        mod = types.ModuleType("openai")
        mod.OpenAI = lambda **kw: None
        cfg = types.SimpleNamespace(provider="openai")
        with patch.dict(sys.modules, {"openai": mod}), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            with self.assertRaises(ProviderApiError) as ctx:
                core._build_provider_client(cfg)
        self.assertEqual(ctx.exception.error_type, "missing_api_key")
        self.assertIn("OPENAI_API_KEY", str(ctx.exception))

    def test_missing_openrouter_key_error_type(self):
        mod = types.ModuleType("openai")
        mod.OpenAI = lambda **kw: None
        cfg = types.SimpleNamespace(provider="openrouter")
        with patch.dict(sys.modules, {"openai": mod}), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENROUTER_API_KEY", None)
            with self.assertRaises(ProviderApiError) as ctx:
                core._build_provider_client(cfg)
        self.assertEqual(ctx.exception.error_type, "missing_api_key")
        self.assertIn("OPENROUTER_API_KEY", str(ctx.exception))

    def test_missing_key_and_dependency_types_are_self_explanatory(self):
        # These carry the full, actionable message already -- a traceback
        # would just be noise, so callers (cli.py, manifest streaming) skip it.
        self.assertIn("missing_api_key", SELF_EXPLANATORY_ERROR_TYPES)
        self.assertIn("missing_dependency", SELF_EXPLANATORY_ERROR_TYPES)


class _UsageMetadataStub:
    def __init__(self):
        self.prompt_token_count = 123
        self.candidates_token_count = 45
        self.total_token_count = 168


class _GeminiResponseStub:
    def __init__(self):
        self.usage_metadata = _UsageMetadataStub()


class _ChatCompletionUsageStub:
    def __init__(self):
        self.prompt_tokens = 321
        self.completion_tokens = 54
        self.total_tokens = 375


class _ChatCompletionMessageStub:
    def __init__(self, content):
        self.content = content


class _ChatCompletionChoiceStub:
    def __init__(self, content, finish_reason):
        self.message = _ChatCompletionMessageStub(content)
        self.finish_reason = finish_reason


class _ChatCompletionResponseStub:
    def __init__(self, content, finish_reason="stop"):
        self.choices = [_ChatCompletionChoiceStub(content, finish_reason)]
        self.usage = _ChatCompletionUsageStub()
        self.model = "moonshotai/kimi-k3"



class TestProviderResolution(unittest.TestCase):
    def test_provider_normalization(self):
        self.assertEqual(utils.normalize_provider("ChatGPT"), "openai")
        self.assertEqual(utils.normalize_provider("claude"), "anthropic")
        self.assertEqual(utils.normalize_provider("Gemini"), "gemini")
        self.assertEqual(utils.normalize_provider("google"), "gemini")
        self.assertEqual(utils.normalize_provider("OpenRouter"), "openrouter")

    def test_claude_model_resolution_alias_and_default(self):
        self.assertEqual(utils.resolve_claude_model("sonnet"), "claude-sonnet-4-6")
        self.assertEqual(utils.resolve_claude_model(""), "claude-sonnet-4-6")
        self.assertEqual(utils.resolve_claude_model("claude-haiku-4-5-20251001"), "claude-haiku-4-5-20251001")

    def test_resolve_model_for_provider(self):
        cfg = utils.Config(provider="anthropic", model="gpt-4o", claude_model_name="haiku")
        self.assertEqual(utils.resolve_model_for_provider(cfg), "claude-haiku-4-5-20251001")

        cfg_openai = utils.Config(provider="openai", model="gpt-4o")
        self.assertEqual(utils.resolve_model_for_provider(cfg_openai), "gpt-4o")

        cfg_gemini = utils.Config(provider="gemini", model="gpt-4o", gemini_model_name="gemini-2.5-flash")
        self.assertEqual(utils.resolve_model_for_provider(cfg_gemini), "gemini-2.5-flash")

        cfg_openrouter = utils.Config(provider="openrouter", model="gpt-4o", openrouter_model_name="moonshotai/kimi-k3")
        self.assertEqual(utils.resolve_model_for_provider(cfg_openrouter), "moonshotai/kimi-k3")

        cfg_openrouter_default = utils.Config(provider="openrouter", openrouter_model_name="")
        self.assertEqual(utils.resolve_model_for_provider(cfg_openrouter_default), "moonshotai/kimi-k3")


class TestClaudeImageBlocks(unittest.TestCase):
    def test_data_url_to_image_block(self):
        data_url = "data:image/jpeg;base64,aGVsbG8="
        block = _data_url_to_image_block(data_url)
        self.assertEqual(block["type"], "image")
        self.assertEqual(block["source"]["type"], "base64")
        self.assertEqual(block["source"]["media_type"], "image/jpeg")
        self.assertEqual(block["source"]["data"], "aGVsbG8=")


class TestProviderRuntimeBehavior(unittest.TestCase):
    def test_archival_upload_toggle_by_provider(self):
        self.assertTrue(core._should_run_archival_upload("openai"))
        self.assertFalse(core._should_run_archival_upload("anthropic"))
        self.assertFalse(core._should_run_archival_upload("gemini"))
        self.assertFalse(core._should_run_archival_upload("openrouter"))

    def test_normalized_error_payload_for_provider_errors(self):
        err = ProviderApiError("rate_limit", "Too many requests", status_code=429)
        payload = core._normalized_error_payload(err)
        self.assertEqual(payload["type"], "rate_limit")
        self.assertEqual(payload["status_code"], 429)
        self.assertEqual(payload["message"], "Too many requests")

    def test_extract_compat_output_text_and_length_error(self):
        resp = _ChatCompletionResponseStub("  {\"caption\": \"ok\"}  ", finish_reason="stop")
        self.assertEqual(extract_openai_compat_output_text(resp), "  {\"caption\": \"ok\"}  ")

        truncated = _ChatCompletionResponseStub("partial", finish_reason="length")
        with self.assertRaises(ProviderApiError) as ctx:
            extract_openai_compat_output_text(truncated)
        self.assertEqual(ctx.exception.error_type, "length")

    def test_extract_usage_from_chat_completions_usage(self):
        usage = utils.extract_usage(_ChatCompletionResponseStub("x"))
        self.assertIsNotNone(usage)
        self.assertEqual(usage["prompt_tokens"], 321)
        self.assertEqual(usage["completion_tokens"], 54)
        self.assertEqual(usage["input_tokens"], 321)
        self.assertEqual(usage["output_tokens"], 54)
        self.assertEqual(usage["total_tokens"], 375)
        self.assertEqual(usage["model"], "moonshotai/kimi-k3")

    def test_extract_usage_from_gemini_usage_metadata(self):
        usage = utils.extract_usage(_GeminiResponseStub())
        self.assertIsNotNone(usage)
        self.assertEqual(usage["prompt_tokens"], 123)
        self.assertEqual(usage["completion_tokens"], 45)
        self.assertEqual(usage["input_tokens"], 123)
        self.assertEqual(usage["output_tokens"], 45)
        self.assertEqual(usage["total_tokens"], 168)

    def test_gemini_client_has_a_request_timeout_configured(self):
        # Regression test: google-genai has no default request timeout,
        # observed in practice as generate_content() hanging indefinitely
        # (over an hour, no error, no response) on a genuinely bad call,
        # silently blocking an entire batch run. Anthropic/OpenAI clients
        # ship with sane SDK defaults; Gemini does not, so it must be set
        # explicitly at client construction.
        cfg = utils.Config(provider="gemini")
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            client = core._build_provider_client(cfg)
        http_options = client._api_client._http_options
        self.assertIsNotNone(http_options.timeout)
        self.assertGreater(http_options.timeout, 0)


if __name__ == "__main__":
    unittest.main()
