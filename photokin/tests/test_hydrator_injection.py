import unittest
from unittest.mock import patch

from photo_archiver import core, utils
from photo_archiver.exiftool import ExiftoolConfig
from photo_archiver.exiftool.hydrate import hydrate_user_comments, make_manifest_hydrator


class TestHydratorInjection(unittest.TestCase):
    def test_process_manifest_stream_calls_injected_hydrator(self):
        manifest = {
            "items": [
                {
                    "path": "/photos/family-front.jpg",
                    "metadata": {"caption": "Existing caption"},
                }
            ]
        }
        cfg = utils.Config(dry_run=True)
        calls: list[list[dict]] = []

        fake_result = {
            "result": {
                "/photos/family-front.jpg": {
                    "caption": "Suggested caption",
                    "keywords": ["family"],
                }
            }
        }

        with patch("photo_archiver.core.analyze_photo", return_value=fake_result), patch(
            "photo_archiver.core.build_canonical_patch",
            return_value=({}, {}),
        ):
            core.process_manifest_stream(
                manifest=manifest,
                cfg=cfg,
                update_policy=core.UPDATE_MASTER_EXACT,
                metadata_hydrator=calls.append,
            )

        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0], manifest["items"])

    def test_no_hydrator_is_fine(self):
        manifest = {"items": []}
        cfg = utils.Config(dry_run=True)
        result = core.process_manifest_stream(manifest=manifest, cfg=cfg)
        self.assertEqual(result, {"results": {}})


class TestHydrateUserComments(unittest.TestCase):
    def _items(self):
        return [
            {"path": "/photos/a.jpg", "metadata": {"userComment": ""}},
            {"path": "/photos/b.jpg", "metadata": {"userComment": "Keep me"}},
            {"path": "/photos/c.jpg", "metadata": {}},
        ]

    def test_fills_only_missing_user_comments(self):
        items = self._items()
        records = [
            {"SourceFile": "/photos/a.jpg", "EXIF:UserComment": "From file A"},
            {"SourceFile": "/photos/b.jpg", "EXIF:UserComment": "Should not apply"},
            {"SourceFile": "/photos/c.jpg", "EXIF:UserComment": "From file C"},
        ]
        with patch(
            "photo_archiver.exiftool.hydrate.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch("mel_exiftool_manifest.run_exiftool_json", return_value=records) as run_mock:
            hydrate_user_comments(items, ExiftoolConfig())

        self.assertEqual(items[0]["metadata"]["userComment"], "From file A")
        self.assertEqual(items[1]["metadata"]["userComment"], "Keep me")
        self.assertEqual(items[2]["metadata"]["userComment"], "From file C")
        # Only the items missing a userComment are queried.
        queried = run_mock.call_args.kwargs["files"]
        self.assertEqual(sorted(queried), ["/photos/a.jpg", "/photos/c.jpg"])

    def test_noop_when_binary_missing(self):
        items = self._items()
        with patch(
            "photo_archiver.exiftool.hydrate.resolve_exiftool_path",
            side_effect=FileNotFoundError("not found"),
        ):
            hydrate_user_comments(items, ExiftoolConfig())
        self.assertEqual(items[0]["metadata"]["userComment"], "")

    def test_noop_when_exiftool_read_fails(self):
        items = self._items()
        with patch(
            "photo_archiver.exiftool.hydrate.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch(
            "mel_exiftool_manifest.run_exiftool_json",
            side_effect=RuntimeError("boom"),
        ):
            hydrate_user_comments(items, ExiftoolConfig())
        self.assertEqual(items[0]["metadata"]["userComment"], "")

    def test_make_manifest_hydrator_wraps_config(self):
        items = self._items()
        hydrator = make_manifest_hydrator(ExiftoolConfig())
        with patch(
            "photo_archiver.exiftool.hydrate.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch(
            "mel_exiftool_manifest.run_exiftool_json",
            return_value=[{"SourceFile": "/photos/a.jpg", "EXIF:UserComment": "Filled"}],
        ):
            hydrator(items)
        self.assertEqual(items[0]["metadata"]["userComment"], "Filled")


if __name__ == "__main__":
    unittest.main()
