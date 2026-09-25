import unittest
from unittest.mock import patch

from app.core.config import settings
from app.domain.enums import Concern, Constraint
from app.schemas.recommend import IngredientResult, ProductResult
from app.services.response_integrity import find_response_integrity_issues
from app.services.recommend_service import (
    _apply_constraint_evidence_guard,
    _build_grounded_product_response,
    _build_no_product_response,
    _build_verified_study_response,
    _compose_user_content,
    _evidence_label,
    _has_product_grounding_violation,
    _normalize_consumer_language,
    _normalize_product_names,
    _product_display_name,
    _remove_hanja,
    _rerank_by_review,
    _verified_study_match,
    filter_explicit_application_area,
    filter_by_target_concerns,
    filter_purpose_mismatch,
)
from eval.run_response_eval import render_evidence_context


class EvidenceLabelTest(unittest.TestCase):
    def test_reviewed_retinol_study_is_limited_to_matching_face_wrinkle_template(self):
        ingredient = IngredientResult(
            name="RETINOL", kor_name="레티놀", claim="Anti-aging",
            eligibility_tier="pubmed_evidence", paper_ref="2",
        )
        product = ProductResult(
            product_id="p1", product_name="주름 세럼", brand="테스트",
            category="세럼", matched_count=1, matched_ingredients=["RETINOL"],
        )
        generated_input = _compose_user_content("입가 잔주름", [ingredient], [product])
        judge_input = render_evidence_context(
            [ingredient], [product], include_verified_studies=True,
        )
        match = _verified_study_match(
            "입가 잔주름이 고민이에요.", [Concern.WRINKLES], [ingredient], [product],
        )
        self.assertIsNotNone(match)
        response = _build_verified_study_response("입가 잔주름이 고민이에요.", match)

        for text in (judge_input["verified_studies"], response):
            self.assertIn("0.1%", text)
        self.assertIn("입가 주름이 신경 쓰이시는군요", response)
        self.assertIn("6개 연구를 종합했을 때 얼굴 주름 개선이 관찰됐습니다", response)
        self.assertIn("제품 성분 정보에서 레티놀이 확인돼 후보로 골랐습니다", response)
        self.assertIn("이 연구는 입가 주름을 따로 평가하지 않았고", response)
        self.assertIn("이 제품의 레티놀 함량·배합도 확인되지 않아", response)
        self.assertIn("연구 결과를 이 제품의 효과로 단정할 수는 없습니다", response)
        self.assertNotIn("추천 제품의 효과를 입증한 연구도 아닙니다", response)
        self.assertIn("[연구 보기](https://pubmed.ncbi.nlm.nih.gov/38564380/)", response)
        self.assertNotIn("사용 4~12주", response)
        self.assertNotIn("논문 근거 2건", response)
        self.assertEqual([], find_response_integrity_issues(response, [ingredient], [product]))
        self.assertFalse(_has_product_grounding_violation(response, [ingredient], [product]))
        eye_response = _build_verified_study_response("눈가 주름이 고민이에요.", match)
        self.assertNotIn("이 연구는 눈가 주름을 따로 평가하지 않았고", eye_response)
        self.assertIn("연구 결과를 이 제품의 효과로 단정할 수는 없습니다", eye_response)
        self.assertIn("https://pubmed.ncbi.nlm.nih.gov/38564380/", judge_input["verified_studies"])
        self.assertNotIn("0.1%", generated_input)
        self.assertEqual("(없음)", render_evidence_context([ingredient], [product])["verified_studies"])
        self.assertIsNone(_verified_study_match("탄력이 떨어져요", [Concern.LOSS_OF_ELASTICITY], [ingredient], [product]))
        self.assertIsNone(_verified_study_match("목주름이 고민이에요", [Concern.WRINKLES], [ingredient], [product]))
        with patch.object(settings, "verified_study_response_enabled", False):
            self.assertIsNone(_verified_study_match(
                "입가 잔주름이 고민이에요.", [Concern.WRINKLES], [ingredient], [product],
            ))
        self.assertNotIn("0.1%", _compose_user_content(
            "건조해요", [ingredient.model_copy(update={"claim": "Hydrating"})], [product],
        ))

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

    def test_difficult_or_broken_language_is_normalized(self):
        self.assertEqual(
            "피부 속의 피지 분비와 피부 장벽을 살펴보세요.",
            _normalize_consumer_language("피부 심부의 지분 분비와 피장벽을 살펴보세요."),
        )
        self.assertEqual(
            "수분과 피지를 조절합니다.",
            _normalize_consumer_language("수분과 지분을 조절합니다."),
        )

    def test_brand_is_rendered_only_once(self):
        self.assertEqual("미샤 비타씨 앰플", _product_display_name("미샤", "미샤 비타씨 앰플"))
        self.assertEqual("미샤 비타씨 앰플", _product_display_name("미샤", "비타씨 앰플"))
        product = ProductResult(
            product_id="p1", product_name="미샤 비타씨 앰플", brand="미샤",
            category="앰플", matched_count=1, matched_ingredients=["NIACINAMIDE"],
        )
        self.assertEqual(
            "미샤 비타씨 앰플을 추천합니다.",
            _normalize_product_names("미샤 미샤 비타씨 앰플을 추천합니다.", [product]),
        )

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

    def test_ingredient_token_in_official_product_name_is_not_a_claim(self):
        ingredients = [IngredientResult(name="RETINAL", kor_name="레틴알")]
        products = [ProductResult(
            product_id="p1", product_name="아렌시아 레틴알 부스터 샷", brand="아렌시아",
            category="세럼", matched_count=1, matched_ingredients=["BAKUCHIOL"],
        )]
        response = "추천 제품\n- [세럼] 아렌시아 레틴알 부스터 샷: 바쿠치올이 확인됩니다."

        self.assertFalse(_has_product_grounding_violation(response, ingredients, products))

    def test_unknown_product_bullet_is_detected(self):
        ingredients, products = self._ingredient_and_product()
        response = "추천 제품\n- [크림] 없는 브랜드 유명 크림을 추천해요."
        self.assertTrue(_has_product_grounding_violation(response, ingredients, products))

    def test_missing_product_section_is_detected(self):
        ingredients, products = self._ingredient_and_product()
        self.assertTrue(_has_product_grounding_violation("제품을 하나 추천합니다.", ingredients, products))

    def test_grounded_fallback_mentions_only_matched_ingredients(self):
        ingredients, products = self._ingredient_and_product()
        response = _build_grounded_product_response("피부 장벽이 약하고 건조해요.", ingredients, products)
        self.assertIn("세라마이드엔피", response)
        self.assertNotIn("비즈왉스", response)

    def test_grounded_fallback_does_not_repeat_brand(self):
        ingredients = [IngredientResult(name="NIACINAMIDE", kor_name="나이아신아마이드")]
        products = [ProductResult(
            product_id="p1", product_name="미샤 비타씨 앰플", brand="미샤",
            category="앰플", matched_count=1, matched_ingredients=["NIACINAMIDE"],
        )]
        response = _build_grounded_product_response("피지가 많아요.", ingredients, products)
        self.assertIn("미샤 비타씨 앰플", response)
        self.assertNotIn("미샤 미샤", response)

    def test_grounded_fallback_connects_claims_to_analysis_and_products(self):
        ingredients = [
            IngredientResult(
                name="MANDELIC ACID", kor_name="만델릭애씨드", claim="Keratolytic",
                eligibility_tier="pubmed_evidence", paper_ref="2",
            ),
            IngredientResult(
                name="NIACINAMIDE", kor_name="나이아신아마이드", claim="Hydrating",
                eligibility_tier="pubmed_evidence", paper_ref="3",
            ),
        ]
        products = [ProductResult(
            product_id="p1", product_name="결 케어 세럼", brand="테스트",
            category="세럼", matched_count=2,
            matched_ingredients=["MANDELIC ACID", "NIACINAMIDE"],
        )]

        response = _build_grounded_product_response(
            "각질이 일어나고 피부결이 거칠어요.", ingredients, products,
        )

        self.assertIn("각질 관리 및 보습 근거를 함께 살폈습니다", response)
        self.assertIn("만델릭애씨드 (MANDELIC ACID): 확인된 효능은 각질 관리", response)
        self.assertIn("근거 수준은 논문 근거 2건입니다", response)
        self.assertIn("나이아신아마이드의 보습", response)
        product_line = next(line for line in response.splitlines() if "결 케어 세럼:" in line)
        self.assertIn("추천 이유는", product_line)
        self.assertNotIn("논문 근거", product_line)
        self.assertFalse(_has_product_grounding_violation(response, ingredients, products))

    def test_fallback_keeps_distinct_ingredients_with_same_benefit(self):
        ingredients = [
            IngredientResult(name="BAKUCHIOL", kor_name="바쿠치올", claim="Anti-aging",
                             eligibility_tier="pubmed_evidence", paper_ref="2"),
            IngredientResult(name="RETINOL", kor_name="레티놀", claim="Anti-aging",
                             eligibility_tier="pubmed_evidence", paper_ref="2"),
            IngredientResult(name="PEPTIDE", kor_name="펩타이드", claim="Anti-aging",
                             eligibility_tier="pubmed_evidence", paper_ref="1"),
        ]

        def product(product_id, name, matched, category="세럼"):
            return ProductResult(
                product_id=product_id, product_name=name, brand="테스트",
                category=category, matched_count=len(matched), matched_ingredients=matched,
            )

        products = [
            product("p1", "첫 제품", ["BAKUCHIOL", "RETINOL"], "크림"),
            product("p2", "근거 중복 제품", ["RETINOL", "BAKUCHIOL"], "앰플"),
            product("p3", "다른 근거 제품", ["BAKUCHIOL", "PEPTIDE"]),
            product("p4", "세 번째 근거 제품", ["RETINOL", "PEPTIDE"]),
        ]

        response = _build_grounded_product_response(
            "입가 잔주름 관리 제품을 추천해 주세요.", ingredients, products,
        )

        self.assertIn("바쿠치올 (BAKUCHIOL):", response)
        self.assertIn("레티놀 (RETINOL):", response)
        self.assertIn("펩타이드 (PEPTIDE):", response)
        self.assertIn("첫 제품: 추천 이유는 바쿠치올 및 레티놀의 탄력·주름 관리", response)
        self.assertIn("다른 근거 제품: 추천 이유는 바쿠치올 및 펩타이드의 탄력·주름 관리", response)
        self.assertIn("세 번째 근거 제품: 추천 이유는 레티놀 및 펩타이드의 탄력·주름 관리", response)
        self.assertNotIn("근거 중복 제품", response)
        self.assertEqual(3, sum(line.startswith("- [") for line in response.splitlines()))
        self.assertFalse(_has_product_grounding_violation(response, ingredients, products))

    def test_grounded_fallback_does_not_invent_a_claim_for_unknown_labels(self):
        ingredients = [IngredientResult(name="UNKNOWN", kor_name="알수없는성분")]
        products = [ProductResult(
            product_id="p1", product_name="테스트 크림", brand="테스트",
            category="크림", matched_count=1, matched_ingredients=["UNKNOWN"],
        )]

        response = _build_grounded_product_response("피부가 고민이에요.", ingredients, products)

        self.assertIn("제품별 매칭 성분에 따라", response)
        self.assertIn("제품 데이터의 매칭 성분이며, 효능 근거는 확인되지 않습니다", response)
        self.assertNotIn("논문 근거", response)

    def test_grounded_fallback_sanitizes_the_quoted_user_concern(self):
        ingredients, products = self._ingredient_and_product()

        response = _build_grounded_product_response(
            "피肤가\n심부까지 건조해요.", ingredients, products,
        )

        self.assertNotIn("肤", response)
        self.assertNotIn("심부", response)
        self.assertIn("피가 피부 속까지 건조해요", response)

    def test_corrupted_generation_is_detected_and_grounded_fallback_is_clean(self):
        ingredients, products = self._ingredient_and_product()
        corrupted = (
            "추천 제품\n- 테스트 크림: 피부 세포 세포 세포 세포 세포 "
            "CELLULAR 재생에 도움을 줍니다."
        )

        codes = [code for code, _ in find_response_integrity_issues(corrupted, ingredients, products)]
        self.assertEqual(["DEGENERATE_REPETITION", "STRAY_ENGLISH_TOKEN"], codes)
        fallback = _build_grounded_product_response("피부가 건조해요.", ingredients, products)
        self.assertEqual([], find_response_integrity_issues(fallback, ingredients, products))

    def test_known_english_names_and_parenthesized_inci_are_not_corruption(self):
        ingredients = [IngredientResult(name="CERAMIDE NP", kor_name="세라마이드엔피")]
        products = [ProductResult(
            product_id="p1", product_name="CELLULAR 리페어 크림", brand="CELLULAR",
            category="크림", matched_count=1, matched_ingredients=["CERAMIDE NP"],
        )]
        clean = (
            "성분 설명\n- 세라마이드엔피 (CERAMIDE NP)는 피부 장벽 관리에 쓰입니다.\n"
            "추천 제품\n- CELLULAR 리페어 크림: 세라마이드엔피가 확인됩니다."
        )

        self.assertEqual([], find_response_integrity_issues(clean, ingredients, products))


class ProductPurposeFilterTest(unittest.TestCase):
    def test_explicit_mouth_request_excludes_neck_and_eye_only_products(self):
        products = [
            {"product_name": "레티놀 넥 샷 목주름 세럼"},
            {"product_name": "눈가 전용 아이크림"},
            {"product_name": "아이크림 포 페이스"},
            {"product_name": "일반 주름 세럼"},
        ]
        self.assertEqual(
            products[2:],
            filter_explicit_application_area(products, "입가 잔주름 관리 제품을 추천해 주세요"),
        )

    def test_neck_product_remains_for_neck_or_unspecified_area(self):
        products = [{"product_name": "레티놀 넥 샷 목주름 세럼"}]
        self.assertEqual(products, filter_explicit_application_area(products, "목주름이 고민이에요"))
        self.assertEqual(products, filter_explicit_application_area(products, "주름 관리 제품을 추천해 주세요"))

    def test_general_face_request_excludes_eye_only_product(self):
        products = [{"product_name": "레티놀 아이크림"}, {"product_name": "레티놀 얼굴 크림"}]
        self.assertEqual(products[1:], filter_explicit_application_area(products, "얼굴 탄력이 고민이에요"))

    def test_name_mismatch_is_not_restored_when_all_products_fail(self):
        products = [{"product_name": "기미 잡티 앰플"}]
        self.assertEqual([], filter_purpose_mismatch(products, [Concern.ACNE]))

    def test_labeled_target_mismatch_is_not_restored(self):
        products = [{"product_id": "known", "product_name": "모공 탄력 앰플"}]
        from app.services import recommend_service

        original = recommend_service._PRODUCT_CONCERNS
        recommend_service._PRODUCT_CONCERNS = {"known": ["ENLARGED_PORES"]}
        try:
            self.assertEqual(
                [],
                filter_by_target_concerns(products, [Concern.SENSITIVE_SKIN]),
            )
        finally:
            recommend_service._PRODUCT_CONCERNS = original

    def test_rerank_prioritizes_exact_concern_before_group_and_reviews(self):
        from app.services import recommend_service

        products = [
            {
                "product_id": "group-only",
                "relevance_score": 3.2,
                "review_count": 10_000,
                "rating": 5.0,
            },
            {
                "product_id": "exact-one",
                "relevance_score": 3.1,
                "review_count": 10,
                "rating": 4.0,
            },
            {
                "product_id": "exact-two",
                "relevance_score": 3.0,
                "review_count": 1,
                "rating": 3.0,
            },
        ]
        original = recommend_service._PRODUCT_CONCERNS
        recommend_service._PRODUCT_CONCERNS = {
            "group-only": ["ENLARGED_PORES"],
            "exact-one": ["ACNE"],
            "exact-two": ["ACNE", "OILY_SKIN"],
        }
        try:
            ranked = _rerank_by_review(products, [Concern.ACNE, Concern.OILY_SKIN])
            self.assertEqual(
                ["exact-two", "exact-one", "group-only"],
                [p["product_id"] for p in ranked],
            )
        finally:
            recommend_service._PRODUCT_CONCERNS = original


if __name__ == "__main__":
    unittest.main()
