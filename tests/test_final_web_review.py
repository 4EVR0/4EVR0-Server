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
