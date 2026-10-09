"""고민 없이 피부 타입만 말한 요청을 기본 고민으로 바꾼다.

추천은 고민 → 효능 → 성분 → 제품 순서로만 이어지고 피부 타입은 쓰이지 않는다. 그래서
"복합성인데 쓰기 좋은 화장품"처럼 타입만 말하면 효능이 비어 제품을 하나도 찾지 못했다.
고민이 하나도 없을 때만 타입별 기본 고민을 채우고, 그렇게 바꿨다는 안내 문장을 돌려준다.
고민을 함께 말한 요청은 그대로 둔다.
"""

from __future__ import annotations

from app.domain.enums import Concern, SkinType
from app.domain.user import UserProfile
from app.services.concern_summary import CONCERN_LABEL_KO
from app.services.taxonomy_normalization_service import infer_effects

DEFAULT_CONCERNS: dict[SkinType, tuple[Concern, ...]] = {
    # T존 유분 + U존 건조
    SkinType.COMBINATION: (Concern.OILY_SKIN, Concern.DEHYDRATED_SKIN),
    SkinType.OILY: (Concern.OILY_SKIN,),
    SkinType.DRY: (Concern.DRY_SKIN,),
    SkinType.SENSITIVE: (Concern.SENSITIVE_SKIN,),
    # 특별한 고민이 없는 피부는 기본 보습
    SkinType.NORMAL: (Concern.DEHYDRATED_SKIN,),
}
SKIN_TYPE_LABEL_KO = {
    SkinType.COMBINATION: "복합성", SkinType.OILY: "지성", SkinType.DRY: "건성",
    SkinType.SENSITIVE: "민감성", SkinType.NORMAL: "중성",
}


def apply_skin_type_defaults(profile: UserProfile) -> tuple[UserProfile, str | None]:
    """고민이 없고 피부 타입이 있으면 기본 고민을 채운다. (새 프로필, 응답 첫머리 안내 또는 None)."""
    if profile.concerns or not profile.skin_types:
        return profile, None
    concerns: list[Concern] = []
    for skin_type in profile.skin_types:
        for concern in DEFAULT_CONCERNS.get(skin_type, ()):
            if concern not in concerns:
                concerns.append(concern)
    if not concerns:
        return profile, None
    types = "·".join(SKIN_TYPE_LABEL_KO[t] for t in profile.skin_types if t in SKIN_TYPE_LABEL_KO)
    if profile.skin_types == [SkinType.NORMAL]:
        note = "중성 피부라 기본 보습 위주로 골랐어요. 특별한 고민이 있으면 알려 주세요."
    else:
        labels = "·".join(CONCERN_LABEL_KO.get(c, c.value) for c in concerns)
        note = f"{types} 피부를 {labels} 고민으로 보고 골랐어요. 특별한 고민이 있으면 알려 주세요."
    return profile.model_copy(update={"concerns": concerns, "effects": infer_effects(concerns)}), note
