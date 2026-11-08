"""고민을 둘 이상 말한 요청에서 한쪽 고민이 빠지지 않는지(추출·성분 선정·생성 입력)."""

import asyncio
import json
from types import SimpleNamespace
from unittest import mock

import pytest

from app.clients.llm_client import _normalize_concerns
from app.domain.enums import Concern as C
from app.schemas.recommend import IngredientResult
from app.services.ingredient_selection import select_recommended_ingredients
from app.services.recommend_service import _compose_user_content
from app.services.taxonomy_normalization_service import normalize_concerns


@pytest.mark.parametrize("message, model, expected", [
    # 보습: 모델이 수분 고민을 냈다가 필터에서 지워지거나 아예 안 낸 경우 모두 되살린다.
    ("보습과 피지조절 동시에 되는 제품 추천해줘", [C.OILY_SKIN, C.DEHYDRATED_SKIN], [C.OILY_SKIN, C.DEHYDRATED_SKIN]),
    ("보습과 피지조절 동시에 되는 제품 추천해줘", [C.OILY_SKIN], [C.OILY_SKIN, C.DEHYDRATED_SKIN]),
    ("수분이랑 미백 같이 되는 거", [C.HYPERPIGMENTATION], [C.HYPERPIGMENTATION, C.DEHYDRATED_SKIN]),
    # 모공: "넓다/막힌다" 없이 이름만 말해도 남는다.
    ("보습이랑 모공 둘 다 되는 제품", [C.DEHYDRATED_SKIN, C.ENLARGED_PORES], [C.DEHYDRATED_SKIN, C.ENLARGED_PORES]),
    ("여드름이랑 모공 관리", [C.ACNE], [C.ACNE, C.ENLARGED_PORES]),
    # 광채·톤업
    ("주름이랑 광채 같이", [C.WRINKLES, C.DULLNESS], [C.WRINKLES, C.DULLNESS]),
    ("미백이랑 광채 동시에", [C.HYPERPIGMENTATION], [C.HYPERPIGMENTATION, C.DULLNESS]),
])
def test_named_concerns_survive(message, model, expected):
    assert _normalize_concerns(message, model) == expected


@pytest.mark.parametrize("message, model, expected", [
    ("기본 보습 화장품을 찾습니다.", [C.DEHYDRATED_SKIN], [C.DEHYDRATED_SKIN]),  # 단독 보습 요청도 보습 고민
    ("모공 말고 보습만 원해요", [C.ENLARGED_PORES], [C.DEHYDRATED_SKIN]),  # 부정한 모공은 되살리지 않고 보습만
    ("지성인데 쓸 만한 거", [C.OILY_SKIN, C.ENLARGED_PORES], [C.OILY_SKIN]),  # 모공을 말하지 않음
    ("모공이 막혀요", [C.PORE_CONGESTION], [C.PORE_CONGESTION]),  # 같은 계열이 이미 있음
])
def test_no_false_restore(message, model, expected):
    assert _normalize_concerns(message, model) == expected


@pytest.mark.parametrize("message, expected", [
    ("보습과 피지조절 동시에 되는 제품", {C.DEHYDRATED_SKIN, C.OILY_SKIN}),
    ("진정이랑 장벽 같이", {C.IRRITATED_SKIN, C.BARRIER_DAMAGE}),
    ("톤업이랑 보습", {C.DULLNESS, C.DEHYDRATED_SKIN}),
])
def test_keyword_fallback_finds_both(message, expected):
    assert expected <= set(normalize_concerns(message))


def _row(name, tier, score, concerns, claim):
    return {"name": name, "eligibility_tier": tier, "graph_score": score, "concerns": concerns, "claim": claim}


SEBUM = ["OILY_SKIN"]
HYDRA = ["DEHYDRATED_SKIN"]
ROWS = [
    _row("NIACINAMIDE", "pubmed_review", 1.0, SEBUM, "Sebum regulation"),
    _row("ZINC PCA", "pubmed_review", 0.9, SEBUM, "Sebum regulation"),
    _row("SALICYLIC ACID", "pubmed_review", 0.8, SEBUM, "Sebum regulation"),
    _row("GLYCERIN", "pubmed_evidence", 1.0, HYDRA, "Hydrating"),
    _row("SODIUM HYALURONATE", "pubmed_evidence", 0.9, HYDRA, "Hydrating"),
]
KW = dict(default_k=3, max_k=3, score_ratio=0.5, product_bonus=0.0)


def test_selection_covers_every_concern():
    # 근거 등급이 높은 피지 성분만 자격이 되지만, 보습 성분도 하나는 들어가야 한다.
    names = [r["name"] for r in select_recommended_ingredients(ROWS, [], concerns=SEBUM + HYDRA, **KW)]
    assert len(names) == 3
    assert "GLYCERIN" in names
    assert sum(n in ("NIACINAMIDE", "ZINC PCA", "SALICYLIC ACID") for n in names) == 2


def test_selection_single_concern_unchanged():
    before = select_recommended_ingredients(ROWS, [], **KW)
    assert [r["name"] for r in before] == ["NIACINAMIDE", "ZINC PCA", "SALICYLIC ACID"]
    assert select_recommended_ingredients(ROWS, [], concerns=SEBUM, **KW) == before


def test_selection_keeps_shared_ingredient():
    rows = [_row("NIACINAMIDE", "pubmed_review", 1.0, SEBUM + HYDRA, "Sebum regulation"),
            _row("ZINC PCA", "pubmed_review", 0.9, SEBUM, "Sebum regulation"),
            _row("GLYCERIN", "pubmed_evidence", 1.0, HYDRA, "Hydrating")]
    names = [r["name"] for r in select_recommended_ingredients(rows, [], concerns=SEBUM + HYDRA, **KW)]
    assert "GLYCERIN" not in names  # 두 고민 모두에 연결된 성분이 이미 있으므로 더하지 않는다


def test_generation_input_lists_concerns_and_tags():
    ingredients = [IngredientResult(name="NIACINAMIDE", kor_name="나이아신아마이드", concerns=SEBUM),
                   IngredientResult(name="GLYCERIN", kor_name="글리세린", concerns=HYDRA)]
    content = _compose_user_content("보습과 피지조절", ingredients, [])
    assert "사용자 고민(모두 다룰 것): 피지, 속건조" in content
    assert "[관련 고민: 피지]" in content and "[관련 고민: 속건조]" in content
    single = _compose_user_content("피지", ingredients[:1], [])
    assert "사용자 고민" not in single and "[관련 고민" not in single


def _fake_llm(content):
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(content)))])
    client = mock.Mock()
    client.chat.completions.create = mock.AsyncMock(return_value=response)
    return client


@pytest.mark.parametrize("message, expected", [
    # 실측(2026-11-05, Qwen3.5-9B): 효과로 말한 복합 요청에서 모델이 concerns를 비워 no_products가 났다.
    ("보습과 피지조절 동시에 되는 제품 추천해줘", [C.OILY_SKIN, C.DEHYDRATED_SKIN]),
    ("기본 보습 화장품을 찾습니다.", [C.DEHYDRATED_SKIN]),
    ("촉촉한 크림 추천", [C.DEHYDRATED_SKIN]),
    ("저자극 크림 추천", []),
])
def test_empty_model_concerns_fall_back_to_keywords(message, expected):
    from app.clients import llm_client
    fake = _fake_llm({"skin_types": [], "concerns": [], "constraints": [], "intent": "recommend"})
    with mock.patch.object(llm_client, "get_async_llm_client", return_value=fake):
        profile = asyncio.run(llm_client.call_llm(message))
    assert profile.concerns == expected


def test_model_concerns_are_not_overridden_by_keywords():
    from app.clients import llm_client
    fake = _fake_llm({"skin_types": [], "concerns": ["ACNE"], "constraints": [], "intent": "recommend"})
    with mock.patch.object(llm_client, "get_async_llm_client", return_value=fake):
        profile = asyncio.run(llm_client.call_llm("여드름 진정 크림"))
    assert profile.concerns == [C.ACNE]


def test_generation_input_uses_korean_claim_labels():
    from app.services.recommend_service import _normalize_consumer_language
    ingredient = IngredientResult(name="SALICYLIC ACID", kor_name="살리실릭애씨드", claim="Comedolytic",
                                  eligibility_tier="pubmed_review", paper_ref="53")
    content = _compose_user_content("여드름", [ingredient], [])
    assert "모공 막힘 관리" in content and "Comedolytic" not in content
    unknown = _compose_user_content("x", [IngredientResult(name="X", claim="New label")], [])
    assert "New label" in unknown  # 사전에 없는 라벨은 원문 유지
    assert _normalize_consumer_language("여드름 기미와 관련된 연구가 있어요.") == "여드름과 관련된 연구가 있어요."
    assert _normalize_consumer_language("여드름 기미 개선") == "여드름 개선"


def _evidence_ingredient(name, kor, acne, oily, effects):
    return IngredientResult(name=name, kor_name=kor, claim="Comedolytic", eligibility_tier="pubmed_review",
                            paper_ref=str(acne), concerns=["ACNE", "OILY_SKIN"],
                            concern_evidence=[{"concern": "ACNE", "paper_count": acne, "effects": effects},
                                              {"concern": "OILY_SKIN", "paper_count": oily,
                                               "effects": ["SEBUM_REGULATION"]}])


def test_generation_input_gives_roles_per_concern_counts_and_links():
    # 베타 보고(2026-11-05): 트러블·피지 질문에 네 성분이 모두 "모공 막힘 관리 연구 N건"으로 나옴.
    ingredients = [
        _evidence_ingredient("SALICYLIC ACID", "살리실릭애씨드", 53, 12,
                             ["ANTIMICROBIAL", "ANTI_INFLAMMATORY", "BLEMISH_CARE", "COMEDOLYTIC", "KERATOLYTIC",
                              "SEBUM_REGULATION"]),
        _evidence_ingredient("CARNITINE", "카르니틴", 6, 5, ["BLEMISH_CARE", "SEBUM_REGULATION"]),
    ]
    content = _compose_user_content("트러블과 과도한 피지가 고민이에요", ingredients, [])
    assert "고민과 역할의 연결" in content and "모공을 막고" in content
    assert ("살리실릭애씨드 (SALICYLIC ACID): 연구에서 다룬 역할: 모공 막힘 관리, 각질 관리, 피지 조절, 항균 관리, "
            "피부 진정 | 고민별 연구 수: 트러블 관련 53건, 피지 관련 12건") in content
    assert "카르니틴 (CARNITINE): 연구에서 다룬 역할: 피지 조절 |" in content  # 목표(트러블 개선)는 역할에서 뺀다
    assert "Comedolytic" not in content


def test_generation_input_without_concern_evidence_keeps_claim_line():
    content = _compose_user_content("x", [IngredientResult(name="GLYCERIN", kor_name="글리세린", claim="Hydrating")], [])
    assert "- 글리세린 (GLYCERIN): 보습 [" in content and "고민과 역할의 연결" not in content


def test_fallback_template_uses_roles_counts_and_link():
    # 베타(2026-11-05): 검사에 걸려 대체 템플릿이 나가면 "확인된 효능은 모공 막힘 관리이며, 근거 수준은 논문 근거 53건"
    from app.schemas.recommend import ProductResult
    from app.services.recommend_service import _build_grounded_product_response
    ingredients = [
        _evidence_ingredient("SALICYLIC ACID", "살리실릭애씨드", 53, 12,
                             ["ANTIMICROBIAL", "COMEDOLYTIC", "KERATOLYTIC", "SEBUM_REGULATION"]),
        _evidence_ingredient("CARNITINE", "카르니틴", 6, 5, ["BLEMISH_CARE", "SEBUM_REGULATION"]),
    ]
    products = [ProductResult(product_id="p1", product_name="세럼A", brand="B", category="세럼", matched_count=2,
                              matched_ingredients=["SALICYLIC ACID", "CARNITINE"])]
    text = _build_grounded_product_response("여드름이랑 피지", ingredients, products, [C.ACNE, C.OILY_SKIN])
    assert "모공을 막고" in text.split("성분 설명")[0]
    assert ("- 살리실릭애씨드 (SALICYLIC ACID): 연구에서 다룬 역할은 모공 막힘 관리, 각질 관리, 피지 조절이고, "
            "트러블 관련 연구 53건, 피지 관련 연구 12건이 있습니다.") in text
    assert "- 카르니틴 (CARNITINE): 연구에서 다룬 역할은 피지 조절이고," in text
    assert "확인된 효능은 모공 막힘 관리" not in text


@pytest.mark.parametrize("message, label", [
    ("기본 보습 화장품 찾아요", "보습"),
    ("촉촉한 크림 추천", "보습"),
    ("속건조가 심해요", "속건조"),
    ("보습 크림인데 속이 당겨요", "속건조"),  # 당김을 직접 말하면 속건조 그대로
    ("수분 부족 피부", "속건조"),
])
def test_moisturizing_request_label(message, label):
    from app.services.concern_summary import concern_label, reset_request_labels, set_request_labels
    token = set_request_labels(message)
    try:
        assert concern_label(C.DEHYDRATED_SKIN) == label
        assert concern_label(C.OILY_SKIN) == "피지"
    finally:
        reset_request_labels(token)
    assert concern_label(C.DEHYDRATED_SKIN) == "속건조"  # 요청이 끝나면 원래 이름


def test_generation_input_calls_plain_moisturizing_request_moisturizing():
    from app.services.concern_summary import reset_request_labels, set_request_labels
    ingredient = IngredientResult(name="UREA", kor_name="우레아", claim="Hydrating", eligibility_tier="pubmed_review",
                                  paper_ref="34", concerns=HYDRA,
                                  concern_evidence=[{"concern": "DEHYDRATED_SKIN", "paper_count": 34,
                                                     "effects": ["HYDRATING", "MOISTURE_RETENTION"]}])
    token = set_request_labels("기본 보습 화장품 찾아요")
    try:
        content = _compose_user_content("기본 보습 화장품 찾아요", [ingredient], [])
    finally:
        reset_request_labels(token)
    assert "속건조" not in content and "피부 속" not in content
    assert "보습 관련 34건" in content and "- 보습: 피부에 수분이 부족하면" in content


def _wrinkle_ingredient():
    return IngredientResult(name="RETINOL", kor_name="레티놀", claim="Anti-aging", eligibility_tier="pubmed_review",
                            paper_ref="47", concerns=["WRINKLES", "LOSS_OF_ELASTICITY"],
                            concern_evidence=[{"concern": "WRINKLES", "paper_count": 47, "effects": ["ANTI_AGING"]}])


def test_off_topic_concern_analysis_is_replaced():
    # 베타(2026-11-08): 탄력·주름 질문에 프롬프트 예시의 트러블 설명이 그대로 나옴
    from app.services.recommend_service import _ground_concern_analysis
    text = ("고민 분석\n피지와 각질이 모공을 막으면 트러블로 이어질 수 있어 피지 조절과 모공 관리 성분을 함께 골랐어요.\n\n"
            "성분 설명\n- 레티놀 (RETINOL): 탄력과 주름 관리에 도움이 된다는 연구 결과가 47 건 있어요.")
    fixed = _ground_concern_analysis(text, "탄력과 주름이 고민이에요. 근거가 있는 성분과 제품을 추천해줘.",
                                     [_wrinkle_ingredient()])
    analysis = fixed.split("성분 설명")[0]
    assert "트러블" not in analysis and "피지" not in analysis and "주름" in analysis
    assert "- 레티놀 (RETINOL): 탄력과 주름 관리에" in fixed  # 성분 설명은 그대로


def test_on_topic_concern_analysis_is_kept():
    from app.services.recommend_service import _ground_concern_analysis
    acne = _evidence_ingredient("SALICYLIC ACID", "살리실릭애씨드", 53, 12, ["COMEDOLYTIC", "SEBUM_REGULATION"])
    text = ("고민 분석\n피지와 각질이 모공을 막으면 트러블로 이어질 수 있어 피지 조절 성분을 골랐어요.\n\n"
            "성분 설명\n- 살리실릭애씨드: …")
    assert _ground_concern_analysis(text, "트러블과 과도한 피지가 고민이에요", [acne]) == text
    wrinkle = ("고민 분석\n피부 탄력과 수분이 줄면 주름이 깊어져 탄력·주름 관리 성분을 골랐어요.\n\n성분 설명\n- 레티놀: …")
    assert _ground_concern_analysis(wrinkle, "탄력과 주름이 고민이에요", [_wrinkle_ingredient()]) == wrinkle
