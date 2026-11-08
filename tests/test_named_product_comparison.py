"""이름으로 두 제품 비교(베타 보고 2026-11-08: "A와 B의 차이가 뭐야?"가 제품 없음·비교할 제품 없음으로 끝남)."""

import asyncio
from unittest.mock import AsyncMock, patch

from app.services import product_info
from app.services import recommend_service as service

Q1 = "리쥬란 더마 힐러 모이스처 트리트먼트 토너와 피지오겔 시카밸런스 포맨 카밍 토너의 차이가 뭐야?"


def test_comparison_splits():
    assert product_info.comparison_splits(Q1) == [["리쥬란 더마 힐러 모이스처 트리트먼트 토너", "피지오겔 시카밸런스 포맨 카밍 토너"]]
    assert product_info.comparison_splits("A세럼 vs B크림 비교해줘") == [["A세럼", "B크림"]]
    assert product_info.comparison_splits("여드름이랑 모공 같이 되는 거") == []  # 비교 질문이 아님
    three = product_info.comparison_splits("토리든 세럼, 라운드랩 토너, 아누아 토너 비교")
    assert ["토리든 세럼", "라운드랩 토너", "아누아 토너"] in three


def _fact(inci, kor, effect, papers=5):
    return {"inci_name": inci, "kor_name": kor, "evidence": [{"effects": effect, "papers": papers}], "affects": []}


ROWS = {
    "리쥬란": {"product_id": "p1", "product_name": "더마 힐러 모이스처 트리트먼트 토너", "brand": "리쥬란", "category": "토너"},
    "피지오겔": {"product_id": "p2", "product_name": "시카밸런스 포맨 카밍 토너", "brand": "피지오겔", "category": "토너"},
}
FACTS = {"p1": [_fact("UREA", "우레아", "HYDRATING"), _fact("GLYCERIN", "글리세린", "HYDRATING")],
         "p2": [_fact("GLYCERIN", "글리세린", "HYDRATING"), _fact("SALICYLIC ACID", "살리실릭애씨드", "SEBUM_REGULATION")]}
INVENTORY = {"p1": [{"name": "UREA", "kor_name": "우레아"}, {"name": "GLYCERIN", "kor_name": "글리세린"}],
             "p2": [{"name": "GLYCERIN", "kor_name": "글리세린"}, {"name": "SALICYLIC ACID", "kor_name": "살리실릭애씨드"}]}


async def _resolve(tokens):
    hits = [row for key, row in ROWS.items()
            if key in tokens or (key == "피지오겔" and "시카밸런스" in tokens) or (key == "리쥬란" and "힐러" in tokens)]
    return ("one", hits) if len(hits) == 1 else ("none", [])


def _run(message, active=None):
    with patch.object(service, "_resolve_product_mention", new=_resolve), \
         patch.object(service, "query_product_ingredient_facts", new=AsyncMock(side_effect=lambda pid: FACTS[pid])), \
         patch.object(service, "query_product_ingredient_inventory", new=AsyncMock(return_value=INVENTORY)), \
         patch.object(service, "_store_turn", new=AsyncMock()):
        return asyncio.run(service._product_comparison_by_name("s", "t", message, active))


def test_compares_two_named_products_with_and_without_brand():
    for message in (Q1, "더마 힐러 모이스처 트리트먼트 토너와 시카밸런스 포맨 카밍 토너의 차이가 뭐야?"):
        response = _run(message)
        assert response.response_mode == "product_comparison"
        assert [p.product_id for p in response.products] == ["p1", "p2"]
        text = response.response_text
        assert text.index("요약") < text.index("| 성분 |")
        assert "피지오겔 시카밸런스 포맨 카밍 토너: 피지·각질·모공 관련 근거 성분이 확인돼요(살리실릭애씨드)" in text
        assert "| 우레아 (UREA) | 확인 | — |" in text


def test_not_found_part_is_named():
    response = _run("리쥬란 더마 힐러 토너와 없는브랜드 수분크림의 차이가 뭐야?")
    assert response.response_mode == "product_not_found"
    assert "‘없는브랜드 수분크림’ 제품을 찾지 못해" in response.response_text


def test_not_a_named_comparison_falls_through():
    assert _run("1번이랑 2번 비교해줘") is None
    assert _run("토너랑 크림 차이가 뭐야?") is None


def test_shown_products_go_to_followup_unless_names_are_exact():
    active = {"visible_products": [{"product_id": "p9"}]}
    assert _run("리쥬란이랑 없는브랜드 비교해줘", active) is None  # 직전 추천 중에서 고르는 후속 비교에 맡긴다
    assert _run(Q1, active).response_mode == "product_comparison"
