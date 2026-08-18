import json
import unittest
from unittest.mock import patch

from photokin import core, utils


class TestGroupAwareUsageModel(unittest.TestCase):
    """Regression test for a bug where process_manifest_stream's group-aware
    merge rebuilt _usage with summed token counts but silently dropped the
    "model" key, so every group-analyzed record (multi-variant sets, multi-page
    sets) reported usage.model=None downstream even though the underlying API
    call did return one.

    The group here holds a "b" rescan, which is what routes it through
    ``analyze_group_front_back``: since the primary retired, the callee follows
    the payload shape rather than a flag, and a plain front/back pair takes
    ``analyze_photo`` at every value of ``--group-by``.
    """

    def test_front_back_group_preserves_usage_model(self):
        manifest = {
            "items": [
                {"path": "/photos/family1.jpg"},
                {"path": "/photos/family1-back.jpg"},
                {"path": "/photos/family1b.jpg"},
            ]
        }
        cfg = utils.Config(dry_run=True)
        lines: list[str] = []

        fake_result = {
            "result": {
                "/photos/family1.jpg": {
                    "caption": "Suggested caption",
                    "keywords": ["family", "portrait"],
                    "all_variant_files": ["/photos/family1.jpg", "/photos/family1-back.jpg"],
                    "_usage": {
                        "prompt_tokens": 1200,
                        "completion_tokens": 350,
                        "total_tokens": 1550,
                        "model": "claude-haiku-4-5-20251001",
                    },
                }
            }
        }

        with patch("photokin.core.analyze_group_front_back", return_value=fake_result), patch(
            "photokin.core.build_canonical_patch",
            return_value=({}, {}),
        ):
            core.process_manifest_stream(
                manifest=manifest,
                cfg=cfg,
                ndjson_writer=lines.append,
            )

        self.assertEqual(len(lines), 3)
        records = [json.loads(line) for line in lines]
        for record in records:
            self.assertEqual(record["status"], "ok")
            self.assertEqual(
                record["usage"]["model"], "claude-haiku-4-5-20251001",
                f"usage.model missing for {record['path']} -- the group merge dropped it",
            )
            self.assertEqual(record["usage"]["total_tokens"], 1550)


if __name__ == "__main__":
    unittest.main()
