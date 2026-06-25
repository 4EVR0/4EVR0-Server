import json

from app.clients.llm_factory import get_async_llm_client
from app.core.config import settings
from app.domain.enums import Concern, Constraint, SkinType
from app.domain.user import UserProfile
from app.prompts import load_prompt
from app.services.taxonomy_normalization_service import infer_effects

# 프롬프트는 app/prompts/profile_extraction.txt 로 분리(버전 관리)
PROMPT_NAME = "profile_extraction"
_SYSTEM_PROMPT = load_prompt(PROMPT_NAME)


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
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
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
