import unittest

from photo_archiver import core, utils
from photo_archiver.api_claude import _data_url_to_image_block
from photo_archiver.api_openai import ProviderApiError


class _UsageMetadataStub:
    def __init__(self):
        self.prompt_token_count = 123
        self.candidates_token_count = 45
        self.total_token_count = 168


class _GeminiResponseStub:
    def __init__(self):
        self.usage_metadata = _UsageMetadataStub()



class TestProviderResolution(unittest.TestCase):
    def test_provider_normalization(self):
        self.assertEqual(utils.normalize_provider("ChatGPT"), "openai")
        self.assertEqual(utils.normalize_provider("claude"), "anthropic")
        self.assertEqual(utils.normalize_provider("Gemini"), "gemini")
        self.assertEqual(utils.normalize_provider("google"), "gemini")

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

    def test_normalized_error_payload_for_provider_errors(self):
        err = ProviderApiError("rate_limit", "Too many requests", status_code=429)
        payload = core._normalized_error_payload(err)
        self.assertEqual(payload["type"], "rate_limit")
        self.assertEqual(payload["status_code"], 429)
        self.assertEqual(payload["message"], "Too many requests")

    def test_extract_usage_from_gemini_usage_metadata(self):
        usage = utils.extract_usage(_GeminiResponseStub())
        self.assertIsNotNone(usage)
        self.assertEqual(usage["prompt_tokens"], 123)
        self.assertEqual(usage["completion_tokens"], 45)
        self.assertEqual(usage["input_tokens"], 123)
        self.assertEqual(usage["output_tokens"], 45)
        self.assertEqual(usage["total_tokens"], 168)


if __name__ == "__main__":
    unittest.main()
