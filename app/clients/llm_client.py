import json
import re

from app.clients.llm_factory import get_async_llm_client
from app.clients.llm_gate import llm_slot
from app.core.config import settings
from app.domain.enums import Concern, Constraint, SkinType
from app.domain.user import UserProfile
from app.prompts import load_prompt
from app.services.taxonomy_normalization_service import infer_effects

# 프롬프트는 app/prompts/profile_extraction.txt 로 분리(버전 관리)
PROMPT_NAME = "profile_extraction"
_SYSTEM_PROMPT = load_prompt(PROMPT_NAME)

# guided decoding용 프로필 스키마 — enum에서 동기화. 추출 출력을 유효 JSON·유효 enum 값으로 강제.
PROFILE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "skin_types": {"type": "array", "items": {"type": "string", "enum": [e.value for e in SkinType]}},
        "concerns": {"type": "array", "items": {"type": "string", "enum": [e.value for e in Concern]}},
        "constraints": {"type": "array", "items": {"type": "string", "enum": [e.value for e in Constraint]}},
    },
    "required": ["skin_types", "concerns", "constraints"],
    "additionalProperties": False,
}

_ENLARGED_PORE_SIGNAL = re.compile(
    r"(?:모공|pores?).{0,12}(?:넓|커|크기|large|visible)"
    r"|(?:넓|커|큰|large|visible).{0,12}(?:모공|pores?)",
    re.IGNORECASE,
)
_REDNESS_SIGNAL = re.compile(
    r"홍조|로사케아|rosacea|redness|빨개지|빨개져|붉어지|붉어져|붉은\s*기"
    r"|(?:얼굴|피부).{0,12}(?:빨갛|붉)",
    re.IGNORECASE,
)
_EXPLICIT_SENSITIVE_SKIN = re.compile(r"민감|예민|sensitive", re.IGNORECASE)
_COMBINATION_SKIN = re.compile(r"복합성|수부지", re.IGNORECASE)
_OILY_SURFACE_SIGNAL = re.compile(r"번들|기름|피지|유분|oily", re.IGNORECASE)
_INNER_DRY_SIGNAL = re.compile(
    r"속.{0,20}?(?:건조|수분.{0,8}부족|당)|(?:볼|뺨).{0,8}?(?:건조|당)",
    re.IGNORECASE,
)
_INNER_ONLY_DRY_SIGNAL = re.compile(r"속.{0,20}?(?:건조|수분.{0,8}부족|당)", re.IGNORECASE)
_SKIN_DRY_SIGNAL = re.compile(r"건성|건조|당김|당기|당겨|메마르", re.IGNORECASE)
_NEGATED_SKIN_SIGNAL = re.compile(
    r"^(?:.{0,4}?(?:지\s*않|지\s*못|안\s*보|없)|.{0,6}?거나.{0,20}?보이지\s*않)",
    re.IGNORECASE,
)
_DEHYDRATION_CONCERN = re.compile(
    r"(?<![가-힣])속(?:건조|당|.{0,6}(?:건조|수분|당김|당겨|당기))"
    r"|피부속.{0,6}(?:건조|수분|당김|당겨|당기)"
    r"|수분.{0,6}부족|수분감.{0,4}없|탈수",
    re.IGNORECASE,
)
_SURFACE_DRYNESS_SIGNAL = re.compile(r"건성|건조|당김|당기|당겨")
_SENSITIVE_CONCERN_SIGNAL = re.compile(
    r"민감성\s*피부|민감성이라|예민한?\s*피부|피부.{0,3}(?:민감|예민)"
)
_ROSACEA_SIGNAL = re.compile(r"로사케아|로사세아|주사(?:성|피부)?")
_COMEDONE_SIGNAL = re.compile(r"면포|블랙헤드|화이트헤드|좁쌀|검은\s*점|하얀\s*알갱이")
_CLOGGED_PORE_SIGNAL = re.compile(
    r"모공.{0,10}(?:막|답답|피지.{0,3}차)|막힌.{0,8}모공"
)
_PIGMENT_SIGNAL = re.compile(r"기미|색소|침착|갈색.{0,5}(?:반점|자국)|검버섯")
_DULLNESS_SIGNAL = re.compile(r"칙칙|생기.{0,4}없|안색.{0,6}탁|얼굴빛.{0,6}탁")
_SPECIFIC_AGING_SIGNALS = (
    (re.compile(r"잔주름|깊은\s*주름|주름(?:이\s*고민|\s*관리|\s*개선)"), Concern.WRINKLES),
    (re.compile(r"탄력(?:이\s*없|이\s*떨어|\s*저하|\s*개선)"), Concern.LOSS_OF_ELASTICITY),
    (re.compile(r"처짐|처져|처지는"), Concern.SAGGING_SKIN),
)


def _has_positive_skin_signal(message: str, signal: re.Pattern[str]) -> bool:
    """A mentioned feature is not evidence when the user explicitly negates it."""
    return any(
        not _NEGATED_SKIN_SIGNAL.match(message[match.end():])
        for match in signal.finditer(message)
    )


def _normalize_skin_types(message: str, skin_types: list[SkinType]) -> list[SkinType]:
    """Keep skin-type labels tied to positive, directly described evidence."""
    normalized = list(skin_types)

    # Disease/symptom or a low-irritation request alone does not establish a
    # sensitive *skin type*. Keep it only when the user explicitly says so.
    if SkinType.SENSITIVE in normalized and not _EXPLICIT_SENSITIVE_SKIN.search(message):
        normalized.remove(SkinType.SENSITIVE)

    oily = _has_positive_skin_signal(message, _OILY_SURFACE_SIGNAL)
    inner_dry = _has_positive_skin_signal(message, _INNER_DRY_SIGNAL)
    # Inner tightness/dehydration alone is a concern, not a dry *skin type*.
    surface_message = _INNER_ONLY_DRY_SIGNAL.sub("", message)
    surface_dry = _has_positive_skin_signal(surface_message, _SKIN_DRY_SIGNAL)

    # Only a named combination type or two positive, complementary signs
    # establish COMBINATION. In particular, a negated oily surface does not.
    is_combination = bool(_COMBINATION_SKIN.search(message) or (oily and inner_dry))
    if is_combination:
        normalized = [
            item for item in normalized
            if item not in {SkinType.OILY, SkinType.DRY, SkinType.COMBINATION}
        ]
        if SkinType.COMBINATION not in normalized:
            normalized.append(SkinType.COMBINATION)
    else:
        normalized = [
            item for item in normalized
            if item != SkinType.COMBINATION
            and not (item == SkinType.DRY and not surface_dry)
            and not (item == SkinType.OILY and not oily)
        ]
        if SkinType.COMBINATION in skin_types:
            if oily and SkinType.OILY not in normalized:
                normalized.append(SkinType.OILY)
            if surface_dry and SkinType.DRY not in normalized:
                normalized.append(SkinType.DRY)
    return normalized


def _normalize_concerns(message: str, concerns: list[Concern]) -> list[Concern]:
    """Keep model labels aligned with directly stated, non-adjacent concerns.

    Oily/combination skin does not imply enlarged pores. Likewise, the words
    ``붉은 자국`` and ``빨간 여드름`` describe post-acne marks or acne itself,
    not independent redness. Nearby dryness, pigmentation and pore labels also
    require their own evidence rather than being inferred from a related label.
    """
    normalized: list[Concern] = []
    for concern in concerns:
        if concern == Concern.ENLARGED_PORES and not _ENLARGED_PORE_SIGNAL.search(message):
            continue
        if concern == Concern.REDNESS and not _REDNESS_SIGNAL.search(message):
            continue
        if concern == Concern.ROSACEA_PRONE and not _ROSACEA_SIGNAL.search(message):
            if _REDNESS_SIGNAL.search(message) and Concern.REDNESS not in concerns:
                normalized.append(Concern.REDNESS)
            continue
        if concern == Concern.DEHYDRATED_SKIN and not _DEHYDRATION_CONCERN.search(message):
            if _SURFACE_DRYNESS_SIGNAL.search(message) and Concern.DRY_SKIN not in concerns:
                normalized.append(Concern.DRY_SKIN)
            continue
        if concern == Concern.COMEDONES and not _COMEDONE_SIGNAL.search(message):
            continue
        if concern == Concern.PORE_CONGESTION and not _CLOGGED_PORE_SIGNAL.search(message):
            continue
        if concern == Concern.HYPERPIGMENTATION and not _PIGMENT_SIGNAL.search(message):
            if "잡티" in message and Concern.BLEMISHES not in concerns:
                normalized.append(Concern.BLEMISHES)
            continue
        if concern == Concern.DULLNESS and not _DULLNESS_SIGNAL.search(message):
            continue
        normalized.append(concern)

    if _SENSITIVE_CONCERN_SIGNAL.search(message) and Concern.SENSITIVE_SKIN not in normalized:
        normalized.append(Concern.SENSITIVE_SKIN)

    # The model sometimes emits the umbrella aging category despite naming a
    # specific symptom. Replace it only when the text positively names one.
    if Concern.AGING_SIGNS in normalized:
        specific = [label for signal, label in _SPECIFIC_AGING_SIGNALS if signal.search(message)]
        if specific:
            normalized.remove(Concern.AGING_SIGNS)
            for label in specific:
                if label not in normalized:
                    normalized.append(label)
    return normalized


def build_extract_extra_body() -> dict:
    """추출 create() 의 extra_body. guided decoding 활성 시 스키마 강제를 추가."""
    body: dict = {"chat_template_kwargs": {"enable_thinking": False}}
    if settings.extract_guided_decoding:
        body["guided_json"] = PROFILE_JSON_SCHEMA
    return body


async def call_llm(message: str) -> UserProfile:
    client = get_async_llm_client()

    # GPU 동시성 게이트 안에서만 호출 — 단일 GPU로 나가는 동시 호출 수를 제한한다.
    async with llm_slot():
        response = await client.chat.completions.create(
            model=settings.gpu_model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ],
            temperature=0,
            response_format={"type": "json_object"},
            extra_body=build_extract_extra_body(),
        )

    raw = response.choices[0].message.content or "{}"
    data = json.loads(raw)

    skin_types = _normalize_skin_types(
        message,
        [SkinType(v) for v in data.get("skin_types", []) if v in SkinType._value2member_map_],
    )
    concerns = _normalize_concerns(
        message,
        [Concern(v) for v in data.get("concerns", []) if v in Concern._value2member_map_],
    )
    constraints = [Constraint(v) for v in data.get("constraints", []) if v in Constraint._value2member_map_]
    effects = infer_effects(concerns)

    return UserProfile(
        skin_types=skin_types,
        concerns=concerns,
        effects=effects,
        constraints=constraints,
    )
