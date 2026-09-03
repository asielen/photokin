import unittest
from unittest.mock import patch

from photokin import core, utils
from photokin.exiftool import ExiftoolConfig
from photokin.exiftool.hydrate import hydrate_item_metadata, make_manifest_hydrator


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

        with patch("photokin.core.analyze_photo", return_value=fake_result), patch(
            "photokin.core.build_canonical_patch",
            return_value=({}, {}),
        ):
            core.process_manifest_stream(
                manifest=manifest,
                cfg=cfg,
                metadata_hydrator=calls.append,
            )

        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0], manifest["items"])

    def test_no_hydrator_is_fine(self):
        manifest = {"items": []}
        cfg = utils.Config(dry_run=True)
        result = core.process_manifest_stream(manifest=manifest, cfg=cfg)
        self.assertEqual(
            result,
            {"results": {}, "errors": {}, "groups_failed": 0, "files_unsent": 0, "cancelled": False},
        )


class TestHydrateItemMetadata(unittest.TestCase):
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
            "photokin.exiftool.hydrate.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch("photokin.exiftool.manifest.run_exiftool_json", return_value=records) as run_mock:
            hydrate_item_metadata(items, ExiftoolConfig())

        self.assertEqual(items[0]["metadata"]["userComment"], "From file A")
        self.assertEqual(items[1]["metadata"]["userComment"], "Keep me")
        self.assertEqual(items[2]["metadata"]["userComment"], "From file C")
        # C3 reads five tags rather than one, so an item holding a userComment
        # is still queried for the other four; only an item holding all five is
        # skipped outright. ExifTool is given normalize_path() output, which is
        # platform-dependent, so normalize the expected paths the same way.
        queried = run_mock.call_args.kwargs["files"]
        expected = sorted(
            utils.normalize_path(p) for p in ("/photos/a.jpg", "/photos/b.jpg", "/photos/c.jpg")
        )
        self.assertEqual(sorted(queried), expected)

    def test_noop_when_binary_missing(self):
        items = self._items()
        with patch(
            "photokin.exiftool.hydrate.resolve_exiftool_path",
            side_effect=FileNotFoundError("not found"),
        ), self.assertLogs("photokin.exiftool.hydrate", level="WARNING") as logs:
            hydrate_item_metadata(items, ExiftoolConfig())
        self.assertEqual(items[0]["metadata"]["userComment"], "")
        self.assertIn("not found", logs.output[0])
        # Every item whose read was requested and never confirmed is marked,
        # so the changeset emitter can decline to propose writes for it.
        self.assertTrue(all(item.get(utils.HYDRATION_FAILED_KEY) for item in items))

    def test_noop_when_exiftool_read_fails(self):
        items = self._items()
        with patch(
            "photokin.exiftool.hydrate.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch(
            "photokin.exiftool.manifest.run_exiftool_json",
            side_effect=RuntimeError("boom"),
        ), self.assertLogs("photokin.exiftool.hydrate", level="WARNING") as logs:
            hydrate_item_metadata(items, ExiftoolConfig())
        self.assertEqual(items[0]["metadata"]["userComment"], "")
        self.assertIn("boom", logs.output[0])
        self.assertTrue(all(item.get(utils.HYDRATION_FAILED_KEY) for item in items))

    def test_a_file_with_no_record_is_marked_hydration_failed(self):
        # A record comes back for every file ExifTool could open, so one
        # missing means that file's read failed -- which is not the same as a
        # successful read of a file holding no metadata, and must not be
        # diffed as if it were.
        items = self._items()
        records = [
            {"SourceFile": "/photos/a.jpg", "EXIF:UserComment": "From file A"},
            {"SourceFile": "/photos/c.jpg", "EXIF:UserComment": "From file C"},
        ]
        with patch(
            "photokin.exiftool.hydrate.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch(
            "photokin.exiftool.manifest.run_exiftool_json", return_value=records
        ), self.assertLogs("photokin.exiftool.hydrate", level="WARNING") as logs:
            hydrate_item_metadata(items, ExiftoolConfig())

        self.assertNotIn(utils.HYDRATION_FAILED_KEY, items[0])
        self.assertTrue(items[1][utils.HYDRATION_FAILED_KEY])
        self.assertNotIn(utils.HYDRATION_FAILED_KEY, items[2])
        self.assertIn("could not read 1 file(s)", logs.output[-1])

    def test_make_manifest_hydrator_wraps_config(self):
        items = self._items()
        hydrator = make_manifest_hydrator(ExiftoolConfig())
        with patch(
            "photokin.exiftool.hydrate.resolve_exiftool_path",
            return_value="/fake/exiftool",
        ), patch(
            "photokin.exiftool.manifest.run_exiftool_json",
            return_value=[{"SourceFile": "/photos/a.jpg", "EXIF:UserComment": "Filled"}],
        ):
            hydrator(items)
        self.assertEqual(items[0]["metadata"]["userComment"], "Filled")


if __name__ == "__main__":
    unittest.main()
