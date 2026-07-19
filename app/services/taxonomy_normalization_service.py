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

# 고민 동의어 — concern_map.md Concern Taxonomy 기준 26개 매핑
_CONCERN_SYNONYMS: dict[str, Concern] = {
    # ACNE (여드름)
    "여드름": Concern.ACNE,
    "트러블": Concern.ACNE,
    "뾰루지": Concern.ACNE,
    "피부트러블": Concern.ACNE,
    "뾰로지": Concern.ACNE,
    # COMEDONES (면포)
    "면포": Concern.COMEDONES,
    "블랙헤드": Concern.COMEDONES,
    "화이트헤드": Concern.COMEDONES,
    "좁쌀": Concern.COMEDONES,
    # PORE_CONGESTION (모공 막힘)
    "모공막힘": Concern.PORE_CONGESTION,
    "모공 막힘": Concern.PORE_CONGESTION,
    "모공 오염": Concern.PORE_CONGESTION,
    "모공오염": Concern.PORE_CONGESTION,
    # ENLARGED_PORES (모공 확대)
    "모공": Concern.ENLARGED_PORES,
    "모공확대": Concern.ENLARGED_PORES,
    "넓은 모공": Concern.ENLARGED_PORES,
    "모공 넓음": Concern.ENLARGED_PORES,
    "모공넓음": Concern.ENLARGED_PORES,
    # OILY_SKIN (지성 피부)
    "번들번들": Concern.OILY_SKIN,
    "번질": Concern.OILY_SKIN,
    "피지": Concern.OILY_SKIN,
    "유분": Concern.OILY_SKIN,
    "번들거림": Concern.OILY_SKIN,
    # SENSITIVE_SKIN (민감성 피부)
    "민감성 피부": Concern.SENSITIVE_SKIN,
    "민감 피부": Concern.SENSITIVE_SKIN,
    "예민 피부": Concern.SENSITIVE_SKIN,
    "피부 민감": Concern.SENSITIVE_SKIN,
    # REDNESS (붉은기)
    "홍조": Concern.REDNESS,
    "붉은기": Concern.REDNESS,
    "붉음": Concern.REDNESS,
    "빨개짐": Concern.REDNESS,
    "피부 붉음": Concern.REDNESS,
    # IRRITATED_SKIN (자극 피부)
    "자극": Concern.IRRITATED_SKIN,
    "따가움": Concern.IRRITATED_SKIN,
    "따끔": Concern.IRRITATED_SKIN,
    "피부 자극": Concern.IRRITATED_SKIN,
    "자극받은": Concern.IRRITATED_SKIN,
    # ATOPIC_PRONE (아토피 피부 경향)
    "아토피": Concern.ATOPIC_PRONE,
    "아토피성": Concern.ATOPIC_PRONE,
    "아토피 피부": Concern.ATOPIC_PRONE,
    # ROSACEA_PRONE (주사 피부 경향)
    "주사": Concern.ROSACEA_PRONE,
    "주사피부": Concern.ROSACEA_PRONE,
    "로사세아": Concern.ROSACEA_PRONE,
    # DRY_SKIN (건성 피부)
    "건성 피부": Concern.DRY_SKIN,
    "건조 피부": Concern.DRY_SKIN,
    "피부 건조": Concern.DRY_SKIN,
    "건조함": Concern.DRY_SKIN,
    "건조한": Concern.DRY_SKIN,
    "건조": Concern.DRY_SKIN,
    # DEHYDRATED_SKIN (수분 부족 피부)
    "수분부족": Concern.DEHYDRATED_SKIN,
    "당기": Concern.DEHYDRATED_SKIN,
    "당김": Concern.DEHYDRATED_SKIN,
    "당겨": Concern.DEHYDRATED_SKIN,
    "수분 부족": Concern.DEHYDRATED_SKIN,
    "탈수": Concern.DEHYDRATED_SKIN,
    "피부 당김": Concern.DEHYDRATED_SKIN,
    # FLAKY_SKIN (각질)
    "각질": Concern.FLAKY_SKIN,
    "각질 일어남": Concern.FLAKY_SKIN,
    "각질일어남": Concern.FLAKY_SKIN,
    "벗겨짐": Concern.FLAKY_SKIN,
    "일어남": Concern.FLAKY_SKIN,
    # ROUGH_TEXTURE (피부결 거침)
    "피부결": Concern.ROUGH_TEXTURE,
    "거친 피부": Concern.ROUGH_TEXTURE,
    "피부 거침": Concern.ROUGH_TEXTURE,
    "울퉁불퉁": Concern.ROUGH_TEXTURE,
    "피부결 거침": Concern.ROUGH_TEXTURE,
    # BARRIER_DAMAGE (피부 장벽 손상)
    "피부장벽": Concern.BARRIER_DAMAGE,
    "장벽 손상": Concern.BARRIER_DAMAGE,
    "피부 장벽": Concern.BARRIER_DAMAGE,
    "장벽 약화": Concern.BARRIER_DAMAGE,
    "피부 장벽 손상": Concern.BARRIER_DAMAGE,
    # HYPERPIGMENTATION (색소침착)
    "색소침착": Concern.HYPERPIGMENTATION,
    "기미": Concern.HYPERPIGMENTATION,
    "색소": Concern.HYPERPIGMENTATION,
    "색소 침착": Concern.HYPERPIGMENTATION,
    # DULLNESS (피부 톤 저하)
    "칙칙": Concern.DULLNESS,
    "칙칙함": Concern.DULLNESS,
    "어두운 피부": Concern.DULLNESS,
    "피부 톤 저하": Concern.DULLNESS,
    "피부톤 저하": Concern.DULLNESS,
    # UNEVEN_SKIN_TONE (피부 톤 불균일)
    "피부톤 불균일": Concern.UNEVEN_SKIN_TONE,
    "피부 톤 불균일": Concern.UNEVEN_SKIN_TONE,
    "얼룩덜룩": Concern.UNEVEN_SKIN_TONE,
    "피부 톤": Concern.UNEVEN_SKIN_TONE,
    "피부톤": Concern.UNEVEN_SKIN_TONE,
    # BLEMISHES (잡티)
    "잡티": Concern.BLEMISHES,
    "트러블 자국": Concern.BLEMISHES,
    "피부 결점": Concern.BLEMISHES,
    # POST_ACNE_MARKS (여드름 자국)
    "여드름 자국": Concern.POST_ACNE_MARKS,
    "여드름자국": Concern.POST_ACNE_MARKS,
    "여드름 흉터": Concern.POST_ACNE_MARKS,
    "여드름흉터": Concern.POST_ACNE_MARKS,
    # DARK_CIRCLES (다크서클)
    "다크서클": Concern.DARK_CIRCLES,
    "눈 밑 다크서클": Concern.DARK_CIRCLES,
    "눈밑 다크서클": Concern.DARK_CIRCLES,
    # SUNBURN (자외선 손상)
    "자외선": Concern.SUNBURN,
    "자외선 손상": Concern.SUNBURN,
    "선번": Concern.SUNBURN,
    "햇빛 손상": Concern.SUNBURN,
    "햇빛손상": Concern.SUNBURN,
    # AGING_SIGNS (노화 징후)
    "노화": Concern.AGING_SIGNS,
    "노화 징후": Concern.AGING_SIGNS,
    "나이듦": Concern.AGING_SIGNS,
    # WRINKLES (주름)
    "주름": Concern.WRINKLES,
    "잔주름": Concern.WRINKLES,
    "깊은 주름": Concern.WRINKLES,
    # LOSS_OF_ELASTICITY (탄력 저하)
    "탄력 저하": Concern.LOSS_OF_ELASTICITY,
    "탄력없음": Concern.LOSS_OF_ELASTICITY,
    "탄력 없음": Concern.LOSS_OF_ELASTICITY,
    # SAGGING_SKIN (피부 처짐)
    "피부 처짐": Concern.SAGGING_SKIN,
    "처짐": Concern.SAGGING_SKIN,
    "피부처짐": Concern.SAGGING_SKIN,
    "탄력": Concern.SAGGING_SKIN,
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
# DB Effect 코드 기준 (15개): ANTI_INFLAMMATORY, SOOTHING, BARRIER_REPAIR, HYDRATING,
# MOISTURE_RETENTION, SEBUM_REGULATION, ANTIMICROBIAL, KERATOLYTIC, COMEDOLYTIC,
# WOUND_HEALING, DEPIGMENTING, BRIGHTENING, ANTIOXIDANT, PHOTOPROTECTIVE, ANTI_AGING
CONCERN_EFFECT_MAP: dict[Concern, list[Effect]] = {
    Concern.ACNE: [
        Effect.ANTI_INFLAMMATORY,
        Effect.SEBUM_REGULATION,
        Effect.KERATOLYTIC,
        Effect.COMEDOLYTIC,
        Effect.ANTIMICROBIAL,
    ],
    Concern.COMEDONES: [
        Effect.KERATOLYTIC,
        Effect.COMEDOLYTIC,
        Effect.SEBUM_REGULATION,
    ],
    Concern.PORE_CONGESTION: [
        Effect.KERATOLYTIC,
        Effect.COMEDOLYTIC,
        Effect.SEBUM_REGULATION,
    ],
    Concern.ENLARGED_PORES: [
        Effect.SEBUM_REGULATION,
        Effect.KERATOLYTIC,
    ],
    Concern.OILY_SKIN: [
        Effect.SEBUM_REGULATION,
        # ANTI_INFLAMMATORY 제거(#40): 지성 자체는 염증 아님 → 진정 성분/제품 유입으로 precision↓. 염증은 ACNE 몫.
    ],
    Concern.SENSITIVE_SKIN: [
        Effect.SOOTHING,
        Effect.ANTI_INFLAMMATORY,
        Effect.BARRIER_REPAIR,
        Effect.HYDRATING,
    ],
    Concern.REDNESS: [
        Effect.ANTI_INFLAMMATORY,
        Effect.SOOTHING,
    ],
    Concern.IRRITATED_SKIN: [
        Effect.ANTI_INFLAMMATORY,
        Effect.SOOTHING,
        Effect.BARRIER_REPAIR,
    ],
    Concern.ATOPIC_PRONE: [
        Effect.ANTI_INFLAMMATORY,
        Effect.SOOTHING,
        Effect.BARRIER_REPAIR,
        Effect.HYDRATING,
    ],
    Concern.ROSACEA_PRONE: [
        Effect.ANTI_INFLAMMATORY,
        Effect.SOOTHING,
    ],
    Concern.DRY_SKIN: [
        Effect.HYDRATING,
        Effect.MOISTURE_RETENTION,
        Effect.BARRIER_REPAIR,
    ],
    Concern.DEHYDRATED_SKIN: [
        Effect.HYDRATING,
        Effect.MOISTURE_RETENTION,
    ],
    Concern.FLAKY_SKIN: [
        Effect.HYDRATING,
        Effect.KERATOLYTIC,
        Effect.MOISTURE_RETENTION,
    ],
    Concern.ROUGH_TEXTURE: [
        Effect.KERATOLYTIC,
        Effect.MOISTURE_RETENTION,
    ],
    Concern.BARRIER_DAMAGE: [
        Effect.BARRIER_REPAIR,
        Effect.HYDRATING,
        Effect.MOISTURE_RETENTION,
        Effect.SOOTHING,
    ],
    Concern.HYPERPIGMENTATION: [
        Effect.DEPIGMENTING,
        Effect.BRIGHTENING,
        # ANTI_INFLAMMATORY 제거(#40): 기존 색소 개선엔 tangential(진정 성분 유입) → precision↓.
    ],
    Concern.DULLNESS: [
        Effect.BRIGHTENING,
        Effect.DEPIGMENTING,
        Effect.KERATOLYTIC,
        Effect.ANTIOXIDANT,
    ],
    Concern.UNEVEN_SKIN_TONE: [
        Effect.BRIGHTENING,
        Effect.DEPIGMENTING,
    ],
    Concern.BLEMISHES: [
        Effect.DEPIGMENTING,
        Effect.BRIGHTENING,
        Effect.WOUND_HEALING,
    ],
    Concern.POST_ACNE_MARKS: [
        Effect.DEPIGMENTING,
        Effect.BRIGHTENING,
        Effect.WOUND_HEALING,
    ],
    Concern.DARK_CIRCLES: [
        Effect.DEPIGMENTING,
        Effect.BRIGHTENING,
    ],
    Concern.SUNBURN: [
        Effect.PHOTOPROTECTIVE,
        Effect.ANTIOXIDANT,
        Effect.SOOTHING,
    ],
    Concern.AGING_SIGNS: [
        Effect.ANTI_AGING,
        Effect.ANTIOXIDANT,
    ],
    Concern.WRINKLES: [
        Effect.ANTI_AGING,
        # HYDRATING 제거(#40): 수분은 주름 '치료'가 아닌 generic 효능 → 보습 제품 유입으로 precision↓.
    ],
    Concern.LOSS_OF_ELASTICITY: [
        Effect.ANTI_AGING,
        Effect.MOISTURE_RETENTION,
    ],
    Concern.SAGGING_SKIN: [
        Effect.ANTI_AGING,
    ],
}


def normalize_skin_types(text: str) -> list[SkinType]:
    results: list[SkinType] = []
    for keyword, skin_type in _SKIN_TYPE_SYNONYMS.items():
        if keyword in text and skin_type not in results:
            results.append(skin_type)
    return results


def normalize_concerns(text: str) -> list[Concern]:
    results: list[Concern] = []
    # 긴 키워드를 먼저 매칭해 짧은 키워드의 오버매칭을 방지
    sorted_synonyms = sorted(_CONCERN_SYNONYMS.items(), key=lambda x: len(x[0]), reverse=True)
    for keyword, concern in sorted_synonyms:
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
        for effect in CONCERN_EFFECT_MAP.get(concern, []):
            if effect not in results:
                results.append(effect)
    return results
