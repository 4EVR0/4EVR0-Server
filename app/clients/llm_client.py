import json

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
