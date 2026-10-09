"""고민별 논문 근거(EVIDENCE_FOR)와 민감 피부 주의 표시 적용 (GraphRAG_Pipeline #49)."""

import unittest
from unittest import mock
from unittest.mock import AsyncMock, patch

from app.clients import neo4j_client
from app.domain.enums import Concern
from app.schemas.recommend import IngredientResult
from app.services import recommend_service as service
from app.services import sensitive_caution
from app.services.concern_coverage import concern_ingredient_pool, merge_concern_candidates
from app.services.concern_summary import build_summary
from app.services.ingredient_selection import select_recommended_ingredients

ACNE_FAMILY = ["ACNE", "COMEDONES", "PORE_CONGESTION", "ENLARGED_PORES", "OILY_SKIN"]


def _cand(name, tier="pubmed_evidence", score=0.5, claim="Soothing", caution="", caution_with=None):
    return {"name": name, "claim": claim, "eligibility_tier": tier, "graph_score": score, "paper_ref": "3",
            "sensitive_caution": caution, "sensitive_caution_with": caution_with or []}


class SensitiveCautionRuleTest(unittest.TestCase):
    def test_exclude_applies_only_to_sensitive_use_concerns(self):
        sa = _cand("SALICYLIC ACID", caution="exclude", caution_with=ACNE_FAMILY)
        self.assertTrue(sensitive_caution.is_excluded(sa, [Concern.SENSITIVE_SKIN]))
        self.assertTrue(sensitive_caution.is_excluded(sa, [Concern.ROSACEA_PRONE]))
        # 아토피·장벽 손상만 요청하면 적용하지 않는다.
        self.assertFalse(sensitive_caution.is_excluded(sa, [Concern.ATOPIC_PRONE]))
        self.assertFalse(sensitive_caution.is_excluded(sa, [Concern.ACNE]))

    def test_acne_family_relaxes_exfoliating_acid(self):
        sa = _cand("SALICYLIC ACID", caution="exclude", caution_with=ACNE_FAMILY)
        concerns = [Concern.SENSITIVE_SKIN, Concern.ACNE]
        self.assertFalse(sensitive_caution.is_excluded(sa, concerns))
        self.assertEqual(sensitive_caution.RELAXED_NOTE, sensitive_caution.caution_note(sa, concerns))
        # 완화 고민이 없는 exclude 성분(레티놀)은 여드름과 함께여도 뺀다.
        self.assertTrue(sensitive_caution.is_excluded(_cand("RETINOL", caution="exclude"), concerns))

    def test_caution_level_gets_note_and_missing_property_is_noop(self):
        rp = _cand("RETINYL PALMITATE", caution="caution")
        self.assertFalse(sensitive_caution.is_excluded(rp, [Concern.SENSITIVE_SKIN]))
        self.assertEqual(sensitive_caution.CAUTION_NOTE, sensitive_caution.caution_note(rp, [Concern.SENSITIVE_SKIN]))
        self.assertIsNone(sensitive_caution.caution_note(rp, [Concern.WRINKLES]))
        plain = {"name": "PANTHENOL"}
        self.assertFalse(sensitive_caution.is_excluded(plain, [Concern.SENSITIVE_SKIN]))
        self.assertIsNone(sensitive_caution.caution_note(plain, [Concern.SENSITIVE_SKIN]))


class ApplyCautionFilterTest(unittest.IsolatedAsyncioTestCase):
    def _rows(self):
        return [
            _cand("SALICYLIC ACID", caution="exclude", caution_with=ACNE_FAMILY),
            _cand("LACTIC ACID", caution="exclude", caution_with=ACNE_FAMILY),
            _cand("MENTHOL", caution="exclude"),
            _cand("RETINYL PALMITATE", caution="caution"),
            _cand("PANTHENOL"),
        ]

    async def test_sensitive_only_request_drops_exclude_and_notes_caution(self):
        with patch.object(service, "query_cautioned_ingredients", new=AsyncMock(return_value=set())):
            kept = await service.apply_caution_filter(self._rows(), [Concern.SENSITIVE_SKIN])
        self.assertEqual(["RETINYL PALMITATE", "PANTHENOL"], [r["name"] for r in kept])
        self.assertEqual(sensitive_caution.CAUTION_NOTE, kept[0]["sensitive_note"])
        self.assertNotIn("sensitive_note", kept[1])

    async def test_sensitive_plus_acne_keeps_acids_even_with_caution_edge(self):
        # 락틱애씨드는 기존 CAUTION 엣지도 있지만 여드름과 함께 요청하면 남긴다.
        with patch.object(service, "query_cautioned_ingredients",
                          new=AsyncMock(return_value={"LACTIC ACID", "MENTHOL"})):
            kept = await service.apply_caution_filter(self._rows(), [Concern.SENSITIVE_SKIN, Concern.ACNE])
        names = [r["name"] for r in kept]
        self.assertEqual(["SALICYLIC ACID", "LACTIC ACID", "RETINYL PALMITATE", "PANTHENOL"], names)
        self.assertEqual(sensitive_caution.RELAXED_NOTE, kept[1]["sensitive_note"])

    async def test_caution_edge_without_relaxation_still_applies(self):
        with patch.object(service, "query_cautioned_ingredients", new=AsyncMock(return_value={"LACTIC ACID"})):
            kept = await service.apply_caution_filter(self._rows(), [Concern.IRRITATED_SKIN])
        self.assertNotIn("LACTIC ACID", [r["name"] for r in kept])

    def test_note_reaches_generation_input(self):
        ingredient = IngredientResult(name="SALICYLIC ACID", claim="Comedolytic",
                                      sensitive_note=sensitive_caution.RELAXED_NOTE)
        content = service._compose_user_content("민감성 피부인데 여드름", [ingredient], [])
        self.assertIn(f"(민감 피부 주의: {sensitive_caution.RELAXED_NOTE})", content)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def __aiter__(self):
        async def gen():
            for row in self._rows:
                yield row
        return gen()


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def run(self, query, **params):
        self.calls.append((query, params))
        return _Result(self.responses.pop(0))


class ConcernQueryTest(unittest.IsolatedAsyncioTestCase):
    async def _query(self, responses, **kwargs):
        session = _Session(responses)
        driver = mock.Mock()
        driver.session.return_value = session
        with mock.patch.object(neo4j_client, "_get_driver", return_value=driver):
            rows = await neo4j_client.query_ingredients_by_effects(["COMEDOLYTIC", "BLEMISH_CARE"], **kwargs)
        return rows, session

    async def test_without_concern_runs_effect_query_only(self):
        rows, session = await self._query([[_cand("NIACINAMIDE") | {"ev_rank": 0}]])
        self.assertEqual(1, len(session.calls))
        self.assertFalse(session.calls[0][1]["by_concern"])
        self.assertEqual(["NIACINAMIDE"], [r["name"] for r in rows])

    async def test_concern_evidence_comes_first_one_row_per_ingredient(self):
        effect_rows = [_cand("NIACINAMIDE", score=1.0) | {"ev_rank": 0},
                       _cand("SALICYLIC ACID", score=0.24) | {"ev_rank": 0},
                       _cand("BORON NITRIDE", "reference_book", 0.2) | {"ev_rank": 1}]
        review_rows = [_cand("SALICYLIC ACID", "pubmed_review", 2.46) | {"ev_rank": -1},
                       _cand("GLYCOLIC ACID", "pubmed_review", 1.10) | {"ev_rank": -1}]
        rows, session = await self._query([effect_rows, review_rows], concern="ACNE")
        effect_query, effect_params = session.calls[0]
        self.assertTrue(effect_params["by_concern"])
        self.assertIn("coalesce(i.evidence_reviewed, false)", effect_query)
        self.assertIn("EVIDENCE_FOR", session.calls[1][0])
        self.assertEqual("ACNE", session.calls[1][1]["concern"])
        self.assertEqual(["SALICYLIC ACID", "GLYCOLIC ACID", "NIACINAMIDE", "BORON NITRIDE"],
                         [r["name"] for r in rows])
        self.assertEqual("pubmed_review", rows[0]["eligibility_tier"])


class ReviewTierTest(unittest.TestCase):
    def test_review_tier_outranks_pubmed_in_selection_and_merge(self):
        rows = [_cand("NIACINAMIDE", "pubmed_evidence", 1.0, "Sebum regulation"),
                _cand("SALICYLIC ACID", "pubmed_review", 0.8, "Comedolytic")]
        chosen = select_recommended_ingredients(rows, [], default_k=1, max_k=1, score_ratio=0.5, product_bonus=0.0)
        self.assertEqual(["SALICYLIC ACID"], [r["name"] for r in chosen])
        merged = merge_concern_candidates({Concern.ACNE: [rows[0]], Concern.COMEDONES: [rows[1], rows[0]]}, 5)
        self.assertEqual({"ACNE", "COMEDONES"}, set(next(r for r in merged if r["name"] == "NIACINAMIDE")["concerns"]))

    def test_labels(self):
        self.assertEqual("논문 근거 34건", service._evidence_label("pubmed_review", "34"))
        self.assertEqual("고민 관련 논문 근거", service._source_label("pubmed_review", "x"))


def _ev(inci, kor, effect, tier="reference_book", score=0.2, papers=0, concern=None, caution="", caution_with=None):
    row = {"inci_name": inci, "kor_name": kor, "effect_code": effect, "evidence_type": tier, "graph_score": score,
           "paper_count": papers, "medical_wording": False, "sensitive_caution": caution,
           "sensitive_caution_with": caution_with or []}
    if concern:
        row["concern_code"] = concern
    return row


class ConcernSummaryReviewTest(unittest.TestCase):
    def test_review_rows_count_only_for_their_concern_and_lead_key_ingredients(self):
        rows = [
            _ev("SALICYLIC ACID", "살리실릭애씨드", "COMEDOLYTIC", "pubmed_review", 2.46, 34, concern="ACNE"),
            _ev("UREA", "우레아", "KERATOLYTIC", "pubmed_review", 2.9, 20, concern="DRY_SKIN"),
            _ev("ZINC PCA", "징크피씨에이", "SEBUM_REGULATION", "pubmed_evidence", 0.5, 2),
        ]
        s = build_summary([Concern.ACNE], rows, {})
        self.assertEqual([("트러블", 2)], [(c["label"], c["count"]) for c in s["concerns"]])
        self.assertEqual("살리실릭애씨드", s["key_ingredients"][0]["name"])
        self.assertEqual("논문 근거 34건", s["key_ingredients"][0]["evidence"])

    def test_sensitive_exclude_is_dropped_from_summary(self):
        rows = [
            _ev("MENTHOL", "멘톨", "SOOTHING", "reference_book", caution="exclude"),
            _ev("PANTHENOL", "판테놀", "SOOTHING", "pubmed_review", 0.45, 7, concern="SENSITIVE_SKIN"),
        ]
        s = build_summary([Concern.SENSITIVE_SKIN], rows, {})
        self.assertEqual(["판테놀"], s["concerns"][0]["names"])


class RelaxedAcidProductTest(unittest.IsolatedAsyncioTestCase):
    async def test_relaxed_acids_share_one_score_group_in_pool(self):
        rows = [_cand("SALICYLIC ACID", "pubmed_review", 2.4, caution="exclude", caution_with=ACNE_FAMILY)
                | {"concerns": ["ACNE"]},
                _cand("GLYCOLIC ACID", "pubmed_review", 1.1, caution="exclude", caution_with=ACNE_FAMILY)
                | {"concerns": ["ACNE"]},
                _cand("NIACINAMIDE", "pubmed_review", 1.4) | {"concerns": ["SENSITIVE_SKIN", "ACNE"]}]
        concerns = [Concern.SENSITIVE_SKIN, Concern.ACNE]
        with patch.object(service, "query_cautioned_ingredients", new=AsyncMock(return_value=set())):
            kept = await service.apply_caution_filter(rows, concerns)
        pool = concern_ingredient_pool(kept, concerns, 10)
        groups = {r["name"]: r.get("score_group") for r in pool}
        self.assertEqual(sensitive_caution.RELAXED_SCORE_GROUP, groups["SALICYLIC ACID"])
        self.assertEqual(sensitive_caution.RELAXED_SCORE_GROUP, groups["GLYCOLIC ACID"])
        self.assertIsNone(groups["NIACINAMIDE"])

    async def test_acne_only_request_has_no_group(self):
        row = _cand("SALICYLIC ACID", caution="exclude", caution_with=ACNE_FAMILY)
        self.assertIsNone(sensitive_caution.score_group(row, [Concern.ACNE]))

    async def test_peel_products_dropped_only_for_sensitive_use(self):
        products = [{"product_id": "1", "product_name": "그린토마토 애시드 20 워시오프 필링 세럼", "category": "세럼",
                     "matched_count": 3, "matched_ingredients": ["SALICYLIC ACID"], "relevance_score": 3.0},
                    {"product_id": "2", "product_name": "시카 포어 세럼", "category": "세럼",
                     "matched_count": 1, "matched_ingredients": ["SALICYLIC ACID"], "relevance_score": 2.0}]
        with patch.object(service, "query_products_by_ingredients", new=AsyncMock(return_value=products)):
            sensitive = await service.select_products("민감성 피부인데 여드름 세럼", [Concern.SENSITIVE_SKIN, Concern.ACNE],
                                                      [{"name": "SALICYLIC ACID", "weight": 2.0}])
            acne = await service.select_products("여드름 세럼", [Concern.ACNE], [{"name": "SALICYLIC ACID", "weight": 2.0}])
        self.assertEqual(["2"], [p["product_id"] for p in sensitive])
        self.assertIn("1", [p["product_id"] for p in acne])

    def test_product_query_counts_score_group_once(self):
        import inspect
        source = inspect.getsource(neo4j_client.query_products_by_ingredients)
        self.assertIn("coalesce(isc.score_group, i.inci_name) AS grp", source)
        self.assertIn("MAX(weight) AS group_weight", source)


class SupportedClaimsTest(unittest.TestCase):
    def test_guard_allows_every_supported_effect_but_not_others(self):
        from app.services.ingredient_claim_guard import has_ingredient_claim_violation
        sa = IngredientResult(name="SALICYLIC ACID", kor_name="살리실릭애씨드", claim="Comedolytic",
                              supported_claims=["Comedolytic", "Sebum regulation", "Anti-inflammatory", "Blemish care"])
        ok = "성분 설명\n- 살리실릭애씨드: 모공 막힘과 피지, 트러블 진정에 도움이 된다고 알려져 있어요."
        bad = "성분 설명\n- 살리실릭애씨드: 주름 개선에 도움이 된다고 알려져 있어요."
        self.assertFalse(has_ingredient_claim_violation(ok, [sa]))
        self.assertTrue(has_ingredient_claim_violation(bad, [sa]))
        # 대표 효능 하나만 있으면 예전처럼 좁게 검사한다.
        narrow = IngredientResult(name="SALICYLIC ACID", kor_name="살리실릭애씨드", claim="Anti-inflammatory")
        self.assertTrue(has_ingredient_claim_violation(ok, [narrow]))

    def test_merge_unions_supported_claims_across_rows_and_concerns(self):
        rows = neo4j_client._merge_concern_evidence(
            [_cand("SALICYLIC ACID", "pubmed_review", 2.4, "Comedolytic")
             | {"ev_rank": -1, "supported_claims": ["Comedolytic", "Anti-inflammatory"]}],
            [_cand("SALICYLIC ACID", "reference_book", 0.2, "Keratolytic") | {"ev_rank": 1, "supported_claims": ["Keratolytic"]}],
            10)
        self.assertEqual("Comedolytic", rows[0]["claim"])
        self.assertEqual(["Comedolytic", "Anti-inflammatory", "Keratolytic"], rows[0]["supported_claims"])
        merged = merge_concern_candidates({
            Concern.ACNE: [rows[0]],
            Concern.HYPERPIGMENTATION: [_cand("SALICYLIC ACID", "pubmed_review", 0.5, "Depigmenting")
                                        | {"supported_claims": ["Depigmenting"]}],
        }, 5)
        self.assertEqual(["Comedolytic", "Anti-inflammatory", "Keratolytic", "Depigmenting"],
                         merged[0]["supported_claims"])

    def test_queries_order_claims_and_limit_affects_claims_to_papers_and_books(self):
        import inspect
        self.assertIn("$claim_priority", neo4j_client._CONCERN_EVIDENCE_QUERY)
        self.assertEqual("COMEDOLYTIC", neo4j_client.CLAIM_PRIORITY[0])
        self.assertEqual(["SOOTHING", "ANTI_INFLAMMATORY", "BLEMISH_CARE"], neo4j_client.CLAIM_PRIORITY[-3:])
        source = inspect.getsource(neo4j_client.query_ingredients_by_effects)
        self.assertIn("r.evidence_type IN ['pubmed_evidence', 'reference_book']", source)


if __name__ == "__main__":
    unittest.main()
