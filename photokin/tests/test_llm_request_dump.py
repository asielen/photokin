import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from photokin import core, utils


class TestLlmRequestDump(unittest.TestCase):
    def test_build_writer_disabled_returns_none(self):
        cfg = utils.Config(debug_dump_llm_request=False)
        writer = core._build_llm_dump_writer(cfg, "/tmp/photo.jpg", "single")
        self.assertIsNone(writer)

    def test_writer_creates_json_dump(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = utils.Config(
                debug_dump_llm_request=True,
                debug_dump_dir=td,
                run_batch_id="batch123",
            )
            writer = core._build_llm_dump_writer(cfg, "/photos/front.jpg", "single")
            self.assertIsNotNone(writer)
            assert writer is not None

            request_payload = {
                "model": "gpt-4o",
                "temperature": 0,
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
            }
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                writer(request_payload)

            json_path = Path(td) / "batch123_llm_request_front_single.json"
            self.assertTrue(json_path.exists())
            parsed = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["model"], "gpt-4o")
            self.assertIn("Wrote LLM request dump:", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
