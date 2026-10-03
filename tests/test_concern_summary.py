from app.domain.enums import Concern
from app.services.concern_summary import build_summary, render_concern_summary, render_sentence, summary_effects

FUNC = {"NIACINAMIDE": ["whitening"], "ADENOSINE": ["anti_wrinkle"], "RETINYL PALMITATE": ["anti_wrinkle"]}


def _row(inci, kor, effect, tier="reference_book", score=0.2, papers=0, medical=False):
    return {"inci_name": inci, "kor_name": kor, "effect_code": effect, "evidence_type": tier,
            "graph_score": score, "paper_count": papers, "medical_wording": medical}


def test_sentence_counts_and_names():
    assert render_sentence([{"label": "미백", "count": 6, "names": []}, {"label": "주름", "count": 15, "names": []}]) \
        == "미백 고민과 관련된 근거가 있는 성분 6가지, 주름 고민 관련 성분 15가지가 들어 있어요."
    # 3개 미만은 숫자 대신 이름, 조사도 받침에 맞춘다
    assert render_sentence([{"label": "미백", "count": 1, "names": ["나이아신아마이드"]}]) \
        == "미백 고민 관련 근거가 있는 성분으로는 나이아신아마이드가 들어 있어요."
    assert render_sentence([{"label": "트러블", "count": 2, "names": ["살리실산", "아젤라산"]}]).endswith("아젤라산이 들어 있어요.")


def test_key_ingredients_prefer_functional_then_paper_and_cap():
    rows = [
        _row("NIACINAMIDE", "나이아신아마이드", "DEPIGMENTING", "pubmed_evidence", 0.4, 1),
        _row("NIACINAMIDE", "나이아신아마이드", "ANTI_AGING", "pubmed_evidence", 0.4, 1),
        _row("ARBUTIN", "알부틴", "BRIGHTENING"),
        _row("KOJIC ACID", "코직산", "DEPIGMENTING"),
        _row("ADENOSINE", "아데노신", "ANTI_AGING"),
        _row("RETINYL PALMITATE", "레티닐팔미테이트", "ANTI_AGING"),
        _row("HYALURONIC ACID", "히알루론산", "ANTI_AGING", "pubmed_evidence", 0.5, 2),
        _row("SQUALANE", "스쿠알란", "ANTI_AGING", "cosing_function", 0.03),  # CosIng은 세지 않음
    ]
    s = build_summary([Concern.HYPERPIGMENTATION, Concern.WRINKLES], rows, FUNC)
    assert [(c["label"], c["count"]) for c in s["concerns"]] == [("미백", 3), ("주름", 4)]
    keys = [k["name"] for k in s["key_ingredients"]]
    # 미백: 고시 원료 나이아신아마이드 → 논문 없는 책 근거 중 알부틴/코직산 하나 / 주름: 고시 원료 2개(아데노신·레티닐팔미테이트)
    assert keys[0] == "나이아신아마이드" and {"아데노신", "레티닐팔미테이트"} <= set(keys)
    assert 2 <= len(keys) <= 5 and len(set(keys)) == len(keys)
    assert s["key_ingredients"][0]["mfds_functional"] == "미백"
    block = render_concern_summary(s)
    assert "식약처 고시 미백 원료, 논문 근거 1건" in block and "문장 그대로 사용" in block


def test_blemish_care_counts_but_is_not_key():
    rows = [_row("ALLANTOIN", "알란토인", "BLEMISH_CARE"),
            _row("PRUNELLA VULGARIS EXTRACT", "꿀풀추출물", "BLEMISH_CARE", medical=True),
            _row("SALICYLIC ACID", "살리실산", "KERATOLYTIC", "pubmed_evidence", 0.3, 1)]
    s = build_summary([Concern.ACNE], rows, FUNC)
    assert s["concerns"][0]["count"] == 3
    assert [k["name"] for k in s["key_ingredients"]] == ["살리실산"]


def test_no_evidence_returns_none_and_effects():
    assert build_summary([Concern.WRINKLES], [_row("X", "엑스", "HYDRATING")], FUNC) is None
    assert "BLEMISH_CARE" in summary_effects([Concern.ACNE])


def test_server_written_product_section_replaces_llm_section():
    from app.schemas.recommend import ProductResult
    from app.services import recommend_service as rs

    summary = build_summary([Concern.HYPERPIGMENTATION], [
        _row("NIACINAMIDE", "나이아신아마이드", "DEPIGMENTING", "pubmed_evidence", 0.4, 1),
        _row("ARBUTIN", "알부틴", "BRIGHTENING"), _row("KOJIC ACID", "코직산", "DEPIGMENTING")], FUNC)
    product = ProductResult(product_id="p1", product_name="비타 세럼", brand="브랜드", category="세럼",
                            matched_count=1, matched_ingredients=["NIACINAMIDE"], concern_summary=summary)
    llm = "고민 분석\n미백을 원하시는군요.\n\n성분 설명\n- 나이아신아마이드: ...\n\n**추천 제품**\n- 엉뚱한 제품: 9가지"
    out = rs._finalize_with_product_section(llm, [product], [])
    assert "엉뚱한 제품" not in out and out.count("추천 제품") == 1
    assert "- [세럼] 브랜드 비타 세럼: 미백 고민과 관련된 근거가 있는 성분 3가지가 들어 있어요." in out
    assert "나이아신아마이드(식약처 고시 미백 원료, 논문 근거 1건)" in out
