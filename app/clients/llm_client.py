import json

import openai

from app.core.config import settings
from app.domain.enums import Concern, Constraint, SkinType
from app.domain.user import UserProfile
from app.services.taxonomy_normalization_service import infer_effects

_SYSTEM_PROMPT = """You are a skincare profile extractor.
Given a user message (written in Korean or English), extract the following attributes and return JSON only.

Valid values:
- skin_types: DRY | OILY | COMBINATION | SENSITIVE | NORMAL
- concerns: ACNE | DRYNESS | OILINESS | WRINKLE | BRIGHTENING | PORE | REDNESS | DARKSPOT
- constraints: FRAGRANCE_FREE | ALCOHOL_FREE | VEGAN | HYPOALLERGENIC | EWG_GREEN

Return format (no markdown, pure JSON):
{"skin_types": [], "concerns": [], "constraints": []}
"""


async def call_llm(message: str) -> UserProfile:
    """OpenAI API를 호출해 UserProfile을 반환한다. 오류 시 예외를 그대로 raise한다."""
    client = openai.AsyncOpenAI(
        api_key=settings.openai_api_key,
        timeout=float(settings.llm_timeout_seconds),
    )

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content or "{}"
    data = json.loads(raw)

    skin_types = [SkinType(v) for v in data.get("skin_types", []) if v in SkinType._value2member_map_]
    concerns = [Concern(v) for v in data.get("concerns", []) if v in Concern._value2member_map_]
    constraints = [Constraint(v) for v in data.get("constraints", []) if v in Constraint._value2member_map_]
    effects = infer_effects(concerns)

    return UserProfile(
        skin_types=skin_types,
        concerns=concerns,
        effects=effects,
        constraints=constraints,
    )
