import json
import logging
import time
import uuid
from pathlib import Path

from app.clients.llm_factory import get_async_llm_client
from app.clients.llm_fallback import extract_with_fallback
from app.clients.llm_gate import LLMOverCapacityError, get_gate_wait_seconds, llm_slot, reset_gate_wait
from app.clients.neo4j_client import (
    query_cautioned_ingredients,
    query_ingredients_by_effects,
    query_products_by_ingredients,
)
from app.core import metrics
from app.core.config import settings
from app.domain.enums import Concern
from app.prompts import load_prompt
from app.repositories import conversation_store, recommend_cache
from app.schemas.recommend import IngredientResult, ProductResult, RecommendResponse
from app.services.product_image_service import build_product_image_url

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


# 사용자가 메시지에서 특정 제품 포맷을 콕 집어 요청하면(이슈 #40 후속) 그 카테고리를 존중한다.
# 그래프 카테고리 값 → 사용자 표현(동의어). '스킨'은 토너의 구어라 토너로 매핑하되 합성어는 아래서 제거.
_CATEGORY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "토너": ("토너", "스킨"),
    "세럼": ("세럼",),
    "앰플": ("앰플",),
    "크림": ("크림",),
    "로션": ("로션",),
    "에센스": ("에센스",),
    "미스트": ("미스트",),
    "올인원": ("올인원",),
    "페이스오일": ("페이스오일", "페이스 오일"),
    "필링스크럽": ("필링스크럽", "스크럽"),
}
# '스킨' 오탐 방지: 제품이 아닌 합성어(스킨케어/스킨타입 등)는 매칭 전에 지운다.
_SKIN_COMPOUNDS = ("스킨케어", "스킨 케어", "스킨타입", "스킨 타입", "스킨톤", "스킨십")


def _requested_categories(message: str) -> set[str]:
    """메시지에서 명시적으로 요청한 제품 카테고리를 추출한다(없으면 빈 집합)."""
    if not message:
        return set()
    m = message
    for w in _SKIN_COMPOUNDS:
        m = m.replace(w, "")
    return {cat for cat, kws in _CATEGORY_SYNONYMS.items() if any(kw in m for kw in kws)}


def _appropriate_categories(concerns: list[Concern],
                            requested: set[str] | None = None) -> list[str]:
    """복수 concern의 교집합 카테고리를 반환한다 (가장 제한적인 조건 적용).

    requested가 있으면(사용자가 포맷을 콕 집음) 그 카테고리를 우선 존중한다 —
    concern 적합 카테고리와 교집합하되, 비면 요청 그대로 따른다(사용자 의도 우선).
    """
    if not concerns:
        base = list(_LEAVE_ON)
    else:
        sets = [set(_CONCERN_CATEGORY_MAP.get(c, _LEAVE_ON)) for c in concerns]
        intersection = sets[0].intersection(*sets[1:])
        base = list(intersection) if intersection else list(_LEAVE_ON)
    if requested:
        narrowed = [c for c in base if c in requested]
        return narrowed or list(requested)
    return base


# 리뷰 재정렬 (부연 신호): 관련도(논문 근거)를 코스 버킷으로 묶어 메인으로 두고,
# 같은 버킷 안에서만 리뷰 강도(리뷰수×평점)로 순서를 조정한다. bucket이 클수록 리뷰 영향↑.
_RELEVANCE_BUCKET = 1.0


def _review_strength(p: dict) -> float:
    return float(p.get("review_count") or 0) * float(p.get("rating") or 0.0)


def _rerank_by_review(products: list[dict]) -> list[dict]:
    """관련도 버킷 내림차순 → 버킷 내 리뷰강도 내림차순. 논문 메인 / 리뷰 부연."""
    def key(p: dict):
        rel = float(p.get("relevance_score") or 0.0)
        bucket = round(rel / _RELEVANCE_BUCKET)
        return (-bucket, -_review_strength(p))
    return sorted(products, key=key)


def _diversify(products: list[dict], per_category: int, total: int) -> list[dict]:
    """랭킹 순서를 유지하되 한 카테고리가 상위를 독식하지 않게 카테고리당 상한을 둔다.
    상한으로 total을 못 채우면 랭킹 순으로 보충한다."""
    out: list[dict] = []
    counts: dict[str, int] = {}
    for p in products:
        c = p.get("category")
        if counts.get(c, 0) >= per_category:
            continue
        out.append(p)
        counts[c] = counts.get(c, 0) + 1
        if len(out) >= total:
            return out
    if len(out) < total:
        chosen = {id(x) for x in out}
        for p in products:
            if id(p) not in chosen:
                out.append(p)
                if len(out) >= total:
                    break
    return out


# 제품 목적 신호 (이슈 #40): 그래프에 product→concern 데이터가 없어 **제품 이름**으로 목적을 추정.
# 이름이 특정 목적을 강하게 시사하는데 그 목적 concern이 요청에 없으면 목적-불일치로 제외
# (예: "기미잡티앰플"이 여드름 요청에 성분만 겹쳐 딸려오던 문제). 보수적으로 색소·노화만 적용.
_PURPOSE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "PIGMENTATION": ("기미", "잡티", "미백", "화이트닝", "브라이트닝", "톤업", "색소"),
    "AGING": ("주름", "링클", "탄력", "퍼밍", "리프팅", "안티에이징", "안티에이징"),
}
_PURPOSE_CONCERNS: dict[str, set[Concern]] = {
    "PIGMENTATION": {Concern.HYPERPIGMENTATION, Concern.DULLNESS, Concern.UNEVEN_SKIN_TONE,
                     Concern.BLEMISHES, Concern.POST_ACNE_MARKS, Concern.DARK_CIRCLES},
    "AGING": {Concern.AGING_SIGNS, Concern.WRINKLES, Concern.LOSS_OF_ELASTICITY, Concern.SAGGING_SKIN},
}


def _purpose_mismatch(product_name: str, concerns: list[Concern]) -> bool:
    """제품 이름이 특정 목적을 강하게 시사하는데 그 목적 concern이 요청에 없으면 True(불일치)."""
    if not product_name:
        return False
    concern_set = set(concerns)
    for purpose, keywords in _PURPOSE_KEYWORDS.items():
        if any(kw in product_name for kw in keywords) and not (concern_set & _PURPOSE_CONCERNS[purpose]):
            return True
    return False


def filter_purpose_mismatch(products: list[dict], concerns: list[Concern]) -> list[dict]:
    """목적-불일치 제품을 걸러낸다. 전부 걸러지면(과필터) 원본 유지(제품 0개 방지)."""
    kept = [p for p in products if not _purpose_mismatch(p.get("product_name", ""), concerns)]
    return kept if kept else products


# ── 제품 목적 필터 (데이터 기반, 이슈 #56) ────────────────────────────────
# LLM 라벨(app/data/product_concerns.json)로 제품의 타겟 고민을 알고, 요청 고민과 **그룹**이
# 겹치는 제품만 남긴다. 성분만 겹치는 목적-불일치 제품(기미앰플→여드름 등)을 근본적으로 컷.
# 라벨 없는 제품은 이름 휴리스틱(filter_purpose_mismatch)으로 폴백.
_CONCERN_GROUP: dict[str, str] = {}
for _grp, _members in {
    "ACNE_OIL": ("ACNE", "COMEDONES", "PORE_CONGESTION", "ENLARGED_PORES", "OILY_SKIN"),
    "SENSITIVITY": ("SENSITIVE_SKIN", "REDNESS", "IRRITATED_SKIN", "ATOPIC_PRONE", "ROSACEA_PRONE"),
    "DRYNESS": ("DRY_SKIN", "DEHYDRATED_SKIN", "FLAKY_SKIN", "ROUGH_TEXTURE", "BARRIER_DAMAGE"),
    "PIGMENTATION": ("HYPERPIGMENTATION", "DULLNESS", "UNEVEN_SKIN_TONE", "BLEMISHES",
                     "POST_ACNE_MARKS", "DARK_CIRCLES"),
    "PROTECTION": ("SUNBURN",),
    "AGING": ("AGING_SIGNS", "WRINKLES", "LOSS_OF_ELASTICITY", "SAGGING_SKIN"),
}.items():
    for _m in _members:
        _CONCERN_GROUP[_m] = _grp

_PRODUCT_CONCERNS_PATH = Path(__file__).resolve().parent.parent / "data" / "product_concerns.json"
try:
    _PRODUCT_CONCERNS: dict[str, list[str]] = json.loads(_PRODUCT_CONCERNS_PATH.read_text())
except Exception:  # 파일 없으면 라벨 필터 비활성(이름 휴리스틱만)
    _PRODUCT_CONCERNS = {}


def _concern_groups(concern_codes) -> set[str]:
    return {_CONCERN_GROUP[c] for c in concern_codes if c in _CONCERN_GROUP}


def filter_by_target_concerns(products: list[dict], concerns: list[Concern]) -> list[dict]:
    """제품의 타겟 고민 그룹이 요청 고민 그룹과 겹치는 제품만 남긴다.
    라벨 없는 제품은 이름 휴리스틱 폴백. 전부 걸러지면 원본 유지(제품 0개 방지)."""
    q_groups = _concern_groups(c.value for c in concerns)
    if not q_groups or not _PRODUCT_CONCERNS:
        return filter_purpose_mismatch(products, concerns)
    kept = []
    for p in products:
        labels = _PRODUCT_CONCERNS.get(str(p.get("product_id")))
        if labels is None:  # 라벨 없음 → 이름 휴리스틱
            if not _purpose_mismatch(p.get("product_name", ""), concerns):
                kept.append(p)
        elif _concern_groups(labels) & q_groups:  # 그룹 겹침
            kept.append(p)
    return kept if kept else products


async def select_products(message: str, concerns: list[Concern],
                          ingredient_scores: list[dict]) -> list[dict]:
    """제품 선정 공통 로직(동기·스트리밍 경로 공유).

    1) 메시지가 카테고리를 콕 집으면 그 카테고리로 제한(요청 존중).
    2) 목적필터로 성분만 겹치는 제품 컷.
    3) 명시 요청이 없으면 카테고리 다양성 보장(한 포맷이 상위 독식 방지).
    """
    requested = _requested_categories(message)
    cats = _appropriate_categories(concerns, requested)
    # 다양성/요청 존중을 위해 후보 풀을 넉넉히 뽑고(랭킹순), 아래서 다듬는다.
    raw = await query_products_by_ingredients(
        ingredient_scores, appropriate_categories=cats,
        min_relevance_ratio=settings.product_min_relevance_ratio,
        min_matched_count=settings.product_min_matched_count,
        limit=30,
    )
    raw = filter_by_target_concerns(raw, concerns)
    raw = _rerank_by_review(raw)  # 관련도 버킷 유지 + 버킷 내 리뷰 우선(부연)
    if requested:  # 요청 카테고리로 이미 좁혀졌으니 랭킹 상위만
        return raw[: settings.product_result_limit]
    return _diversify(raw, per_category=2, total=settings.product_result_limit)


# ── 근거 기반 금기 필터 (CAUTION 엣지, Option A) ──────────────────────────
# 민감성 계열 요청 시, "성분이 자극/홍반을 유발"한다는 논문 근거(CAUTION 엣지)가 있는
# 성분을 후보에서 제거. AFFECTS(효능)와 분리된 안전 오버레이 — 여드름 요청엔 적용 안 함.
_SENSITIVITY_CONCERN_CODES = ["SENSITIVE_SKIN", "REDNESS", "IRRITATED_SKIN",
                              "ATOPIC_PRONE", "ROSACEA_PRONE", "BARRIER_DAMAGE"]


def _is_sensitivity_query(concerns: list[Concern]) -> bool:
    return any(_CONCERN_GROUP.get(c.value) == "SENSITIVITY" for c in concerns)


async def apply_caution_filter(raw_ingredients: list[dict], concerns: list[Concern]) -> list[dict]:
    """민감성 요청 시 CAUTION 엣지가 있는 자극 성분을 컷. 전부 걸러지면 원본 유지."""
    if not _is_sensitivity_query(concerns):
        return raw_ingredients
    cautioned = await query_cautioned_ingredients(_SENSITIVITY_CONCERN_CODES)
    if not cautioned:
        return raw_ingredients
    kept = [r for r in raw_ingredients if r.get("name") not in cautioned]
    return kept if kept else raw_ingredients


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


def _slim_products(products) -> list[dict]:
    """대화 이력용 제품 요약(ProductResult 또는 캐시 dict 둘 다 처리)."""
    out = []
    for p in products or []:
        if isinstance(p, dict):
            out.append({"name": p.get("product_name"), "brand": p.get("brand"),
                        "category": p.get("category"), "rating": p.get("rating")})
        else:
            out.append({"name": p.product_name, "brand": p.brand,
                        "category": p.category, "rating": p.rating})
    return out


async def _store_turn(session_id, message, products, response_text, concerns=None) -> None:
    """이 턴을 대화 이력에 저장(best-effort). 캐시 히트/미스 모든 경로에서 호출 —
    캐시는 글로벌이라 히트여도 이 세션 이력엔 남겨야 후속 질문이 맥락을 본다."""
    await conversation_store.append_turn(
        session_id, user=message, assistant=response_text or "",
        products=_slim_products(products),
        concerns=[c.value for c in concerns] if concerns else [],
    )


# ── 멀티턴: 후속(이전 추천에 대한 질문) 감지 + 처리 (P2) ────────────────────
# 후속 지시어/비교 큐 — 있으면 이전 추천에 대한 질문
_FOLLOWUP_CUES = ("그 중", "그중", "이 중", "이중", "저 중", "저중", "중에서", "비교",
                  "차이", "몇 번", "몇번", "이거", "그거", "저거", "골라", "방금", "위에",
                  "장단점", "뭐가 더", "어떤 게", "어떤게", "어느", "낫", "추천한")
# 새 추천 신호(피부 고민 어휘) — 있으면 새 요청
_CONCERN_CUES = ("여드름", "모공", "블랙헤드", "피지", "지성", "건조", "속건조", "수분",
                 "민감", "붉은", "홍조", "자극", "트러블", "기미", "잡티", "미백", "색소",
                 "칙칙", "주름", "탄력", "노화", "각질", "아토피", "다크서클")


def _heuristic_kind(message: str, history: list[dict]) -> str | None:
    """휴리스틱 분류: 'followup' | 'new' | None(애매 → LLM)."""
    if not history:
        return "new"
    if any(c in message for c in _FOLLOWUP_CUES):
        return "followup"
    if any(c in message for c in _CONCERN_CUES):
        return "new"
    return None


_CLASSIFY_SYSTEM = (
    "You classify a Korean chat turn in a cosmetics recommendation chat. "
    "FOLLOWUP = the message is about the previously recommended products "
    "(compare them, ask details, pick one). "
    "NEW = the message states a new skin concern or asks for a fresh recommendation. "
    "Answer with exactly one word: FOLLOWUP or NEW."
)


async def _llm_classify(message: str, history: list[dict]) -> str:
    """애매한 턴만 vLLM으로 분류. 실패 시 안전하게 'new'."""
    last_products: list[str] = []
    for turn in reversed(history):
        if turn.get("products"):
            last_products = [p.get("name") for p in turn["products"] if p.get("name")]
            break
    prod_str = ", ".join(last_products[:6]) or "(없음)"
    try:
        client = get_async_llm_client()
        resp = await client.chat.completions.create(
            model=settings.gpu_model, temperature=0, max_tokens=8,
            messages=[{"role": "system", "content": _CLASSIFY_SYSTEM},
                      {"role": "user", "content": f"이전 추천 제품: {prod_str}\n새 메시지: {message}"}],
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        out = (resp.choices[0].message.content or "").strip().upper()
        return "followup" if "FOLLOW" in out else "new"
    except Exception as exc:  # noqa: BLE001
        logger.warning("turn classify failed: %s", exc)
        return "new"


async def _is_followup(message: str, history: list[dict]) -> bool:
    kind = _heuristic_kind(message, history)
    if kind is None:  # 애매 → LLM
        kind = await _llm_classify(message, history)
    return kind == "followup"


_FOLLOWUP_SYSTEM = (
    "You are a Korean cosmetics assistant answering a FOLLOW-UP question about products "
    "you already recommended. Use ONLY the previously recommended products and the prior "
    "conversation provided. NEVER invent products, ingredients, studies, or facts. "
    "Answer the user's question (compare the products, explain differences, help them choose) "
    "concisely in Korean within ~600 characters. Base comparisons on the given info "
    "(category, rating, reviews). If the info is insufficient, say so briefly."
)


def _followup_context(history: list[dict]) -> str:
    """이전 추천 제품 + 최근 대화를 후속 생성용 컨텍스트로 조립."""
    lines: list[str] = []
    last = next((t for t in reversed(history) if t.get("products")), None)
    if last and last.get("products"):
        lines.append("이전에 추천한 제품:")
        for p in last["products"]:
            rate = f" (⭐{p['rating']})" if p.get("rating") else ""
            lines.append(f"- [{p.get('category', '')}] {p.get('brand', '')} {p.get('name', '')}{rate}")
    lines.append("\n이전 대화:")
    for turn in history[-3:]:
        if turn.get("user"):
            lines.append(f"사용자: {turn['user']}")
        if turn.get("assistant"):
            lines.append(f"어시스턴트: {turn['assistant'][:200]}")
    return "\n".join(lines)


async def _handle_followup(session_id: str, turn_id: str, message: str,
                           history: list[dict]) -> RecommendResponse:
    """후속 턴: 검색 스킵, 이전 추천 + 대화 맥락으로 답변(비교 등). 캐시 우회."""
    user_content = f"{_followup_context(history)}\n\n현재 질문: {message}"
    try:
        async with llm_slot():
            client = get_async_llm_client()
            resp = await client.chat.completions.create(
                model=settings.gpu_model,
                messages=[{"role": "system", "content": _FOLLOWUP_SYSTEM},
                          {"role": "user", "content": user_content}],
                temperature=settings.gen_temperature, max_tokens=settings.gen_max_tokens,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
        response_text = resp.choices[0].message.content or ""
    except LLMOverCapacityError:
        metrics.recommend_requests_total.labels(status="rejected").inc()
        raise
    except Exception:
        metrics.recommend_requests_total.labels(status="error").inc()
        raise
    metrics.recommend_requests_total.labels(status="ok").inc()
    await _store_turn(session_id, message, [], response_text, None)
    return RecommendResponse(session_id=session_id, turn_id=turn_id, ingredients=[],
                             products=[], response_text=response_text, model_used=settings.gpu_model)


async def recommend(session_id: str, message: str, gen_prompt_name: str | None = None) -> RecommendResponse:
    turn_id = str(uuid.uuid4())
    reset_gate_wait()
    t_req = time.perf_counter()
    spans: dict[str, float] = {}

    # 멀티턴(P2): 이력이 있고 이번 턴이 후속(이전 추천에 대한 질문)이면 검색을 스킵하고
    # 대화 맥락으로 답한다. 세션 의존이라 콘텐츠 캐시는 우회.
    history = await conversation_store.load_recent(session_id)
    if history and await _is_followup(message, history):
        return await _handle_followup(session_id, turn_id, message, history)

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
        await _store_turn(session_id, message, cached.get("products"), cached.get("response_text"))
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
                await _store_turn(session_id, message, cached.get("products"), cached.get("response_text"))
                return RecommendResponse(session_id=session_id, turn_id=turn_id, **cached)

            # 1) 프로필 추출 (LLM, 실패 시 규칙 기반 폴백)
            _t = time.perf_counter()
            profile, extraction_method = await extract_with_fallback(message)
            spans["extract"] = time.perf_counter() - _t
            metrics.profile_extraction_method_total.labels(method=extraction_method).inc()

            # 2) Neo4j 조회 (효능→성분, 성분→제품)
            _t = time.perf_counter()
            effect_names = [e.value for e in profile.effects]
            raw_ingredients = await query_ingredients_by_effects(
                effect_names, min_graph_score=settings.ingredient_min_graph_score)
            raw_ingredients = await apply_caution_filter(raw_ingredients, profile.concerns)

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
            # 성분의 고민-관련도(graph_score)를 제품 랭킹까지 전달 → 성분 개수가 아니라 관련도 가중.
            ingredient_scores = [
                {"name": r["name"], "weight": float(r.get("graph_score") or 1.0)}
                for r in raw_ingredients[:10]
            ]
            raw_products = await select_products(message, profile.concerns, ingredient_scores)
            spans["retrieval"] = time.perf_counter() - _t
            metrics.recommend_ingredients_found.observe(len(ingredients))

            products = [
                ProductResult(
                    product_id=row["product_id"],
                    goods_no=row.get("goods_no"),
                    product_name=row["product_name"],
                    brand=row["brand"],
                    category=row["category"],
                    image_url=build_product_image_url(row.get("goods_no") or row["product_id"]),
                    product_url=row.get("product_url"),
                    matched_count=row["matched_count"],
                    matched_ingredients=row["matched_ingredients"],
                    rating=row.get("rating"),
                    review_count=row.get("review_count"),
                    review_stats=_parse_review_stats(row.get("review_stats")),
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
        await _store_turn(session_id, message, products, response_text, profile.concerns)
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


def _parse_review_stats(raw) -> dict | None:
    """Neo4j에 JSON 문자열로 저장된 review_stats를 dict로. 실패 시 None."""
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _pct(v) -> float:
    try:
        return float(str(v).rstrip("%"))
    except (ValueError, TypeError):
        return 0.0


def _review_note(p: ProductResult) -> str:
    """제품 리뷰를 한 줄 부연으로 요약. 리뷰 없으면 빈 문자열.
    자극도·피부고민 축에서 최상위 항목만 뽑아 간결하게(논문 메인/리뷰 부연)."""
    if not p.rating:
        return ""
    parts = [f"⭐{p.rating}·리뷰 {p.review_count or 0}개"]
    stats = p.review_stats or {}
    for axis in ("자극도", "피부고민"):
        d = stats.get(axis)
        if isinstance(d, dict) and d:
            label, val = max(d.items(), key=lambda kv: _pct(kv[1]))
            if _pct(val) > 0:
                parts.append(f"{label} {val}")
    return " · ".join(parts)


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

        def _product_line(p: ProductResult) -> str:
            base = (f"- [{p.category}] {p.brand} {p.product_name} "
                    f"(핵심 성분 {p.matched_count}개 포함: {_annotate(p.matched_ingredients)})")
            note = _review_note(p)
            return f"{base}\n  · 사용자 리뷰(참고): {note}" if note else base

        product_lines = "\n".join(_product_line(p) for p in products)
        sections.append(
            "추천 제품 데이터:\n" + product_lines +
            "\n\n(성분의 논문 근거가 주된 추천 이유입니다. 사용자 리뷰는 보조 참고로만, "
            "'리뷰에서는 …라는 평가가 많아요' 식으로 가볍게 덧붙이세요. 리뷰를 근거로 단정하지 마세요.)"
        )

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
        await _store_turn(session_id, message, cached.get("products"), cached.get("response_text"))
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
                await _store_turn(session_id, message, cached.get("products"), cached.get("response_text"))
                yield _sse("done", {"finish_reason": "cache"})
                return

            _t = time.perf_counter()
            profile, extraction_method = await extract_with_fallback(message)
            spans["extract"] = time.perf_counter() - _t
            metrics.profile_extraction_method_total.labels(method=extraction_method).inc()

            _t = time.perf_counter()
            effect_names = [e.value for e in profile.effects]
            raw_ingredients = await query_ingredients_by_effects(
                effect_names, min_graph_score=settings.ingredient_min_graph_score)
            raw_ingredients = await apply_caution_filter(raw_ingredients, profile.concerns)
            ingredients = [
                IngredientResult(name=row["name"], kor_name=row.get("kor_name"), claim=row.get("claim"),
                                 eligibility_tier=row.get("eligibility_tier"), paper_ref=row.get("paper_ref"))
                for row in raw_ingredients
            ]
            # 성분의 고민-관련도(graph_score)를 제품 랭킹까지 전달 → 성분 개수가 아니라 관련도 가중.
            ingredient_scores = [
                {"name": r["name"], "weight": float(r.get("graph_score") or 1.0)}
                for r in raw_ingredients[:10]
            ]
            raw_products = await select_products(message, profile.concerns, ingredient_scores)
            spans["retrieval"] = time.perf_counter() - _t
            metrics.recommend_ingredients_found.observe(len(ingredients))
            products = [
                ProductResult(product_id=row["product_id"], goods_no=row.get("goods_no"),
                              product_name=row["product_name"], brand=row["brand"],
                              category=row["category"],
                              image_url=build_product_image_url(row.get("goods_no") or row["product_id"]),
                              product_url=row.get("product_url"),
                              matched_count=row["matched_count"],
                              matched_ingredients=row["matched_ingredients"],
                              rating=row.get("rating"),
                              review_count=row.get("review_count"),
                              review_stats=_parse_review_stats(row.get("review_stats")))
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
        await _store_turn(session_id, message, products, response_text, profile.concerns)
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
