import json
import unittest

from photo_archiver import utils


class TestMetadataForwarding(unittest.TestCase):
    def test_prompt_bundle_forwards_required_metadata_and_normalizes_state(self):
        forwarded = {
            "keywords": ["wedding"],
            "title": "Family photo",
            "caption": "At the square",
            "userComment": "Scanned from album",
            "dateTimeOriginal": "1948:05:01 10:23:00",
            "location": "Town Square",
            "city": "Springfield",
            "stateProvince": "Illinois",
            "country": "USA",
            "locationShown": "Springfield, Illinois",
            "gps": {"lat": 39.78, "lon": -89.64},
            "faceTags": {
                "faces": [
                    {"name": "Bob", "center": {"x": 0.42, "y": 0.53}, "width": 0.112, "height": 0.146},
                    {"name": "Alice", "center": {"x": 0.21, "y": 0.50}, "width": 0.101, "height": 0.121},
                ]
            },
            "ignoredField": "not-forwarded",
        }

        bundle = utils.build_prompt_bundle(
            "gpt-4o",
            "2026-01-01",
            forwarded_meta=forwarded,
            forward_fields=["title"],
            cfg=utils.Config(),
        )

        texts = [item["text"] for item in bundle if item.get("type") == "input_text"]
        forwarded_line = next(text for text in texts if text.startswith("Forwarded metadata: "))
        payload = json.loads(forwarded_line[len("Forwarded metadata: "):])

        self.assertIn("state", payload)
        self.assertEqual(payload["state"], "Illinois")
        self.assertIn("faceTags", payload)
        self.assertEqual(payload["userComment"], "Scanned from album")
        self.assertNotIn("ignoredField", payload)

        joined = "\n".join(texts)
        self.assertIn("[FACE TAGS — AUTHORITATIVE]", joined)
        self.assertIn("1. Alice (cx=0.210, cy=0.500, w=0.101, h=0.121)", joined)
        self.assertIn("2. Bob (cx=0.420, cy=0.530, w=0.112, h=0.146)", joined)

    def test_no_faces_omits_authoritative_face_section(self):
        bundle = utils.build_prompt_bundle(
            "gpt-4o",
            "2026-01-01",
            forwarded_meta={"title": "No people", "gps": {"lat": 1, "lon": 2}},
            cfg=utils.Config(),
        )
        texts = [item["text"] for item in bundle if item.get("type") == "input_text"]
        joined = "\n".join(texts)
        self.assertNotIn("[FACE TAGS — AUTHORITATIVE]", joined)
        self.assertIn("Forwarded metadata:", joined)


if __name__ == "__main__":
    unittest.main()
