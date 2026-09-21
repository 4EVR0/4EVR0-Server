import unittest

from app.domain.enums import Constraint
from app.schemas.recommend import IngredientResult, ProductResult
from app.services.recommend_service import (
    _apply_constraint_evidence_guard,
    _build_grounded_product_response,
    _build_no_product_response,
    _evidence_label,
    _has_product_grounding_violation,
    _remove_hanja,
)


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


class DeterministicOutputGuardTest(unittest.TestCase):
    @staticmethod
    def _ingredient_and_product():
        ingredients = [
            IngredientResult(name="CERAMIDE NP", kor_name="세라마이드엔피"),
            IngredientResult(name="BEESWAX", kor_name="비즈왉스"),
        ]
        products = [ProductResult(
            product_id="p1",
            product_name="테스트 크림",
            brand="테스트",
            category="크림",
            matched_count=1,
            matched_ingredients=["CERAMIDE NP"],
        )]
        return ingredients, products

    def test_hanja_is_removed_without_changing_clean_korean(self):
        clean, removed = _remove_hanja("피肤 진정에 도움이 돼요")
        self.assertEqual("피 진정에 도움이 돼요", clean)
        self.assertTrue(removed)
        self.assertEqual(("피부 진정", False), _remove_hanja("피부 진정"))

    def test_unverified_product_constraints_clear_candidates(self):
        products = [{"product_id": "p1"}]
        self.assertEqual(
            [],
            _apply_constraint_evidence_guard(products, [Constraint.FRAGRANCE_FREE]),
        )
        self.assertEqual(products, _apply_constraint_evidence_guard(products, []))

    def test_no_product_response_never_invents_a_product(self):
        response = _build_no_product_response(
            [IngredientResult(name="PANTHENOL", kor_name="덱스판테놀")],
            [],
        )
        self.assertIn("구체적인 제품명을 추천하지 않겠습니다", response)
        self.assertIn("덱스판테놀", response)

    def test_unverified_constraint_response_is_explicit(self):
        response = _build_no_product_response([], [Constraint.FRAGRANCE_FREE])
        self.assertIn("향료 미포함", response)
        self.assertIn("확인할 수 있는 제품 속성 데이터가 없어", response)

    def test_product_ingredient_association_violation_is_detected(self):
        ingredients, products = self._ingredient_and_product()
        invalid = (
            "추천 제품\n"
            "- [크림] 테스트 크림은 비즈왉스가 들어 있어요."
        )
        valid = (
            "추천 제품\n"
            "- [크림] 테스트 크림은 세라마이드엔피가 확인됩니다."
        )
        self.assertTrue(_has_product_grounding_violation(invalid, ingredients, products))
        self.assertFalse(_has_product_grounding_violation(valid, ingredients, products))

    def test_unknown_product_bullet_is_detected(self):
        ingredients, products = self._ingredient_and_product()
        response = "추천 제품\n- [크림] 없는 브랜드 유명 크림을 추천해요."
        self.assertTrue(_has_product_grounding_violation(response, ingredients, products))

    def test_missing_product_section_is_detected(self):
        ingredients, products = self._ingredient_and_product()
        self.assertTrue(_has_product_grounding_violation("제품을 하나 추천합니다.", ingredients, products))

    def test_grounded_fallback_mentions_only_matched_ingredients(self):
        ingredients, products = self._ingredient_and_product()
        response = _build_grounded_product_response(ingredients, products)
        self.assertIn("세라마이드엔피", response)
        self.assertNotIn("비즈왉스", response)


if __name__ == "__main__":
    unittest.main()
