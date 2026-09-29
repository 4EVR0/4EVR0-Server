from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.clients.llm_factory import get_async_llm_client
from app.clients.llm_fallback import extract_with_fallback
from app.clients.llm_gate import LLMOverCapacityError, get_gate_wait_seconds, llm_slot, reset_gate_wait
from app.clients.neo4j_client import (
    query_cautioned_ingredients,
    query_ingredient_kor_names,
    query_ingredients_by_effects,
    query_product_ingredient_inventory,
    query_product_fragrance_evidence,
    query_products_by_ingredients,
)
from app.core import metrics
from app.core.config import settings
from app.domain.enums import Concern, Constraint
from app.domain.user import UserProfile
from app.prompts import load_prompt
from app.repositories import conversation_store, recommend_cache
from app.schemas.recommend import IngredientResult, ProductResult, RecommendResponse
from app.services.product_image_service import build_product_image_url
from app.services.response_integrity import find_response_integrity_issues
from app.services.ingredient_explanations import (
    cards_for_inventory, generation_system_prompt, product_explanations,
    render_product_explanations, supports_concerns,
)
from app.services.verified_ingredient_studies import verified_study_for
from app.services.fragrance_policy import (
    FRAGRANCE_RATIONALE_EXCLUSIONS, fragrance_decision, fragrance_preference,
    merge_fragrance_constraint, mentions_excluded_rationale, parse_evidence,
)

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
    concern 적합 카테고리와 교집합하되, 비면 요청 그대로 따른다.
    단, 로사케아 경향에서는 토너를 허용하지 않는다.
    """
    if not concerns:
        base = list(_LEAVE_ON)
    else:
        sets = [set(_CONCERN_CATEGORY_MAP.get(c, _LEAVE_ON)) for c in concerns]
        intersection = sets[0].intersection(*sets[1:])
        base = list(intersection) if intersection else list(_LEAVE_ON)
    if requested:
        narrowed = [c for c in base if c in requested]
        base = narrowed or list(requested)
    # 로사케아 경향에서는 토너를 권하지 않는 피부과 지침을 사용자 포맷 요청보다 우선한다.
    if Concern.ROSACEA_PRONE in concerns:
        base = [category for category in base if category != "토너"]
    return base


# 리뷰 재정렬 (부연 신호): 관련도(논문 근거)를 코스 버킷으로 묶어 메인으로 두고,
# 같은 버킷 안에서 제품 타겟 고민의 정확 일치·그룹 일치를 먼저 본 다음 리뷰 강도로
# 순서를 조정한다. 리뷰가 많다는 이유로 직접 고민 라벨이 없는 제품이 앞서는 것을 막는다.
_RELEVANCE_BUCKET = 1.0


def _review_strength(p: dict) -> float:
    return float(p.get("review_count") or 0) * float(p.get("rating") or 0.0)


def _rerank_by_review(products: list[dict], concerns: list[Concern] | None = None) -> list[dict]:
    """관련도 버킷 → 정확 고민 → 고민 그룹 → 리뷰강도 순으로 재정렬한다.

    제품 라벨이 없거나 concerns가 없으면 기존처럼 리뷰강도만 부연 신호로 사용한다.
    """
    query_codes = {c.value for c in (concerns or [])}
    query_groups = _concern_groups(query_codes)

    def key(p: dict):
        rel = float(p.get("relevance_score") or 0.0)
        bucket = round(rel / _RELEVANCE_BUCKET)
        labels = set(_PRODUCT_CONCERNS.get(str(p.get("product_id"))) or [])
        exact_matches = len(labels & query_codes)
        group_matches = len(_concern_groups(labels) & query_groups)
        return (-bucket, -exact_matches, -group_matches, -_review_strength(p), -rel)

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
    """목적-불일치 제품을 걸러낸다.

    전부 탈락해도 원본을 되살리지 않는다. 제품이 없는 것이 피부 고민과 맞지 않는
    제품을 내보내는 것보다 안전하다.
    """
    return [p for p in products if not _purpose_mismatch(p.get("product_name", ""), concerns)]


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
    라벨 없는 제품은 이름 휴리스틱 폴백. 전부 걸러지면 빈 결과를 유지한다."""
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
    return kept


def filter_explicit_application_area(products: list[dict], message: str) -> list[dict]:
    """요청 부위와 상품명에 명시된 전용 부위가 충돌하는 후보를 제외한다."""
    query = message.casefold()
    asks_face = any(term in query for term in ("입가", "팔자", "눈가", "이마", "얼굴", "볼주름"))
    asks_neck = any(term in query for term in ("목주름", "목 피부", "목 관리", "넥", "neck"))
    asks_eyes = any(term in query for term in ("눈가", "눈밑", "다크서클"))

    def compatible(product: dict) -> bool:
        name = str(product.get("product_name") or "").casefold()
        face_compatible = any(term in name for term in ("페이스", "얼굴", "face"))
        neck_only = any(term in name for term in ("목주름", "넥 샷", "넥샷", "넥크림", "넥 크림", "neck"))
        eye_only = any(term in name for term in ("아이크림", "아이 크림", "눈가 전용", "아이세럼"))
        if asks_face and not asks_neck and neck_only and not face_compatible:
            return False
        if asks_face and not asks_eyes and eye_only and not face_compatible:
            return False
        return True

    return [product for product in products if compatible(product)]


async def select_products(message: str, concerns: list[Concern],
                          ingredient_scores: list[dict],
                          requested_override: set[str] | None = None,
                          constraints: list[Constraint] | None = None) -> list[dict]:
    """제품 선정 공통 로직(동기·스트리밍 경로 공유).

    1) 메시지가 카테고리를 콕 집으면 그 카테고리로 제한(요청 존중).
    2) 목적필터로 성분만 겹치는 제품 컷.
    3) 명시 요청이 없으면 카테고리 다양성 보장(한 포맷이 상위 독식 방지).
    """
    requested = requested_override if requested_override is not None else _requested_categories(message)
    cats = _appropriate_categories(concerns, requested)
    # 다양성/요청 존중을 위해 후보 풀을 넉넉히 뽑고(랭킹순), 아래서 다듬는다.
    raw = await query_products_by_ingredients(
        ingredient_scores, appropriate_categories=cats,
        min_relevance_ratio=settings.product_min_relevance_ratio,
        min_matched_count=settings.product_min_matched_count,
        limit=30,
        excluded_ingredients=(sorted(_REDNESS_ROSACEA_AVOID_INCI)
                              if _is_redness_rosacea_query(concerns) else []),
    )
    raw = filter_by_target_concerns(raw, concerns)
    raw = filter_explicit_application_area(raw, message)
    raw = await _filter_products_with_constraints(raw, constraints or [])
    raw = _rerank_by_review(raw, concerns)  # 관련도 버킷 유지 + 정확 목적 우선 + 리뷰 부연
    if requested:  # 요청 카테고리로 이미 좁혀졌으니 랭킹 상위만
        return raw[: settings.product_result_limit]
    return _diversify(raw, per_category=2, total=settings.product_result_limit)


# ── 근거 기반 금기 필터 (CAUTION 엣지, Option A) ──────────────────────────
# 민감성 계열 요청 시, "성분이 자극/홍반을 유발"한다는 논문 근거(CAUTION 엣지)가 있는
# 성분을 후보에서 제거. AFFECTS(효능)와 분리된 안전 오버레이 — 여드름 요청엔 적용 안 함.
_SENSITIVITY_CONCERN_CODES = ["SENSITIVE_SKIN", "REDNESS", "IRRITATED_SKIN",
                              "ATOPIC_PRONE", "ROSACEA_PRONE", "BARRIER_DAMAGE"]
_REDNESS_ROSACEA_CONCERNS = {Concern.REDNESS, Concern.ROSACEA_PRONE}
# 성분의 일반적인 항염 라벨만으로 홍조·로사케아 적합성을 주장하지 않는다.
# 향료 성분 및 레티노이드는 이 두 고민에서 긍정 근거/제품 후보로 사용하지 않는다.
# 이는 모든 사용자에게 해롭다는 판정이 아니라, 현재 데이터로 적합성을 확인할 수 없다는 보수적 정책이다.
_REDNESS_ROSACEA_AVOID_INCI = frozenset({"LINALOOL", "FARNESOL", "RETINOL", "RETINAL"})


def _is_redness_rosacea_query(concerns: list[Concern]) -> bool:
    return bool(_REDNESS_ROSACEA_CONCERNS.intersection(concerns))


def _is_sensitivity_query(concerns: list[Concern]) -> bool:
    return any(_CONCERN_GROUP.get(c.value) == "SENSITIVITY" for c in concerns)


async def apply_caution_filter(raw_ingredients: list[dict], concerns: list[Concern]) -> list[dict]:
    """민감성 CAUTION을 적용하고 홍조·로사케아에는 별도 보수적 정책을 적용한다."""
    if not _is_sensitivity_query(concerns):
        return raw_ingredients
    cautioned = await query_cautioned_ingredients(_SENSITIVITY_CONCERN_CODES)
    redness_guard = _is_redness_rosacea_query(concerns)
    excluded = cautioned | FRAGRANCE_RATIONALE_EXCLUSIONS | (_REDNESS_ROSACEA_AVOID_INCI if redness_guard else set())
    if not excluded:
        return raw_ingredients
    kept = [r for r in raw_ingredients if r.get("name") not in excluded]
    # 빈 결과를 피하기 위해 차단된 근거를 되살리지 않는다.
    return kept


logger = logging.getLogger(__name__)

# 프롬프트는 app/prompts/recommend_response*.txt 로 분리(버전 관리).
# v3: 근거 수준(논문 근거 N건 / 성분 기능 근거)을 인용·구분하도록 강화 — 공정 judge eval에서
# grounding 4.05→4.84, overall 4.47→4.77(temp=0, 결정론적).
# v4: 한글 성분명을 우선 사용하고 제품 수·분량을 제한해 간결성과 완결성을 강화.
#     응답 구조도 "성분 설명 → 제품 추천" 순서로 정렬(성분별 효능을 먼저 설명).
# v5: 출력 길이를 더 줄여(제품 2개·~400자) decode latency를 낮추는 실험용(P3). GEN_PROMPT_NAME로 선택.
_SYSTEM_PROMPT = load_prompt(settings.gen_prompt_name)
_HANJA_OUTPUT_PATTERN = re.compile(r"[\u4e00-\u9fff]")
_CONSTRAINT_LABELS = {
    Constraint.FRAGRANCE_FREE: "향료 미포함",
    Constraint.ALCOHOL_FREE: "알코올 미포함",
    Constraint.VEGAN: "비건",
    Constraint.HYPOALLERGENIC: "저자극",
    Constraint.EWG_GREEN: "EWG 그린 등급",
}


def _remove_hanja(text: str) -> tuple[str, bool]:
    """응답에 섞인 CJK 통합 한자를 결정론적으로 제거한다.

    스트리밍에서는 이 함수를 청크별로 적용해 문자가 클라이언트에 전송되기 전에
    차단한다. 반환 bool은 가드 메트릭을 응답당 한 번만 올리기 위한 표시다.
    """
    cleaned = _HANJA_OUTPUT_PATTERN.sub("", text)
    return cleaned, cleaned != text


_PLAIN_LANGUAGE_REPLACEMENTS = {
    "피부 심부": "피부 속",
    "심부 피부": "피부 속",
    "심부": "피부 속",
    "피장벽": "피부 장벽",
    "피분비": "피지 분비",
    "지분 분비": "피지 분비",
    "지분 조절": "피지 조절",
    "지분을": "피지를",
    "지분이": "피지가",
    "지분은": "피지는",
    "지분과": "피지와",
    "지분": "피지",
}


def _normalize_consumer_language(text: str) -> str:
    """모델이 만든 어려운 용어와 반복적으로 관측된 오타를 소비자 표현으로 바꾼다."""
    for source, target in _PLAIN_LANGUAGE_REPLACEMENTS.items():
        text = text.replace(source, target)
    return text


def _product_display_name(brand: str | None, product_name: str | None) -> str:
    """상품명에 브랜드가 이미 포함되어 있으면 한 번만 표시한다."""
    brand = (brand or "").strip()
    product_name = (product_name or "").strip()
    if not brand:
        return product_name
    if not product_name:
        return brand
    if product_name.casefold().startswith(brand.casefold()):
        return product_name
    return f"{brand} {product_name}"


def _normalize_product_names(text: str, products: list[ProductResult]) -> str:
    """`미샤 미샤 ...`처럼 브랜드를 필드와 상품명에서 중복 조립한 출력을 정리한다."""
    for product in products:
        raw = f"{(product.brand or '').strip()} {(product.product_name or '').strip()}".strip()
        display = _product_display_name(product.brand, product.product_name)
        if raw and raw != display:
            text = text.replace(raw, display)
    return text


def _normalize_response_text(
    text: str,
    products: list[ProductResult],
) -> tuple[str, bool]:
    """클라이언트에 보내기 전 한자·어려운 표현·브랜드 중복을 한곳에서 정리한다."""
    text, hanja_removed = _remove_hanja(text)
    text = _normalize_consumer_language(text)
    text = _normalize_product_names(text, products)
    return text, hanja_removed


def _apply_constraint_evidence_guard(
    products: list[dict],
    constraints: list[Constraint],
) -> list[dict]:
    """Allow only product-specific verified claims; missing data is not absence."""
    if any(item != Constraint.FRAGRANCE_FREE for item in constraints):
        metrics.recommend_output_guard_total.labels(kind="unverified_constraints").inc()
        return []
    if Constraint.FRAGRANCE_FREE in constraints:
        kept = []
        for product in products:
            decision = fragrance_decision(product.get("product_id"), product.get("fragrance_evidence"))
            if decision == "verified_claim":
                kept.append(product)
            else:
                metrics.recommend_output_guard_total.labels(kind=f"fragrance_{decision}").inc()
        return kept
    return products


async def _filter_products_with_constraints(products: list[dict], constraints: list[Constraint]) -> list[dict]:
    if not constraints or not products:
        return products
    if any(item != Constraint.FRAGRANCE_FREE for item in constraints):
        return _apply_constraint_evidence_guard(products, constraints)
    # Re-read even for session products: metadata may change after the first turn.
    evidence = await query_product_fragrance_evidence([p["product_id"] for p in products if p.get("product_id")])
    enriched = []
    for product in products:
        data = parse_evidence(evidence.get(product.get("product_id")))
        enriched.append({**product, "fragrance_evidence": data,
                         "fragrance_free_source_url": (data.get("manufacturer_claim") or {}).get("source_url")
                         if isinstance(data.get("manufacturer_claim"), dict) else None})
    return _apply_constraint_evidence_guard(enriched, constraints)


def _build_no_product_response(
    ingredients: list[IngredientResult],
    constraints: list[Constraint],
    concerns: list[Concern] | None = None,
    message: str = "",
) -> str:
    """검색 공백에서 제품명을 만들지 않는 결정론적 응답."""
    metrics.recommend_output_guard_total.labels(kind="no_products").inc()
    if constraints:
        if set(constraints) == {Constraint.FRAGRANCE_FREE}:
            return (
                "향료 미포함 조건을 확인할 수 있는 제품을 찾지 못했어요. "
                "향료 표기가 확인된 제품은 제외하며, 표기에 없더라도 제조사의 무첨가 안내 등 "
                "확인 근거가 부족한 제품은 무향료 제품으로 추천하지 않습니다."
            )
        labels = ", ".join(_CONSTRAINT_LABELS[item] for item in constraints)
        return (
            f"요청하신 조건({labels})을 확인할 수 있는 제품 속성 데이터가 없어 "
            "구체적인 제품명을 추천하지 않겠습니다. 구매 전에 전성분 표시와 인증 정보를 "
            "직접 확인해 주세요."
        )
    if _is_redness_rosacea_query(concerns or []):
        if Concern.ROSACEA_PRONE in concerns and _requested_categories(message) == {"토너"}:
            return (
                "로사케아 경향 피부에는 토너 대신 순한 보습 제품을 고려하는 편이 좋습니다. "
                "요청하신 토너는 추천하지 않겠습니다. 구매 전 향료 표시와 전성분을 확인해 주세요."
            )
        concern = "로사케아 경향" if Concern.ROSACEA_PRONE in concerns else "붉은 기"
        return (
            f"현재 제품 성분 정보만으로는 {concern}에 맞는 후보를 확인하지 못해 "
            "구체적인 제품명을 추천하지 않겠습니다. 구매 전 향료 표시와 전성분을 "
            "확인하고, 증상이 계속되면 피부과에서 상담해 주세요."
        )
    if ingredients:
        names = ", ".join(_ingredient_display_name(item) for item in ingredients[:3])
        return (
            "현재 제공된 제품 데이터에서 조건에 맞는 제품을 찾지 못해 구체적인 "
            f"제품명을 추천하지 않겠습니다. 성분 표시에서 {names}의 포함 여부를 확인해 보세요."
        )
    return (
        "현재 제공된 성분과 제품 데이터에서 조건에 맞는 결과를 찾지 못해 "
        "구체적인 제품명을 추천하지 않겠습니다."
    )


async def _attach_ingredient_explanations(products: list[ProductResult], concerns: list[Concern]) -> None:
    """랭킹 확정 후 CONTAINS를 읽어 설명만 추가. 조회 실패/미확인은 그대로 둔다."""
    for product in products:
        product.ingredient_explanations = []
    if not settings.dictionary_explanations_enabled or not products or not supports_concerns(concerns):
        return
    inventory = await query_product_ingredient_inventory([product.product_id for product in products])
    for product in products:
        product.ingredient_explanations = cards_for_inventory(inventory.get(product.product_id, []), concerns)


def _has_product_grounding_violation(
    response_text: str,
    ingredients: list[IngredientResult],
    products: list[ProductResult],
) -> bool:
    """제품 추천 bullet의 제품/성분 연결이 제공 데이터의 부분집합인지 검사."""
    ingredient_aliases: list[tuple[str, tuple[str, ...]]] = []
    explanations = [card for product in products for card in product_explanations(product)]
    for ingredient in [*ingredients, *explanations]:
        aliases = tuple(
            alias.casefold()
            for alias in (ingredient.name, ingredient.kor_name)
            if alias and len(alias.strip()) >= 2
        )
        ingredient_aliases.append((ingredient.name, aliases))

    in_product_section = False
    saw_product_bullet = False
    for raw_line in response_text.splitlines():
        line = raw_line.strip()
        if line.replace("*", "") == "추천 제품":
            in_product_section = True
            continue
        if not in_product_section or not line.startswith("-"):
            continue
        saw_product_bullet = True
        matched = [product for product in products if product.product_name and product.product_name in line]
        if not matched:
            return True
        allowed = {
            name.casefold()
            for product in matched
            for name in [*product.matched_ingredients, *[card.name for card in product_explanations(product)]]
        }
        # An ingredient-looking token inside the official product name is not a
        # generated ingredient claim. Validate only the explanatory remainder.
        claim_text = line
        for product in matched:
            claim_text = claim_text.replace(product.product_name, "")
        folded_line = claim_text.casefold()
        for inci_name, aliases in ingredient_aliases:
            if any(alias in folded_line for alias in aliases) and inci_name.casefold() not in allowed:
                return True
    return bool(products) and (not in_product_section or not saw_product_bullet)


_CLAIM_BENEFIT_PHRASES = {
    "anti-aging": "탄력·주름 관리",
    "anti-inflammatory": "피부 진정",
    "antimicrobial": "항균 관리",
    "antioxidant": "항산화 관리",
    "barrier repair": "피부 장벽 회복",
    "brightening": "피부 톤 개선",
    "comedolytic": "모공 막힘 관리",
    "depigmenting": "색소 침착 완화",
    "hydrating": "보습",
    "keratolytic": "각질 관리",
    "moisture retention": "수분 유지",
    "photoprotective": "자외선 손상 보호",
    "sebum regulation": "피지 조절",
    "soothing": "피부 진정",
    "wound healing": "피부 회복",
}


def _claim_benefit_phrase(ingredient: IngredientResult | None) -> str | None:
    """그래프의 제한된 효능 라벨을 소비자용 표현으로만 바꾼다."""
    if not ingredient or not ingredient.claim:
        return None
    return _CLAIM_BENEFIT_PHRASES.get(ingredient.claim.strip().casefold())


def _join_korean(items: list[str]) -> str:
    """짧은 한국어 나열을 쉼표와 '및'으로 연결한다."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} 및 {items[-1]}"


def _distinct_evidence_products(products: list[ProductResult]) -> list[ProductResult]:
    """랭킹을 유지하면서 동일한 매칭 성분 근거의 반복 설명을 피한다."""
    selected: list[ProductResult] = []
    seen_signatures: set[frozenset[str]] = set()
    for product in products:
        signature = frozenset(product.matched_ingredients)
        if signature in seen_signatures:
            continue
        selected.append(product)
        seen_signatures.add(signature)
        if len(selected) == 3:
            break
    return selected


def _build_redness_rosacea_response(
    ingredients: list[IngredientResult],
    products: list[ProductResult],
    concerns: list[Concern],
) -> str:
    """검증되지 않은 작용기전이나 제품 효과를 만들지 않는 보수적 후보 설명."""
    ingredient_map = {item.name: item for item in ingredients}
    selected = _distinct_evidence_products(products)
    headline = (
        "말씀하신 로사케아 경향을" if Concern.ROSACEA_PRONE in concerns
        else "말씀하신 붉은 기를"
    )
    lines = [
        "고민 분석",
        f"{headline} 고려해 진정 관련 성분이 확인된 제품을 후보로 골랐습니다.",
        "",
        "성분 설명",
    ]
    seen: set[str] = set()
    for product in selected:
        for name in product.matched_ingredients:
            if name in seen or name not in ingredient_map:
                continue
            item = ingredient_map[name]
            benefit = _claim_benefit_phrase(item) or "피부 관리"
            lines.append(
                f"- {_ingredient_display_name(item)}: 제공된 데이터에서 "
                f"{benefit} 관련 성분으로 분류되어 있습니다."
            )
            seen.add(name)
            if len(seen) == 3:
                break
        if len(seen) == 3:
            break
    lines.extend(["", "추천 제품"])
    for product in selected:
        matched = [
            ingredient_map[name].kor_name or name
            for name in product.matched_ingredients
            if name in ingredient_map
        ][:2]
        reason = _join_korean(matched) or "진정 관련 매칭 성분"
        lines.append(
            f"- [{product.category}] {_product_display_name(product.brand, product.product_name)}: "
            f"진정 관련 성분 {reason}의 포함이 제품 데이터에서 확인돼 비교 후보로 골랐습니다."
        )
    lines.extend([
        "",
        "성분 분류만으로 추천 제품의 실제 개선 효과를 확인할 수는 없습니다. "
        "구매 전 향료 표시와 전체 성분을 확인해 주세요.",
    ])
    return "\n".join(lines)


def _redness_study_match(
    concerns: list[Concern],
    ingredients: list[IngredientResult],
    products: list[ProductResult],
) -> tuple[IngredientResult, ProductResult, dict] | None:
    """질문 고민·성분 효능·제품 함유가 모두 맞는 별도 검토 연구만 사용한다."""
    if (
        not settings.verified_study_response_enabled
        or not settings.redness_verified_study_response_enabled
        or not _is_redness_rosacea_query(concerns)
    ):
        return None
    concern = Concern.ROSACEA_PRONE if Concern.ROSACEA_PRONE in concerns else Concern.REDNESS
    ingredient_map = {item.name: item for item in ingredients[:10]}
    for product in products:
        for name in product.matched_ingredients:
            ingredient = ingredient_map.get(name)
            study = verified_study_for(ingredient) if ingredient else None
            if study and concern.value in study.get("reviewed_concern_codes", []):
                return ingredient, product, study
    return None


def _build_redness_study_response(
    concerns: list[Concern],
    match: tuple[IngredientResult, ProductResult, dict],
) -> str:
    """측정된 연구 결과와 제품의 성분 포함 사실을 분리해 한 후보만 설명한다."""
    ingredient, product, study = match
    concern = (
        "말씀하신 로사케아 경향을" if Concern.ROSACEA_PRONE in concerns
        else "말씀하신 붉은 기를"
    )
    ingredient_name = ingredient.kor_name or ingredient.name
    product_name = _product_display_name(product.brand, product.product_name)
    return "\n".join([
        "고민 분석",
        f"{concern} 고려해 피부 변화를 측정한 연구를 참고했습니다.",
        "",
        "성분 설명",
        f"- {ingredient_name}: {study['brief_summary_ko']} [연구 보기]({study['url']})",
        "",
        "추천 제품",
        f"- [{product.category}] {product_name}: 제품 데이터에서 {ingredient_name} 성분이 확인돼 "
        f"{study['product_bridge_ko']} 비교 후보로 골랐습니다. 다만 {study['limitation_ko']}",
        "",
        "구매 전 향료 표시와 전체 성분을 확인해 주세요.",
    ])


def _verified_study_match(
    message: str,
    concerns: list[Concern],
    ingredients: list[IngredientResult],
    products: list[ProductResult],
) -> tuple[IngredientResult, ProductResult, dict] | None:
    """얼굴 주름 질문에만 검토된 연구와 해당 성분이 매칭된 제품을 연결한다."""
    if not settings.verified_study_response_enabled or Concern.WRINKLES not in concerns:
        return None
    query = message.casefold()
    if not any(term in query for term in ("입가", "팔자", "눈가", "이마", "얼굴", "볼주름")):
        return None
    ingredient_map = {item.name: item for item in ingredients[:10]}
    for product in products:
        for name in product.matched_ingredients:
            ingredient = ingredient_map.get(name)
            study = verified_study_for(ingredient) if ingredient else None
            if study:
                return ingredient, product, study
    return None


def _build_verified_study_response(
    message: str, match: tuple[IngredientResult, ProductResult, dict],
) -> str:
    """별도 검증 연구와 제품 성분 매칭을 구분해 한 후보만 설명한다."""
    ingredient, product, study = match
    area = next((part for part in ("입가", "팔자", "눈가", "이마", "얼굴") if part in message), "얼굴")
    display = ingredient.kor_name or ingredient.name
    product_name = _product_display_name(product.brand, product.product_name)
    limitation = (
        f"이 연구는 {area} 주름을 따로 평가하지 않았고, "
        if area in ("입가", "팔자") else ""
    )
    return "\n".join([
        "고민 분석",
        f"{area} 주름이 신경 쓰이시는군요. 얼굴 주름 개선이 관찰된 "
        f"{display} 연구를 참고해 제품을 살펴봤습니다.",
        "",
        "성분 설명",
        f"- {display}: {study['brief_summary_ko']} "
        f"[연구 보기]({study['url']})",
        "",
        "추천 제품",
        f"- [{product.category}] {product_name}: 제품 성분 정보에서 {display}이 "
        f"확인돼 후보로 골랐습니다. 다만 {limitation}이 제품의 {display} "
        "함량·배합도 확인되지 않아 연구 결과를 이 제품의 효과로 "
        "단정할 수는 없습니다.",
    ])


def _build_grounded_product_response(
    message: str,
    ingredients: list[IngredientResult],
    products: list[ProductResult],
    concerns: list[Concern] | None = None,
) -> str:
    """생성 본문 검증 실패 시 근거 부분집합으로 유용한 응답을 재구성한다.

    생성 모델의 문장을 수선하지 않고 검색 결과의 구조화 필드만 사용한다. 따라서
    제품-성분 연결을 새로 추측하지 않으면서도 고민→효능→제품 이유를 보존한다.
    """
    ingredient_map = {item.name: item for item in ingredients}
    selected_products = _distinct_evidence_products(products)
    highlighted_cards = {}
    for product in selected_products:
        for card in product_explanations(product):
            highlighted_cards.setdefault(card.name, card)
    highlighted: list[IngredientResult] = []
    seen_ingredients: set[str] = set(highlighted_cards)
    # 제품별 첫 번째 성분을 먼저 살펴 한 제품의 성분이 설명 공간을 독점하지 않게 한다.
    for position in range(3):
        for product in selected_products:
            if position >= len(product.matched_ingredients):
                continue
            name = product.matched_ingredients[position]
            item = ingredient_map.get(name)
            if item and item.name not in seen_ingredients:
                highlighted.append(item)
                seen_ingredients.add(item.name)
            if len(highlighted) + len(highlighted_cards) == 4:
                break
        if len(highlighted) + len(highlighted_cards) == 4:
            break
    benefits = ["보습"] if highlighted_cards else []
    for item in highlighted:
        benefit = _claim_benefit_phrase(item)
        if benefit and benefit not in benefits:
            benefits.append(benefit)
    concern_text, _ = _remove_hanja(message)
    concern_text = _normalize_consumer_language(" ".join(concern_text.split()))
    concern_text = concern_text[:80].rstrip(".!?。！？")
    wrinkle_area = next((area for area in ("입가", "눈가", "이마", "목") if area in concern_text), None)
    if Concern.WRINKLES in (concerns or []) and wrinkle_area:
        analysis = f"{wrinkle_area} 주름 관리에 맞는 성분 근거와 제품 사용 부위를 살폈습니다."
    elif benefits:
        concern_prefix = f"말씀하신 “{concern_text}”를 기준으로" if concern_text else "말씀하신 피부 고민을 기준으로"
        analysis = f"{concern_prefix} {_join_korean(benefits)} 근거를 함께 살폈습니다."
    else:
        concern_prefix = f"말씀하신 “{concern_text}”를 기준으로" if concern_text else "말씀하신 피부 고민을 기준으로"
        analysis = f"{concern_prefix} 제품별 매칭 성분에 따라 추천 후보를 정리했습니다."

    lines = [
        "고민 분석",
        analysis,
        "",
        "성분 설명",
    ]
    for card in highlighted_cards.values():
        lines.append(f"- {card.kor_name}: {card.explanation}")
    if highlighted:
        for item in highlighted:
            display = _ingredient_display_name(item)
            benefit = _claim_benefit_phrase(item)
            evidence = _evidence_label(item.eligibility_tier, item.paper_ref)
            if benefit and evidence != "근거 미상":
                lines.append(f"- {display}: 확인된 효능은 {benefit}이며, 근거 수준은 {evidence}입니다.")
            elif benefit:
                lines.append(f"- {display}: 확인된 효능은 {benefit}이지만, 근거 수준은 확인되지 않습니다.")
            else:
                lines.append(f"- {display}: 제품 데이터의 매칭 성분이며, 효능 근거는 확인되지 않습니다.")
    elif not highlighted_cards:
        lines.append("- 제공된 제품의 매칭 성분만 사용했습니다.")

    lines.extend(["", "추천 제품"])
    for product in selected_products:
        cards = product_explanations(product)
        if cards:
            # 완제품 효과를 보장하지 않고 확인된 함유→일반 역할→고민으로 연결한다.
            card = cards[0]
            product_name = _product_display_name(product.brand, product.product_name)
            lines.append(f"- [{product.category}] {product_name}: {card.kor_name} 함유가 확인됩니다. {card.explanation}")
            continue
        reasons_by_benefit: dict[str, list[str]] = {}
        reason_ingredient_count = 0
        matched_names: list[str] = []
        for name in product.matched_ingredients[:3]:
            item = ingredient_map.get(name)
            short_name = (item.kor_name or item.name) if item else name
            matched_names.append(short_name)
            benefit = _claim_benefit_phrase(item)
            if benefit and reason_ingredient_count < 2:
                reasons_by_benefit.setdefault(benefit, []).append(short_name)
                reason_ingredient_count += 1
        reasons = [
            f"{_join_korean(names)}의 {benefit}"
            for benefit, names in reasons_by_benefit.items()
        ]
        product_name = _product_display_name(product.brand, product.product_name)
        if reasons:
            lines.append(
                f"- [{product.category}] {product_name}: 추천 이유는 "
                f"{_join_korean(reasons)} 근거가 제품 매칭 성분에서 확인되기 때문입니다."
            )
        else:
            matched_text = _join_korean(matched_names) or "제공된 매칭 성분"
            lines.append(
                f"- [{product.category}] {product_name}: 제품 데이터에서 {matched_text}만 "
                "확인되며, 구체적인 효능 근거는 확인되지 않습니다."
            )
    return "\n".join(lines)


def _record_latency(spans: dict[str, float], cache: str) -> None:
    """요청당 latency 트레이스를 메트릭에 관측하고 trace_id 로그로 1줄 남긴다.

    gate_wait는 extract·generate 안에 포함된 '대기' 성분이라 overhead에 더하지 않고 별도 보고.
    """
    for span, sec in spans.items():
        metrics.recommend_latency_span_seconds.labels(span=span).observe(max(sec, 0.0))
    parts = " ".join(f"{k}={v * 1000:.1f}ms" for k, v in spans.items())
    logger.info("latency_trace cache=%s %s", cache, parts)


def _refresh_cached_images(cached: dict | None) -> dict | None:
    """캐시된 products의 presigned image_url을 goods_no로 재생성.
    presigned URL은 1h 만료인데 캐시 TTL은 24h이라, 만료된 URL이 서빙되면 이미지가 깨진다.
    → 서빙 시점에 항상 새 URL로 갱신(만료 무관)."""
    if not cached:
        return cached
    for p in cached.get("products") or []:
        gid = p.get("goods_no") or p.get("product_id")
        if gid:
            p["image_url"] = build_product_image_url(gid)
    return cached


def _slim_products(products) -> list[dict]:
    """대화 이력용 제품 요약(ProductResult 또는 캐시 dict 둘 다 처리).
    후속 턴에서 카드를 복원할 수 있게 표시 필드를 담는다. image_url은 presigned라
    만료되므로 저장하지 않고 goods_no로 후속 시점에 재생성한다."""
    def g(p, attr, key):
        return p.get(key) if isinstance(p, dict) else getattr(p, attr, None)
    out = []
    for p in products or []:
        out.append({
            "product_id": g(p, "product_id", "product_id"),
            "name": g(p, "product_name", "product_name"),
            "brand": g(p, "brand", "brand"),
            "category": g(p, "category", "category"),
            "goods_no": g(p, "goods_no", "goods_no"),
            "product_url": g(p, "product_url", "product_url"),
            "rating": g(p, "rating", "rating"),
            "review_count": g(p, "review_count", "review_count"),
            "review_stats": g(p, "review_stats", "review_stats"),
            "matched_count": g(p, "matched_count", "matched_count"),
            "matched_ingredients": g(p, "matched_ingredients", "matched_ingredients") or [],
            "fragrance_free_source_url": g(p, "fragrance_free_source_url", "fragrance_free_source_url"),
        })
    return out


def _reconstruct_products(slim: list[dict]) -> list[ProductResult]:
    """이력의 slim 제품을 카드 표시용 ProductResult로 복원. image_url은 goods_no로 재생성."""
    out = []
    for p in slim or []:
        gid = p.get("goods_no") or p.get("product_id")
        out.append(ProductResult(
            product_id=p.get("product_id") or "",
            goods_no=p.get("goods_no"),
            product_name=p.get("name") or "",
            brand=p.get("brand") or "",
            category=p.get("category") or "",
            image_url=build_product_image_url(gid) if gid else None,
            product_url=p.get("product_url"),
            matched_count=p.get("matched_count") or 0,
            matched_ingredients=p.get("matched_ingredients") or [],
            rating=p.get("rating"),
            review_count=p.get("review_count"),
            review_stats=p.get("review_stats"),
            fragrance_free_source_url=p.get("fragrance_free_source_url"),
        ))
    return out


def _active_recommendation(profile: UserProfile, base_message: str,
                           products, turn_id: str, ingredients=None) -> dict:
    slim = _slim_products(products)
    return {
        "profile": profile.model_dump(mode="json"),
        "base_message": base_message,
        "ingredients": [item.model_dump() if isinstance(item, IngredientResult) else item
                        for item in (ingredients or [])],
        "source_products": slim,
        "visible_products": slim,
        "turn_id": turn_id,
    }


def _cached_active(cached: dict, message: str, turn_id: str,
                   context: ContextualSearch | None = None) -> dict | None:
    try:
        profile = UserProfile.model_validate(cached["_profile"])
    except (KeyError, ValueError, TypeError):
        return None
    return _active_recommendation(
        profile, context.base_message if context else message,
        cached.get("products"), turn_id, cached.get("ingredients"),
    )


def _contextual_messages(message: str, context: ContextualSearch | None) -> tuple[str, str]:
    """Separate the cache identity and generation prompt from the user's utterance."""
    if context is None:
        return message, message
    cache_message = (
        f"contextual-search|{context.profile.model_dump_json()}|"
        f"{','.join(sorted(context.categories))}|{message}"
    )
    generation_message = (
        f"이전 피부 고민(여기에 있던 제품 제형 요청은 무시): {context.base_message}\n"
        f"현재 요청(제형은 이 요청을 우선): {message}\n"
        f"검색할 제품 유형: {', '.join(sorted(context.categories))}"
    )
    return cache_message, generation_message


async def _store_turn(session_id, message, products, response_text, concerns=None,
                      active_state: dict | None = None) -> None:
    """이 턴을 대화 이력에 저장(best-effort). 캐시 히트/미스 모든 경로에서 호출 —
    캐시는 글로벌이라 히트여도 이 세션 이력엔 남겨야 후속 질문이 맥락을 본다."""
    await conversation_store.append_turn(
        session_id, user=message, assistant=response_text or "",
        products=_slim_products(products),
        concerns=[c.value for c in concerns] if concerns else [],
    )
    if active_state is not None:
        await conversation_store.save_active(session_id, active_state)


# ── 멀티턴: 후속(이전 추천에 대한 질문) 감지 + 처리 (P2) ────────────────────
# 이력이 없는 요청을 막을 때는 이전 답변을 *지칭하는* 표현만 사용한다.
# "톤 차이", "제품 중에서", "골라주세요" 같은 일반 표현은 첫 질문에도 등장한다.
_DEICTIC_SET_REF = re.compile(
    r"(?:이|그|저)\s+중(?:에서|에|은|엔)?(?![가-힣])"
    r"|(?:이|그|저)중(?:에서|에|은|엔)(?![가-힣])"
)
_PRIOR_REPLY_REF = re.compile(
    r"(?:이전|앞서|방금|아까|직전|위에|위에서)\s*(?:추천|보여|말|언급|나온|제품)"
    r"|추천해\s*준|추천해\s*주신"
    # 문장 첫머리의 '추천한 제품들'은 직전 답변을 가리킨다. 중간에 나오는
    # '친구가 추천한 제품들' 같은 외부 추천은 이전 답변으로 오인하지 않는다.
    r"|^추천한\s*제품(?:들)?(?=$|[\s은는이가을를의,.!?])",
    re.IGNORECASE,
)
_PREVIOUS_RECOMMENDATION_REF = re.compile(
    _DEICTIC_SET_REF.pattern
    + r"|(?:이|그|저)\s*(?:거|것|제품)(?:들)?"
    + r"|(?:이것|그것|저것)들?"
    + r"|" + _PRIOR_REPLY_REF.pattern,
    re.IGNORECASE,
)
# 새 추천 신호(피부 고민 어휘) — 있으면 새 요청
_CONCERN_CUES = ("여드름", "모공", "블랙헤드", "피지", "지성", "건조", "속건조", "수분",
                 "민감", "붉은", "홍조", "자극", "트러블", "기미", "잡티", "미백", "색소",
                 "칙칙", "주름", "탄력", "노화", "각질", "아토피", "다크서클", "진정")

_RESEARCH_CUE = re.compile(
    r"(?:다시|새로|새롭게|재검색).*(?:추천|찾|검색)"
    r"|(?:추천|찾|검색).*(?:다시|새로|새롭게)"
)
_RESTORE_ALL_CUE = re.compile(r"(?:전체|모두|전부).*(?:보여|추천)|(?:보여|추천).*(?:전체|모두|전부)")
_SEARCH_CONFIRMATION = re.compile(r"^(?:응|네|예|좋아|그래|그럼|부탁해)[\s,!.]*(?:새로|다시)?[\s,!.]*(?:찾아|검색해|추천해)(?:줘|주세요)?[\s!.]*$")
_SAME_CONCERN_CUE = re.compile(r"(?:같은|기존|이전)\s*(?:피부\s*)?고민")


@dataclass(frozen=True)
class ContextualSearch:
    """Re-run retrieval with the saved concern profile and a new product category."""

    profile: UserProfile
    base_message: str
    categories: set[str]


def _has_followup_cue(message: str) -> bool:
    return bool(_PREVIOUS_RECOMMENDATION_REF.search(message))


def _has_missing_history_cue(message: str) -> bool:
    """Only an explicit prior reply/set can justify the expired-history answer."""
    return bool(_PRIOR_REPLY_REF.search(message) or _DEICTIC_SET_REF.search(message))


def _is_product_comparison_request(message: str) -> bool:
    return bool(re.search(r"비교|공통점|차이점", message))


# 지시적(deictic) 후속 — "이 중에서/그 중에서/이것들 중"처럼 **직전 추천 세트**를 콕 집어
# 좁히는 요청. 이런 요청에 옛 턴의 대화 맥락(다른 고민)이 섞이면 필터가 오염된다
# ("건조" 추천 뒤 "이 중에서 지성용" → 옛 '건조' 맥락이 새면 안 됨). 제품 후보는 이미
# 직전 턴만 보므로, 생성 컨텍스트의 '이전 대화'도 직전 턴 하나로 한정한다.
def _is_deictic(message: str) -> bool:
    return bool(_DEICTIC_SET_REF.search(message) or
                re.search(r"(?:이것|그것|저것)들", message))


def _heuristic_kind(message: str, history: list[dict]) -> str | None:
    """휴리스틱 분류: 'followup' | 'new' | None(애매 → LLM)."""
    if not history:
        return "new"
    if _has_followup_cue(message):
        return "followup"
    if _is_product_comparison_request(message) and any(
        _comparison_product_indexes(message, turn.get("products") or []) for turn in history[-3:]
    ):
        return "followup"
    if any(c in message for c in _CONCERN_CUES):
        return "new"
    if _is_product_comparison_request(message) and any(t.get("products") for t in history):
        return "followup"
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
    "you already recommended. Use ONLY the currently selected products and the prior "
    "conversation provided. NEVER invent products, ingredients, studies, or facts.\n"
    "Write in Hangul Korean ONLY. Do NOT use any Chinese characters (Hanja/漢字); use pure Hangul.\n"
    "Organize the answer with short SECTION HEADINGS, each on its own line, chosen from: "
    "추천 / 비교 / 이유 / 사용 팁 (use only the relevant ones). Under each heading, concise sentences.\n"
    "Formatting for readability:\n"
    "- Wrap every PRODUCT name in **...** (double asterisks).\n"
    "- For INGREDIENTS, use the EXACT '한글명 (INCI)' form given in the context, and wrap ONLY the "
    "Korean part in *...* (single asterisks). Example: *트라넥사믹애씨드* (TRANEXAMIC ACID). "
    "Never write an ingredient in English only.\n"
    "When you explain WHY a product has a property (heavy, rich, moisturizing, gentle, exfoliating, etc.), "
    "cite the concrete reason from the given data — the responsible ingredient(s) and/or the "
    "formulation/category (e.g., cream vs serum). Do NOT fabricate reasons.\n"
    "Keep the whole answer within ~700 Korean characters.\n"
    "If you recommend or rank specific products as better choices, END your answer with a separate "
    "final line EXACTLY: [추천순위] 제품명1 | 제품명2 (most to least recommended). "
    "Include only products you actually recommend. If you are NOT ranking, OMIT this line."
)


def _fmt_ingredient(inci: str, ing_kor: dict[str, str]) -> str:
    """'한글 (INCI)' 표기. 한글명 없으면 INCI만."""
    kor = ing_kor.get(inci)
    return f"{kor} ({inci})" if kor else inci


def _followup_context(history: list[dict], ing_kor: dict[str, str], deictic: bool = False,
                      products_override: list[dict] | None = None) -> str:
    """이전 추천 제품 + 최근 대화를 후속 생성용 컨텍스트로 조립. 성분은 '한글 (INCI)'.

    deictic=True("이 중에서" 류)면 '이전 대화'를 직전 추천 턴 하나로 한정해, 옛 고민이
    필터에 새는 것을 막는다(제품 후보는 항상 직전 턴만 본다). 아니면 최근 3턴을 맥락으로 준다.
    """
    lines: list[str] = []
    last = next((t for t in reversed(history) if t.get("products")), None)
    if products_override is not None:
        last = {**(last or {}), "products": products_override}
    if last and last.get("products"):
        lines.append("이전에 추천한 제품:")
        for p in last["products"]:
            rate = f" ⭐{p['rating']}" if p.get("rating") else ""
            ings = ", ".join(_fmt_ingredient(i, ing_kor) for i in (p.get("matched_ingredients") or [])[:4])
            ing_str = f" · 핵심성분: {ings}" if ings else ""
            # product_name에 이미 브랜드가 포함된 경우가 많아 brand를 앞에 안 붙인다(중복 방지).
            name = p.get("name") or ""
            brand = p.get("brand") or ""
            display = name if (brand and brand in name) else f"{brand} {name}".strip()
            lines.append(f"- [{p.get('category', '')}] {display}{rate}{ing_str}")
    lines.append("\n이전 대화:")
    # 지시적 요청은 직전 추천 턴 하나만(옛 고민 차단), 아니면 최근 3턴.
    recent = [last] if (deictic and last) else history[-3:]
    for turn in recent:
        if turn.get("user"):
            lines.append(f"사용자: {turn['user']}")
        # A narrowed set must not leak an excluded product name via the old reply.
        if products_override is None and turn.get("assistant"):
            lines.append(f"어시스턴트: {turn['assistant'][:200]}")
    return "\n".join(lines)


_RANK_RE = re.compile(r"^\s*\[?\s*추천순위\s*\]?\s*[:：]?\s*(.+)$")


def _extract_ranking(response_text: str) -> tuple[str, list[str]]:
    """응답에서 '[추천순위] a | b' 마커 줄을 뽑아 (마커 제거된 텍스트, [이름...]) 반환.
    마커 없으면 (원문, [])."""
    kept, ranking = [], []
    for line in response_text.split("\n"):
        m = _RANK_RE.match(line)
        if m and "추천순위" in line:
            ranking = [n.strip() for n in m.group(1).split("|") if n.strip()]
        else:
            kept.append(line)
    return "\n".join(kept).strip(), ranking


def _mention_order(products: list[ProductResult], text: str) -> list[str]:
    """응답 텍스트에서 각 제품이 처음 언급된 위치 순으로 제품명 리스트를 만든다.
    LLM은 보통 추천 섹션에서 최선을 먼저 말하므로, 마커가 없을 때의 정렬 폴백."""
    pos = []
    for p in products:
        name = p.product_name or ""
        # 제품명 전체 또는 앞부분(브랜드+첫 토큰)으로 첫 등장 위치 탐색
        idx = text.find(name)
        if idx < 0 and name:
            head = " ".join(name.split()[:2])  # 앞 두 토큰
            idx = text.find(head) if head else -1
        pos.append((idx if idx >= 0 else 10**9, name))
    pos.sort()
    return [n for _, n in pos if _ < 10**9]


def _reorder_by_ranking(products: list[ProductResult], ranking: list[str],
                        response_text: str = "") -> list[ProductResult]:
    """최종 추천 순서로 카드를 재정렬.
    우선순위: (1) [추천순위] 마커 → (2) 응답 내 첫 언급 순 → (3) 원래 순서."""
    if not ranking and response_text:
        ranking = _mention_order(products, response_text)  # 폴백: 언급 순
    if not ranking:
        return products

    def rank_of(p: ProductResult) -> int:
        name = p.product_name or ""
        for i, rn in enumerate(ranking):
            if rn and (rn in name or name in rn):  # 이름 부분매칭
                return i
        return len(ranking) + 1  # 랭킹에 없는 제품은 뒤로(원래 순서 유지)

    return sorted(products, key=rank_of)  # stable — 미매칭끼리는 원래 순서


_USAGE_ORDER_STAGES = (
    ("기초(스킨·토너)", frozenset({"스킨", "토너"})),
    ("앰플", frozenset({"앰플"})),
    ("세럼", frozenset({"세럼"})),
    ("크림", frozenset({"크림"})),
)


def _is_usage_order_request(message: str) -> bool:
    return "순서" in message or "루틴" in message


def _usage_order_choices(
    products: list[ProductResult], ingredients: list[IngredientResult],
) -> list[tuple[str, ProductResult, IngredientResult | None]]:
    """Use the original evidence rank, then the previous product rank, per stage."""
    ranked = {item.name.casefold(): (index, item) for index, item in enumerate(ingredients)}
    choices = []
    for label, categories in _USAGE_ORDER_STAGES:
        candidates = [(index, product) for index, product in enumerate(products)
                      if product.category.strip() in categories and product.matched_ingredients]
        if not candidates:
            continue

        def rank(candidate):
            index, product = candidate
            evidence_rank = min(
                (ranked[name.casefold()][0] for name in product.matched_ingredients
                 if name.casefold() in ranked),
                default=len(ingredients),
            )
            return evidence_rank, index

        _, chosen = min(candidates, key=rank)
        matched = [ranked[name.casefold()] for name in chosen.matched_ingredients
                   if name.casefold() in ranked]
        best = min(matched, key=lambda row: row[0])[1] if matched else None
        choices.append((label, chosen, best))
    return choices


def _build_usage_order_response(
    choices: list[tuple[str, ProductResult, IngredientResult | None]],
    base_message: str = "",
) -> str:
    if not choices:
        return "앞서 추천한 제품 중 사용 순서를 정리할 제형의 제품이 없어요."
    concern_text, _ = _remove_hanja(base_message)
    concern_text = _normalize_consumer_language(" ".join(concern_text.split()))[:80].rstrip(".!?。！？")
    if "에 맞는" in concern_text:
        concern = f"앞서 말씀하신 {concern_text.split('에 맞는', 1)[0]} 고민에 맞춰"
    elif concern_text:
        concern = f"앞선 요청(“{concern_text}”)을 기준으로"
    else:
        concern = "앞서 말씀하신 피부 고민에 맞춰"
    lines = [
        f"{concern}, 각 제형에서 관련 근거 성분이 가장 우선인 제품을 하나씩 골랐어요.",
    ]
    shared = choices[0][2] if len(choices) > 1 else None
    if shared and not all(item and item.name == shared.name for _, _, item in choices):
        shared = None
    if shared:
        name = shared.kor_name or shared.name
        benefit = _claim_benefit_phrase(shared)
        source = ("논문 기반 성분 근거" if shared.eligibility_tier == "pubmed_evidence"
                  else "성분 기능 데이터" if shared.eligibility_tier == "cosing_function"
                  else "제공된 성분 근거")
        if benefit:
            lines.append(
                f"선택 이유: 아래 {len(choices)}개 제품 모두 {name} 성분이 매칭되며, "
                f"이 성분은 {source}에서 {benefit} 관련으로 분류됩니다."
            )
        else:
            lines.append(f"선택 이유: 아래 {len(choices)}개 제품 모두 {name} 성분이 매칭됩니다.")
    lines.extend(["", "추천 제품"])
    for index, (stage, product, ingredient) in enumerate(choices, start=1):
        if ingredient:
            name = ingredient.kor_name or ingredient.name
            benefit = _claim_benefit_phrase(ingredient)
            source = ("논문 기반 성분 근거" if ingredient.eligibility_tier == "pubmed_evidence"
                      else "성분 기능 데이터" if ingredient.eligibility_tier == "cosing_function"
                      else "제공된 성분 근거")
            reason = (f"매칭 성분: {name}." if shared else
                      f"매칭 성분: {name}. {source}에서 {benefit} 관련으로 분류됩니다."
                      if benefit else f"매칭 성분: {name}. 앞선 고민과의 매칭이 제품 데이터에서 확인됩니다.")
        elif product.matched_ingredients:
            reason = (f"매칭 성분: {product.matched_ingredients[0]}. "
                      "구체적인 작용 근거는 저장된 정보에서 확인되지 않습니다.")
        else:
            continue
        lines.append(f"- {index}. {stage}: **{product.product_name}** — {reason}")
    lines.extend(["", "이 순서는 제품 제형에 따른 안내입니다. 사용 횟수와 시점은 각 제품 안내를 확인해 주세요."])
    return "\n".join(lines)


def _comparison_product_indexes(message: str, products: list[dict]) -> list[int]:
    """Resolve explicit product names, unique brands and numbered cards conservatively."""
    normalized = " ".join(message.casefold().split())
    indexes: set[int] = set()
    brand_counts: dict[str, int] = {}
    for product in products:
        brand = str(product.get("brand") or "").strip().casefold()
        if brand:
            brand_counts[brand] = brand_counts.get(brand, 0) + 1
    for index, product in enumerate(products):
        name = " ".join(str(product.get("name") or "").casefold().split())
        brand = str(product.get("brand") or "").strip().casefold()
        if name and name in normalized:
            indexes.add(index)
        elif len(brand) >= 2 and brand_counts[brand] == 1 and brand in normalized:
            indexes.add(index)
    numbers = re.findall(r"(?<!\d)([1-9])\s*(?:번|번째)(?!\d)", normalized)
    numbers += re.findall(r"제품\s*([1-9])(?!\d)", normalized)
    for number in numbers:
        index = int(number) - 1
        if index < len(products):
            indexes.add(index)
    for ordinal, index in (("첫", 0), ("두", 1), ("세", 2)):
        if index < len(products) and re.search(fr"{ordinal}\s*(?:번|번째)", normalized):
            indexes.add(index)
    return sorted(indexes)


def _select_comparison_products(message: str, visible: list[dict]) -> tuple[list[dict], str | None]:
    """Do not silently choose a subset from a longer recommendation list."""
    indexes = _comparison_product_indexes(message, visible)
    if indexes:
        selected = [visible[i] for i in indexes]
    else:
        requested = _requested_categories(message)
        candidates = [p for p in visible if p.get("category") in requested] if requested else visible
        count = re.search(r"(?:상위|첫)\s*([23])\s*개", message)
        selected = candidates[:int(count.group(1))] if count else candidates
    if len(selected) < 2:
        return [], "비교할 제품을 2~3개 지정해 주세요. 앞서 보여드린 제품명이나 번호로 알려주시면 됩니다."
    if len(selected) > 3:
        return [], "비교할 제품이 3개보다 많아요. 제품명이나 번호로 2~3개를 골라 주세요."
    if any(not p.get("product_id") for p in selected):
        return [], "선택한 제품의 식별 정보가 없어 성분을 확인할 수 없습니다. 다른 제품을 골라 주세요."
    return selected, None


def _ingredient_key(name: str) -> str:
    return " ".join(name.strip().casefold().split())


def _comparison_markdown_cell(value: str) -> str:
    return " ".join(str(value).replace("|", "·").split())


def _comparison_ingredient_label(row: dict) -> str:
    inci = str(row.get("name") or "").strip()
    kor = str(row.get("kor_name") or "").strip()
    return f"{kor} ({inci})" if kor and kor.casefold() != inci.casefold() else inci


def _build_ingredient_comparison(
    products: list[ProductResult],
    inventory: dict[str, list[dict]],
    evidence: list[IngredientResult],
) -> tuple[str, list[IngredientResult]] | None:
    """Compare only graph-confirmed INCI edges; absence of an edge is not absence in formula."""
    per_product: list[dict[str, str]] = []
    for product in products:
        rows = inventory.get(product.product_id) or []
        names = {
            _ingredient_key(str(row.get("name") or "")): _comparison_ingredient_label(row)
            for row in rows if row.get("name")
        }
        if not names:
            return None
        per_product.append(names)
    sets = [set(row) for row in per_product]
    union = set.union(*sets)
    common = set.intersection(*sets)
    evidence_by_name = {_ingredient_key(item.name): item for item in evidence}
    ordered: list[str] = []

    def add(keys):
        for key in keys:
            if key in union and key not in ordered and len(ordered) < 12:
                ordered.append(key)

    add(_ingredient_key(item.name) for item in evidence[:6])
    add(sorted(common)[:2])
    for index, own in enumerate(sets):
        unique = own - set.union(*(other for pos, other in enumerate(sets) if pos != index))
        add(sorted(unique)[:1])
    add(sorted(union))

    labels = {key: next((row[key] for row in per_product if key in row), key) for key in ordered}
    supported = [evidence_by_name[key] for key in ordered if key in evidence_by_name]
    lines = ["비교"]
    for index, product in enumerate(products, 1):
        lines.append(f"제품 {index}: **{product.product_name}** (확인된 성분 {len(sets[index - 1])}개)")
    lines.extend([
        "",
        "| 성분 | " + " | ".join(f"제품 {i}" for i in range(1, len(products) + 1)) + " | 고민 관련 근거 |",
        "| --- | " + " | ".join("---" for _ in products) + " | --- |",
    ])
    for key in ordered:
        item = evidence_by_name.get(key)
        benefit = _claim_benefit_phrase(item)
        tier = item.eligibility_tier if item else None
        source_label = ("논문 기반 성분 근거" if tier == "pubmed_evidence"
                        else "성분 기능 데이터" if tier == "cosing_function"
                        else "성분 근거")
        source = f"{benefit} · {source_label}" if benefit else "—"
        marks = ["확인" if key in names else "—" for names in per_product]
        lines.append("| " + " | ".join([_comparison_markdown_cell(labels[key]), *marks, source]) + " |")
    common_names = ", ".join(_comparison_markdown_cell(labels[key]) for key in ordered if key in common) or "표시한 성분 중 없음"
    lines.extend([
        "",
        f"공통점: 표시한 성분 중 모든 제품에서 확인된 성분은 {common_names}입니다.",
    ])
    for index, own in enumerate(sets, 1):
        other = set.union(*(row for pos, row in enumerate(sets) if pos != index - 1))
        unique = [_comparison_markdown_cell(labels[key]) for key in ordered if key in own - other]
        if unique:
            lines.append(f"차이점: 제품 {index}에서만 확인된 표시 성분은 {', '.join(unique)}입니다.")
    lines.append(
        "표는 그래프에서 INCI로 매핑된 성분 중 최대 12개만 보여줍니다. "
        "‘—’는 이 데이터에서 확인되지 않았다는 뜻이며, 실제 제품에 없다는 뜻은 아닙니다. "
        "성분의 함량이나 완제품 효과·자극도도 이 표만으로 판단할 수 없습니다."
    )
    return "\n".join(lines), supported


def _build_safe_followup_response(
    message: str, products: list[ProductResult], ing_kor: dict[str, str],
) -> tuple[str, list[ProductResult]]:
    """Replace corrupted follow-up prose without inventing effects or usage claims."""
    lines = ["추천 제품", "앞서 보여드린 제품 중 현재 요청에 맞는 제품입니다."]
    for product in products:
        names = list(dict.fromkeys(
            ing_kor[name] for name in product.matched_ingredients if ing_kor.get(name)
        ))[:3]
        matched = f" 확인된 매칭 성분: {', '.join(names)}." if names else ""
        cards = product_explanations(product)
        if cards:
            matched += f" {cards[0].kor_name}: {cards[0].explanation}"
        lines.append(f"- [{product.category}] **{product.product_name}**.{matched}")
    lines.append("제품별 효과나 우열은 이 정보만으로 단정할 수 없습니다.")
    return "\n".join(lines), products


async def _handle_followup(session_id: str, turn_id: str, message: str,
                           history: list[dict], active: dict | None = None) -> RecommendResponse:
    """후속 턴: 검색 스킵, 이전 추천 + 대화 맥락으로 답변(비교 등). 캐시 우회."""
    last = next((t for t in reversed(history) if t.get("products")), None)
    visible = (active["visible_products"] if active and "visible_products" in active
               else ((last or {}).get("products") or []))
    source = (active["source_products"] if active and "source_products" in active else visible)
    profile_data = (active or {}).get("profile") or {}
    concerns = [Concern(code) for code in profile_data.get("concerns", (last or {}).get("concerns") or [])
                if code in Concern._value2member_map_]
    constraints = merge_fragrance_constraint(message, [
        Constraint(code) for code in profile_data.get("constraints", [])
        if code in Constraint._value2member_map_
    ])
    if _is_sensitivity_query(concerns):
        # Refresh positive matches from old sessions; inventory facts stay intact.
        def sanitize(rows):
            result = []
            for row in rows:
                names = [name for name in row.get("matched_ingredients", [])
                         if name not in FRAGRANCE_RATIONALE_EXCLUSIONS]
                if row.get("matched_ingredients") and not names:
                    continue  # No positive recommendation evidence remains.
                result.append({**row, "matched_ingredients": names, "matched_count": len(names)})
            return result
        visible, source = sanitize(visible), sanitize(source)
    if active or constraints or fragrance_preference(message) is not None:
        active = {**(active or {}), "profile": {**profile_data,
                  "concerns": [c.value for c in concerns], "constraints": [c.value for c in constraints]},
                  "source_products": source, "visible_products": visible,
                  "ingredients": [row for row in (active or {}).get("ingredients", [])
                                  if not _is_sensitivity_query(concerns)
                                  or row.get("name") not in FRAGRANCE_RATIONALE_EXCLUSIONS]}
    if _RESTORE_ALL_CUE.search(message) and not _requested_categories(message):
        visible = source
    if fragrance_preference(message) is False and not visible:
        visible = source
    requested = _requested_categories(message)
    if _is_usage_order_request(message) and "기초" in message:
        requested.add("토너")
    selected = ([p for p in visible if p.get("category") in requested or
                 (p.get("category") == "스킨" and "토너" in requested)]
                if requested else visible)
    if not visible and not source:
        # 이전 턴이 안전한 0-product 거절이었다면 LLM에 빈 제품 컨텍스트를
        # 넘기지 않는다. 빈 컨텍스트 비교는 제품/성분을 새로 만들기 쉽다.
        response_text = (
            "이전 추천에서 조건에 맞는 제품을 찾지 못해 비교할 제품이 없습니다. "
            "원하시면 제품 조건을 조정하거나 피부 고민을 다시 알려주세요."
        )
        metrics.recommend_output_guard_total.labels(kind="followup_without_products").inc()
        metrics.recommend_requests_total.labels(status="ok").inc()
        await _store_turn(session_id, message, [], response_text, active_state=active)
        return RecommendResponse(
            session_id=session_id,
            turn_id=turn_id,
            ingredients=[],
            products=[],
            response_text=response_text,
            model_used=settings.gpu_model,
        )
    if _is_product_comparison_request(message):
        selected_rows, clarification = _select_comparison_products(message, visible)
        if clarification:
            metrics.recommend_requests_total.labels(status="ok").inc()
            await _store_turn(session_id, message, [], clarification, active_state=active)
            return RecommendResponse(
                session_id=session_id, turn_id=turn_id, ingredients=[], products=[],
                response_text=clarification, model_used="deterministic",
                response_mode="followup_comparison_clarification",
            )
        checked_rows = await _filter_products_with_constraints(selected_rows, constraints)
        if len(checked_rows) != len(selected_rows):
            response_text = "선택한 제품 중 요청 조건을 확인할 수 없는 제품이 있어 비교하지 않았어요. " + _build_no_product_response([], constraints)
            await _store_turn(session_id, message, [], response_text,
                              active_state={**active, "visible_products": [], "turn_id": turn_id})
            return RecommendResponse(session_id=session_id, turn_id=turn_id, ingredients=[], products=[],
                                     response_text=response_text, model_used="deterministic",
                                     response_mode="followup_constraints")
        products = _reconstruct_products(checked_rows)
        inventory = await query_product_ingredient_inventory([p.product_id for p in products])
        evidence = []
        for row in (active or {}).get("ingredients") or []:
            try:
                evidence.append(IngredientResult.model_validate(row))
            except (TypeError, ValueError):
                continue
        comparison = _build_ingredient_comparison(products, inventory, evidence)
        if comparison is None:
            response_text = (
                "선택한 제품 중 성분 데이터가 확인되지 않는 제품이 있어 비교표를 만들 수 없어요. "
                "다른 제품 2~3개를 골라 주세요."
            )
            metrics.recommend_requests_total.labels(status="ok").inc()
            await _store_turn(session_id, message, [], response_text, active_state=active)
            return RecommendResponse(
                session_id=session_id, turn_id=turn_id, ingredients=[], products=[],
                response_text=response_text, model_used="deterministic",
                response_mode="followup_comparison_unavailable",
            )
        response_text, matched_evidence = comparison
        response_text, _ = _normalize_response_text(response_text, products)
        next_active = ({**active, "visible_products": _slim_products(products),
                        "pending_categories": [], "turn_id": turn_id} if active else None)
        metrics.recommend_requests_total.labels(status="ok").inc()
        await _store_turn(session_id, message, products, response_text,
                          active_state=next_active)
        return RecommendResponse(
            session_id=session_id, turn_id=turn_id, ingredients=matched_evidence,
            products=products, response_text=response_text,
            model_used="deterministic", response_mode="followup_comparison",
        )
    if constraints:
        selected = await _filter_products_with_constraints(selected, constraints)
        if not selected or not _is_usage_order_request(message):
            products = _reconstruct_products(selected)
            ingredients = [IngredientResult.model_validate(row) for row in (active or {}).get("ingredients", [])]
            response_text = (_build_grounded_product_response(message, ingredients, products, concerns)
                             if products else _build_no_product_response(ingredients, constraints, concerns, message))
            if products:
                response_text += "\n제조사의 향료 무첨가 안내와 전성분 정보를 기준으로 골랐습니다. 저자극을 보장하는 뜻은 아닙니다."
            await _store_turn(session_id, message, products, response_text,
                              active_state={**active, "visible_products": _slim_products(products), "turn_id": turn_id})
            return RecommendResponse(session_id=session_id, turn_id=turn_id, ingredients=ingredients,
                                     products=products, response_text=response_text, model_used="deterministic",
                                     response_mode="followup_constraints")
    if not selected:
        categories = ", ".join(sorted(requested))
        response_text = (
            f"앞서 보여드린 제품 중 {categories} 제품은 없어요. "
            f"같은 피부 고민으로 {categories} 제품을 새로 찾아볼까요?"
        )
        metrics.recommend_requests_total.labels(status="ok").inc()
        next_active = {**active, "pending_categories": sorted(requested)} if active else None
        await _store_turn(session_id, message, [], response_text, active_state=next_active)
        return RecommendResponse(
            session_id=session_id, turn_id=turn_id, ingredients=[], products=[],
            response_text=response_text, model_used=settings.gpu_model,
            response_mode="followup_no_category_match",
        )
    if _is_usage_order_request(message):
        evidence = []
        for row in (active or {}).get("ingredients") or []:
            try:
                evidence.append(IngredientResult.model_validate(row))
            except (TypeError, ValueError):
                continue
        choices = _usage_order_choices(_reconstruct_products(selected), evidence)
        products = [product for _, product, _ in choices]
        matched_evidence = []
        seen_ingredients = set()
        for _, product, matched in choices:
            item = matched
            if item is None and product.matched_ingredients:
                item = IngredientResult(name=product.matched_ingredients[0])
            if item and item.name not in seen_ingredients:
                matched_evidence.append(item)
                seen_ingredients.add(item.name)
        response_text = _build_usage_order_response(
            choices, str((active or {}).get("base_message") or ""),
        )
        response_text, _ = _normalize_response_text(response_text, products)
        next_active = ({**active, "visible_products": _slim_products(products),
                        "pending_categories": [], "turn_id": turn_id} if active else None)
        metrics.recommend_requests_total.labels(status="ok").inc()
        await _store_turn(session_id, message, products, response_text,
                          active_state=next_active)
        return RecommendResponse(
            session_id=session_id, turn_id=turn_id, ingredients=matched_evidence,
            products=products, response_text=response_text,
            model_used=settings.gpu_model, response_mode="followup_usage_order",
        )
    # Only the selected products are passed to generation and shown as cards.
    inci_all = {i for p in selected for i in (p.get("matched_ingredients") or [])}
    ing_kor = await query_ingredient_kor_names(sorted(inci_all))
    if active and active.get("base_message"):
        base_context = f"현재 피부 고민의 원래 질문: {active['base_message']}\n"
    else:
        base_context = ""
    user_content = (
        f"{base_context}{_followup_context(history, ing_kor, deictic=_is_deictic(message), products_override=selected)}"
        f"\n\n현재 질문: {message}"
    )
    # 세션의 이전 카드에 의존하지 않고 현재 선택 제품에서 다시 확인한다.
    explained_products = _reconstruct_products(selected)
    await _attach_ingredient_explanations(explained_products, concerns)
    if any(product_explanations(product) for product in explained_products):
        user_content += "\n\n" + _product_evidence_lines([], explained_products)
    followup_system = generation_system_prompt(_FOLLOWUP_SYSTEM, explained_products)
    try:
        async with llm_slot():
            client = get_async_llm_client()
            resp = await client.chat.completions.create(
                model=settings.gpu_model,
                messages=[{"role": "system", "content": followup_system},
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
    # LLM이 최종 추천 순위 마커를 냈으면 그 순서로 카드 재정렬(없으면 원래 순서 폴백).
    response_text, ranking = _extract_ranking(response_text)
    products = _reorder_by_ranking(_reconstruct_products(selected),
                                   ranking, response_text)
    explanations_by_id = {p.product_id: p.ingredient_explanations for p in explained_products}
    for product in products:
        product.ingredient_explanations = explanations_by_id.get(product.product_id, [])
    response_text, hanja_removed = _normalize_response_text(response_text, products)
    ingredients = [IngredientResult(name=inci, kor_name=kor) for inci, kor in ing_kor.items()]
    excluded_names = {
        p.get("name") for p in source if p.get("name")
    } - {p.get("name") for p in selected if p.get("name")}
    integrity_issues = find_response_integrity_issues(response_text, ingredients, products)
    excluded_mentioned = any(name in response_text for name in excluded_names)
    response_mode = "followup_filtered" if requested else "followup"
    fragrance_violation = _is_sensitivity_query(concerns) and mentions_excluded_rationale(response_text)
    if not response_text.strip() or integrity_issues or excluded_mentioned or fragrance_violation:
        products = _reconstruct_products(selected)  # discard ranking from corrupted prose
        for product in products:
            product.ingredient_explanations = explanations_by_id.get(product.product_id, [])
        response_text, products = _build_safe_followup_response(message, products, ing_kor)
        response_text, _ = _normalize_response_text(response_text, products)
        response_mode = "followup_quality_fallback"
        metrics.recommend_output_guard_total.labels(kind="followup_quality_fallback").inc()
    if hanja_removed:
        metrics.recommend_output_guard_total.labels(kind="hanja_removed").inc()
    metrics.recommend_requests_total.labels(status="ok").inc()
    # 성분 목록도 함께 넘긴다 → 프론트가 응답 텍스트의 성분명을 올리브색으로 강조(마커 유무 무관).
    next_active = ({**active, "visible_products": _slim_products(products),
                    "pending_categories": [], "turn_id": turn_id} if active else None)
    await _store_turn(session_id, message, products, response_text, active_state=next_active)
    return RecommendResponse(session_id=session_id, turn_id=turn_id, ingredients=ingredients,
                             products=products, response_text=response_text,
                             model_used=settings.gpu_model,
                             response_mode=response_mode)


async def _resolve_conversation_response(
    session_id: str,
    turn_id: str,
    message: str,
) -> RecommendResponse | ContextualSearch | None:
    """Batch/SSE가 공통으로 사용하는 대화 분기.

    후속 질문이면 이전 추천을 사용하고, 이력이 만료된 후속 표현이면
    두 전송 경로 모두 같은 안내 응답을 반환한다. 신규 요청은 None이다.
    """
    history = await conversation_store.load_recent(session_id)
    active = await conversation_store.load_active(session_id)
    requested = _requested_categories(message)
    explicit_prior = _has_followup_cue(message)
    pending = set(active.get("pending_categories") or []) if active else set()
    if pending and _SEARCH_CONFIRMATION.fullmatch(message.strip()):
        requested = pending
    contextual_request = bool(requested and (
        _RESEARCH_CUE.search(message) or (pending and _SEARCH_CONFIRMATION.fullmatch(message.strip()))
    ))
    new_concern = any(cue in message for cue in _CONCERN_CUES) and not _SAME_CONCERN_CUE.search(message)
    if (active or history) and fragrance_preference(message) is not None and not new_concern and not contextual_request:
        return await _handle_followup(session_id, turn_id, message, history, active)
    if active and contextual_request and not explicit_prior and not new_concern:
        try:
            profile = UserProfile.model_validate(active["profile"])
        except (KeyError, ValueError, TypeError):
            profile = None
        if profile and (profile.effects or profile.concerns):
            return ContextualSearch(profile, str(active.get("base_message") or ""), requested)
    if (active or history) and _is_usage_order_request(message) and not new_concern:
        return await _handle_followup(session_id, turn_id, message, history, active)
    if (active or history) and requested and not explicit_prior and not _RESEARCH_CUE.search(message) \
            and not any(c in message for c in _CONCERN_CUES):
        return await _handle_followup(session_id, turn_id, message, history, active)
    if (active or history) and explicit_prior:
        return await _handle_followup(session_id, turn_id, message, history, active)
    if (active or history) and _RESTORE_ALL_CUE.search(message) and not requested:
        return await _handle_followup(session_id, turn_id, message, history, active)
    if history and await _is_followup(message, history):
        return await _handle_followup(session_id, turn_id, message, history, active)
    if not history and not active and _has_missing_history_cue(message):
        metrics.recommend_requests_total.labels(status="ok").inc()
        text = ("이전 추천 내역을 찾지 못했어요. 세션이 새로 시작됐을 수 있어요.\n"
                "어떤 피부 고민이 있으신지 말씀해 주시면 처음부터 추천해 드릴게요. "
                "(예: \"여드름이랑 모공이 고민이에요\")")
        return RecommendResponse(
            session_id=session_id,
            turn_id=turn_id,
            ingredients=[],
            products=[],
            response_text=text,
            model_used=settings.gpu_model,
        )
    return None


async def recommend(session_id: str, message: str, gen_prompt_name: str | None = None) -> RecommendResponse:
    turn_id = str(uuid.uuid4())
    reset_gate_wait()
    t_req = time.perf_counter()
    spans: dict[str, float] = {}

    # 멀티턴: 후속/이력 만료 분기를 SSE와 공유해 기능 차이를 막는다.
    resolution = await _resolve_conversation_response(session_id, turn_id, message)
    if isinstance(resolution, RecommendResponse):
        return resolution
    context = resolution if isinstance(resolution, ContextualSearch) else None
    cache_message, generation_message = _contextual_messages(message, context)

    # 캐시 조회(추출 이전) — 히트 시 extract·neo4j·generate를 통째로 건너뛴다 → GPU 비용 0.
    _t = time.perf_counter()
    cached = _refresh_cached_images(await recommend_cache.get(cache_message, gen_prompt_name))
    spans["cache_lookup"] = time.perf_counter() - _t
    if cached is not None:
        metrics.recommend_cache_total.labels(result="hit").inc()
        metrics.recommend_requests_total.labels(status="ok").inc()
        spans["total"] = time.perf_counter() - t_req
        spans["overhead"] = spans["total"] - spans["cache_lookup"]
        _record_latency(spans, cache="hit")
        # session_id·turn_id는 요청마다 새로 부여(캐시는 콘텐츠만 보관).
        await _store_turn(session_id, message, cached.get("products"), cached.get("response_text"),
                          active_state=_cached_active(cached, message, turn_id, context))
        return RecommendResponse(session_id=session_id, turn_id=turn_id, **cached)
    metrics.recommend_cache_total.labels(result="miss").inc()

    # gen_prompt_name 지정 시 응답 프롬프트 교체(실험용). 미지정이면 프로덕션 기본.
    system_prompt = load_prompt(gen_prompt_name) if gen_prompt_name else _SYSTEM_PROMPT

    try:
        # 같은 키 동시 미스는 리더 1건만 GPU 계산(single-flight, 캐시 스탬피드 제거).
        _t = time.perf_counter()
        async with recommend_cache.single_flight(cache_message, gen_prompt_name):
            spans["flight_wait"] = time.perf_counter() - _t

            # 대기 중 리더가 캐시를 채웠으면 GPU 없이 히트로 처리(coalesced).
            cached = _refresh_cached_images(await recommend_cache.get(cache_message, gen_prompt_name))
            if cached is not None:
                metrics.recommend_cache_total.labels(result="coalesced").inc()
                metrics.recommend_requests_total.labels(status="ok").inc()
                spans["total"] = time.perf_counter() - t_req
                spans["overhead"] = spans["total"] - spans["cache_lookup"] - spans["flight_wait"]
                _record_latency(spans, cache="coalesced")
                await _store_turn(session_id, message, cached.get("products"), cached.get("response_text"),
                                  active_state=_cached_active(cached, message, turn_id, context))
                return RecommendResponse(session_id=session_id, turn_id=turn_id, **cached)

            # 1) 프로필 추출 (LLM, 실패 시 규칙 기반 폴백)
            _t = time.perf_counter()
            if context:
                profile, extraction_method = context.profile, "context_reuse"
            else:
                profile, extraction_method = await extract_with_fallback(message)
            profile = UserProfile.model_validate(profile, from_attributes=True)
            constraints = merge_fragrance_constraint(message, list(profile.constraints))
            profile = profile.model_copy(update={"constraints": constraints})
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
            raw_products = await select_products(
                message, profile.concerns, ingredient_scores,
                requested_override=context.categories if context else None,
                constraints=constraints,
            )
            raw_products = _apply_constraint_evidence_guard(raw_products, constraints)
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
                    fragrance_free_source_url=row.get("fragrance_free_source_url"),
                )
                for row in raw_products
            ]

            # 3) 응답 생성. 제품 0건은 LLM을 거치지 않아 제품명 날조를 차단.
            _t = time.perf_counter()
            response_mode = "generated" if products else "no_products"
            study_match = _verified_study_match(generation_message, profile.concerns, ingredients, products)
            redness_match = _redness_study_match(profile.concerns, ingredients, products)
            if redness_match:
                # 본문이 근거를 설명하는 한 후보만 카드에도 노출한다.
                products = [redness_match[1]]
            await _attach_ingredient_explanations(products, profile.concerns)
            if products and redness_match:
                response_mode = "redness_verified_study_template"
                response_text = _build_redness_study_response(profile.concerns, redness_match)
            elif products and _is_redness_rosacea_query(profile.concerns):
                response_mode = "redness_evidence_template"
                response_text = _build_redness_rosacea_response(
                    ingredients, products, profile.concerns,
                )
            elif products and study_match:
                response_mode = "verified_study_template"
                response_text = _build_verified_study_response(message, study_match)
            elif products:
                response_text = await _build_llm_response(generation_message, ingredients, products, system_prompt)
            else:
                response_text = _build_no_product_response(
                    ingredients, constraints, profile.concerns, generation_message,
                )
            response_text, hanja_removed = _normalize_response_text(response_text, products)
            if hanja_removed:
                metrics.recommend_output_guard_total.labels(kind="hanja_removed").inc()
            integrity_issues = find_response_integrity_issues(response_text, ingredients, products)
            grounding_violation = products and _has_product_grounding_violation(response_text, ingredients, products)
            fragrance_violation = _is_sensitivity_query(profile.concerns) and mentions_excluded_rationale(response_text)
            if products and (integrity_issues or grounding_violation or fragrance_violation):
                kind = "fragrance_rationale_fallback" if fragrance_violation else "quality_fallback" if integrity_issues else "grounding_fallback"
                metrics.recommend_output_guard_total.labels(kind=kind).inc()
                response_mode = kind
                response_text = _build_grounded_product_response(
                    generation_message, ingredients, products, profile.concerns,
                )
            spans["generate"] = time.perf_counter() - _t

            # 같은 문장 재요청이 GPU를 다시 치지 않도록 콘텐츠를 캐시에 저장(session/turn 제외).
            await recommend_cache.set(cache_message, gen_prompt_name, {
                "_profile": profile.model_dump(mode="json"),
                "ingredients": [i.model_dump() for i in ingredients],
                "products": [p.model_dump() for p in products],
                "response_text": response_text,
                "model_used": settings.gpu_model,
                "response_mode": response_mode,
            })

        spans["gate_wait"] = get_gate_wait_seconds()
        spans["total"] = time.perf_counter() - t_req
        spans["overhead"] = (spans["total"] - spans["cache_lookup"] - spans["flight_wait"]
                             - spans["extract"] - spans["retrieval"] - spans["generate"])
        _record_latency(spans, cache="miss")

        metrics.recommend_requests_total.labels(status="ok").inc()
        await _store_turn(
            session_id, message, products, response_text, profile.concerns,
            active_state=_active_recommendation(
                profile, context.base_message if context else message, products, turn_id,
                ingredients,
            ),
        )
        return RecommendResponse(
            session_id=session_id,
            turn_id=turn_id,
            ingredients=ingredients,
            products=products,
            response_text=response_text,
            model_used=settings.gpu_model,
            response_mode=response_mode,
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


def _product_evidence_lines(
    ingredients: list[IngredientResult], products: list[ProductResult],
) -> str:
    """생성기와 평가기가 공유하는 제품 근거 문자열."""
    ingredient_by_name = {ingredient.name: ingredient for ingredient in ingredients}

    def _annotate(names: list[str]) -> str:
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
        base = (f"- [{p.category}] {_product_display_name(p.brand, p.product_name)} "
                f"(핵심 성분 {p.matched_count}개 포함: {_annotate(p.matched_ingredients)})")
        if p.fragrance_free_source_url:
            base += (f"\n  · 검토된 제조사 향료 무첨가 안내: {p.fragrance_free_source_url}. "
                     "현재 전성분과 대조한 안내이며 무취·저자극·알레르기 안전성을 보장하지 않음.")
        explanation = render_product_explanations(p)
        if explanation:
            base += "\n" + explanation
        note = _review_note(p)
        return f"{base}\n  · 사용자 리뷰(참고): {note}" if note else base

    return "\n".join(_product_line(p) for p in products)


def _compose_user_content(
    message: str,
    ingredients: list[IngredientResult],
    products: list[ProductResult],
) -> str:
    """생성 LLM에 줄 user 메시지(사용자 고민 + 성분/제품 데이터)를 조립한다. 스트리밍/비스트리밍 공용."""
    sections = [f"사용자 메시지: {message}"]

    # INCI 성분명 → 표시명·근거. 제품 매칭 결과의 영문 이름도 같은 소비자용 표기로 변환한다.
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
        product_lines = _product_evidence_lines(ingredients, products)
        sections.append(
            "추천 제품 데이터:\n" + product_lines +
            ("\n\n(제품에서 확인된 검토 보습 설명을 추천 이유에 활용하세요. 사용자 리뷰는 보조 참고로만, "
             if any(product_explanations(product) for product in products) else
             "\n\n(성분의 논문 근거가 주된 추천 이유입니다. 사용자 리뷰는 보조 참고로만, ") +
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
    system_prompt = generation_system_prompt(system_prompt, products)

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
            prod_names = ", ".join(
                _product_display_name(p.brand, p.product_name) for p in products[:3]
            )
            return f"피부 고민 분석 결과, 다음 제품들을 추천드립니다: {prod_names}"
        if ingredients:
            names = ", ".join(_ingredient_display_name(i) for i in ingredients[:5])
            return f"피부 고민 분석 결과, 다음 성분들을 추천드립니다: {names}"
        return "죄송합니다. 현재 추천 서비스를 이용할 수 없습니다. 잠시 후 다시 시도해 주세요."


def _sse(event: str, data: dict) -> str:
    """Server-Sent Events 한 프레임."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def recommend_stream(session_id: str, message: str, gen_prompt_name: str | None = None):
    """SSE 추천: meta(구조 데이터 즉시) → delta(검증된 본문) → done.

    성분·제품 카드를 생성 전에 보내 빈 화면을 줄인다. 본문은 한자·
    출력 무결성·제품-성분 연결을 검증한 뒤 전송한다. 모델 내부 생성 단계는
    generate_ttft/generate_decode로 계속 분리 계측한다.
    """
    turn_id = str(uuid.uuid4())
    reset_gate_wait()
    t_req = time.perf_counter()
    spans: dict[str, float] = {}

    # 일반 응답과 같은 대화 분기를 탄다. 후속 응답은 아직 토큰 단위로
    # 생성하지 않지만, meta → delta → done 규약으로 전달해 기능을 동일하게 유지한다.
    try:
        resolution = await _resolve_conversation_response(session_id, turn_id, message)
    except LLMOverCapacityError:
        yield _sse("error", {"error_code": "LLM_OVER_CAPACITY", "message": "요청이 많아 잠시 후 다시 시도해 주세요."})
        return
    except Exception as exc:
        logger.warning("streaming conversation handling failed: %s", exc)
        yield _sse("error", {"error_code": "INTERNAL_ERROR",
                             "message": "일시적인 오류가 발생했어요. 잠시 후 다시 시도해 주세요."})
        return
    if isinstance(resolution, RecommendResponse):
        conversation_response = resolution
        yield _sse("meta", {
            "session_id": conversation_response.session_id,
            "turn_id": conversation_response.turn_id,
            "ingredients": [i.model_dump() for i in conversation_response.ingredients],
            "products": [p.model_dump() for p in conversation_response.products],
            "model_used": conversation_response.model_used,
        })
        yield _sse("delta", {"text": conversation_response.response_text})
        yield _sse("done", {"finish_reason": "conversation",
                            "response_mode": conversation_response.response_mode})
        return

    context = resolution if isinstance(resolution, ContextualSearch) else None
    cache_message, generation_message = _contextual_messages(message, context)

    _t = time.perf_counter()
    cached = _refresh_cached_images(await recommend_cache.get(cache_message, gen_prompt_name))
    spans["cache_lookup"] = time.perf_counter() - _t
    if cached is not None:
        metrics.recommend_cache_total.labels(result="hit").inc()
        metrics.recommend_requests_total.labels(status="ok").inc()
        active_state = _cached_active(cached, message, turn_id, context)
        if active_state is not None:
            await conversation_store.save_active(session_id, active_state)
        yield _sse("meta", {"session_id": session_id, "turn_id": turn_id,
                            "ingredients": cached["ingredients"], "products": cached["products"],
                            "model_used": cached["model_used"]})
        yield _sse("delta", {"text": cached["response_text"]})
        spans["total"] = time.perf_counter() - t_req
        spans["overhead"] = spans["total"] - spans["cache_lookup"]
        _record_latency(spans, cache="hit")
        await _store_turn(session_id, message, cached.get("products"), cached.get("response_text"))
        yield _sse("done", {"finish_reason": "cache",
                            "response_mode": cached.get("response_mode", "generated")})
        return
    metrics.recommend_cache_total.labels(result="miss").inc()
    system_prompt = load_prompt(gen_prompt_name) if gen_prompt_name else _SYSTEM_PROMPT

    try:
        # 같은 키 동시 미스는 리더 1건만 GPU 계산(single-flight, 캐시 스탬피드 제거).
        _t = time.perf_counter()
        async with recommend_cache.single_flight(cache_message, gen_prompt_name):
            spans["flight_wait"] = time.perf_counter() - _t

            # 대기 중 리더가 캐시를 채웠으면 캐시 히트와 같은 프레임으로 서빙(coalesced).
            cached = _refresh_cached_images(await recommend_cache.get(cache_message, gen_prompt_name))
            if cached is not None:
                metrics.recommend_cache_total.labels(result="coalesced").inc()
                metrics.recommend_requests_total.labels(status="ok").inc()
                active_state = _cached_active(cached, message, turn_id, context)
                if active_state is not None:
                    await conversation_store.save_active(session_id, active_state)
                yield _sse("meta", {"session_id": session_id, "turn_id": turn_id,
                                    "ingredients": cached["ingredients"], "products": cached["products"],
                                    "model_used": cached["model_used"]})
                yield _sse("delta", {"text": cached["response_text"]})
                spans["total"] = time.perf_counter() - t_req
                spans["overhead"] = spans["total"] - spans["cache_lookup"] - spans["flight_wait"]
                _record_latency(spans, cache="coalesced")
                await _store_turn(session_id, message, cached.get("products"), cached.get("response_text"))
                yield _sse("done", {"finish_reason": "cache",
                                    "response_mode": cached.get("response_mode", "generated")})
                return

            _t = time.perf_counter()
            if context:
                profile, extraction_method = context.profile, "context_reuse"
            else:
                profile, extraction_method = await extract_with_fallback(message)
            profile = UserProfile.model_validate(profile, from_attributes=True)
            constraints = merge_fragrance_constraint(message, list(profile.constraints))
            profile = profile.model_copy(update={"constraints": constraints})
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
            raw_products = await select_products(
                message, profile.concerns, ingredient_scores,
                requested_override=context.categories if context else None,
                constraints=constraints,
            )
            raw_products = _apply_constraint_evidence_guard(raw_products, constraints)
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
                              review_stats=_parse_review_stats(row.get("review_stats")),
                              fragrance_free_source_url=row.get("fragrance_free_source_url"))
                for row in raw_products
            ]
            redness_match = _redness_study_match(profile.concerns, ingredients, products)
            if redness_match:
                products = [redness_match[1]]
            await _attach_ingredient_explanations(products, profile.concerns)

            # 구조 데이터는 생성 전에 확보되므로 즉시 전송 → 사용자는 빈 화면 대신 성분·제품을 바로 본다.
            active_state = _active_recommendation(
                profile, context.base_message if context else message, products, turn_id,
                ingredients,
            )
            await conversation_store.save_active(session_id, active_state)
            yield _sse("meta", {"session_id": session_id, "turn_id": turn_id,
                                "ingredients": [i.model_dump() for i in ingredients],
                                "products": [p.model_dump() for p in products],
                                "model_used": settings.gpu_model})

            # 생성 스트리밍 (TTFT 측정). 제품 0건은 모델을 거치지 않는다.
            chunks: list[str] = []
            hanja_removed = False
            response_mode = "generated" if products else "no_products"
            if not products:
                response_text = _build_no_product_response(
                    ingredients, constraints, profile.concerns, generation_message,
                )
                chunks.append(response_text)
                yield _sse("delta", {"text": response_text})
                gen_total = 0.0
                spans["generate_ttft"] = 0.0
                spans["generate_decode"] = 0.0
            elif redness_match:
                response_mode = "redness_verified_study_template"
                response_text = _build_redness_study_response(profile.concerns, redness_match)
                gen_total = 0.0
                spans["generate_ttft"] = 0.0
                spans["generate_decode"] = 0.0
            elif _is_redness_rosacea_query(profile.concerns):
                response_mode = "redness_evidence_template"
                response_text = _build_redness_rosacea_response(
                    ingredients, products, profile.concerns,
                )
                gen_total = 0.0
                spans["generate_ttft"] = 0.0
                spans["generate_decode"] = 0.0
            elif study_match := _verified_study_match(generation_message, profile.concerns, ingredients, products):
                response_mode = "verified_study_template"
                response_text = _build_verified_study_response(message, study_match)
                gen_total = 0.0
                spans["generate_ttft"] = 0.0
                spans["generate_decode"] = 0.0
            else:
                user_content = _compose_user_content(generation_message, ingredients, products)
                ttft: float | None = None
                gen_start = time.perf_counter()
                async with llm_slot():
                    client = get_async_llm_client()
                    stream = await client.chat.completions.create(
                        model=settings.gpu_model,
                        messages=[{"role": "system", "content": generation_system_prompt(system_prompt, products)},
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
                            safe_delta, removed = _remove_hanja(delta)
                            hanja_removed = hanja_removed or removed
                            if safe_delta:
                                chunks.append(safe_delta)
                gen_total = time.perf_counter() - gen_start
                response_text = "".join(chunks)
                spans["generate_ttft"] = ttft if ttft is not None else gen_total
                spans["generate_decode"] = gen_total - spans["generate_ttft"]
                response_text = _normalize_consumer_language(response_text)
                response_text = _normalize_product_names(response_text, products)
            if hanja_removed:
                metrics.recommend_output_guard_total.labels(kind="hanja_removed").inc()
            integrity_issues = find_response_integrity_issues(response_text, ingredients, products)
            grounding_violation = products and _has_product_grounding_violation(response_text, ingredients, products)
            fragrance_violation = _is_sensitivity_query(profile.concerns) and mentions_excluded_rationale(response_text)
            if products and (integrity_issues or grounding_violation or fragrance_violation):
                kind = "fragrance_rationale_fallback" if fragrance_violation else "quality_fallback" if integrity_issues else "grounding_fallback"
                metrics.recommend_output_guard_total.labels(kind=kind).inc()
                response_mode = kind
                response_text = _build_grounded_product_response(
                    generation_message, ingredients, products, profile.concerns,
                )
            # 출력 무결성과 제품-성분 연결을 검사한 뒤에만 본문을 전송한다.
            # meta(성분/제품 카드)는 이미 먼저 전송되어 빈 화면은 유지되지 않는다.
            if products:
                yield _sse("delta", {"text": response_text})

            await recommend_cache.set(cache_message, gen_prompt_name, {
                "_profile": profile.model_dump(mode="json"),
                "ingredients": [i.model_dump() for i in ingredients],
                "products": [p.model_dump() for p in products],
                "response_text": response_text,
                "model_used": settings.gpu_model,
                "response_mode": response_mode,
            })

        spans["gate_wait"] = get_gate_wait_seconds()
        spans["total"] = time.perf_counter() - t_req
        spans["overhead"] = (spans["total"] - spans["cache_lookup"] - spans["flight_wait"]
                             - spans["extract"] - spans["retrieval"] - gen_total)
        _record_latency(spans, cache="miss")
        metrics.recommend_requests_total.labels(status="ok").inc()
        await _store_turn(session_id, message, products, response_text, profile.concerns)
        yield _sse("done", {"finish_reason": "stop", "response_mode": response_mode})
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
