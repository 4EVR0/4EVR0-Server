import json

from app.clients.llm_factory import get_async_llm_client
from app.core.config import settings
from app.domain.enums import Concern, Constraint, SkinType
from app.domain.user import UserProfile
from app.services.taxonomy_normalization_service import infer_effects

_SYSTEM_PROMPT = """You are a skincare profile extractor.
Given a user message (written in Korean or English), extract the following attributes and return JSON only.

Valid values:
- skin_types: DRY | OILY | COMBINATION | SENSITIVE | NORMAL
- concerns (pick all that apply):
    acne group      → ACNE | COMEDONES | PORE_CONGESTION
    oil group       → ENLARGED_PORES | OILY_SKIN
    sensitivity     → SENSITIVE_SKIN | REDNESS | IRRITATED_SKIN | ATOPIC_PRONE | ROSACEA_PRONE
    dryness         → DRY_SKIN | DEHYDRATED_SKIN | FLAKY_SKIN | ROUGH_TEXTURE
    barrier         → BARRIER_DAMAGE
    pigmentation    → HYPERPIGMENTATION | DULLNESS | UNEVEN_SKIN_TONE | BLEMISHES | POST_ACNE_MARKS | DARK_CIRCLES
    protection      → SUNBURN
    aging           → AGING_SIGNS | WRINKLES | LOSS_OF_ELASTICITY | SAGGING_SKIN
- constraints: FRAGRANCE_FREE | ALCOHOL_FREE | VEGAN | HYPOALLERGENIC | EWG_GREEN

Return format (no markdown, pure JSON):
{"skin_types": [], "concerns": [], "constraints": []}
"""


async def call_llm(message: str) -> UserProfile:
    client = get_async_llm_client()

    response = await client.chat.completions.create(
        model=settings.gpu_model,
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
