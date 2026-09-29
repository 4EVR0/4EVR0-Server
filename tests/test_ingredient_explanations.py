import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings
from app.domain.enums import Concern
from app.repositories import recommend_cache
from app.schemas.recommend import ProductResult
from app.services import recommend_service as service
from app.services.ingredient_explanations import cards_for_inventory, product_explanations
from app.services.response_integrity import find_response_integrity_issues
from eval.hard_checks import check_response
from eval.run_response_eval import render_evidence_context


def product(product_id="p1"):
    return ProductResult(product_id=product_id, product_name=f"제품 {product_id}", brand="브랜드",
                         category="크림", matched_count=1, matched_ingredients=["CAFFEINE"])


def explained(product_id="p1"):
    row = product(product_id)
    row.ingredient_explanations = cards_for_inventory([{"name": "GLYCERIN"}], [Concern.DRY_SKIN])
    return row


def test_exact_inci_not_substring_or_related_substance():
    rows = [{"name": "Hyaluronic Acid"}, {"name": "GLYCERIN EXTRACT"},
            {"name": " sodium hyaluronate "}]
    cards = cards_for_inventory(rows, [Concern.DRY_SKIN])
    assert [card.name for card in cards] == ["SODIUM HYALURONATE"]
    assert cards[0].source_page == 93


@pytest.mark.parametrize("concerns", [[], [Concern.HYPERPIGMENTATION], [Concern.SENSITIVE_SKIN],
                                    [Concern.BARRIER_DAMAGE], [Concern.ROUGH_TEXTURE]])
def test_no_extension_to_unreviewed_concerns(concerns):
    assert cards_for_inventory([{"name": "GLYCERIN"}], concerns) == []


def test_explanation_version_and_paraphrase_validated():
    row = explained()
    assert len(product_explanations(row)) == 1
    archived = row.model_dump()
    assert product_explanations(archived) == product_explanations(row)
    archived["ingredient_explanations"][0]["explanation"] = "자극 없이 피부 깊이 흡수됩니다."
    assert product_explanations(archived) == []
    archived["ingredient_explanations"] = [{"name": "GLYCERIN"}]
    assert product_explanations(archived) == []


def test_attach_only_confirmed_products_without_changing_ranking_or_matches():
    rows = [product("p2"), product("p1")]
    before = [row.model_dump() for row in rows]
    inventory = AsyncMock(return_value={"p1": [{"name": "GLYCERIN"}, {"name": "GLYCERIN"}]})
    with patch.object(settings, "dictionary_explanations_enabled", True), \
         patch.object(service, "query_product_ingredient_inventory", inventory):
        asyncio.run(service._attach_ingredient_explanations(rows, [Concern.DRY_SKIN]))
    inventory.assert_awaited_once_with(["p2", "p1"])
    assert rows[0].ingredient_explanations == []
    assert len(rows[1].ingredient_explanations) == 1
    assert [{k: v for k, v in row.model_dump().items() if k != "ingredient_explanations"} for row in rows] == \
           [{k: v for k, v in row.items() if k != "ingredient_explanations"} for row in before]


@pytest.mark.parametrize("enabled,concerns", [(False, [Concern.DRY_SKIN]),
                                            (True, [Concern.HYPERPIGMENTATION]), (True, [])])
def test_disabled_or_off_target_does_not_read_inventory_and_clears_stale_card(enabled, concerns):
    rows = [explained()]
    inventory = AsyncMock()
    with patch.object(settings, "dictionary_explanations_enabled", enabled), \
         patch.object(service, "query_product_ingredient_inventory", inventory):
        asyncio.run(service._attach_ingredient_explanations(rows, concerns))
    inventory.assert_not_awaited()
    assert rows[0].ingredient_explanations == []


def test_missing_inventory_fails_closed():
    rows = [product()]
    with patch.object(settings, "dictionary_explanations_enabled", True), \
         patch.object(service, "query_product_ingredient_inventory", AsyncMock(return_value={})):
        asyncio.run(service._attach_ingredient_explanations(rows, [Concern.DRY_SKIN]))
    assert rows[0].ingredient_explanations == []


def test_generation_judge_and_fallback_share_product_evidence():
    row = explained()
    card = row.ingredient_explanations[0]
    generated_input = service._compose_user_content("건조해요", [], [row])
    judge_input = render_evidence_context([], [row])["products"]
    fallback = service._build_grounded_product_response("건조해요", [], [row], [Concern.DRY_SKIN])
    followup, _ = service._build_safe_followup_response("이 중 크림만", [row], {})
    for text in (generated_input, judge_input, fallback, followup):
        assert card.explanation in text
        assert card.kor_name in text
    assert "화장품성분학" not in fallback
    assert "p.25" not in fallback
    assert not service._has_product_grounding_violation(fallback, [], [row])
    assert check_response({}, {"products": [row.model_dump()], "response_text": fallback}) == []


def test_grounding_does_not_transfer_card_to_another_product():
    rows = [explained("p1"), product("p2")]
    text = "추천 제품\n- 제품 p2: 글리세린의 수분 유지 역할 때문에 추천합니다."
    assert service._has_product_grounding_violation(text, [], rows)
    failures = check_response({}, {"products": [row.model_dump() for row in rows], "response_text": text})
    assert "PRODUCT_INGREDIENT_MISMATCH" in {failure.code for failure in failures}


def test_known_dictionary_inci_is_not_stray_english():
    assert find_response_integrity_issues("GLYCERIN 성분을 확인했습니다.", [], [explained()]) == []


def test_cache_keys_separate_off_on_and_card_changes_without_changing_off_key():
    with patch.object(settings, "dictionary_explanations_enabled", False):
        off = recommend_cache._key("건조해요", None)
    with patch.object(settings, "dictionary_explanations_enabled", True):
        on = recommend_cache._key("건조해요", None)
        with patch.object(recommend_cache, "CARD_SHA256", "new-version"):
            changed = recommend_cache._key("건조해요", None)
    assert len({off, on, changed}) == 3
    with patch.object(settings, "dictionary_explanations_enabled", False):
        assert recommend_cache._key("건조해요", None) == off


def test_session_does_not_permanently_copy_explanations():
    slim = service._slim_products([explained()])
    assert "ingredient_explanations" not in slim[0]
    assert service._reconstruct_products(slim)[0].ingredient_explanations == []


@pytest.mark.parametrize("transport", ["batch", "stream"])
@pytest.mark.parametrize("corrupted", [False, True])
def test_serving_transports_attach_before_generation_and_keep_cards_after_guard(transport, corrupted):
    from app.domain.user import UserProfile
    profile = UserProfile(concerns=[Concern.DRY_SKIN])
    text = ("推薦 BROKENWORD" if corrupted else
            "고민 분석\n건조함을 고려했습니다.\n성분 설명\n글리세린은 보습 역할을 합니다.\n"
            "추천 제품\n- 제품 p1: 글리세린이 확인되어 건조함 관리 후보입니다.")
    contents = []

    async def chunks():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])

    async def create(**kwargs):
        contents.append(kwargs["messages"][1]["content"])
        return chunks() if kwargs.get("stream") else SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

    async def run():
        if transport == "batch":
            return (await service.recommend("test", "건조해요")).model_dump()
        frames = [frame async for frame in service.recommend_stream("test", "건조해요")]
        meta = next(json.loads(frame.split("data: ", 1)[1]) for frame in frames if frame.startswith("event: meta"))
        done = json.loads(frames[-1].split("data: ", 1)[1])
        response_text = "".join(json.loads(frame.split("data: ", 1)[1])["text"]
                                for frame in frames if frame.startswith("event: delta"))
        return {**meta, **done, "response_text": response_text}

    inventory = AsyncMock(return_value={"p1": [{"name": "GLYCERIN"}]})
    with (
        patch.object(settings, "dictionary_explanations_enabled", True),
        patch.object(settings, "recommend_cache_enabled", False),
        patch.object(settings, "conversation_enabled", False),
        patch.object(service, "_resolve_conversation_response", AsyncMock(return_value=None)),
        patch.object(service, "_store_turn", AsyncMock()),
        patch.object(service, "extract_with_fallback", AsyncMock(return_value=(profile, "llm"))),
        patch.object(service, "query_ingredients_by_effects", AsyncMock(return_value=[])),
        patch.object(service, "select_products", AsyncMock(return_value=[product().model_dump()])),
        patch.object(service, "query_product_ingredient_inventory", inventory),
        patch.object(service, "get_async_llm_client", return_value=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)))),
    ):
        result = asyncio.run(run())
    inventory.assert_awaited_once_with(["p1"])
    assert "수분을 끌어당기고 유지" in contents[0]
    assert result["products"][0]["ingredient_explanations"][0]["name"] == "GLYCERIN"
    assert result["response_mode"] == ("quality_fallback" if corrupted else "generated")
    assert "글리세린" in result["response_text"]
    assert check_response({}, result) == []
