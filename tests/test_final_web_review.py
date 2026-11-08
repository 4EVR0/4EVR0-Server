"""실제 웹 최종 점검(2026-11-08)에서 찾은 문제: 처방 치료 대체 질문, 고민 없는 요청, 보여 준 제품 사용 여부 질문."""

import pytest

from app.domain.enums import Concern
from app.services import recommend_service as service


@pytest.mark.parametrize("message, expected", [
    ("피부과에서 아토피 진단받았는데 스테로이드 연고 대신 쓸 크림 추천해줘", service.MEDICAL_NOTICE),
    ("처방받은 약 끊고 화장품으로 관리하고 싶어요", service.MEDICAL_NOTICE),
    ("피부과에서 여드름 진단받았어요 세럼 추천", service.DIAGNOSED_NOTICE),
    ("여드름 피부에 좋은 세럼 추천해줘", None),
    ("약산성 클렌저 추천", None),
])
def test_medical_notice(message, expected):
    assert service._medical_notice(message) == expected


@pytest.mark.parametrize("message, lead", [
    ("추천해줘", ""),
    ("오늘 날씨 어때?", ""),
    ("토너 추천해줘", "토너 제품을 찾으시는군요. "),
    ("선크림 추천해줘", "선크림(자외선 차단제)은 아직 추천 데이터에 없어요. "),
])
def test_no_concern_request_asks_for_concern(message, lead):
    text = service._build_no_product_response_base([], [], [], message)
    assert text.startswith(lead + "4EVR0는 피부 고민에 맞춰")
    assert "조건에 맞는 결과를 찾지 못해" not in text


def test_concern_without_products_keeps_old_message():
    text = service._build_no_product_response_base([], [], [Concern.WRINKLES], "주름")
    assert "조건에 맞는 결과를 찾지 못해" in text


HISTORY = [{"user": "여드름 제품 추천해줘", "products": [{"name": "A"}, {"name": "B"}]}]


@pytest.mark.parametrize("message, kind", [
    ("민감한 피부인데 써도 될까?", "followup"),
    ("민감성인데 발라도 괜찮나요?", "followup"),
    ("민감한 피부용 크림 추천해줘", "new"),       # 새 추천 요청
    ("건조한 피부에 좋은 크림", "new"),
])
def test_use_permission_question_is_followup(message, kind):
    assert service._heuristic_kind(message, HISTORY) == kind


def test_use_permission_without_shown_products_is_new():
    assert service._heuristic_kind("민감한 피부인데 써도 될까?", [{"user": "x", "products": []}]) == "new"


def test_sunscreen_is_not_matched_as_cream():
    assert service._requested_categories("선크림 추천해줘") == set()
    assert service._requested_categories("건조한 피부 썬크림") == set()
    assert service._requested_categories("수분크림이랑 선크림") == {"크림"}
    assert service._requested_categories("아이크림 추천") == {"크림"}


def test_sunscreen_with_concern_and_no_products():
    text = service._build_no_product_response_base(
        [service.IngredientResult(name="BUTYL METHOXYDIBENZOYLMETHANE")], [], [Concern.SUNBURN, Concern.DRY_SKIN],
        "건조한 피부에 맞는 선크림 추천해줘")
    assert text.startswith(service.SUNSCREEN_NOTE) and "BUTYL" not in text


def test_treatment_endorsement_is_removed_but_cautions_kept():
    # 실제 웹(2026-11-08): "처방받은 여드름 약 끊고 화장품으로만 관리하고 싶어요"의 모델 고민 분석
    text = ("고민 분석\n처방약 중단 후 화장품으로 트러블 관리를 원하시는 분께 모공 막힘, 각질 관리 역할을 하는 "
            "성분이 포함된 제품을 추천해 드립니다.\n\n성분 설명\n"
            "- 살리실릭애씨드 (SALICYLIC ACID): 트러블과 관련된 연구가 53건 있어요. 연고 대신 꾸준히 써 보세요.\n"
            "- 나이아신아마이드: 화장품은 치료를 대신할 수 없으니 의사와 상의해 주세요.")
    acne = service.IngredientResult(name="SALICYLIC ACID", kor_name="살리실릭애씨드", concerns=["ACNE"])
    out = service._strip_treatment_endorsement(text, [acne])
    assert "처방약 중단" not in out and "연고 대신 꾸준히" not in out
    assert "화장품은 치료를 대신할 수 없으니 의사와 상의해 주세요." in out  # 부정·상의 권유는 남김
    assert "트러블과 관련된 연구가 53건 있어요." in out
    analysis = out.split("성분 설명")[0]
    assert "모공을 막고" in analysis  # 비면 고민 연결 설명으로 채움


def test_text_without_endorsement_unchanged():
    text = "고민 분석\n트러블 관리 성분을 골랐어요.\n\n성분 설명\n- 살리실릭애씨드: …"
    assert service._strip_treatment_endorsement(text, []) == text


def test_daeyong_counts_as_replacement():
    assert service._medical_notice("스테로이드 연고 대용으로 쓸 크림") == service.MEDICAL_NOTICE
    text = ("고민 분석\n스테로이드 연고 대용으로 피부 진정과 보습에 도움이 되는 크림을 추천해 드립니다.\n\n"
            "성분 설명\n- 우레아: 보습과 관련된 연구가 41건 있어요.")
    atopy = service.IngredientResult(name="UREA", kor_name="우레아", concerns=["ATOPIC_PRONE"])
    out = service._strip_treatment_endorsement(text, [atopy])
    assert "대용" not in out and "우레아: 보습과 관련된 연구가 41건" in out


def test_no_product_path_gets_medical_notice():
    text = service._build_no_product_response([], [], [], "스테로이드 연고 끊고 쓸 수 있는 크림 추천해줘")
    assert text.startswith(service.MEDICAL_NOTICE)
    assert "4EVR0는 피부 고민에 맞춰" in text
    plain = service._build_no_product_response([], [], [], "추천해줘")
    assert not plain.startswith(service.MEDICAL_NOTICE)
