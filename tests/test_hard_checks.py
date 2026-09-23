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


def test_generation_corruption_fails_hard_gate():
    response = _response(
        text=("성분 설명\n- 레티놀 (RETINOL): 피부 세포 세포 세포 세포 세포 "
              "CELLULAR 재생을 돕습니다.\n추천 제품\n- 수분 앰플: 레티놀이 확인됩니다."),
        products=[_product(matched_ingredients=["RETINOL"])],
        ingredients=[{"name": "RETINOL", "kor_name": "레티놀"}],
    )

    assert _codes({}, response) == ["DEGENERATE_REPETITION", "STRAY_ENGLISH_TOKEN"]


def test_known_inci_and_product_english_do_not_fail_hard_gate():
    response = _response(
        text=("성분 설명\n- 글리세린 (GLYCERIN)을 확인했습니다.\n"
              "추천 제품\n- CELLULAR 크림: 글리세린이 확인됩니다."),
        products=[_product(product_name="CELLULAR 크림")],
        ingredients=[{"name": "GLYCERIN", "kor_name": "글리세린"}],
    )

    assert _codes({}, response) == []


def test_first_turn_false_followup_is_hard_failure():
    response = _response(text="이전 추천 내역을 찾지 못했어요. 다시 알려주세요.")
    case = {"message": "민감성 제품 중에서 무향인 것만 보여주세요."}

    assert _codes(case, response) == ["FALSE_FOLLOWUP"]
    assert _codes({"message": "이 제품 추천해줘."}, response) == ["FALSE_FOLLOWUP"]
    assert _codes({"message": "그 중에서 하나만 골라줘"}, response) == []


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


def test_ingredient_token_in_official_product_name_is_not_a_claim():
    response = _response(
        text="추천 제품\n- 구달 나이아신아마이드 앰플: 글리세린이 확인됩니다.",
        products=[_product(product_name="나이아신아마이드 앰플")],
        ingredients=[
            {"name": "GLYCERIN", "kor_name": "글리세린"},
            {"name": "NIACINAMIDE", "kor_name": "나이아신아마이드"},
        ],
    )

    assert check_response({}, response) == []


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
