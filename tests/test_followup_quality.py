"""후속 답변 품질: 효능 검사 허용 범위, 제품 지목, 민감 피부 질문, 단정 문장."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.domain.enums import Concern, Effect
from app.domain.user import UserProfile
from app.schemas.recommend import IngredientResult
from app.services import recommend_service as service
from app.services.recommend_service import _extract_ranking

PRODUCTS = [
    {"product_id": "t", "product_name": "토너 A", "brand": "A", "category": "토너",
     "matched_count": 1, "matched_ingredients": ["NIACINAMIDE"]},
    {"product_id": "c", "product_name": "크림 B", "brand": "B", "category": "크림",
     "matched_count": 1, "matched_ingredients": ["UREA"]},
]
KOR = {"NIACINAMIDE": "나이아신아마이드", "UREA": "우레아"}


def _active():
    profile = UserProfile(concerns=[Concern.DRY_SKIN], effects=[Effect.HYDRATING])
    active = service._active_recommendation(profile, "건조한 피부 제품 추천해줘", PRODUCTS, "turn-1")
    active["ingredients"] = [IngredientResult(name="UREA", kor_name="우레아", claim="hydrating",
                                              supported_claims=["hydrating", "barrier repair"]).model_dump()]
    return active


def _run(message, answer, graph_claims=None, facts=None):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(
        return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=answer))])))))
    patches = [
        patch.object(service, "get_async_llm_client", return_value=client),
        patch.object(service, "query_ingredient_kor_names", AsyncMock(return_value=KOR)),
        patch.object(service, "query_supported_claims", AsyncMock(return_value=graph_claims or {})),
        patch.object(service, "query_product_ingredient_facts", AsyncMock(side_effect=lambda pid: (facts or {}).get(pid, []))),
        patch.object(service, "query_product_fragrance_evidence", AsyncMock(return_value=[])),
        patch.object(service, "_store_turn", AsyncMock()),
    ]
    for p in patches:
        p.start()
    try:
        response = asyncio.run(service._handle_followup("s", "turn-2", message, [], _active()))
    finally:
        for p in patches:
            p.stop()
    return response, client.chat.completions.create


def test_claims_from_recommend_turn_and_graph_are_allowed():
    answer = "*우레아*가 보습과 장벽에 도움을 줍니다.\n*나이아신아마이드*는 장벽을 돕습니다."
    response, _ = _run("왜 이 제품들을 추천했어?", answer, graph_claims={"NIACINAMIDE": ["barrier repair"]})
    assert response.response_mode == "followup"
    # 근거에 없는 효능은 여전히 막는다.
    response, _ = _run("왜 이 제품들을 추천했어?", "*나이아신아마이드*가 주름을 개선합니다.",
                       graph_claims={"NIACINAMIDE": ["barrier repair"]})
    assert response.response_mode == "followup_quality_fallback"


def test_numbered_product_narrows_generation_and_cards():
    response, create = _run("2번 제품 어때?", "**크림 B**는 *우레아*가 들어 있는 크림이에요.")
    assert [p.product_id for p in response.products] == ["c"]
    prompt = create.await_args.kwargs["messages"][1]["content"]
    assert "크림 B" in prompt and "토너 A" not in prompt


def test_sensitive_use_question_is_answered_from_caution_list_without_llm():
    facts = {"t": [{"inci_name": "NIACINAMIDE", "kor_name": "나이아신아마이드", "sensitive_caution": ""}],
             "c": [{"inci_name": "UREA", "kor_name": "우레아", "sensitive_caution": "exclude"},
                   {"inci_name": "LACTIC ACID", "kor_name": "락틱애씨드", "sensitive_caution": "exclude"}]}
    for message in ("이거 민감한 피부에 써도 돼?", "이 중에 제일 순한 거 뭐야?"):
        response, create = _run(message, "안전합니다.", facts=facts)
        create.assert_not_awaited()
        assert response.response_mode == "followup_sensitive_use"
        assert "성분만으로 단정할 수 없어요" in response.response_text
        assert "- **크림 B**: 우레아, 락틱애씨드" in response.response_text
        assert "- **토너 A**: 민감 피부 주의 목록에 있는 성분은 확인되지 않았어요." in response.response_text
        assert "좁은 부위에 먼저" in response.response_text


def test_unsupported_safety_and_amount_claims_fall_back():
    for answer in ("**토너 A**는 민감한 피부에도 안전합니다.", "*우레아*가 주성분이라 촉촉해요.",
                   "**토너 A**는 자극이 적어요.",
                   "**크림 B**는 *우레아*가 고농도로 배합돼 있어요.",
                   "*우레아*는 자극을 줄여주는 역할을 해요.",
                   "민감한 피부일 경우 아침과 저녁 모두에 사용해도 좋습니다."):
        response, _ = _run("왜 이 제품들을 추천했어?", answer)
        assert response.response_mode == "followup_quality_fallback", answer


def test_foreign_script_falls_back():
    for answer in ("*우레아*로 보습을 줍니다. 바르면效果更好합니다.", "*우레아*로 수분 유держание 효과."):
        response, _ = _run("왜 이 제품들을 추천했어?", answer)
        assert response.response_mode == "followup_quality_fallback", answer


def test_ranking_marker_alone_on_line_takes_next_line():
    clean, ranking = _extract_ranking("답변입니다.\n[추천순위]\n**토너 A** | 크림 B")
    assert clean == "답변입니다."
    assert ranking == ["토너 A", "크림 B"]


def test_unknown_ingredient_name_falls_back():
    inventory = {"t": [{"name": "NIACINAMIDE", "kor_name": "나이아신아마이드"}],
                 "c": [{"name": "UREA", "kor_name": "우레아"}]}
    with patch.object(service, "query_product_ingredient_inventory", AsyncMock(return_value=inventory)), \
            patch.object(service, "query_ingredient_vocabulary", AsyncMock(return_value=frozenset())):
        response, _ = _run("왜 이 제품들을 추천했어?", "**크림 B**는 **마데카솔 (MADECASSOL)**이 들어 있어요.")
        assert response.response_mode == "followup_quality_fallback"
        response, _ = _run("왜 이 제품들을 추천했어?", "**크림 B**는 *우레아* (UREA)가 들어 있어요.")
        assert response.response_mode == "followup"


def test_patch_test_advice_is_not_a_safety_claim():
    for advice in ("민감한 피부라면 좁은 부위에 먼저 발라 보세요.", "민감한 피부라면 패치 테스트를 먼저 해보시는 것이 좋습니다."):
        response, _ = _run("왜 이 제품들을 추천했어?", f"**크림 B**는 *우레아*가 들어 있어요. {advice}")
        assert response.response_mode == "followup", advice


def test_wording_is_not_an_error():
    for answer in ("**크림 B**가 가장 보습력이 뛰어납니다.", "**크림 B**는 *우레아*가 주력인 크림으로 보습 효과를 극대화해요."):
        response, _ = _run("왜 이 제품들을 추천했어?", answer)
        assert response.response_mode == "followup", answer
