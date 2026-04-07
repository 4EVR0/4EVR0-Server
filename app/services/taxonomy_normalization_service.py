from app.domain.enums import SkinType, Concern, Effect, Constraint

# 피부 타입 동의어
_SKIN_TYPE_SYNONYMS: dict[str, SkinType] = {
    "건성": SkinType.DRY,
    "건조": SkinType.DRY,
    "건조한": SkinType.DRY,
    "건조함": SkinType.DRY,
    "지성": SkinType.OILY,
    "기름진": SkinType.OILY,
    "기름기": SkinType.OILY,
    "복합성": SkinType.COMBINATION,
    "수부지": SkinType.COMBINATION,
    "중성": SkinType.NORMAL,
    "일반": SkinType.NORMAL,
    "민감성": SkinType.SENSITIVE,
    "민감한": SkinType.SENSITIVE,
    "예민한": SkinType.SENSITIVE,
    "예민": SkinType.SENSITIVE,
}

# 고민 동의어
_CONCERN_SYNONYMS: dict[str, Concern] = {
    "여드름": Concern.ACNE,
    "트러블": Concern.ACNE,
    "뾰루지": Concern.ACNE,
    "건조": Concern.DRYNESS,
    "당김": Concern.DRYNESS,
    "수분부족": Concern.DRYNESS,
    "번들번들": Concern.OILINESS,
    "번질": Concern.OILINESS,
    "피지": Concern.OILINESS,
    "주름": Concern.WRINKLE,
    "노화": Concern.WRINKLE,
    "잡티": Concern.DARKSPOT,
    "색소침착": Concern.DARKSPOT,
    "기미": Concern.DARKSPOT,
    "미백": Concern.BRIGHTENING,
    "칙칙": Concern.BRIGHTENING,
    "모공": Concern.PORE,
    "모공넓음": Concern.PORE,
    "홍조": Concern.REDNESS,
    "붉음": Concern.REDNESS,
}

# 제약 동의어
_CONSTRAINT_SYNONYMS: dict[str, Constraint] = {
    "무향": Constraint.FRAGRANCE_FREE,
    "향없는": Constraint.FRAGRANCE_FREE,
    "무알콜": Constraint.ALCOHOL_FREE,
    "알코올없는": Constraint.ALCOHOL_FREE,
    "비건": Constraint.VEGAN,
    "저자극": Constraint.HYPOALLERGENIC,
    "민감피부용": Constraint.HYPOALLERGENIC,
    "ewg": Constraint.EWG_GREEN,
    "ewg그린": Constraint.EWG_GREEN,
}

# concern → 자동 추론 effect 매핑
CONCERN_EFFECT_MAP: dict[Concern, Effect] = {
    Concern.DRYNESS: Effect.MOISTURIZING,
    Concern.OILINESS: Effect.SEBUM_CONTROL,
    Concern.WRINKLE: Effect.ANTI_AGING,
    Concern.BRIGHTENING: Effect.WHITENING,
    Concern.PORE: Effect.PORE_CARE,
    Concern.ACNE: Effect.ACNE_CARE,
    Concern.REDNESS: Effect.SOOTHING,
    Concern.DARKSPOT: Effect.WHITENING,
}


def normalize_skin_types(text: str) -> list[SkinType]:
    results: list[SkinType] = []
    for keyword, skin_type in _SKIN_TYPE_SYNONYMS.items():
        if keyword in text and skin_type not in results:
            results.append(skin_type)
    return results


def normalize_concerns(text: str) -> list[Concern]:
    results: list[Concern] = []
    for keyword, concern in _CONCERN_SYNONYMS.items():
        if keyword in text and concern not in results:
            results.append(concern)
    return results


def normalize_constraints(text: str) -> list[Constraint]:
    results: list[Constraint] = []
    for keyword, constraint in _CONSTRAINT_SYNONYMS.items():
        if keyword in text and constraint not in results:
            results.append(constraint)
    return results


def infer_effects(concerns: list[Concern]) -> list[Effect]:
    results: list[Effect] = []
    for concern in concerns:
        effect = CONCERN_EFFECT_MAP.get(concern)
        if effect and effect not in results:
            results.append(effect)
    return results
