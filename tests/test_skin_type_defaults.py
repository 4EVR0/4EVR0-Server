"""고민 없이 피부 타입만 말한 요청을 기본 고민으로 추천한다."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings
from app.domain.enums import Concern, SkinType
from app.domain.user import UserProfile
from app.schemas.recommend import ProductResult
from app.services import recommend_service as service
from app.services.skin_type_defaults import apply_skin_type_defaults

TAIL = "특별한 고민이 있으면 알려 주세요."


@pytest.mark.parametrize("skin_types,concerns,note", [
    ([SkinType.COMBINATION], [Concern.OILY_SKIN, Concern.DEHYDRATED_SKIN],
     "복합성 피부를 피지·속건조 고민으로 보고 골랐어요. " + TAIL),
    ([SkinType.OILY], [Concern.OILY_SKIN], "지성 피부를 피지 고민으로 보고 골랐어요. " + TAIL),
    ([SkinType.DRY], [Concern.DRY_SKIN], "건성 피부를 건조 고민으로 보고 골랐어요. " + TAIL),
    ([SkinType.SENSITIVE], [Concern.SENSITIVE_SKIN], "민감성 피부를 민감 고민으로 보고 골랐어요. " + TAIL),
    ([SkinType.NORMAL], [Concern.DEHYDRATED_SKIN], "중성 피부라 기본 보습 위주로 골랐어요. " + TAIL),
    ([SkinType.COMBINATION, SkinType.OILY], [Concern.OILY_SKIN, Concern.DEHYDRATED_SKIN],
     "복합성·지성 피부를 피지·속건조 고민으로 보고 골랐어요. " + TAIL),
])
def test_skin_type_only_gets_default_concerns(skin_types, concerns, note):
    profile, text = apply_skin_type_defaults(UserProfile(skin_types=skin_types))
    assert profile.concerns == concerns
    assert profile.effects  # 효능도 채워져 성분 조회가 일어난다
    assert text == note


def test_explicit_concern_or_no_skin_type_is_unchanged():
    with_concern = UserProfile(skin_types=[SkinType.COMBINATION], concerns=[Concern.ACNE])
    assert apply_skin_type_defaults(with_concern) == (with_concern, None)
    empty = UserProfile()
    assert apply_skin_type_defaults(empty) == (empty, None)


def _product():
    return ProductResult(product_id="p1", product_name="제품 p1", brand="브랜드", category="크림",
                         matched_count=1, matched_ingredients=["CAFFEINE"])


@pytest.mark.parametrize("transport", ["batch", "stream"])
def test_combination_request_reaches_products_and_explains_the_mapping(transport):
    profile = UserProfile(skin_types=[SkinType.COMBINATION])
    text = ("고민 분석\n복합성 피부를 고려했습니다.\n성분 설명\n글리세린은 보습 역할을 합니다.\n"
            "추천 제품\n- 제품 p1: 글리세린이 확인되어 보습 관리 후보입니다.")

    async def chunks():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])

    async def create(**kwargs):
        return chunks() if kwargs.get("stream") else SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

    async def run():
        if transport == "batch":
            return (await service.recommend("test", "복합성인데 쓰기 좋은 화장품 추천")).model_dump()
        frames = [frame async for frame in service.recommend_stream("test", "복합성인데 쓰기 좋은 화장품 추천")]
        done = json.loads(frames[-1].split("data: ", 1)[1])
        response_text = "".join(json.loads(frame.split("data: ", 1)[1])["text"]
                                for frame in frames if frame.startswith("event: delta"))
        return {**done, "response_text": response_text}

    query = AsyncMock(return_value=[{"name": "GLYCERIN", "kor_name": "글리세린", "claim": "Hydrating",
                                     "eligibility_tier": "pubmed_review", "graph_score": 1.0, "paper_ref": "9"}])
    with (
        patch.object(settings, "recommend_cache_enabled", False),
        patch.object(settings, "conversation_enabled", False),
        patch.object(service, "_resolve_conversation_response", AsyncMock(return_value=None)),
        patch.object(service, "_store_turn", AsyncMock()),
        patch.object(service, "extract_with_fallback", AsyncMock(return_value=(profile, "llm"))),
        patch.object(service, "query_ingredients_by_effects", query),
        patch.object(service, "query_cautioned_ingredients", AsyncMock(return_value=set())),
        patch.object(service, "select_products", AsyncMock(return_value=[_product().model_dump()])),
        patch.object(service, "query_product_ingredient_inventory", AsyncMock(return_value={"p1": []})),
        patch.object(service, "query_product_concern_evidence", AsyncMock(return_value={})),
        patch.object(service, "get_async_llm_client", return_value=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)))),
    ):
        result = asyncio.run(run())
    # 고민별로 성분을 조회했다(피지·속건조).
    assert {call.kwargs.get("concern") for call in query.await_args_list} == {"OILY_SKIN", "DEHYDRATED_SKIN"}
    assert result["response_text"].startswith("복합성 피부를 피지·속건조 고민으로 보고 골랐어요. " + TAIL)
    assert "요청하신" not in result["response_text"]  # 부분 충족 안내 생략
