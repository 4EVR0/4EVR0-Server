"""착향제 알레르기 유발 성분 표시 안내 (GraphRAG_Pipeline #49 후속)."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings
from app.domain.enums import Concern
from app.schemas.recommend import ProductResult
from app.services import recommend_service as service
from app.services.fragrance_allergens import allergen_names, allergen_note, allergens_in
from eval.hard_checks import check_response

NOTE_TAIL = "향료에 민감하다면 제품 상세 정보의 전성분을 꼭 확인해 주세요."


def test_list_is_the_25_regulated_allergens_with_alias():
    names = allergen_names()
    assert len({v for v in names.values()}) == 25
    assert names["LINALOOL"] == "리날룰" and names["LIMONENE"] == "리모넨"
    assert names["ANISYL ALCOHOL"] == names["ANISE ALCOHOL"] == "아니스에탄올"
    assert "SALICYLIC ACID" not in names


def test_allergens_in_inventory_and_note_wording():
    found = allergens_in([{"name": "LIMONENE"}, {"name": "glycerin"}, {"name": "linalool"}, {"name": "LIMONENE"}])
    assert found == ["리날룰", "리모넨"]
    assert allergen_note([("브랜드 토너", found), ("브랜드 크림", [])]) == (
        "참고: 브랜드 토너 제품에는 향료 알레르기 유발 가능 성분(리날룰·리모넨)이 표시돼 있어요. " + NOTE_TAIL)
    assert allergen_note([("브랜드 크림", [])]) is None


def _product(product_id="p1"):
    return ProductResult(product_id=product_id, product_name=f"제품 {product_id}", brand="브랜드",
                         category="크림", matched_count=1, matched_ingredients=["CAFFEINE"])


@pytest.mark.parametrize("transport", ["batch", "stream"])
@pytest.mark.parametrize("with_allergen", [True, False])
@pytest.mark.parametrize("concern", [Concern.DRY_SKIN, Concern.SENSITIVE_SKIN])
def test_note_is_appended_after_guard_on_every_transport(transport, with_allergen, concern):
    from app.domain.user import UserProfile
    # 민감 계열 요청은 생성 문장에 리날룰이 나오면 가드가 응답을 교체한다. 안내는 가드 뒤에 붙어
    # 향료 가드(fragrance_rationale_fallback)를 일으키지 않아야 하고, 어떤 응답 모드에도 붙어야 한다.
    profile = UserProfile(concerns=[concern])
    text = ("고민 분석\n민감한 피부를 고려했습니다.\n성분 설명\n글리세린은 보습 역할을 합니다.\n"
            "추천 제품\n- 제품 p1: 글리세린이 확인되어 민감한 피부 관리 후보입니다.")

    async def chunks():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])

    async def create(**kwargs):
        return chunks() if kwargs.get("stream") else SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))])

    async def run():
        if transport == "batch":
            return (await service.recommend("test", "민감성 피부 토너")).model_dump()
        frames = [frame async for frame in service.recommend_stream("test", "민감성 피부 토너")]
        meta = next(json.loads(frame.split("data: ", 1)[1]) for frame in frames if frame.startswith("event: meta"))
        done = json.loads(frames[-1].split("data: ", 1)[1])
        response_text = "".join(json.loads(frame.split("data: ", 1)[1])["text"]
                                for frame in frames if frame.startswith("event: delta"))
        return {**meta, **done, "response_text": response_text}

    # GLYCERIN은 건조 고민의 검토 설명 카드가 붙는 성분
    rows = [{"name": "GLYCERIN"}] + ([{"name": "LINALOOL"}, {"name": "LIMONENE"}] if with_allergen else [])
    inventory = AsyncMock(return_value={"p1": rows})
    with (
        patch.object(settings, "dictionary_explanations_enabled", True),
        patch.object(settings, "recommend_cache_enabled", False),
        patch.object(settings, "conversation_enabled", False),
        patch.object(service, "_resolve_conversation_response", AsyncMock(return_value=None)),
        patch.object(service, "_store_turn", AsyncMock()),
        patch.object(service, "extract_with_fallback", AsyncMock(return_value=(profile, "llm"))),
        patch.object(service, "query_ingredients_by_effects", AsyncMock(return_value=[])),
        patch.object(service, "query_cautioned_ingredients", AsyncMock(return_value=set())),
        patch.object(service, "select_products", AsyncMock(return_value=[_product().model_dump()])),
        patch.object(service, "query_product_ingredient_inventory", inventory),
        patch.object(service, "query_product_concern_evidence", AsyncMock(return_value={})),
        patch.object(service, "get_async_llm_client", return_value=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)))),
    ):
        result = asyncio.run(run())
    inventory.assert_awaited_once_with(["p1"])
    assert result["response_mode"] != "fragrance_rationale_fallback"
    if concern == Concern.DRY_SKIN:
        assert result["response_mode"] == "generated"
    if with_allergen:
        assert result["products"][0]["fragrance_allergens"] == ["리날룰", "리모넨"]
        assert result["response_text"].rstrip().endswith(
            "참고: 브랜드 제품 p1 제품에는 향료 알레르기 유발 가능 성분(리날룰·리모넨)이 표시돼 있어요. " + NOTE_TAIL)
    else:
        assert result["products"][0]["fragrance_allergens"] == []
        assert "향료 알레르기" not in result["response_text"]
    if concern == Concern.DRY_SKIN:  # 생성 응답은 품질 검사도 통과해야 한다(대체 응답의 표현은 이 PR 범위 밖).
        assert check_response({}, result) == []
