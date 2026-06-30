import logging
import uuid

from app.clients.llm_factory import get_async_llm_client
from app.clients.llm_fallback import extract_with_fallback
from app.clients.llm_gate import LLMOverCapacityError, llm_slot
from app.clients.neo4j_client import query_ingredients_by_effects, query_products_by_ingredients
from app.core import metrics
from app.core.config import settings
from app.domain.enums import Concern
from app.prompts import load_prompt
from app.repositories import recommend_cache
from app.schemas.recommend import IngredientResult, ProductResult, RecommendResponse

# concern별 적합한 제품 카테고리 (leave-on 제품 기준, 씻어내는 클렌징 계열 제외)
_LEAVE_ON = ["크림", "세럼", "앰플", "에센스", "로션", "토너", "미스트", "올인원"]
_CONCERN_CATEGORY_MAP: dict[Concern, list[str]] = {
    Concern.ACNE:            [c for c in _LEAVE_ON if c != "크림"] + ["필링스크럽"],
    Concern.COMEDONES:       [c for c in _LEAVE_ON if c != "크림"],
    Concern.PORE_CONGESTION: [c for c in _LEAVE_ON if c != "크림"],
    Concern.ENLARGED_PORES:  [c for c in _LEAVE_ON if c != "크림"],
    Concern.OILY_SKIN:       [c for c in _LEAVE_ON if c != "크림"],
    Concern.FLAKY_SKIN:      _LEAVE_ON + ["페이스오일", "필링스크럽"],
    Concern.ROUGH_TEXTURE:   _LEAVE_ON + ["필링스크럽"],
    Concern.DRY_SKIN:        _LEAVE_ON + ["페이스오일"],
    Concern.DEHYDRATED_SKIN: _LEAVE_ON + ["페이스오일"],
    Concern.BARRIER_DAMAGE:  _LEAVE_ON + ["페이스오일"],
}
# 위에 없는 concern은 _LEAVE_ON을 기본값으로 사용


def _appropriate_categories(concerns: list[Concern]) -> list[str]:
    """복수 concern의 교집합 카테고리를 반환한다 (가장 제한적인 조건 적용)."""
    if not concerns:
        return _LEAVE_ON
    sets = [set(_CONCERN_CATEGORY_MAP.get(c, _LEAVE_ON)) for c in concerns]
    intersection = sets[0].intersection(*sets[1:])
    return list(intersection) if intersection else _LEAVE_ON

logger = logging.getLogger(__name__)

# 프롬프트는 app/prompts/recommend_response*.txt 로 분리(버전 관리).
# v3: 근거 수준(논문 근거 N건 / 성분 기능 근거)을 인용·구분하도록 강화 — 공정 judge eval에서
# grounding 4.05→4.84, overall 4.47→4.77(temp=0, 결정론적).
# v4: 한글 성분명을 우선 사용하고 제품 수·분량을 제한해 간결성과 완결성을 강화.
#     응답 구조도 "성분 설명 → 제품 추천" 순서로 정렬(성분별 효능을 먼저 설명).
_SYSTEM_PROMPT = load_prompt("recommend_response.v4")


async def recommend(session_id: str, message: str, gen_prompt_name: str | None = None) -> RecommendResponse:
    turn_id = str(uuid.uuid4())

    # 캐시 조회(추출 이전) — 히트 시 extract·neo4j·generate를 통째로 건너뛴다 → GPU 비용 0.
    cached = await recommend_cache.get(message, gen_prompt_name)
    if cached is not None:
        metrics.recommend_cache_total.labels(result="hit").inc()
        metrics.recommend_requests_total.labels(status="ok").inc()
        # session_id·turn_id는 요청마다 새로 부여(캐시는 콘텐츠만 보관).
        return RecommendResponse(session_id=session_id, turn_id=turn_id, **cached)
    metrics.recommend_cache_total.labels(result="miss").inc()

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
                    kor_name=row.get("kor_name"),
                    claim=row.get("claim"),
                    eligibility_tier=row.get("eligibility_tier"),
                    paper_ref=row.get("paper_ref"),
                )
                for row in raw_ingredients
            ]

            # 추천 성분 상위 10개로 제품 조회 (pubmed_evidence 우선, concern 카테고리 필터 적용)
            top_ingredient_names = [i.name for i in ingredients[:10]]
            cats = _appropriate_categories(profile.concerns)
            raw_products = await query_products_by_ingredients(top_ingredient_names, appropriate_categories=cats)
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

        # 같은 문장 재요청이 GPU를 다시 치지 않도록 콘텐츠를 캐시에 저장(session/turn 제외).
        await recommend_cache.set(message, gen_prompt_name, {
            "ingredients": [i.model_dump() for i in ingredients],
            "products": [p.model_dump() for p in products],
            "response_text": response_text,
            "model_used": settings.gpu_model,
        })

        metrics.recommend_requests_total.labels(status="ok").inc()
        return RecommendResponse(
            session_id=session_id,
            turn_id=turn_id,
            ingredients=ingredients,
            products=products,
            response_text=response_text,
            model_used=settings.gpu_model,
        )
    except LLMOverCapacityError:
        # 부하 차단으로 거절된 요청은 서버 '에러'가 아니라 의도된 백프레셔 → 별도 status로 집계.
        metrics.recommend_requests_total.labels(status="rejected").inc()
        raise
    except Exception:
        metrics.recommend_requests_total.labels(status="error").inc()
        raise


def _ingredient_display_name(ingredient: IngredientResult) -> str:
    """소비자용 한글명을 우선하고 INCI 이름을 괄호 안에 보존한다."""
    kor_name = (ingredient.kor_name or "").strip()
    if kor_name and kor_name.casefold() != ingredient.name.casefold():
        return f"{kor_name} ({ingredient.name})"
    return ingredient.name


def _evidence_label(eligibility_tier: str | None, paper_ref: str | None) -> str:
    """근거 종류를 사람이 읽을 수 있는 한국어 라벨로 변환한다.

    query_ingredients_by_effects는 eligibility_tier에 evidence_type을 담아 반환한다.
    pubmed_evidence(논문 근거) > cosing_function(성분 기능 근거).
    """
    if eligibility_tier == "pubmed_evidence":
        try:
            n = int(float(paper_ref)) if paper_ref not in (None, "", "None") else 0
        except (TypeError, ValueError):
            n = 0
        return f"논문 근거 {n}건" if n > 0 else "논문 근거"
    if eligibility_tier == "cosing_function":
        return "성분 기능 근거"
    return "근거 미상"


async def _build_llm_response(
    message: str,
    ingredients: list[IngredientResult],
    products: list[ProductResult],
    system_prompt: str = _SYSTEM_PROMPT,
) -> str:
    sections = [f"사용자 메시지: {message}"]

    # INCI 성분명 → 표시명·근거. 제품 매칭 결과의 영문 이름도 같은 소비자용 표기로 변환한다.
    ingredient_by_name = {ingredient.name: ingredient for ingredient in ingredients}

    if ingredients:
        ingredient_lines = "\n".join(
            f"- {_ingredient_display_name(i)}: {i.claim or '효능 데이터 없음'} "
            f"[{_evidence_label(i.eligibility_tier, i.paper_ref)}]"
            for i in ingredients[:10]
        )
        sections.append(f"관련 성분 데이터:\n{ingredient_lines}")
    else:
        sections.append("(현재 성분 데이터베이스에 해당 고민에 맞는 성분 데이터가 없습니다. 일반적인 추천을 제공해 주세요.)")

    if products:
        def _annotate(names: list[str]) -> str:
            # 제품의 핵심 성분에 근거 라벨을 붙여 모델이 제품 추천을 근거에 묶을 수 있게 한다.
            annotated = []
            for name in names[:3]:
                ingredient = ingredient_by_name.get(name)
                if ingredient:
                    annotated.append(
                        f"{_ingredient_display_name(ingredient)} "
                        f"[{_evidence_label(ingredient.eligibility_tier, ingredient.paper_ref)}]"
                    )
                else:
                    annotated.append(name)
            return ", ".join(annotated)

        product_lines = "\n".join(
            f"- [{p.category}] {p.brand} {p.product_name} (핵심 성분 {p.matched_count}개 포함: {_annotate(p.matched_ingredients)})"
            for p in products
        )
        sections.append(f"추천 제품 데이터:\n{product_lines}")

    user_content = "\n\n".join(sections)

    try:
        client = get_async_llm_client()
        # GPU 동시성 게이트 안에서만 생성 호출 — 가장 무거운 단계라 동시성 제한의 핵심 대상.
        async with llm_slot():
            response = await client.chat.completions.create(
                model=settings.gpu_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=settings.gen_temperature,
                max_tokens=settings.gen_max_tokens,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
        return response.choices[0].message.content or ""
    except LLMOverCapacityError:
        # 동시성 한도 초과는 템플릿 폴백으로 덮지 않고 거절(429)로 전파.
        raise
    except Exception as exc:
        logger.warning("LLM response generation failed: %s", exc)
        if products:
            prod_names = ", ".join(f"{p.brand} {p.product_name}" for p in products[:3])
            return f"피부 고민 분석 결과, 다음 제품들을 추천드립니다: {prod_names}"
        if ingredients:
            names = ", ".join(_ingredient_display_name(i) for i in ingredients[:5])
            return f"피부 고민 분석 결과, 다음 성분들을 추천드립니다: {names}"
        return "죄송합니다. 현재 추천 서비스를 이용할 수 없습니다. 잠시 후 다시 시도해 주세요."
