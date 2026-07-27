import tempfile
import unittest
from pathlib import Path

from photokin import utils


class TestSectionIdsMatchRealVocabFile(unittest.TestCase):
    """Regression test for a bug where SECTION_IDS didn't match the actual
    TOML headers in vocab_keywords_examples.toml ("photo_format_characteristics"
    vs. the file's "photo_format", and "documents_records"/"date_refernce"
    missing entirely) -- load_vocab_sections silently dropped those sections'
    keywords from flatten_known_keywords, so real controlled-vocabulary
    keywords looked "new" to both core.py's dedup and the model_compare
    scorer.
    """

    def test_real_vocab_file_every_section_is_loaded(self):
        vocab_path = (
            Path(__file__).resolve().parent.parent / "prompts_photo_ai" / "vocab_keywords_examples.toml"
        )
        sections, new_keywords_log = utils.load_vocab_sections(str(vocab_path))
        for sid in utils.SECTION_IDS:
            self.assertGreater(len(sections[sid]), 0, f"section '{sid}' loaded no keywords -- "
                                                        "check it against the TOML file's real headers")
        known = utils.flatten_known_keywords(sections, new_keywords_log)
        self.assertIn("House", known)


class TestLoadVocabSections(unittest.TestCase):
    def test_section_header_mismatch_yields_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vocab.toml"
            path.write_text('[people_subjects]\nkeywords = ["Adults"]\n', encoding="utf-8")
            sections, _ = utils.load_vocab_sections(str(path))
        self.assertEqual(sections["people_subjects"], ["Adults"])
        self.assertEqual(sections["documents_records"], [])  # section absent from this file

    def test_legacy_misspelled_date_header_still_loads(self):
        # Vocab files written before the rename use "[date_refernce]"; they must
        # keep loading into the corrected "date_reference" section id.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vocab.toml"
            path.write_text('[date_refernce]\nkeywords = ["DATE: Y!"]\n', encoding="utf-8")
            sections, _ = utils.load_vocab_sections(str(path))
        self.assertEqual(sections["date_reference"], ["DATE: Y!"])

    def test_flatten_known_keywords_includes_object_form_and_new_keywords_log(self):
        sections = {
            "people_subjects": ["Adults", {"keyword": "Wedding couple", "note": "..."}],
            "documents_records": [],
        }
        new_keywords_log = [{"keyword": "Gorilla mascot"}]
        known = utils.flatten_known_keywords(sections, new_keywords_log)
        self.assertEqual(known, {"Adults", "Wedding couple", "Gorilla mascot"})


if __name__ == "__main__":
    unittest.main()
