import json
import unittest
from unittest.mock import patch

from photo_archiver import core, utils


class TestDryRunStreaming(unittest.TestCase):
    def test_process_manifest_stream_emits_usage_and_dry_run_flag(self):
        manifest = {
            "items": [
                {
                    "path": "/photos/family-front.jpg",
                    "metadata": {"caption": "Existing caption"},
                }
            ]
        }
        cfg = utils.Config(dry_run=True)
        lines: list[str] = []

        fake_result = {
            "result": {
                "/photos/family-front.jpg": {
                    "caption": "Suggested caption",
                    "keywords": ["family", "portrait"],
                    "_usage": {
                        "prompt_tokens": 1200,
                        "completion_tokens": 350,
                        "total_tokens": 1550,
                    },
                }
            }
        }

        with patch("photo_archiver.core.analyze_photo", return_value=fake_result), patch(
            "photo_archiver.core.build_canonical_patch",
            return_value=({}, {}),
        ):
            result = core.process_manifest_stream(
                manifest=manifest,
                cfg=cfg,
                update_policy=core.UPDATE_MASTER_EXACT,
                ndjson_writer=lines.append,
            )

        self.assertIn("/photos/family-front.jpg", result["results"])
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertTrue(record["dry_run"])
        self.assertEqual(record["usage"]["total_tokens"], 1550)
        self.assertEqual(record["usage"]["prompt_tokens"], 1200)
        self.assertEqual(record["usage"]["completion_tokens"], 350)


if __name__ == "__main__":
    unittest.main()
