import logging
import uuid

from app.clients.llm_factory import get_async_llm_client
from app.clients.llm_fallback import extract_with_fallback
from app.clients.neo4j_client import query_ingredients_by_effects
from app.core.config import settings
from app.schemas.recommend import IngredientResult, RecommendResponse

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a cosmetic ingredient recommendation assistant.
Given a user's skin concern message and a list of relevant ingredients with scientific evidence,
provide a helpful recommendation in Korean.

Format your response as:
1. Brief analysis of the user's skin concerns (1-2 sentences)
2. Top recommended ingredients with brief explanations
3. Usage tips if relevant

Keep the response concise and practical."""


async def recommend(session_id: str, message: str) -> RecommendResponse:
    turn_id = str(uuid.uuid4())

    profile, _ = await extract_with_fallback(message)

    effect_names = [e.value for e in profile.effects]
    raw_ingredients = await query_ingredients_by_effects(effect_names)

    ingredients = [
        IngredientResult(
            name=row["name"],
            claim=row.get("claim"),
            eligibility_tier=row.get("eligibility_tier"),
            paper_ref=row.get("paper_ref"),
        )
        for row in raw_ingredients
    ]

    response_text = await _build_llm_response(message, ingredients)

    return RecommendResponse(
        session_id=session_id,
        turn_id=turn_id,
        ingredients=ingredients,
        response_text=response_text,
        model_used=settings.gpu_model,
    )


async def _build_llm_response(message: str, ingredients: list[IngredientResult]) -> str:
    if ingredients:
        ingredient_context = "\n".join(
            f"- {i.name}: {i.claim or '효능 데이터 없음'} (근거 수준: {i.eligibility_tier or 'unknown'})"
            for i in ingredients[:10]
        )
        user_content = f"사용자 메시지: {message}\n\n관련 성분 데이터:\n{ingredient_context}"
    else:
        user_content = f"사용자 메시지: {message}\n\n(현재 성분 데이터베이스에 해당 고민에 맞는 성분 데이터가 없습니다. 일반적인 추천을 제공해 주세요.)"

    try:
        client = get_async_llm_client()
        response = await client.chat.completions.create(
            model=settings.gpu_model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0.3,
        )
        return response.choices[0].message.content or ""
    except Exception as exc:
        logger.warning("LLM response generation failed: %s", exc)
        if ingredients:
            names = ", ".join(i.name for i in ingredients[:5])
            return f"피부 고민 분석 결과, 다음 성분들을 추천드립니다: {names}"
        return "죄송합니다. 현재 추천 서비스를 이용할 수 없습니다. 잠시 후 다시 시도해 주세요."
