from app.clients.llm_client import _normalize_concerns, _normalize_skin_types
from app.domain.enums import Concern, SkinType


def test_oily_surface_and_inner_dryness_normalize_to_combination():
    assert _normalize_skin_types(
        "속건조가 심해서 겉은 번들거리는데 속은 당겨요.", [SkinType.OILY]
    ) == [SkinType.COMBINATION]


def test_sensitive_skin_requires_explicit_skin_type_language():
    assert _normalize_skin_types(
        "아토피가 잘 올라오고 가려워서 저자극 제품을 원해요.", [SkinType.SENSITIVE]
    ) == []
    assert _normalize_skin_types(
        "예민한 피부라 새 제품을 쓰기 어려워요.", [SkinType.SENSITIVE]
    ) == [SkinType.SENSITIVE]


def test_oily_t_zone_does_not_imply_enlarged_pores():
    concerns = [Concern.ENLARGED_PORES, Concern.FLAKY_SKIN, Concern.BARRIER_DAMAGE]

    assert _normalize_concerns(
        "T존은 기름지고 볼은 건조한 복합성인데 각질도 일어나요.", concerns
    ) == [Concern.FLAKY_SKIN, Concern.BARRIER_DAMAGE]


def test_explicit_large_pores_are_preserved():
    assert _normalize_concerns(
        "볼 쪽 모공 크기가 눈에 띄게 커 보여요.", [Concern.ENLARGED_PORES]
    ) == [Concern.ENLARGED_PORES]


def test_red_acne_and_post_acne_marks_do_not_imply_redness():
    assert _normalize_concerns(
        "턱에 빨갛고 아픈 여드름이 반복해서 올라와요.", [Concern.ACNE, Concern.REDNESS]
    ) == [Concern.ACNE]
    assert _normalize_concerns(
        "예전에 났던 여드름 흉터랑 붉은 자국이 남았어요.",
        [Concern.POST_ACNE_MARKS, Concern.REDNESS],
    ) == [Concern.POST_ACNE_MARKS]


def test_independent_redness_signal_is_preserved():
    assert _normalize_concerns(
        "피부가 자극받으면 바로 붉어져요.", [Concern.IRRITATED_SKIN, Concern.REDNESS]
    ) == [Concern.IRRITATED_SKIN, Concern.REDNESS]
    assert _normalize_concerns(
        "아토피 피부라 자주 빨개져요.", [Concern.ATOPIC_PRONE, Concern.REDNESS]
    ) == [Concern.ATOPIC_PRONE, Concern.REDNESS]


def test_surface_dryness_does_not_imply_inner_moisture_loss():
    assert _normalize_concerns(
        "건성이라 씻고 나면 뺨이 당기고 건조합니다.",
        [Concern.DRY_SKIN, Concern.DEHYDRATED_SKIN],
    ) == [Concern.DRY_SKIN]
    assert _normalize_concerns(
        "건성이고 속까지 수분이 부족합니다.",
        [Concern.DRY_SKIN, Concern.DEHYDRATED_SKIN],
    ) == [Concern.DRY_SKIN, Concern.DEHYDRATED_SKIN]
    assert _normalize_concerns(
        "뺨이 건조해요.",
        [Concern.DEHYDRATED_SKIN],
    ) == [Concern.DRY_SKIN]
    assert _normalize_concerns(
        "기본 보습 화장품을 찾습니다.",
        [Concern.DEHYDRATED_SKIN],
    ) == []


def test_pore_and_blemish_neighbors_require_their_own_signal():
    assert _normalize_concerns(
        "붉은 여드름이 올라왔어요.",
        [Concern.ACNE, Concern.COMEDONES],
    ) == [Concern.ACNE]
    assert _normalize_concerns(
        "코에 블랙헤드가 보이고 모공도 막혀요.",
        [Concern.COMEDONES, Concern.PORE_CONGESTION],
    ) == [Concern.COMEDONES, Concern.PORE_CONGESTION]
    assert _normalize_concerns(
        "콧등에 검은 점이 보여요.",
        [Concern.COMEDONES, Concern.PORE_CONGESTION],
    ) == [Concern.COMEDONES]


def test_pigmentation_dullness_and_spots_are_distinct():
    assert _normalize_concerns(
        "얼굴의 갈색 색소침착을 관리하고 싶어요.",
        [Concern.HYPERPIGMENTATION, Concern.DULLNESS],
    ) == [Concern.HYPERPIGMENTATION]
    assert _normalize_concerns(
        "볼에 잡티가 보여요.",
        [Concern.HYPERPIGMENTATION],
    ) == [Concern.BLEMISHES]
    assert _normalize_concerns(
        "볼에 잡티가 보여요.",
        [Concern.HYPERPIGMENTATION, Concern.BLEMISHES],
    ) == [Concern.BLEMISHES]
    assert _normalize_concerns(
        "기미도 있고 안색이 칙칙해요.",
        [Concern.HYPERPIGMENTATION, Concern.DULLNESS],
    ) == [Concern.HYPERPIGMENTATION, Concern.DULLNESS]


def test_named_wrinkles_replace_generic_aging_label():
    assert _normalize_concerns(
        "눈가 잔주름을 관리하고 싶어요.",
        [Concern.AGING_SIGNS],
    ) == [Concern.WRINKLES]
    assert _normalize_concerns(
        "전체적인 노화가 걱정돼요.",
        [Concern.AGING_SIGNS],
    ) == [Concern.AGING_SIGNS]
