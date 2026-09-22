from eval.hard_checks import check_response, summarize_hard_failures


def _response(*, text="정상 응답", products=None, ingredients=None):
    return {
        "response_text": text,
        "products": products or [],
        "ingredients": ingredients or [],
    }


def _product(
    product_id="p1",
    product_name="수분 앰플",
    brand="브랜드",
    matched_ingredients=None,
):
    return {
        "product_id": product_id,
        "product_name": product_name,
        "brand": brand,
        "category": "에센스/세럼/앰플",
        "matched_ingredients": matched_ingredients or ["GLYCERIN"],
    }


def _codes(case, response):
    return [failure.code for failure in check_response(case, response)]


def test_clean_grounded_response_passes():
    response = _response(
        text="추천 제품\n- [앰플] 브랜드 수분 앰플: 글리세린이 확인됩니다.",
        products=[_product()],
        ingredients=[{"name": "GLYCERIN", "kor_name": "글리세린"}],
    )

    assert check_response({}, response) == []


def test_text_integrity_failures_are_deterministic():
    codes = _codes({}, _response(text="피肤 심부 안내"))

    assert codes == ["HANJA_LEAK", "BANNED_TERM"]
    assert _codes({}, _response(text="")) == ["EMPTY_RESPONSE"]


def test_unknown_product_and_ingredient_mismatch_fail():
    ingredients = [
        {"name": "GLYCERIN", "kor_name": "글리세린"},
        {"name": "MANDELIC ACID", "kor_name": "만델릭애씨드"},
    ]
    product = _product(matched_ingredients=["GLYCERIN"])

    unknown = _response(
        text="추천 제품\n- 존재하지 않는 앰플: 글리세린이 확인됩니다.",
        products=[product],
        ingredients=ingredients,
    )
    mismatch = _response(
        text="추천 제품\n- 브랜드 수분 앰플: 만델릭애씨드가 확인됩니다.",
        products=[product],
        ingredients=ingredients,
    )

    assert "UNKNOWN_PRODUCT" in _codes({}, unknown)
    assert "PRODUCT_INGREDIENT_MISMATCH" in _codes({}, mismatch)


def test_unverified_constraint_brand_duplication_and_target_mismatch_fail():
    product = _product(product_name="브랜드 모공 탄력 앰플", brand="브랜드")
    response = _response(
        text="추천 제품\n- 브랜드 브랜드 모공 탄력 앰플: 글리세린이 확인됩니다.",
        products=[product],
        ingredients=[{"name": "GLYCERIN", "kor_name": "글리세린"}],
    )
    case = {"constraints": ["HYPOALLERGENIC"], "concerns": ["SENSITIVE_SKIN"]}
    codes = _codes(case, response)

    assert "UNVERIFIED_CONSTRAINT" in codes
    assert "BRAND_DUPLICATION" in codes
    assert "TARGET_MISMATCH" in codes


def test_hard_failure_summary_counts_cases_and_codes():
    summary = summarize_hard_failures(
        [
            {"hard_failures": [{"code": "HANJA_LEAK"}, {"code": "BANNED_TERM"}]},
            {"hard_failures": [{"code": "HANJA_LEAK"}]},
            {"hard_failures": []},
        ],
        denominator=3,
    )

    assert summary == {
        "hard_failure_cases": 2,
        "hard_failure_count": 3,
        "hard_failure_rate": 0.6667,
        "hard_failure_counts": {"BANNED_TERM": 1, "HANJA_LEAK": 2},
    }
