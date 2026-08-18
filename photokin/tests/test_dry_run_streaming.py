import json
import unittest
from unittest.mock import patch

from photokin import core, utils


class TestDryRunStreaming(unittest.TestCase):
    def test_process_manifest_stream_emits_usage_and_dry_run_flag(self):
        # process_manifest_stream keys results by utils.normalize_path() output,
        # which is platform-dependent (backslashes on Windows). Derive the key the
        # same way so the fixture matches production keying on every OS.
        photo_path = utils.normalize_path("/photos/family-front.jpg")
        manifest = {
            "items": [
                {
                    "path": photo_path,
                    "metadata": {"caption": "Existing caption"},
                }
            ]
        }
        cfg = utils.Config(dry_run=True)
        lines: list[str] = []

        fake_result = {
            "result": {
                photo_path: {
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

        with patch("photokin.core.analyze_photo", return_value=fake_result), patch(
            "photokin.core.build_canonical_patch",
            return_value=({}, {}),
        ):
            result = core.process_manifest_stream(
                manifest=manifest,
                cfg=cfg,
                ndjson_writer=lines.append,
            )

        self.assertIn(photo_path, result["results"])
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertTrue(record["dry_run"])
        self.assertEqual(record["usage"]["total_tokens"], 1550)
        self.assertEqual(record["usage"]["prompt_tokens"], 1200)
        self.assertEqual(record["usage"]["completion_tokens"], 350)


if __name__ == "__main__":
    unittest.main()
