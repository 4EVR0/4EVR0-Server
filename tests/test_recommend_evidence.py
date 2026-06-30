import unittest

from app.services.recommend_service import _evidence_label


class EvidenceLabelTest(unittest.TestCase):
    def test_pubmed_with_count(self):
        self.assertEqual("논문 근거 4건", _evidence_label("pubmed_evidence", "4"))

    def test_pubmed_zero_or_missing_count(self):
        self.assertEqual("논문 근거", _evidence_label("pubmed_evidence", "0"))
        self.assertEqual("논문 근거", _evidence_label("pubmed_evidence", None))
        self.assertEqual("논문 근거", _evidence_label("pubmed_evidence", ""))

    def test_cosing_function(self):
        self.assertEqual("성분 기능 근거", _evidence_label("cosing_function", "0"))

    def test_unknown(self):
        self.assertEqual("근거 미상", _evidence_label(None, None))
        self.assertEqual("근거 미상", _evidence_label("something_else", "3"))

    def test_bad_count_does_not_raise(self):
        self.assertEqual("논문 근거", _evidence_label("pubmed_evidence", "n/a"))


if __name__ == "__main__":
    unittest.main()
