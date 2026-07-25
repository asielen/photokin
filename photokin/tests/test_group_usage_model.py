import json
import unittest
from unittest.mock import patch

from photokin import core, utils


class TestGroupAwareUsageModel(unittest.TestCase):
    """Regression test for a bug where process_manifest_stream's group-aware
    merge (cfg.process_all_variants=True) rebuilt _usage with summed token
    counts but silently dropped the "model" key, so every group-analyzed
    record (front/back pairs, multi-page sets) reported usage.model=None
    downstream even though the underlying API call did return one.
    """

    def test_front_back_group_preserves_usage_model(self):
        manifest = {
            "items": [
                {"path": "/photos/family.jpg"},
                {"path": "/photos/family-back.jpg"},
            ]
        }
        cfg = utils.Config(dry_run=True, process_all_variants=True)
        lines: list[str] = []

        fake_result = {
            "result": {
                "/photos/family.jpg": {
                    "caption": "Suggested caption",
                    "keywords": ["family", "portrait"],
                    "all_variant_files": ["/photos/family.jpg", "/photos/family-back.jpg"],
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
                update_policy=core.UPDATE_MERGE_PER_VARIANT,
                ndjson_writer=lines.append,
            )

        self.assertEqual(len(lines), 2)
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
