import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.core.config import settings
from app.schemas.recommend import IngredientResult
from app.services.ingredient_selection import ingredient_family, select_recommended_ingredients
from app.services.recommend_service import _finalize_ingredients

POLICY = dict(default_k=3, max_k=5, score_ratio=0.6, product_bonus=0.2)


def _row(name, score, claim="Brightening", tier="pubmed_evidence"):
    return {"name": name, "graph_score": score, "claim": claim, "eligibility_tier": tier}


def _names(rows):
    return [r["name"] for r in rows]


def test_single_effect_defaults_to_three_above_ratio():
    rows = [_row("NIACINAMIDE", 1.0), _row("ARBUTIN", 0.9), _row("TRANEXAMIC ACID", 0.7),
            _row("GLYCERIN", 0.65), _row("ADENOSINE", 0.3)]
    assert _names(select_recommended_ingredients(rows, [], **POLICY)) == [
        "NIACINAMIDE", "ARBUTIN", "TRANEXAMIC ACID"]


def test_score_ratio_cuts_weak_candidates_but_keeps_at_least_one():
    rows = [_row("NIACINAMIDE", 1.0), _row("ADENOSINE", 0.5), _row("GLYCERIN", 0.2)]
    assert _names(select_recommended_ingredients(rows, [], **POLICY)) == ["NIACINAMIDE"]


def test_cosing_only_candidates_do_not_follow_pubmed_top():
    rows = [_row("NIACINAMIDE", 0.2), _row("BUTYLENE GLYCOL", 0.15, tier="cosing_function")]
    assert _names(select_recommended_ingredients(rows, [], **POLICY)) == ["NIACINAMIDE"]


def test_same_family_is_deduplicated():
    rows = [_row("SODIUM HYALURONATE", 1.0), _row("HYALURONIC ACID", 0.95),
            _row("ASCORBIC ACID", 0.9), _row("3-O-ETHYL ASCORBIC ACID", 0.9), _row("PANTHENOL", 0.8)]
    assert _names(select_recommended_ingredients(rows, [], **POLICY)) == [
        "SODIUM HYALURONATE", "ASCORBIC ACID", "PANTHENOL"]
    assert ingredient_family("CERAMIDE NP") == ingredient_family("CERAMIDE AP")
    assert ingredient_family("SODIUM PCA") == "PCA"


def test_product_contained_bonus_reorders():
    rows = [_row("ARBUTIN", 1.0), _row("NIACINAMIDE", 0.9), _row("TRANEXAMIC ACID", 0.85),
            _row("ADENOSINE", 0.84)]
    products = [{"matched_ingredients": ["ADENOSINE"]}]
    # ADENOSINE 0.84 × 1.2 = 1.008 → 1위
    assert _names(select_recommended_ingredients(rows, products, **POLICY)) == [
        "ADENOSINE", "ARBUTIN", "NIACINAMIDE"]


def test_weak_effect_groups_do_not_get_a_slot():
    # 효능이 여러 개여도 1위 × 0.6 미만 그룹은 자리를 받지 못한다
    rows = [_row("NIACINAMIDE", 1.0, "Brightening"), _row("ARBUTIN", 0.9, "Brightening"),
            _row("TRANEXAMIC ACID", 0.9, "Brightening"),
            _row("CERAMIDE NP", 0.5, "Barrier"), _row("TREHALOSE", 0.1, "Antimicrobial")]
    assert _names(select_recommended_ingredients(rows, [], **POLICY)) == [
        "NIACINAMIDE", "ARBUTIN", "TRANEXAMIC ACID"]


def test_product_bonus_scales_with_how_many_products_contain_it():
    # 6개 제품 중 1개에만 있는 성분보다 모든 제품에 있는 성분이 앞선다(케이스 39 재현)
    rows = [_row("SALMON EGG EXTRACT", 0.693, "Hydrating"), _row("NIACINAMIDE", 0.673, "Hydrating")]
    products = [{"matched_ingredients": ["NIACINAMIDE"]} for _ in range(5)] + [
        {"matched_ingredients": ["SALMON EGG EXTRACT", "NIACINAMIDE"]}]
    assert _names(select_recommended_ingredients(rows, products, **POLICY))[0] == "NIACINAMIDE"


def test_eligible_effects_are_covered_round_robin():
    rows = [_row("NIACINAMIDE", 1.0, "Brightening"), _row("ARBUTIN", 0.95, "Brightening"),
            _row("TRANEXAMIC ACID", 0.9, "Brightening"),
            _row("CERAMIDE NP", 0.8, "Barrier"), _row("PANTHENOL", 0.7, "Soothing")]
    assert _names(select_recommended_ingredients(rows, [], **POLICY)) == [
        "NIACINAMIDE", "CERAMIDE NP", "PANTHENOL"]


def test_many_effects_are_capped_at_max():
    rows = [_row(f"ING {i}", 1.0 - i * 0.01, f"Effect {i}") for i in range(7)]
    assert len(select_recommended_ingredients(rows, [], **POLICY)) == 5


def test_two_effects_fill_second_slot_from_stronger_effect():
    rows = [_row("NIACINAMIDE", 1.0, "Brightening"), _row("ARBUTIN", 0.9, "Brightening"),
            _row("CERAMIDE NP", 0.7, "Barrier"), _row("CERAMIDE AP", 0.7, "Barrier")]
    assert _names(select_recommended_ingredients(rows, [], **POLICY)) == [
        "NIACINAMIDE", "CERAMIDE NP", "ARBUTIN"]


def test_empty_candidates():
    assert select_recommended_ingredients([], [], **POLICY) == []


def test_finalize_respects_flag_and_keeps_candidate_objects():
    rows = [_row("NIACINAMIDE", 1.0), _row("ARBUTIN", 0.9), _row("TRANEXAMIC ACID", 0.8),
            _row("ADENOSINE", 0.75)]
    candidates = [IngredientResult(name=r["name"]) for r in rows]
    with patch.object(settings, "ingredient_selection_enabled", True):
        final = _finalize_ingredients(rows, [], candidates)
    assert [i.name for i in final] == ["NIACINAMIDE", "ARBUTIN", "TRANEXAMIC ACID"]
    assert final[0] is candidates[0]
    with patch.object(settings, "ingredient_selection_enabled", False):
        assert _finalize_ingredients(rows, [], candidates) is candidates


class RecommendSelectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_recommend_returns_final_ingredients_but_templates_see_candidates(self):
        from app.services import recommend_service as service

        rows = [_row(n, s) for n, s in (("NIACINAMIDE", 1.0), ("ARBUTIN", 0.9), ("TRANEXAMIC ACID", 0.8),
                                         ("ADENOSINE", 0.75), ("GLYCERIN", 0.7))]
        product = {"product_id": "p1", "product_name": "앰플", "brand": "B", "category": "앰플",
                   "matched_count": 2, "matched_ingredients": ["NIACINAMIDE", "GLYCERIN"]}
        profile = SimpleNamespace(effects=[], concerns=[], constraints=[])
        with (
            patch.object(settings, "recommend_cache_enabled", False),
            patch.object(service, "_resolve_conversation_response", new=AsyncMock(return_value=None)),
            patch.object(service, "_store_turn", new=AsyncMock()),
            patch.object(service, "extract_with_fallback", new=AsyncMock(return_value=(profile, "llm"))),
            patch.object(service, "query_ingredients_by_effects", new=AsyncMock(return_value=rows)),
            patch.object(service, "select_products", new=AsyncMock(return_value=[product])),
            patch.object(service, "_build_llm_response", new=AsyncMock(return_value="완결된 추천 응답입니다.")) as llm,
            patch.object(service, "_verified_study_match", return_value=None) as study,
        ):
            response = await service.recommend("selection-test", "칙칙해요")
        # GLYCERIN(0.7×1.2=0.84)이 제품 가점으로 TRANEXAMIC ACID(0.8)를 앞선다
        assert [i.name for i in response.ingredients] == ["NIACINAMIDE", "ARBUTIN", "GLYCERIN"]
        assert [i.name for i in llm.await_args.args[1]] == ["NIACINAMIDE", "ARBUTIN", "GLYCERIN"]
        assert len(study.call_args.args[2]) == 5


def test_cache_key_changes_with_selection_policy():
    from app.repositories import recommend_cache

    with patch.object(settings, "ingredient_selection_enabled", True):
        base = recommend_cache._key("칙칙해요", None)
        with patch.object(settings, "ingredient_score_ratio", 0.7):
            assert recommend_cache._key("칙칙해요", None) != base
        with patch.object(settings, "ingredient_final_default", 5):
            assert recommend_cache._key("칙칙해요", None) != base
    with patch.object(settings, "ingredient_selection_enabled", False):
        assert recommend_cache._key("칙칙해요", None) != base
