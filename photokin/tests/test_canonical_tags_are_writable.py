"""Every canonical tag must be a spelling the real ExifTool can write.

This is the check whose absence let three unwritable tag names ship. ``canonical.py``
declared ``XMP:dc:Subject``, ``XMP:dc:Title`` and ``XMP:dc:Description``; ExifTool
answers "Sorry, XMP:dc:Description doesn't exist or isn't writable", exits 1 and
writes nothing, so ``-w`` could not put a keyword, a title or a caption into a file
at all. Every unit test in the suite either mocked the binary or exercised only the
default write set (``EXIF:DateTimeOriginal``, ``EXIF:CreateDate``,
``EXIF:UserComment``), all three of which are valid -- so nothing failed.

Two design choices make this test hold the line rather than merely record today's
answer:

- **The tag list is derived, never restated.** ``_canonical_tags()`` reflects over
  the ``canonical`` module for every ``CANONICAL_*_TAG`` string and every value of
  every ``CANONICAL_*_TAGS`` mapping. A tag added to that module is covered by this
  test the day it is added, with no edit here. ``test_the_derivation_sees_every_tag``
  guards the reflection itself, so the list going quietly empty is also a failure.
- **The write is verified by reading the value back**, not by trusting the exit
  code. ExifTool can exit 0 having written nothing (the ``-o`` sidecar path does
  exactly that), and an exit-code-only assertion would pass for a tag whose value
  never reaches the file.

The single skip condition is a missing ExifTool binary, so CI without one is clean
while a developer machine with one always runs the check. The fixture image is
embedded rather than generated so that a missing Pillow cannot become a second,
quieter reason for this test to not run.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from photokin import canonical

#: A 283-byte 4x4 baseline JPEG, the smallest thing ExifTool will accept EXIF,
#: IPTC and XMP segments into. Embedded as bytes so this test's only dependency
#: is the ExifTool binary itself.
_MINIMAL_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABsSFBcUERsXFhceHBsgKEIrKCUlKFE6PTBCYFVlZF9V"
    "XVtqeJmBanGQc1tdhbWGkJ6jq62rZ4C8ybqmx5moq6T/2wBDARweHigjKE4rK06kbl1upKSkpKSk"
    "pKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKT/wAARCAAEAAQDASIA"
    "AhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAAAP/EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAUAQEA"
    "AAAAAAAAAAAAAAAAAAAA/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAwDAQACEQMRAD8AAA//2Q=="
)

#: ExifTool's date tags only accept ``YYYY:MM:DD HH:MM:SS``; everything else in
#: canonical.py takes free text. Keyed by tag so a new date-shaped tag can be
#: given a valid sample without changing the assertion logic.
_SAMPLE_VALUES = {
    "EXIF:DateTimeOriginal": "1975:06:01 12:00:00",
    "EXIF:CreateDate": "1975:06:01 12:00:00",
}
_DEFAULT_SAMPLE = "photokin writability probe"

#: ExifTool's two ways of saying "I do not know this tag name", measured against
#: 13.10: ``XMP:dc:Description`` -> "Sorry, ... doesn't exist or isn't writable",
#: ``XMP:foo:Bar`` -> "Tag 'XMP:foo:Bar' is not defined". Matching both is what
#: separates a bad *name* from a bad *value*, which fail very differently.
_UNKNOWN_TAG_SIGNATURES = ("isn't writable", "is not defined")


def _canonical_tags() -> dict[str, str]:
    """Reflect over ``canonical`` for every tag it declares.

    Returns a ``{attribute_name: tag}`` mapping so a failure can name the
    constant that is wrong, not just the tag string. Covers both shapes the
    module uses: ``CANONICAL_*_TAG`` scalars and ``CANONICAL_*_TAGS`` mappings.
    """
    tags: dict[str, str] = {}
    for name, value in vars(canonical).items():
        if not name.startswith("CANONICAL_"):
            continue
        if isinstance(value, str):
            tags[name] = value
        elif isinstance(value, dict):
            for key, tag in value.items():
                if isinstance(tag, str):
                    tags[f"{name}[{key!r}]"] = tag
    return tags


def _require_exiftool() -> str:
    """Return an ExifTool path, or skip cleanly when the binary is absent."""
    exiftool = shutil.which("exiftool")
    if not exiftool:
        raise unittest.SkipTest(
            "no exiftool binary on PATH; this test needs the real one because "
            "the defect it guards is ExifTool's tag-name parsing"
        )
    return exiftool


class TestEveryCanonicalTagIsWritableByRealExiftool(unittest.TestCase):
    """Drive the real ExifTool binary against a real image, one tag at a time."""

    def setUp(self) -> None:
        self.exiftool = _require_exiftool()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)

    def _fresh_image(self, name: str) -> Path:
        """Write an untouched copy of the fixture JPEG and return its path."""
        path = self.tmpdir / name
        path.write_bytes(base64.b64decode(_MINIMAL_JPEG_B64))
        return path

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        """Invoke ExifTool with argv built in Python and stdin closed.

        ``stdin=DEVNULL`` is load-bearing: ExifTool reads stdin when it is a
        pipe and will consume the caller's input if it is left attached.
        """
        return subprocess.run(
            [self.exiftool, *args],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )

    def test_every_canonical_tag_can_be_written_and_read_back(self) -> None:
        """Each tag in canonical.py must survive a real write/read round trip.

        Two distinct failure modes are reported separately, because conflating
        them misdiagnoses the caller. ExifTool rejects an unknown *tag name*
        with "doesn't exist or isn't writable" -- that is the defect this file
        exists for. It rejects a well-named tag given an unsuitable *value*
        with a type complaint ("Not a floating point number for
        XMP-xmp:Rating"). The second is not a spelling bug, and telling a
        developer to fix their spelling when the name was fine would send them
        after the wrong thing.
        """
        tags = _canonical_tags()
        self.assertTrue(tags, "derivation found no canonical tags to check")

        for attribute, tag in sorted(tags.items()):
            with self.subTest(constant=attribute, tag=tag):
                value = _SAMPLE_VALUES.get(tag, _DEFAULT_SAMPLE)
                image = self._fresh_image("probe.jpg")

                write = self._run("-overwrite_original", f"-{tag}={value}", str(image))
                output = f"{write.stdout}\n{write.stderr}"

                # The invariant this file was created to hold.
                unknown = [s for s in _UNKNOWN_TAG_SIGNATURES if s in output]
                self.assertFalse(
                    unknown,
                    f"{attribute} = {tag!r} is not a writable ExifTool tag name.\n"
                    f"  {output.strip()}\n"
                    f"  ExifTool takes 'FAMILY:Tag' or 'FAMILY-namespace:Tag' -- write it "
                    f"as 'XMP-dc:Description', not 'XMP:dc:Description'.",
                )

                # A valid name whose sample value ExifTool will not accept: the
                # tag is fine, the fixture is not. Still a failure -- the tag is
                # unproven until something lands -- but diagnosed accurately.
                self.assertEqual(
                    write.returncode,
                    0,
                    f"{attribute} = {tag!r} is a valid tag name, but ExifTool rejected the "
                    f"sample value {value!r}:\n"
                    f"  {output.strip()}\n"
                    f"  Add a value of the right type for this tag to _SAMPLE_VALUES in "
                    f"this file; do not change the tag name.",
                )

                readback = self._run("-s3", f"-{tag}", str(image))
                self.assertEqual(
                    readback.stdout.strip(),
                    value,
                    f"{attribute} = {tag!r} reported success but the value did not "
                    f"reach the file.",
                )

    def test_the_derivation_sees_every_tag_the_module_declares(self) -> None:
        """Guard the reflection itself, which is what makes the check self-extending.

        Two ways the derivation could go quietly slack, both covered here:

        - It returns nothing at all -- after a rename of the ``CANONICAL_``
          prefix, say -- turning the round-trip test into a no-op that still
          passes. The five scalars and the location mapping are named
          explicitly to catch that.
        - A canonical constant is declared in a *container shape* the
          derivation does not walk. ``_canonical_tags`` handles ``str`` and
          ``dict``; a tag added as, say, a list or a tuple would be silently
          uncovered while every existing assertion stayed green. So every
          ``CANONICAL_*`` attribute is required to contribute at least one tag.

        Note what is deliberately *not* asserted: an exact tag count. Pinning
        one would make a legitimately added tag fail here, which would defeat
        the point -- a new tag is meant to be covered the day it is added,
        without editing this file.
        """
        derived = _canonical_tags()
        found = set(derived.values())

        for constant in (
            canonical.CANONICAL_KEYWORDS_TAG,
            canonical.CANONICAL_TITLE_TAG,
            canonical.CANONICAL_DESCRIPTION_TAG,
            canonical.CANONICAL_USER_COMMENT_TAG,
            canonical.CANONICAL_DATE_TAG,
        ):
            self.assertIn(constant, found)
        for tag in canonical.CANONICAL_LOCATION_TAGS.values():
            self.assertIn(tag, found)

        for name, value in vars(canonical).items():
            if not name.startswith("CANONICAL_"):
                continue
            with self.subTest(constant=name):
                contributed = [k for k in derived if k == name or k.startswith(f"{name}[")]
                self.assertTrue(
                    contributed,
                    f"{name} is a canonical constant of type {type(value).__name__} that "
                    f"the derivation does not walk, so its tags are never checked for "
                    f"writability. Teach _canonical_tags() that shape.",
                )


class TestCanonicalTagSpellingWithoutTheBinary(unittest.TestCase):
    """The part of the guard that needs no ExifTool, so CI without one still checks.

    Deliberately not a member of the class above: that one's ``setUp`` skips when
    the binary is missing, which would take this string-only assertion down with
    it and leave a no-exiftool CI run asserting nothing at all.
    """

    def test_no_canonical_tag_uses_the_two_colon_form(self) -> None:
        """House rule: exactly one separator, so the namespace is explicit.

        Deliberately stricter than ExifTool, because ExifTool's own behaviour
        here is a trap rather than a rule. Measured against 13.10:

            XMP:dc:Description   rejected -- "doesn't exist or isn't writable"
            XMP:foo:Bar          rejected -- "Tag 'XMP:foo:Bar' is not defined"
            XMP:xmp:Rating       ACCEPTED, value lands

        The third works only because the middle token ``xmp`` happens to
        collide with the family-0 group name ``XMP``; ``dc`` collides with
        nothing, so the identical-looking spelling fails. A form that works or
        fails depending on whether a namespace label coincides with a group
        name is not one to leave available, so all of it is banned here and
        ``XMP-dc:Tag`` is the single spelling used.

        Runs anywhere the module imports -- the round-trip test above covers
        the same ground but only where ExifTool is installed.
        """
        for attribute, tag in sorted(_canonical_tags().items()):
            with self.subTest(constant=attribute, tag=tag):
                self.assertLessEqual(
                    tag.count(":"),
                    1,
                    f"{attribute} = {tag!r} uses the 'FAMILY:namespace:Tag' form. "
                    f"Write it as 'XMP-dc:Tag' -- one separator, namespace explicit.",
                )


class TestTheWritabilityCheckIsNotVacuous(unittest.TestCase):
    """Show the check above can fail, using the spelling that actually shipped."""

    def setUp(self) -> None:
        self.exiftool = _require_exiftool()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)

    def test_the_old_spelling_is_still_rejected_by_this_exiftool(self) -> None:
        """``XMP:dc:*`` must still fail, or the assertion above proves nothing.

        If a future ExifTool started accepting the two-colon form, this test
        fails and tells us the guard has gone slack -- rather than leaving a
        green suite that is no longer measuring anything.
        """
        for tag in ("XMP:dc:Subject", "XMP:dc:Title", "XMP:dc:Description"):
            with self.subTest(tag=tag):
                image = self.tmpdir / "old.jpg"
                image.write_bytes(base64.b64decode(_MINIMAL_JPEG_B64))
                result = subprocess.run(
                    [self.exiftool, "-overwrite_original", f"-{tag}=x", str(image)],
                    capture_output=True,
                    text=True,
                    check=False,
                    stdin=subprocess.DEVNULL,
                )
                self.assertNotEqual(
                    result.returncode,
                    0,
                    f"{tag!r} unexpectedly succeeded; the writability guard may be stale.",
                )
                self.assertIn("isn't writable", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
