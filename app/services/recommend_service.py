import json
import logging
import time
import uuid

from app.clients.llm_factory import get_async_llm_client
from app.clients.llm_fallback import extract_with_fallback
from app.clients.llm_gate import LLMOverCapacityError, get_gate_wait_seconds, llm_slot, reset_gate_wait
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
# v5: 출력 길이를 더 줄여(제품 2개·~400자) decode latency를 낮추는 실험용(P3). GEN_PROMPT_NAME로 선택.
_SYSTEM_PROMPT = load_prompt(settings.gen_prompt_name)


def _record_latency(spans: dict[str, float], cache: str) -> None:
    """요청당 latency 트레이스를 메트릭에 관측하고 trace_id 로그로 1줄 남긴다.

    gate_wait는 extract·generate 안에 포함된 '대기' 성분이라 overhead에 더하지 않고 별도 보고.
    """
    for span, sec in spans.items():
        metrics.recommend_latency_span_seconds.labels(span=span).observe(max(sec, 0.0))
    parts = " ".join(f"{k}={v * 1000:.1f}ms" for k, v in spans.items())
    logger.info("latency_trace cache=%s %s", cache, parts)


async def recommend(session_id: str, message: str, gen_prompt_name: str | None = None) -> RecommendResponse:
    turn_id = str(uuid.uuid4())
    reset_gate_wait()
    t_req = time.perf_counter()
    spans: dict[str, float] = {}

    # 캐시 조회(추출 이전) — 히트 시 extract·neo4j·generate를 통째로 건너뛴다 → GPU 비용 0.
    _t = time.perf_counter()
    cached = await recommend_cache.get(message, gen_prompt_name)
    spans["cache_lookup"] = time.perf_counter() - _t
    if cached is not None:
        metrics.recommend_cache_total.labels(result="hit").inc()
        metrics.recommend_requests_total.labels(status="ok").inc()
        spans["total"] = time.perf_counter() - t_req
        spans["overhead"] = spans["total"] - spans["cache_lookup"]
        _record_latency(spans, cache="hit")
        # session_id·turn_id는 요청마다 새로 부여(캐시는 콘텐츠만 보관).
        return RecommendResponse(session_id=session_id, turn_id=turn_id, **cached)
    metrics.recommend_cache_total.labels(result="miss").inc()

    # gen_prompt_name 지정 시 응답 프롬프트 교체(실험용). 미지정이면 프로덕션 기본.
    system_prompt = load_prompt(gen_prompt_name) if gen_prompt_name else _SYSTEM_PROMPT

    try:
        # 같은 키 동시 미스는 리더 1건만 GPU 계산(single-flight, 캐시 스탬피드 제거).
        _t = time.perf_counter()
        async with recommend_cache.single_flight(message, gen_prompt_name):
            spans["flight_wait"] = time.perf_counter() - _t

            # 대기 중 리더가 캐시를 채웠으면 GPU 없이 히트로 처리(coalesced).
            cached = await recommend_cache.get(message, gen_prompt_name)
            if cached is not None:
                metrics.recommend_cache_total.labels(result="coalesced").inc()
                metrics.recommend_requests_total.labels(status="ok").inc()
                spans["total"] = time.perf_counter() - t_req
                spans["overhead"] = spans["total"] - spans["cache_lookup"] - spans["flight_wait"]
                _record_latency(spans, cache="coalesced")
                return RecommendResponse(session_id=session_id, turn_id=turn_id, **cached)

            # 1) 프로필 추출 (LLM, 실패 시 규칙 기반 폴백)
            _t = time.perf_counter()
            profile, extraction_method = await extract_with_fallback(message)
            spans["extract"] = time.perf_counter() - _t
            metrics.profile_extraction_method_total.labels(method=extraction_method).inc()

            # 2) Neo4j 조회 (효능→성분, 성분→제품)
            _t = time.perf_counter()
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
            spans["retrieval"] = time.perf_counter() - _t
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
            _t = time.perf_counter()
            response_text = await _build_llm_response(message, ingredients, products, system_prompt)
            spans["generate"] = time.perf_counter() - _t

            # 같은 문장 재요청이 GPU를 다시 치지 않도록 콘텐츠를 캐시에 저장(session/turn 제외).
            await recommend_cache.set(message, gen_prompt_name, {
                "ingredients": [i.model_dump() for i in ingredients],
                "products": [p.model_dump() for p in products],
                "response_text": response_text,
                "model_used": settings.gpu_model,
            })

        spans["gate_wait"] = get_gate_wait_seconds()
        spans["total"] = time.perf_counter() - t_req
        spans["overhead"] = (spans["total"] - spans["cache_lookup"] - spans["flight_wait"]
                             - spans["extract"] - spans["retrieval"] - spans["generate"])
        _record_latency(spans, cache="miss")

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


def _compose_user_content(
    message: str,
    ingredients: list[IngredientResult],
    products: list[ProductResult],
) -> str:
    """생성 LLM에 줄 user 메시지(사용자 고민 + 성분/제품 데이터)를 조립한다. 스트리밍/비스트리밍 공용."""
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

    return "\n\n".join(sections)


async def _build_llm_response(
    message: str,
    ingredients: list[IngredientResult],
    products: list[ProductResult],
    system_prompt: str = _SYSTEM_PROMPT,
) -> str:
    user_content = _compose_user_content(message, ingredients, products)

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


def _sse(event: str, data: dict) -> str:
    """Server-Sent Events 한 프레임."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def recommend_stream(session_id: str, message: str, gen_prompt_name: str | None = None):
    """SSE 스트리밍 추천: meta(구조 데이터 즉시) → delta(생성 토큰) → done.

    체감 latency(TTFT)를 낮춘다. 생성 단계를 generate_ttft/generate_decode 로 분리 계측해
    "prefill vs decode" 비중을 단건으로 드러낸다.
    """
    turn_id = str(uuid.uuid4())
    reset_gate_wait()
    t_req = time.perf_counter()
    spans: dict[str, float] = {}

    _t = time.perf_counter()
    cached = await recommend_cache.get(message, gen_prompt_name)
    spans["cache_lookup"] = time.perf_counter() - _t
    if cached is not None:
        metrics.recommend_cache_total.labels(result="hit").inc()
        metrics.recommend_requests_total.labels(status="ok").inc()
        yield _sse("meta", {"session_id": session_id, "turn_id": turn_id,
                            "ingredients": cached["ingredients"], "products": cached["products"],
                            "model_used": cached["model_used"]})
        yield _sse("delta", {"text": cached["response_text"]})
        spans["total"] = time.perf_counter() - t_req
        spans["overhead"] = spans["total"] - spans["cache_lookup"]
        _record_latency(spans, cache="hit")
        yield _sse("done", {"finish_reason": "cache"})
        return
    metrics.recommend_cache_total.labels(result="miss").inc()
    system_prompt = load_prompt(gen_prompt_name) if gen_prompt_name else _SYSTEM_PROMPT

    try:
        # 같은 키 동시 미스는 리더 1건만 GPU 계산(single-flight, 캐시 스탬피드 제거).
        _t = time.perf_counter()
        async with recommend_cache.single_flight(message, gen_prompt_name):
            spans["flight_wait"] = time.perf_counter() - _t

            # 대기 중 리더가 캐시를 채웠으면 캐시 히트와 같은 프레임으로 서빙(coalesced).
            cached = await recommend_cache.get(message, gen_prompt_name)
            if cached is not None:
                metrics.recommend_cache_total.labels(result="coalesced").inc()
                metrics.recommend_requests_total.labels(status="ok").inc()
                yield _sse("meta", {"session_id": session_id, "turn_id": turn_id,
                                    "ingredients": cached["ingredients"], "products": cached["products"],
                                    "model_used": cached["model_used"]})
                yield _sse("delta", {"text": cached["response_text"]})
                spans["total"] = time.perf_counter() - t_req
                spans["overhead"] = spans["total"] - spans["cache_lookup"] - spans["flight_wait"]
                _record_latency(spans, cache="coalesced")
                yield _sse("done", {"finish_reason": "cache"})
                return

            _t = time.perf_counter()
            profile, extraction_method = await extract_with_fallback(message)
            spans["extract"] = time.perf_counter() - _t
            metrics.profile_extraction_method_total.labels(method=extraction_method).inc()

            _t = time.perf_counter()
            effect_names = [e.value for e in profile.effects]
            raw_ingredients = await query_ingredients_by_effects(effect_names)
            ingredients = [
                IngredientResult(name=row["name"], kor_name=row.get("kor_name"), claim=row.get("claim"),
                                 eligibility_tier=row.get("eligibility_tier"), paper_ref=row.get("paper_ref"))
                for row in raw_ingredients
            ]
            top_ingredient_names = [i.name for i in ingredients[:10]]
            cats = _appropriate_categories(profile.concerns)
            raw_products = await query_products_by_ingredients(top_ingredient_names, appropriate_categories=cats)
            spans["retrieval"] = time.perf_counter() - _t
            metrics.recommend_ingredients_found.observe(len(ingredients))
            products = [
                ProductResult(product_id=row["product_id"], product_name=row["product_name"], brand=row["brand"],
                              category=row["category"], matched_count=row["matched_count"],
                              matched_ingredients=row["matched_ingredients"])
                for row in raw_products
            ]

            # 구조 데이터는 생성 전에 확보되므로 즉시 전송 → 사용자는 빈 화면 대신 성분·제품을 바로 본다.
            yield _sse("meta", {"session_id": session_id, "turn_id": turn_id,
                                "ingredients": [i.model_dump() for i in ingredients],
                                "products": [p.model_dump() for p in products],
                                "model_used": settings.gpu_model})

            # 생성 스트리밍 (TTFT 측정)
            user_content = _compose_user_content(message, ingredients, products)
            chunks: list[str] = []
            ttft: float | None = None
            gen_start = time.perf_counter()
            async with llm_slot():
                client = get_async_llm_client()
                stream = await client.chat.completions.create(
                    model=settings.gpu_model,
                    messages=[{"role": "system", "content": system_prompt},
                              {"role": "user", "content": user_content}],
                    temperature=settings.gen_temperature,
                    max_tokens=settings.gen_max_tokens,
                    stream=True,
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
                async for chunk in stream:
                    delta = (chunk.choices[0].delta.content or "") if chunk.choices else ""
                    if delta:
                        if ttft is None:
                            ttft = time.perf_counter() - gen_start
                        chunks.append(delta)
                        yield _sse("delta", {"text": delta})
            gen_total = time.perf_counter() - gen_start
            response_text = "".join(chunks)
            spans["generate_ttft"] = ttft if ttft is not None else gen_total
            spans["generate_decode"] = gen_total - spans["generate_ttft"]

            await recommend_cache.set(message, gen_prompt_name, {
                "ingredients": [i.model_dump() for i in ingredients],
                "products": [p.model_dump() for p in products],
                "response_text": response_text,
                "model_used": settings.gpu_model,
            })

        spans["gate_wait"] = get_gate_wait_seconds()
        spans["total"] = time.perf_counter() - t_req
        spans["overhead"] = (spans["total"] - spans["cache_lookup"] - spans["flight_wait"]
                             - spans["extract"] - spans["retrieval"] - gen_total)
        _record_latency(spans, cache="miss")
        metrics.recommend_requests_total.labels(status="ok").inc()
        yield _sse("done", {"finish_reason": "stop"})
    except LLMOverCapacityError:
        # 스트림은 이미 200으로 시작됐을 수 있어 429 대신 error 이벤트로 전달.
        metrics.recommend_requests_total.labels(status="rejected").inc()
        yield _sse("error", {"error_code": "LLM_OVER_CAPACITY", "message": "요청이 많아 잠시 후 다시 시도해 주세요."})
    except Exception as exc:
        # 상세(내부 호스트·경로·라이브러리 정보 가능)는 서버 로그에만. 클라이언트엔 일반 메시지.
        logger.warning("streaming recommend failed: %s", exc)
        metrics.recommend_requests_total.labels(status="error").inc()
        yield _sse("error", {"error_code": "INTERNAL_ERROR",
                             "message": "일시적인 오류가 발생했어요. 잠시 후 다시 시도해 주세요."})
