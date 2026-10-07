"""복합 고민 검색의 고민별 커버리지 회귀 테스트 (이슈 #113).

Neo4j·LLM 없이 순수 함수와 목(mock) 조회만으로 검증한다.
  - "미백"이 추출 정규화에서 사라지지 않는다(사건의 직접 원인).
  - 고민별 성분 후보·풀이 한 고민의 고득점 성분에 밀려 사라지지 않는다.
  - 제품 정렬이 실제 매칭 성분 기준 고민 커버리지를 반영한다.
  - 두 고민을 함께 충족하는 제품이 없으면 부분 충족을 명시한다.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.clients.llm_client import _normalize_concerns
from app.core.config import settings
from app.domain.enums import Concern, Effect
from app.schemas.profile import UserProfile
from app.services import recommend_service as service
from app.services.concern_coverage import (
    concern_ingredient_pool,
    merge_concern_candidates,
    order_by_coverage,
    partial_coverage_note,
)
from app.services.taxonomy_normalization_service import infer_effects
from app.services.user_profile_extraction_service import extract_profile

ISSUE_QUERY = "미백이랑 주름 개선 둘 다 되는 세럼 추천해줘"
PIG, WRK = Concern.HYPERPIGMENTATION, Concern.WRINKLES


def _row(name, score, claim="Depigmenting", tier="pubmed_evidence"):
    return {"name": name, "kor_name": None, "claim": claim, "eligibility_tier": tier,
            "paper_ref": "3", "graph_score": score, "kr_reg_status": None, "kr_limit_note": None}


def _product(pid, matched, rel=1.0, name=None):
    return {"product_id": pid, "product_name": name or f"제품{pid}", "brand": "B", "category": "세럼",
            "matched_count": len(matched), "matched_ingredients": matched, "relevance_score": rel}


class WhiteningExtractionTest(unittest.TestCase):
    def test_whitening_label_is_kept_with_wrinkles(self):
        self.assertEqual([PIG, WRK], _normalize_concerns(ISSUE_QUERY, [PIG, WRK]))

    def test_whitening_restored_when_model_omits_or_mislabels_it(self):
        self.assertEqual([WRK, PIG], _normalize_concerns(ISSUE_QUERY, [WRK]))
        # 칙칙함 언급이 없어 DULLNESS는 제거되지만 미백 요청은 남는다.
        self.assertEqual([WRK, PIG], _normalize_concerns(ISSUE_QUERY, [Concern.DULLNESS, WRK]))

    def test_specific_pigmentation_label_is_not_duplicated(self):
        self.assertEqual([Concern.UNEVEN_SKIN_TONE, WRK],
                         _normalize_concerns(ISSUE_QUERY, [Concern.UNEVEN_SKIN_TONE, WRK]))

    def test_negated_whitening_is_not_added(self):
        self.assertEqual([WRK], _normalize_concerns("미백 말고 주름 개선 세럼만 추천해줘", [WRK]))
        self.assertEqual([WRK], _normalize_concerns("미백은 필요 없고 주름 관리만 하고 싶어요", [PIG, WRK]))

    def test_rule_based_fallback_keeps_both_concerns(self):
        profile = extract_profile(ISSUE_QUERY)
        self.assertEqual({PIG, WRK}, set(profile.concerns))
        self.assertTrue({Effect.DEPIGMENTING, Effect.ANTI_AGING} <= set(profile.effects))


class CandidatePoolTest(unittest.TestCase):
    def test_single_concern_keeps_previous_order_and_pool(self):
        rows = [{**_row(f"A{i}", 1 - i / 100), "concerns": ["WRINKLES"]} for i in range(15)]
        pool = concern_ingredient_pool(rows, [WRK], 10)
        self.assertEqual([r["name"] for r in rows[:10]], [r["name"] for r in pool])
        self.assertEqual([float(r["graph_score"]) for r in rows[:10]], [r["weight"] for r in pool])

    def test_high_scoring_concern_does_not_push_out_the_other(self):
        aging = [_row(f"AGE{i}", 1.2 - i / 100, "Anti-aging") for i in range(20)]
        pigment = [_row(f"PIG{i}", 0.3 - i / 100) for i in range(20)]
        merged = merge_concern_candidates({PIG: pigment, WRK: aging}, settings.ingredient_candidate_limit)
        self.assertEqual(settings.ingredient_candidate_limit, len(merged))  # 후보 수는 늘리지 않는다
        pool = concern_ingredient_pool(merged, [PIG, WRK], 10)
        self.assertEqual(10, len(pool))
        self.assertEqual(5, sum(1 for r in pool if r["concerns"] == ["HYPERPIGMENTATION"]))
        self.assertEqual(5, sum(1 for r in pool if r["concerns"] == ["WRINKLES"]))
        # 각 고민 안의 순서는 원래 근거 순서를 따른다.
        self.assertEqual(["PIG0", "PIG1", "PIG2"], [r["name"] for r in pool if r["name"].startswith("PIG")][:3])

    def test_same_ingredient_with_multiple_effects_is_one_row_tagged_with_both(self):
        pigment = [_row("RETINOL", 0.2, "Depigmenting", "cosing_function"), _row("ARBUTIN", 0.5)]
        aging = [_row("RETINOL", 0.9, "Anti-aging"), _row("ADENOSINE", 0.8, "Anti-aging")]
        merged = merge_concern_candidates({PIG: pigment, WRK: aging}, 20)
        self.assertEqual(["RETINOL", "ADENOSINE", "ARBUTIN"], [r["name"] for r in merged])
        retinol = merged[0]
        self.assertEqual(["HYPERPIGMENTATION", "WRINKLES"], retinol["concerns"])
        # 대표 근거는 지어내지 않고 실제 행 중 더 강한 것(논문 근거)을 쓴다.
        self.assertEqual(("Anti-aging", "pubmed_evidence", 0.9),
                         (retinol["claim"], retinol["eligibility_tier"], retinol["graph_score"]))


class CoverageOrderTest(unittest.TestCase):
    POOL = [{"name": "TXA", "weight": 1.2, "concerns": ["HYPERPIGMENTATION"]},
            {"name": "RETINOL", "weight": 0.5, "concerns": ["WRINKLES"]},
            {"name": "SALMON", "weight": 0.7, "concerns": ["HYPERPIGMENTATION", "WRINKLES"]}]

    def test_full_coverage_ranks_before_higher_single_concern_relevance(self):
        products = [_product("a", ["RETINOL"], 3.0), _product("b", ["TXA"], 2.0),
                    _product("c", ["TXA", "RETINOL"], 1.0)]
        ordered = order_by_coverage(products, [PIG, WRK], self.POOL)
        self.assertEqual("c", ordered[0]["product_id"])
        self.assertEqual(["HYPERPIGMENTATION", "WRINKLES"], ordered[0]["concern_coverage"])

    def test_one_ingredient_with_both_effects_counts_for_both(self):
        ordered = order_by_coverage([_product("a", ["TXA"], 2.0), _product("s", ["SALMON"], 0.7)],
                                    [PIG, WRK], self.POOL)
        self.assertEqual(["s", "a"], [p["product_id"] for p in ordered])

    def test_ties_keep_previous_order(self):
        products = [_product(pid, ["TXA", "RETINOL"]) for pid in ("x", "y", "z")]
        ordered = order_by_coverage(products, [PIG, WRK], self.POOL)
        self.assertEqual(["x", "y", "z"], [p["product_id"] for p in ordered])

    def test_single_concern_order_is_unchanged(self):
        products = [_product("a", ["RETINOL"], 3.0), _product("c", ["TXA", "RETINOL"], 1.0)]
        ordered = order_by_coverage(products, [WRK], self.POOL)
        self.assertEqual(["a", "c"], [p["product_id"] for p in ordered])

    def test_partial_only_alternates_concerns(self):
        products = [_product("w1", ["RETINOL"], 3.0), _product("w2", ["RETINOL"], 2.9),
                    _product("w3", ["RETINOL"], 2.8), _product("p1", ["TXA"], 1.0)]
        ordered = order_by_coverage(products, [PIG, WRK], self.POOL)
        self.assertEqual(["p1", "w1", "w2", "w3"], [p["product_id"] for p in ordered])


class PartialCoverageNoteTest(unittest.TestCase):
    def test_note_names_each_products_covered_concern(self):
        note = partial_coverage_note([("레티놀 세럼", ["WRINKLES"]), ("트라넥 세럼", ["HYPERPIGMENTATION"])],
                                     [PIG, WRK])
        self.assertIn("미백·주름 고민을 모두 뒷받침하는 대표 근거 성분이 함께 들어 있는 제품은", note)
        self.assertIn("- 레티놀 세럼: 주름 고민 관련 성분 포함", note)
        self.assertIn("- 트라넥 세럼: 미백 고민 관련 성분 포함", note)
        for claim in ("효과가 더", "더 효과", "함량", "%"):
            self.assertNotIn(claim, note)

    def test_no_note_when_any_product_covers_all_or_single_concern(self):
        self.assertIsNone(partial_coverage_note([("A", ["HYPERPIGMENTATION", "WRINKLES"]), ("B", ["WRINKLES"])],
                                                [PIG, WRK]))
        self.assertIsNone(partial_coverage_note([("A", ["WRINKLES"])], [WRK]))
        self.assertIsNone(partial_coverage_note([], [PIG, WRK]))


class PurposeFilterTest(unittest.TestCase):
    def test_whitening_wrinkle_product_survives_only_when_whitening_is_extracted(self):
        # 라벨 없는 제품은 이름 휴리스틱을 쓴다. 미백이 추출에서 빠지면 '미백' 이름 제품이 목적 불일치로 제외됐다.
        product = _product("unlabeled-113", ["TXA", "RETINOL"], name="미백 주름 2중 기능성 세럼")
        self.assertEqual([], service.filter_by_target_concerns([product], [WRK]))
        self.assertEqual([product], service.filter_by_target_concerns([product], [PIG, WRK]))


def _profile(concerns):
    return UserProfile(skin_types=[], concerns=concerns, effects=infer_effects(concerns), constraints=[])


class RetrieveCandidatesTest(unittest.IsolatedAsyncioTestCase):
    async def test_single_concern_uses_one_query_with_profile_effects(self):
        rows = [_row(f"A{i}", 1 - i / 100, "Anti-aging") for i in range(12)]
        with (
            patch.object(service, "query_ingredients_by_effects", new=AsyncMock(return_value=rows)) as query,
            patch.object(service, "query_cautioned_ingredients", new=AsyncMock(return_value=set())),
        ):
            candidates, pool = await service.retrieve_ingredient_candidates(_profile([WRK]))
        query.assert_awaited_once()
        self.assertEqual(["ANTI_AGING"], query.await_args.args[0])
        self.assertEqual("WRINKLES", query.await_args.kwargs["concern"])
        self.assertEqual([r["name"] for r in rows], [r["name"] for r in candidates])
        self.assertEqual([r["name"] for r in rows[:settings.ingredient_product_pool]], [r["name"] for r in pool])

    async def test_multi_concern_queries_each_concern_effects(self):
        by_effects = {
            ("DEPIGMENTING", "BRIGHTENING"): [_row("TXA", 0.3), _row("ARBUTIN", 0.2)],
            ("ANTI_AGING",): [_row(f"AGE{i}", 1.2 - i / 100, "Anti-aging") for i in range(20)],
        }

        concerns_seen = []

        async def fake(effects, min_graph_score=0.0, concern=None):
            concerns_seen.append(concern)
            return by_effects[tuple(effects)]

        with patch.object(service, "query_ingredients_by_effects", new=AsyncMock(side_effect=fake)):
            candidates, pool = await service.retrieve_ingredient_candidates(_profile([PIG, WRK]))
        self.assertEqual(["HYPERPIGMENTATION", "WRINKLES"], concerns_seen)
        self.assertEqual(settings.ingredient_candidate_limit, len(candidates))
        self.assertIn("TXA", [r["name"] for r in pool])
        self.assertIn("ARBUTIN", [r["name"] for r in pool])
        self.assertEqual(settings.ingredient_product_pool, len(pool))

    async def test_caution_policy_still_applies_to_multi_concern_candidates(self):
        async def fake(effects, min_graph_score=0.0, concern=None):
            return [_row("IRRITANT", 0.9, "Soothing"), _row(f"OK{len(effects)}", 0.5, "Soothing")]

        with (
            patch.object(service, "query_ingredients_by_effects", new=AsyncMock(side_effect=fake)),
            patch.object(service, "query_cautioned_ingredients", new=AsyncMock(return_value={"IRRITANT"})),
        ):
            candidates, pool = await service.retrieve_ingredient_candidates(
                _profile([Concern.SENSITIVE_SKIN, PIG]))
        self.assertNotIn("IRRITANT", [r["name"] for r in candidates])
        self.assertNotIn("IRRITANT", [r["name"] for r in pool])


class SelectProductsCoverageTest(unittest.IsolatedAsyncioTestCase):
    async def test_select_products_ranks_by_coverage_and_keeps_exclusions(self):
        pool = CoverageOrderTest.POOL
        rows = [_product("a", ["RETINOL"], 3.0), _product("c", ["TXA", "RETINOL"], 1.0)]
        with patch.object(service, "query_products_by_ingredients",
                          new=AsyncMock(return_value=rows)) as query:
            out = await service.select_products("홍조랑 기미 세럼", [Concern.REDNESS, PIG], pool)
        self.assertEqual(["FARNESOL", "LINALOOL", "RETINAL", "RETINOL"],
                         query.call_args.kwargs["excluded_ingredients"])
        self.assertEqual("c", out[0]["product_id"])


class RecommendCoverageNoteTest(unittest.IsolatedAsyncioTestCase):
    async def _run(self, products):
        profile = SimpleNamespace(effects=infer_effects([PIG, WRK]), concerns=[PIG, WRK], constraints=[])
        rows = [{**_row("TXA", 1.2), "concerns": ["HYPERPIGMENTATION"]},
                {**_row("RETINOL", 0.5, "Anti-aging"), "concerns": ["WRINKLES"]}]
        pool = concern_ingredient_pool(rows, [PIG, WRK], 10)
        for product in products:
            product["concern_coverage"] = [code for row in pool if row["name"] in product["matched_ingredients"]
                                           for code in row["concerns"]]
        with (
            patch.object(settings, "recommend_cache_enabled", False),
            patch.object(service, "_resolve_conversation_response", new=AsyncMock(return_value=None)),
            patch.object(service, "_store_turn", new=AsyncMock()),
            patch.object(service, "extract_with_fallback", new=AsyncMock(return_value=(profile, "llm"))),
            patch.object(service, "retrieve_ingredient_candidates", new=AsyncMock(return_value=(rows, pool))),
            patch.object(service, "select_products", new=AsyncMock(return_value=products)),
            patch.object(service, "_attach_concern_summaries", new=AsyncMock()),
            patch.object(service, "_attach_ingredient_explanations", new=AsyncMock()),
            patch.object(service, "_build_llm_response", new=AsyncMock(return_value="완결된 추천 응답입니다.")),
            patch.object(service, "_has_product_grounding_violation", return_value=False),
            patch.object(service, "find_response_integrity_issues", return_value=[]),
            patch.object(service, "_verified_study_match", return_value=None),
        ):
            return await service.recommend("coverage-test", ISSUE_QUERY)

    async def test_partial_coverage_is_stated(self):
        response = await self._run([_product("w", ["RETINOL"], name="레티놀 세럼"),
                                    _product("p", ["TXA"], name="트라넥 세럼")])
        self.assertTrue(response.response_text.startswith("완결된 추천 응답입니다."))
        self.assertIn("미백·주름 고민을 모두 뒷받침하는", response.response_text)
        self.assertIn("- B 레티놀 세럼: 주름 고민 관련 성분 포함", response.response_text)

    async def test_full_coverage_has_no_note(self):
        response = await self._run([_product("c", ["TXA", "RETINOL"], name="둘다 세럼")])
        self.assertEqual("완결된 추천 응답입니다.", response.response_text)


if __name__ == "__main__":
    unittest.main()
