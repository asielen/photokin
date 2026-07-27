import tempfile
import unittest
from pathlib import Path

from photokin import utils


class TestPromptBundleIncludesGuardrailsAndVocab(unittest.TestCase):
    """Regression tests for a gap where the assembled prompt bundle never
    included forbidden_inferences.txt or the vocabulary TOML, even though the
    other rule fragments tell the model to use "the provided preferred
    vocabulary" and to comply with the forbidden-inference list.
    """

    def _texts(self, bundle):
        return [item["text"] for item in bundle if item.get("type") == "input_text"]

    def test_bundle_contains_forbidden_inferences_and_vocab(self):
        bundle = utils.build_prompt_bundle(
            "gpt-4o", "2026-01-01", provider_name="ChatGPT", cfg=utils.Config()
        )
        joined = "\n".join(self._texts(bundle))

        self.assertIn("FORBIDDEN INFERENCES", joined)
        self.assertIn("PREFERRED VOCABULARY (TOML)", joined)
        self.assertIn("[people_subjects]", joined)
        self.assertIn("[date_reference]", joined)
        # Placeholders in forbidden_inferences.txt must be substituted.
        self.assertIn("ChatGPT gpt-4o Analyzed", joined)
        self.assertNotIn("{{PROVIDER_NAME}}", joined)
        self.assertNotIn("{{MODEL_NAME}}", joined)

    def test_guardrails_ordered_between_categories_and_output_format(self):
        bundle = utils.build_prompt_bundle("gpt-4o", "2026-01-01", cfg=utils.Config())
        texts = self._texts(bundle)

        idx_categories = next(i for i, t in enumerate(texts) if t.startswith("CHOOSE EXACTLY ONE CATEGORY"))
        idx_forbidden = next(i for i, t in enumerate(texts) if t.startswith("FORBIDDEN INFERENCES"))
        idx_vocab = next(i for i, t in enumerate(texts) if t.startswith("PREFERRED VOCABULARY"))
        idx_output = next(i for i, t in enumerate(texts) if t.startswith("OUTPUT FORMAT"))

        self.assertLess(idx_categories, idx_forbidden)
        self.assertLess(idx_forbidden, idx_vocab)
        self.assertLess(idx_vocab, idx_output)

    def test_custom_vocab_and_forbidden_paths_are_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            vocab = Path(tmp) / "vocab.toml"
            vocab.write_text('[people_subjects]\nkeywords = ["Zebra handler"]\n', encoding="utf-8")
            forbidden = Path(tmp) / "forbidden.txt"
            forbidden.write_text("CUSTOM FORBIDDEN RULES for {{MODEL_NAME}}", encoding="utf-8")

            cfg = utils.Config(vocab_path=str(vocab), forbidden_path=str(forbidden))
            bundle = utils.build_prompt_bundle("gpt-4o", "2026-01-01", cfg=cfg)
            joined = "\n".join(self._texts(bundle))

        self.assertIn("Zebra handler", joined)
        self.assertIn("CUSTOM FORBIDDEN RULES for gpt-4o", joined)


if __name__ == "__main__":
    unittest.main()
