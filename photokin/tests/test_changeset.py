import unittest

from photokin.changeset import diff_canonical_metadata, ordered_group_keys
from photokin.canonical import CANONICAL_DESCRIPTION_TAG, CANONICAL_KEYWORDS_TAG


class TestChangesetDiffs(unittest.TestCase):
    def test_keyword_add_dedup(self):
        before = {CANONICAL_KEYWORDS_TAG: ["Cat"]}
        after = {CANONICAL_KEYWORDS_TAG: ["Cat", "cat", "Dog", "DOG"]}
        diff = diff_canonical_metadata(before, after)
        self.assertEqual(diff["keywords_add"], ["Dog"])
        self.assertEqual(diff["keywords_remove"], [])

    def test_caption_set_diff(self):
        before = {CANONICAL_DESCRIPTION_TAG: "Old caption"}
        after = {CANONICAL_DESCRIPTION_TAG: "New caption"}
        diff = diff_canonical_metadata(before, after)
        self.assertEqual(diff["set"], {CANONICAL_DESCRIPTION_TAG: "New caption"})

    def test_no_changes(self):
        before = {CANONICAL_DESCRIPTION_TAG: "Same", CANONICAL_KEYWORDS_TAG: ["A"]}
        after = {CANONICAL_DESCRIPTION_TAG: "Same", CANONICAL_KEYWORDS_TAG: ["A"]}
        diff = diff_canonical_metadata(before, after)
        self.assertEqual(diff["set"], {})
        self.assertEqual(diff["keywords_add"], [])
        self.assertEqual(diff["keywords_remove"], [])


class TestGroupOrdering(unittest.TestCase):
    def test_ordered_group_keys(self):
        buckets = {"b": [{"path": "b"}], "A": [{"path": "a"}], "aa": [{"path": "aa"}]}
        self.assertEqual(ordered_group_keys(buckets), ["A", "aa", "b"])


if __name__ == "__main__":
    unittest.main()
