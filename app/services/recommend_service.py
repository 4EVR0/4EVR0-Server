import logging
import uuid

from app.clients.llm_factory import get_async_llm_client
from app.clients.llm_fallback import extract_with_fallback
from app.clients.neo4j_client import query_ingredients_by_effects, query_products_by_ingredients
from app.core import metrics
from app.core.config import settings
from app.prompts import load_prompt
from app.schemas.recommend import IngredientResult, ProductResult, RecommendResponse

logger = logging.getLogger(__name__)

# 프롬프트는 app/prompts/recommend_response.txt 로 분리(버전 관리)
_SYSTEM_PROMPT = load_prompt("recommend_response")


async def recommend(session_id: str, message: str, gen_prompt_name: str | None = None) -> RecommendResponse:
    turn_id = str(uuid.uuid4())
    # gen_prompt_name 지정 시 응답 프롬프트 교체(실험용). 미지정이면 프로덕션 기본.
    system_prompt = load_prompt(gen_prompt_name) if gen_prompt_name else _SYSTEM_PROMPT

    try:
        # 1) 프로필 추출 (LLM, 실패 시 규칙 기반 폴백)
        with metrics.track_stage("extract"):
            profile, extraction_method = await extract_with_fallback(message)
        metrics.profile_extraction_method_total.labels(method=extraction_method).inc()

        # 2) Neo4j 조회 (효능→성분, 성분→제품)
        with metrics.track_stage("neo4j"):
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

            # 추천 성분 상위 10개로 제품 조회 (pubmed_evidence 우선)
            top_ingredient_names = [i.name for i in ingredients[:10]]
            raw_products = await query_products_by_ingredients(top_ingredient_names)
        metrics.recommend_ingredients_found.observe(len(ingredients))

        products = [
            ProductResult(
                product_id=row["product_id"],
                product_name=row["product_name"],
                brand=row["brand"],
                category=row["category"],
                matched_count=row["matched_count"],
                matched_ingredients=row["matched_ingredients"],
            )
            for row in raw_products
        ]

        # 3) LLM 응답 생성
        with metrics.track_stage("llm_response"):
            response_text = await _build_llm_response(message, ingredients, products, system_prompt)

        metrics.recommend_requests_total.labels(status="ok").inc()
        return RecommendResponse(
            session_id=session_id,
            turn_id=turn_id,
            ingredients=ingredients,
            products=products,
            response_text=response_text,
            model_used=settings.gpu_model,
        )
    except Exception:
        metrics.recommend_requests_total.labels(status="error").inc()
        raise


async def _build_llm_response(
    message: str,
    ingredients: list[IngredientResult],
    products: list[ProductResult],
    system_prompt: str = _SYSTEM_PROMPT,
) -> str:
    sections = [f"사용자 메시지: {message}"]

    if ingredients:
        ingredient_lines = "\n".join(
            f"- {i.name}: {i.claim or '효능 데이터 없음'} (근거 수준: {i.eligibility_tier or 'unknown'})"
            for i in ingredients[:10]
        )
        sections.append(f"관련 성분 데이터:\n{ingredient_lines}")
    else:
        sections.append("(현재 성분 데이터베이스에 해당 고민에 맞는 성분 데이터가 없습니다. 일반적인 추천을 제공해 주세요.)")

    if products:
        product_lines = "\n".join(
            f"- [{p.category}] {p.brand} {p.product_name} (핵심 성분 {p.matched_count}개 포함: {', '.join(p.matched_ingredients[:3])})"
            for p in products
        )
        sections.append(f"추천 제품 데이터:\n{product_lines}")

    user_content = "\n\n".join(sections)

    try:
        client = get_async_llm_client()
        response = await client.chat.completions.create(
            model=settings.gpu_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.3,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        return response.choices[0].message.content or ""
    except Exception as exc:
        logger.warning("LLM response generation failed: %s", exc)
        if products:
            prod_names = ", ".join(f"{p.brand} {p.product_name}" for p in products[:3])
            return f"피부 고민 분석 결과, 다음 제품들을 추천드립니다: {prod_names}"
        if ingredients:
            names = ", ".join(i.name for i in ingredients[:5])
            return f"피부 고민 분석 결과, 다음 성분들을 추천드립니다: {names}"
        return "죄송합니다. 현재 추천 서비스를 이용할 수 없습니다. 잠시 후 다시 시도해 주세요."
