import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from photo_archiver import utils


class TestPhotoContext(unittest.TestCase):
    def test_cli_text_overrides_everything(self):
        with tempfile.TemporaryDirectory() as td:
            context_file = Path(td) / "ctx.txt"
            context_file.write_text("from-file", encoding="utf-8")
            manifest = {
                "photo_context_text": "from-manifest-text",
                "photo_context_path": str(context_file),
            }

            resolved = utils.resolve_photo_context(
                cli_text="from-cli-text",
                cli_file=str(context_file),
                manifest=manifest,
            )

        self.assertEqual(resolved, "from-cli-text")

    def test_cli_file_overrides_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            cli_file = Path(td) / "cli.txt"
            cli_file.write_text("cli-file", encoding="utf-8")
            manifest_file = Path(td) / "manifest.txt"
            manifest_file.write_text("manifest-file", encoding="utf-8")
            manifest = {
                "photo_context_text": "manifest-text",
                "photo_context_path": str(manifest_file),
            }

            resolved = utils.resolve_photo_context(
                cli_text=None,
                cli_file=str(cli_file),
                manifest=manifest,
            )

        self.assertEqual(resolved, "cli-file")

    def test_manifest_prefers_text_over_path(self):
        with tempfile.TemporaryDirectory() as td:
            context_file = Path(td) / "ctx.txt"
            context_file.write_text("from-file", encoding="utf-8")
            manifest = {
                "photo_context_text": "from-text",
                "photo_context_path": str(context_file),
            }

            resolved = utils.resolve_photo_context(
                cli_text=None,
                cli_file=None,
                manifest=manifest,
            )

        self.assertEqual(resolved, "from-text")

    def test_unreadable_file_warns_and_returns_none(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            resolved = utils.resolve_photo_context(
                cli_text=None,
                cli_file="/tmp/definitely_missing_context_file_12345.txt",
                manifest=None,
            )

        self.assertIsNone(resolved)
        self.assertIn("Unable to read photo context file", stderr.getvalue())

    def test_truncates_over_limit(self):
        huge_text = "a" * (utils.MAX_PHOTO_CONTEXT_BYTES + 17)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            resolved = utils.resolve_photo_context(
                cli_text=huge_text,
                cli_file=None,
                manifest=None,
            )

        self.assertIsNotNone(resolved)
        assert resolved is not None
        self.assertEqual(len(resolved.encode("utf-8")), utils.MAX_PHOTO_CONTEXT_BYTES)
        self.assertIn("was truncated", stderr.getvalue())

    def test_prompt_injection_has_authoritative_block(self):
        cfg = utils.Config(photo_context_text="Known family notes")
        bundle = utils.build_prompt_bundle("gpt-4o", "2026-01-01", cfg=cfg)
        texts = [item["text"] for item in bundle if item.get("type") == "input_text"]
        joined = "\n".join(texts)
        self.assertIn("[PHOTO CONTEXT — AUTHORITATIVE]\nKnown family notes", joined)


if __name__ == "__main__":
    unittest.main()
