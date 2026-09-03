"""Guards the invariant behind the default write set: photokin writes by
default only what hydration reads back first.

Several review findings on the 0.6.1 branch were one defect wearing different
clothes: the write side trusting a "before" the read side never confirmed
(location tags written but never read; a failed read diffed as an empty one;
an error record counted as a read). The per-file hydration-failure marking
guards the runtime half; this case guards the configuration half, so widening
``DEFAULT_PIPELINE_FIELDS`` without teaching hydration to read the new tag
fails here instead of shipping a silent overwrite hazard.
"""
from __future__ import annotations

import unittest

from photokin.exiftool.config import DEFAULT_PIPELINE_FIELDS
from photokin.exiftool.manifest import DEFAULT_EXIFTOOL_FIELDS

#: Written only when an external changeset carries it, never produced by
#: photokin's own analysis -- so no diff of photokin's ever proposes it
#: against an unread file, and it needs no read-back. See the constant's
#: comment in ``exiftool/config.py``.
_WRITE_ONLY_EXCEPTIONS = frozenset({"EXIF:CreateDate"})


def _bare(tag: str) -> str:
    """Return the group-less tag name (``XMP-dc:Subject`` -> ``Subject``).

    The read side deliberately uses the tolerant family-0 spellings
    (``XMP:Description``) while the write side uses the unambiguous writable
    ones (``XMP-dc:Description``); the bare name is what the two share.
    """
    return tag.rsplit(":", 1)[-1]


class TestDefaultWritesAreReadBackFirst(unittest.TestCase):
    """Every default write tag has a hydrated read tag with the same bare name."""

    def test_every_default_write_tag_is_hydrated_first(self) -> None:
        read_bare = {_bare(tag) for tag in DEFAULT_EXIFTOOL_FIELDS}
        unread = sorted(
            tag
            for tag in DEFAULT_PIPELINE_FIELDS
            if tag not in _WRITE_ONLY_EXCEPTIONS and _bare(tag) not in read_bare
        )
        self.assertEqual(
            unread,
            [],
            "these default write tags are never read back by -r, so a model "
            "guess would be diffed against an empty before-snapshot and could "
            "overwrite metadata the file already holds; either teach "
            "DEFAULT_EXIFTOOL_FIELDS (exiftool/manifest.py) to read them or "
            "keep them out of DEFAULT_PIPELINE_FIELDS (exiftool/config.py)",
        )


if __name__ == "__main__":
    unittest.main()
