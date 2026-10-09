"""특정 제품 설명 요청(#124)."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core.config import settings
from app.domain.user import UserProfile
from app.schemas.recommend import IngredientResult, ProductResult
from app.services import product_info
from app.services import recommend_service as service

BRANDS = frozenset({"브랜드a", "브랜드b"})
ROW_A = {"product_id": "pA", "goods_no": "gA", "product_name": "수딩 크림 EX", "brand": "브랜드A", "category": "크림",
         "rating": 4.8, "review_count": 10, "review_stats": None, "product_url": None}
ROW_B = {**ROW_A, "product_id": "pB", "goods_no": "gB", "product_name": "수딩 크림 라이트"}
FACTS = [
    {"inci_name": "UREA", "kor_name": "우레아", "kr_reg_status": None, "kr_limit_note": None,
     "sensitive_caution": "exclude", "affects": [],
     "evidence": [{"effects": "HYDRATING|MOISTURE_RETENTION|BARRIER_REPAIR", "papers": 34}]},
    {"inci_name": "NIACINAMIDE", "kor_name": "나이아신아마이드", "kr_reg_status": None, "kr_limit_note": None,
     "sensitive_caution": "", "affects": [],
     "evidence": [{"effects": "DEPIGMENTING|BRIGHTENING", "papers": 45}, {"effects": "HYDRATING", "papers": 5}]},
    {"inci_name": "SALICYLIC ACID", "kor_name": "살리실릭애씨드", "kr_reg_status": "restricted",
     "kr_limit_note": "보존제로서 0.5%", "sensitive_caution": "exclude", "affects": [],
     "evidence": [{"effects": "COMEDOLYTIC|SEBUM_REGULATION|BLEMISH_CARE", "papers": 53}]},
    {"inci_name": "ALLANTOIN", "kor_name": "알란토인", "kr_reg_status": None, "kr_limit_note": None,
     "sensitive_caution": "", "affects": [{"effect": "SOOTHING", "type": "reference_book", "papers": 0, "medical": False}],
     "evidence": []},
    {"inci_name": "LINALOOL", "kor_name": "리날룰", "kr_reg_status": None, "kr_limit_note": None,
     "sensitive_caution": "exclude", "affects": [{"effect": "SOOTHING", "type": "pubmed_evidence", "papers": 1, "medical": False}],
     "evidence": []},
    {"inci_name": "WATER", "kor_name": "정제수", "kr_reg_status": None, "kr_limit_note": None,
     "sensitive_caution": "", "affects": [], "evidence": []},
]


# ── 규칙 ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("message,tokens", [
    ("브랜드A 수딩 크림 EX 어때?", ["브랜드a", "수딩", "크림", "ex"]),
    ("브랜드A 수딩크림은 어떤 제품이야?", ["브랜드a", "수딩크림"]),
    ("브랜드A 독도 토너는 어떤 화장품인가요?", ["브랜드a", "독도", "토너"]),
    ("토너 어때?", []),             # 제형만으로는 특정 제품이 아님
    ("이 제품 어때?", []),           # 직전 추천 지시
    ("1번 제품 설명해줘", []),
])
def test_mention_tokens(message, tokens):
    assert product_info.looks_like_product_info(message)
    assert product_info.mention_tokens(message) == tokens


def test_brand_required_for_rule_path():
    assert product_info.has_brand(["브랜드a", "수딩"], BRANDS)
    assert product_info.has_brand(["브랜드a수딩"], BRANDS)  # 붙여 쓴 경우
    assert not product_info.has_brand(["레티놀", "크림"], BRANDS)


def test_choice_from_message():
    choices = [ROW_A, ROW_B]
    assert product_info.choice_from_message("2번", choices) is ROW_B
    assert product_info.choice_from_message("두 번째 거요", choices) is ROW_B
    assert product_info.choice_from_message("라이트", choices) is ROW_B
    assert product_info.choice_from_message("9번", choices) is None
    assert product_info.choice_from_message("수딩 크림", choices) is None  # 둘 다 맞으면 못 고른다


# ── 설명 구성 ────────────────────────────────────────────────────────────────
def test_group_facts_by_effect_without_unsupported_or_fragrance():
    groups = dict(product_info.group_facts(FACTS))
    assert [it.name for it in groups["보습"]] == ["우레아", "나이아신아마이드"]
    # 식약처 고시 미백 원료가 앞에 오고, 표시한다.
    assert groups["미백·피부 톤"][0].label() == "나이아신아마이드(식약처 고시 미백 원료, 논문 근거 45건)"
    assert groups["진정"][0].label() == "알란토인(참고 도서 근거)"
    assert groups["피지·각질·모공"][0].label().startswith("살리실릭애씨드(")
    names = {it.name for items in groups.values() for it in items}
    assert "리날룰" not in names and "정제수" not in names  # 향료 추천 근거 제외, 근거 없는 성분 제외


def test_render_sections_states_limits_and_cautions():
    groups = product_info.group_facts(FACTS)
    text = product_info.render_sections(groups, FACTS)
    assert text.startswith("효능별 성분\n- 보습: 우레아(논문 근거 34건)")
    assert "국내 배합한도가 있는 성분: 살리실릭애씨드(보존제로서 0.5%)" in text
    assert product_info.LIMITATION_NOTE in text
    assert "참고: 우레아·살리실릭애씨드는 민감한 피부라면 좁은 부위에 먼저 사용해 확인해 주세요." in text
    assert "주성분" not in text


# ── 흐름 ────────────────────────────────────────────────────────────────────
def _patches(rows, facts=FACTS, intro_text=None, active=None):
    create = AsyncMock(side_effect=RuntimeError("no gpu")) if intro_text is None else AsyncMock(
        return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=intro_text))]))
    store = AsyncMock()
    return store, [
        patch.object(service.conversation_store, "load_recent", AsyncMock(return_value=[])),
        patch.object(service.conversation_store, "load_active", AsyncMock(return_value=active)),
        patch.object(service, "_store_turn", store),
        patch.object(service, "query_product_brands", AsyncMock(return_value=BRANDS)),
        patch.object(service, "query_products_by_name_tokens", AsyncMock(return_value=rows)),
        patch.object(service, "query_product_ingredient_facts", AsyncMock(return_value=facts)),
        patch.object(service, "get_async_llm_client", return_value=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)))),
    ]


def _run(coro, patches):
    for p in patches:
        p.start()
    try:
        return asyncio.run(coro)
    finally:
        for p in patches:
            p.stop()


def test_single_match_explains_product_with_server_sections():
    store, patches = _patches([ROW_A])
    resp = _run(service._resolve_conversation_response("s", "t", "브랜드A 수딩 크림 EX 어때?"), patches)
    assert resp.response_mode == "product_info"
    assert resp.products[0].product_id == "pA"
    assert resp.response_text.startswith("브랜드A 수딩 크림 EX(크림)은 확인된 성분 6개 가운데 4개가 효능 근거가 있는 성분이에요.")
    assert "효능별 성분" in resp.response_text and product_info.LIMITATION_NOTE in resp.response_text
    # 향료 알레르기 표시 성분은 사실로 안내한다(추천 근거로는 쓰지 않음).
    assert resp.response_text.rstrip().endswith("향료에 민감하다면 제품 상세 정보의 전성분을 꼭 확인해 주세요.")
    assert resp.products[0].fragrance_allergens == ["리날룰"]
    active = store.await_args.kwargs["active_state"]
    assert [p["product_id"] for p in active["visible_products"]] == ["pA"]  # 후속 질문이 이 제품을 본다


def test_clean_llm_intro_is_used_and_bad_one_is_rejected():
    good = ("이 제품에는 보습 관련 근거가 있는 우레아가 들어 있어요.\n"
            "미백 관련 근거가 있는 나이아신아마이드도 들어 있어요.")
    _, patches = _patches([ROW_A], intro_text=good)
    resp = _run(service._resolve_conversation_response("s", "t", "브랜드A 수딩 크림 EX 어때?"), patches)
    assert resp.response_text.startswith(good)
    bad = "브랜드A 수딩 크림 EX의 우레아는 주름을 없애 주는 가장 강력한 성분이에요."
    _, patches = _patches([ROW_A], intro_text=bad)
    resp = _run(service._resolve_conversation_response("s", "t", "브랜드A 수딩 크림 EX 어때?"), patches)
    assert not resp.response_text.startswith(bad)
    assert resp.response_text.startswith("브랜드A 수딩 크림 EX(크림)은 확인된 성분")


def test_many_matches_ask_then_number_reply_explains():
    store, patches = _patches([ROW_A, ROW_B])
    resp = _run(service._resolve_conversation_response("s", "t", "브랜드A 수딩 크림 어때?"), patches)
    assert resp.response_mode == "product_choice"
    assert "1. 브랜드A 수딩 크림 EX (크림)" in resp.response_text and "2. 브랜드A 수딩 크림 라이트 (크림)" in resp.response_text
    active = store.await_args.kwargs["active_state"]
    assert [c["product_id"] for c in active["pending_product_choices"]] == ["pA", "pB"]
    _, patches = _patches([], active=active)
    resp = _run(service._resolve_conversation_response("s", "t2", "2번"), patches)
    assert resp.response_mode == "product_info" and resp.products[0].product_id == "pB"


def test_rule_path_leaves_other_messages_alone():
    # 브랜드 없이 성분·제형만: 규칙은 가로채지 않는다.
    _, patches = _patches([ROW_A])
    assert _run(service._product_info_by_rule("s", "t", "레티놀 크림 어때?", None), patches) is None
    # 직전 추천에 이미 있는 제품은 기존 후속 처리로 둔다.
    _, patches = _patches([ROW_A])
    active = {"visible_products": [{"product_id": "pA"}]}
    assert _run(service._product_info_by_rule("s", "t", "브랜드A 수딩 크림 EX 어때?", active), patches) is None
    # 너무 넓으면 추천 흐름.
    _, patches = _patches([ROW_A] * product_info.TOO_BROAD)
    assert _run(service._product_info_by_rule("s", "t", "브랜드A 크림 어때?", None), patches) is None


def test_llm_mention_not_found_says_so():
    _, patches = _patches([])
    resp = _run(service._product_info_by_mention("s", "t", "다이브인 세럼 어때?", "다이브인 세럼"), patches)
    assert resp.response_mode == "product_not_found" and resp.products == []


@pytest.mark.parametrize("transport", ["batch", "stream"])
def test_llm_intent_path_on_both_transports(transport):
    profile = UserProfile(intent="product_info", product_mention="다이브인 세럼")

    async def run():
        if transport == "batch":
            return (await service.recommend("s", "다이브인 세럼은 어떤 거야")).model_dump()
        frames = [f async for f in service.recommend_stream("s", "다이브인 세럼은 어떤 거야")]
        done = json.loads(frames[-1].split("data: ", 1)[1])
        text = "".join(json.loads(f.split("data: ", 1)[1])["text"] for f in frames if f.startswith("event: delta"))
        return {**done, "response_text": text}

    _, patches = _patches([ROW_A])
    patches += [patch.object(settings, "recommend_cache_enabled", False),
                patch.object(service, "extract_with_fallback", AsyncMock(return_value=(profile, "llm")))]
    result = _run(run(), patches)
    assert result["response_mode"] == "product_info"
    assert "효능별 성분" in result["response_text"]
