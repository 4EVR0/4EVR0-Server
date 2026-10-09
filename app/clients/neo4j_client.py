import logging
import time
from typing import Any

from neo4j import AsyncGraphDatabase

from app.core.config import settings
from app.services.fragrance_policy import FRAGRANCE_RATIONALE_EXCLUSIONS

logger = logging.getLogger(__name__)


def _log_query(func_name: str, params: dict, duration_ms: float, result_count: int) -> None:
    # params는 dict repr이라 logfmt 파싱이 깨질 수 있어 항상 줄 끝에 둔다.
    logger.info(
        "event=graph_query func=%s duration_ms=%.2f result_count=%d params=%s",
        func_name, duration_ms, result_count, params,
    )

_driver = None


def _get_driver():
    global _driver
    if _driver is None:
        _driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
    return _driver


async def ping() -> None:
    """드라이버 싱글턴 생성 + 커넥션 1개 선확보 (startup 워밍업용). 실패 시 예외 전파."""
    driver = _get_driver()
    async with driver.session() as session:
        await session.run("RETURN 1")


async def close_driver() -> None:
    """앱 종료 시 드라이버 정리 (lifespan shutdown에서 호출)."""
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None


async def query_products_by_ingredients(
    ingredient_scores: list[dict[str, Any]],
    appropriate_categories: list[str],
    min_relevance_ratio: float = 0.0,
    min_matched_count: int = 1,
    limit: int = 5,
    excluded_ingredients: list[str] | None = None,
) -> list[dict[str, Any]]:
    """고민-관련도가 높은 성분을 가진 제품을 관련도 순으로 반환한다.

    ingredient_scores: [{"name": inci_name, "weight": float, "score_group"?: str}, ...]
        weight = 그 성분의 이 고민에 대한 관련도(graph_score). 제품 점수 = 매칭 성분 weight 합.
        score_group이 같은 성분은 제품당 가장 큰 weight 하나만 더한다.
        → 단순 "성분 개수"가 아니라 관련도 가중이라, 제너럴리스트 성분(예: 나이아신아마이드)만
          겹치는 목적-불일치 제품이 위로 못 올라온다.
    appropriate_categories: 허용 카테고리(포맷) — recommend_service에서 concern 기반 결정.
    min_relevance_ratio: 최고 점수 대비 이 비율 미만 제품은 컷(0=컷 없음, 가중 랭킹만).
    min_matched_count: 최소 매칭 성분 수 — 제너럴리스트 성분 1개만 겹치는 목적-불일치 제품 컷(1=컷 없음).
    excluded_ingredients: 제품의 전체 CONTAINS 성분에 있으면 후보에서 제외할 INCI명.
    """
    if not ingredient_scores:
        return []

    driver = _get_driver()
    params: dict[str, Any] = {
        "ingredient_scores": ingredient_scores,
        "appropriate_categories": appropriate_categories,
        "min_ratio": float(min_relevance_ratio),
        "min_matched": int(min_matched_count),
        "limit": int(limit),
        "excluded_ingredients": excluded_ingredients or [],
    }

    query = """
    UNWIND $ingredient_scores AS isc
    MATCH (i:Ingredient {inci_name: isc.name})<-[:CONTAINS]-(prod:Product)
    WHERE prod.category IN $appropriate_categories
      AND NOT EXISTS {
          MATCH (prod)-[:CONTAINS]->(excluded:Ingredient)
          WHERE excluded.inci_name IN $excluded_ingredients
      }
    // 같은 score_group(예: 민감 요청에서 완화로 남긴 각질 제거 산)은 제품당 최댓값 하나만 점수에 넣는다.
    WITH prod, coalesce(isc.score_group, i.inci_name) AS grp,
         i.inci_name AS name, coalesce(isc.weight, 1.0) AS weight
    WITH prod, grp, COLLECT(DISTINCT name) AS names, MAX(weight) AS group_weight
    WITH prod,
         REDUCE(acc = [], ns IN COLLECT(names) | acc + ns) AS matched_ingredients,
         SUM(group_weight) AS relevance_score
    WITH prod, size(matched_ingredients) AS matched_count, matched_ingredients, relevance_score
    ORDER BY relevance_score DESC, prod.product_name
    // 동일 이름 제품 중복 제거(원본 동작 유지)
    WITH prod.product_name                  AS product_name,
         head(collect(prod))                AS prod,
         head(collect(matched_count))       AS matched_count,
         head(collect(matched_ingredients)) AS matched_ingredients,
         head(collect(relevance_score))     AS relevance_score
    // (b) 상대 임계: 최고 점수 대비 min_ratio 미만은 컷
    WITH collect({
             product_name: product_name, prod: prod, matched_count: matched_count,
             matched_ingredients: matched_ingredients, relevance_score: relevance_score
         }) AS rows,
         max(relevance_score) AS max_score
    UNWIND rows AS row
    WITH row, max_score
    WHERE max_score > 0
      AND row.matched_count >= $min_matched
      AND row.relevance_score >= $min_ratio * max_score
    RETURN
        toString(coalesce(row.prod.product_id, row.prod.goodsNo, row.prod.goods_no)) AS product_id,
        toString(coalesce(row.prod.goodsNo, row.prod.goods_no, row.prod.product_id)) AS goods_no,
        row.product_name        AS product_name,
        row.prod.brand          AS brand,
        row.prod.category       AS category,
        row.matched_count       AS matched_count,
        row.matched_ingredients AS matched_ingredients,
        row.relevance_score     AS relevance_score,
        row.prod.rating         AS rating,
        row.prod.review_count   AS review_count,
        row.prod.review_stats   AS review_stats,
        row.prod.product_url    AS product_url
    // 풀은 관련도(논문 근거) 순으로 뽑아 상위 후보를 확보. 리뷰 재정렬은 앱에서(튜닝 가능).
    ORDER BY row.relevance_score DESC, row.matched_count DESC, product_name
    LIMIT $limit
    """
    try:
        start = time.perf_counter()
        async with driver.session() as session:
            result = await session.run(query, **params)
            rows = [dict(record) async for record in result]
            # 폴백: 커버리지 임계가 결과를 통째로 비우면(희소 고민) 임계 없이 재조회 —
            # "무관 제품 컷"이 "제품 0개"가 되지 않게. (가중 랭킹 순서는 유지)
            if not rows and int(min_matched_count) > 1:
                fb = {**params, "min_matched": 1}
                result = await session.run(query, **fb)
                rows = [dict(record) async for record in result]
        _log_query("query_products_by_ingredients", {"count": len(ingredient_scores)}, (time.perf_counter() - start) * 1000, len(rows))
        return rows
    except Exception as exc:
        logger.warning("Neo4j product query failed: %s", exc)
        return []


async def query_product_ingredient_inventory(product_ids: list[str]) -> dict[str, list[dict[str, str | None]]]:
    """Return INCI-mapped CONTAINS edges for exact Product IDs.

    This graph is not a guaranteed complete package ingredient list: unmapped
    ingredients never become CONTAINS edges. Callers must say "not confirmed"
    rather than "absent" when an edge is missing.
    """
    if not product_ids:
        return {}
    query = """
    UNWIND $product_ids AS product_id
    MATCH (p:Product {product_id: product_id})-[:CONTAINS]->(i:Ingredient)
    WHERE i.inci_name IS NOT NULL AND i.inci_name <> ''
    WITH product_id, collect(DISTINCT {name: i.inci_name, kor_name: i.kor_name}) AS ingredients
    RETURN product_id, ingredients
    """
    try:
        start = time.perf_counter()
        driver = _get_driver()
        async with driver.session() as session:
            result = await session.run(query, product_ids=list(dict.fromkeys(product_ids)))
            rows = [dict(record) async for record in result]
        _log_query("query_product_ingredient_inventory", {"count": len(product_ids)},
                   (time.perf_counter() - start) * 1000, len(rows))
        return {row["product_id"]: row["ingredients"] for row in rows}
    except Exception as exc:
        logger.warning("Neo4j product ingredient inventory query failed: %s", exc)
        return {}


_PRODUCT_CONCERN_EVIDENCE_FOR_QUERY = """
UNWIND $product_ids AS product_id
MATCH (p:Product {product_id: product_id})-[:CONTAINS]->(i:Ingredient)-[r:EVIDENCE_FOR]->(c:Concern)
WHERE c.concern_code IN $concerns
  AND NOT toUpper(trim(i.inci_name)) IN $rationale_exclusions
  AND coalesce(i.kr_reg_status, 'none') <> 'banned'
WITH product_id, i, r, c, split(coalesce(r.effects, ''), '|') AS effects
RETURN product_id, i.inci_name AS inci_name, i.kor_name AS kor_name, c.concern_code AS concern_code,
       // 작용 근거가 있는 효능을 대표로(BLEMISH_CARE만 있으면 그대로 두어 핵심 성분에서 빠지게 한다)
       head([x IN effects WHERE x <> 'BLEMISH_CARE'] + effects) AS effect_code,
       'pubmed_review' AS evidence_type, r.graph_score AS graph_score, r.paper_count AS paper_count,
       false AS medical_wording,
       coalesce(i.sensitive_caution, '') AS sensitive_caution,
       coalesce(i.sensitive_caution_with, []) AS sensitive_caution_with
"""


async def query_product_concern_evidence(product_ids: list[str], effect_codes: list[str],
                                         concern_codes: list[str] | None = None) -> dict[str, list[dict[str, Any]]]:
    """제품별 성분×효능 근거(논문·참고 도서만). 고민별 근거 성분 요약용. 실패하면 빈 dict.

    concern_codes를 주면 고민별 논문 근거(EVIDENCE_FOR, #49) 행(concern_code 포함)을 함께 돌려주고,
    근거를 검수한 성분(evidence_reviewed)의 AFFECTS 논문 행은 뺀다(질환을 구분하지 않는 근거라서).
    """
    if not product_ids or not effect_codes:
        return {}
    query = """
    UNWIND $product_ids AS product_id
    MATCH (p:Product {product_id: product_id})-[:CONTAINS]->(i:Ingredient)-[r:AFFECTS]->(e:Effect)
    WHERE e.effect_code IN $effects
      AND r.evidence_type IN ['pubmed_evidence', 'reference_book']
      AND NOT toUpper(trim(i.inci_name)) IN $rationale_exclusions
      AND coalesce(i.kr_reg_status, 'none') <> 'banned'
      AND NOT ($by_concern AND r.evidence_type = 'pubmed_evidence' AND coalesce(i.evidence_reviewed, false))
    RETURN product_id, i.inci_name AS inci_name, i.kor_name AS kor_name, e.effect_code AS effect_code,
           r.evidence_type AS evidence_type, r.graph_score AS graph_score, r.paper_count AS paper_count,
           coalesce(r.medical_wording, false) AS medical_wording,
           coalesce(i.sensitive_caution, '') AS sensitive_caution,
           coalesce(i.sensitive_caution_with, []) AS sensitive_caution_with
    """
    params = {"product_ids": list(dict.fromkeys(product_ids)), "effects": list(effect_codes),
              "rationale_exclusions": sorted(FRAGRANCE_RATIONALE_EXCLUSIONS),
              "by_concern": bool(concern_codes), "concerns": list(concern_codes or [])}
    try:
        start = time.perf_counter()
        async with _get_driver().session() as session:
            result = await session.run(query, **params)
            rows = [dict(record) async for record in result]
            if concern_codes:
                result = await session.run(_PRODUCT_CONCERN_EVIDENCE_FOR_QUERY, **params)
                rows += [dict(record) async for record in result]
        _log_query("query_product_concern_evidence", {"count": len(product_ids)},
                   (time.perf_counter() - start) * 1000, len(rows))
        out: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            out.setdefault(row.pop("product_id"), []).append(row)
        return out
    except Exception as exc:
        logger.warning("Neo4j product concern evidence query failed: %s", exc)
        return {}


async def query_product_fragrance_evidence(product_ids: list[str]) -> dict[str, Any]:
    """Read product-specific label metadata; old/missing properties stay unknown."""
    if not product_ids:
        return {}
    try:
        start = time.perf_counter()
        async with _get_driver().session() as session:
            result = await session.run(
                "MATCH (p:Product) WHERE p.product_id IN $ids "
                "RETURN p.product_id AS product_id, p.fragrance_evidence AS evidence",
                ids=list(dict.fromkeys(product_ids)),
            )
            rows = {row["product_id"]: row["evidence"] async for row in result}
        _log_query("query_product_fragrance_evidence", {"count": len(product_ids)},
                   (time.perf_counter() - start) * 1000, len(rows))
        return rows
    except Exception as exc:
        logger.warning("Neo4j fragrance evidence query failed: %s", exc)
        return {}


# 고민별 논문 근거(GraphRAG_Pipeline #49). graph_score는 log1p(논문별 가중치 합)이라 AFFECTS 임계를 적용하지 않는다.
# claim(대표 효능)은 엣지가 근거로 쓴 요청 효능 가운데 CLAIM_PRIORITY 순으로 가장 구체적인 것.
# supported_claims는 근거가 있는 요청 효능 전체로, 생성 문장 효능 검사(ingredient_claim_guard)의 허용 범위가 된다.
CLAIM_PRIORITY = [
    "COMEDOLYTIC", "KERATOLYTIC", "SEBUM_REGULATION", "ANTIMICROBIAL", "DEPIGMENTING", "BRIGHTENING",
    "ANTI_AGING", "BARRIER_REPAIR", "MOISTURE_RETENTION", "HYDRATING", "PHOTOPROTECTIVE", "WOUND_HEALING",
    "ANTIOXIDANT", "SOOTHING", "ANTI_INFLAMMATORY", "BLEMISH_CARE",
]
_CONCERN_EVIDENCE_QUERY = """
MATCH (c:Concern {concern_code: $concern})<-[r:EVIDENCE_FOR]-(i:Ingredient)
WHERE NOT toUpper(trim(i.inci_name)) IN $rationale_exclusions
  AND coalesce(i.kr_reg_status, 'none') <> 'banned'
OPTIONAL MATCH (e:Effect)
WHERE e.effect_code IN split(coalesce(r.effects, ''), '|') AND e.effect_code IN $effects
WITH i, r, e,
     coalesce(head([x IN range(0, size($claim_priority) - 1) WHERE $claim_priority[x] = e.effect_code]), 999) AS prio
ORDER BY prio
WITH i, r, collect(e.effect_name_en) AS claims
RETURN
    i.inci_name                             AS name,
    i.kor_name                              AS kor_name,
    head(claims)                            AS claim,
    claims                                  AS supported_claims,
    'pubmed_review'                         AS eligibility_tier,
    toString(r.paper_count)                 AS paper_ref,
    r.graph_score                           AS graph_score,
    i.kr_reg_status                         AS kr_reg_status,
    i.kr_limit_note                         AS kr_limit_note,
    -1                                      AS ev_rank,
    coalesce(i.sensitive_caution, '')       AS sensitive_caution,
    coalesce(i.sensitive_caution_with, [])  AS sensitive_caution_with
ORDER BY r.graph_score DESC, i.inci_name
LIMIT $limit
"""


# 성분마다 그래프에 근거가 있는 효능 전체(요청 고민과 무관). 생성 문장 효능 검사의 허용 범위로만 쓴다.
# 검수한 성분은 고민별 근거(EVIDENCE_FOR)와 참고 도서만, 검수하지 않은 성분은 논문·참고 도서 AFFECTS를 쓴다.
# CosIng 기능 표기는 근거로 넓히지 않는다.
_SUPPORTED_CLAIMS_QUERY = """
UNWIND $names AS n
MATCH (i:Ingredient {inci_name: n})
OPTIONAL MATCH (i)-[a:AFFECTS]->(e:Effect)
WHERE a.evidence_type = 'reference_book'
   OR (a.evidence_type = 'pubmed_evidence' AND NOT coalesce(i.evidence_reviewed, false))
WITH i, collect(DISTINCT e.effect_name_en) AS affects_claims
OPTIONAL MATCH (i)-[f:EVIDENCE_FOR]->(:Concern)
WITH i, affects_claims,
     reduce(codes = [], x IN collect(f.effects) | codes + split(coalesce(x, ''), '|')) AS codes
OPTIONAL MATCH (e2:Effect) WHERE e2.effect_code IN codes
WITH i, affects_claims, collect(DISTINCT e2.effect_name_en) AS evidence_claims
RETURN i.inci_name AS name, affects_claims + evidence_claims AS claims
"""


async def _add_graph_supported_claims(session, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """후보 행의 supported_claims에 그래프 전체의 근거 효능을 더한다. 조회 실패는 호출 쪽 예외 처리를 따른다."""
    if not rows:
        return rows
    result = await session.run(_SUPPORTED_CLAIMS_QUERY, names=[r["name"] for r in rows])
    extra = {record.get("name"): record.get("claims") or [] async for record in result}
    out = []
    for row in rows:
        merged = list(row.get("supported_claims") or ([row["claim"]] if row.get("claim") else []))
        for claim in extra.get(row["name"], []):
            if claim and claim not in merged:
                merged.append(claim)
        out.append({**row, "supported_claims": merged})
    return out


def _merge_concern_evidence(evidence_rows: list[dict[str, Any]], effect_rows: list[dict[str, Any]],
                            limit: int) -> list[dict[str, Any]]:
    """고민별 근거 행을 앞에 두고, 성분당 1행(등급·점수가 가장 좋은 행)으로 limit개까지 합친다."""
    best: dict[str, dict[str, Any]] = {}
    claims: dict[str, list[str]] = {}
    for row in [*evidence_rows, *effect_rows]:
        current = best.get(row["name"])
        key = (row.get("ev_rank", 0), -float(row.get("graph_score") or 0.0))
        if current is None or key < (current.get("ev_rank", 0), -float(current.get("graph_score") or 0.0)):
            best[row["name"]] = row
        merged = claims.setdefault(row["name"], [])
        for claim in row.get("supported_claims") or [row.get("claim")]:
            if claim and claim not in merged:
                merged.append(claim)
    best = {name: {**row, "supported_claims": claims[name]} for name, row in best.items()}
    ordered = sorted(best.values(),
                     key=lambda r: (r.get("ev_rank", 0), -float(r.get("graph_score") or 0.0), r["name"]))
    return ordered[:limit]


async def query_ingredients_by_effects(
    effects: list[str],
    min_graph_score: float = 0.0,
    concern: str | None = None,
) -> list[dict[str, Any]]:
    """효능에 관련된 성분을 근거·관련도 순으로 반환한다.

    min_graph_score: AFFECTS 엣지 graph_score 임계 — 이 미만 엣지는 무시(노이즈 컷).
        스키마: (Ingredient)-[:AFFECTS {graph_score, evidence_type, paper_count}]->(Effect)
        evidence_type: "pubmed_evidence"(논문, score 0.1~1.2) > "cosing_function"(성분기능, 0~0.15).
        cosing 엣지(5천+개, 저품질)가 pubmed 부족한 효능에서 노이즈로 상위를 채우는 문제 →
        임계로 약한 엣지를 걷어낸다. 결과가 비면(희소 효능) 임계 없이 폴백.

    국내 배합금지(kr_reg_status='banned', 식약처 사용제한 원료정보) 성분은 LIMIT 전에 제외한다.
    속성이 없는 노드(적재 전)는 'none'으로 본다. restricted는 kr_limit_note(배합한도)를 함께 반환.

    concern: 고민 코드. 주면 고민별 논문 근거(EVIDENCE_FOR, GraphRAG_Pipeline #49)를 맨 앞에 둔다.
        EVIDENCE_FOR는 그 고민에 맞는 질환의 사람 대상 연구만 센 근거라, 질환을 구분하지 않는
        AFFECTS 논문 엣지보다 우선한다. 근거를 검수한 성분(evidence_reviewed)은 고민 순위에서
        AFFECTS 논문 엣지를 쓰지 않는다. EVIDENCE_FOR가 없는 그래프에서는 기존 결과와 같다.
    반환 행의 ev_rank: EVIDENCE_FOR -1, 논문 0, 기타 1, BLEMISH_CARE 기타 2.
    """
    if not effects:
        return []

    driver = _get_driver()
    # head(collect()) 패턴으로 성분당 최강 근거 1건만 남김 → LIMIT = distinct 성분 수 보장
    query = """
    UNWIND $effects AS effect_code
    MATCH (e:Effect {effect_code: effect_code})<-[r:AFFECTS]-(i:Ingredient)
    WHERE r.graph_score >= $min_score
      AND NOT toUpper(trim(i.inci_name)) IN $rationale_exclusions
      AND coalesce(i.kr_reg_status, 'none') <> 'banned'
      AND NOT ($by_concern AND r.evidence_type = 'pubmed_evidence' AND coalesce(i.evidence_reviewed, false))
    WITH i, e, r,
         // 작용 근거가 없는 결과 효능(BLEMISH_CARE)은 후보가 부족할 때만 채우도록 맨 뒤
         CASE WHEN e.effect_code = 'BLEMISH_CARE' THEN 2
              WHEN r.evidence_type = 'pubmed_evidence' THEN 0 ELSE 1 END AS ev_rank
    ORDER BY ev_rank, r.graph_score DESC
    WITH i,
         // 허용 효능은 논문·참고 도서 근거만. CosIng 기능 표기는 근거로 넓히지 않는다(collect는 null을 뺀다).
         collect(DISTINCT CASE WHEN r.evidence_type IN ['pubmed_evidence', 'reference_book']
                               THEN e.effect_name_en END) AS supported_claims,
         head(collect({
             claim:            e.effect_name_en,
             eligibility_tier: r.evidence_type,
             paper_ref:        toString(r.paper_count),
             graph_score:      r.graph_score,
             ev_rank:          ev_rank
         })) AS best
    RETURN
        i.inci_name           AS name,
        i.kor_name            AS kor_name,
        best.claim            AS claim,
        best.eligibility_tier AS eligibility_tier,
        best.paper_ref        AS paper_ref,
        best.graph_score      AS graph_score,
        i.kr_reg_status       AS kr_reg_status,
        i.kr_limit_note       AS kr_limit_note,
        best.ev_rank          AS ev_rank,
        supported_claims      AS supported_claims,
        coalesce(i.sensitive_caution, '')       AS sensitive_caution,
        coalesce(i.sensitive_caution_with, [])  AS sensitive_caution_with
    ORDER BY best.ev_rank, best.graph_score DESC, i.inci_name
    LIMIT $limit
    """
    params = {"effects": effects, "limit": settings.ingredient_candidate_limit,
              "rationale_exclusions": sorted(FRAGRANCE_RATIONALE_EXCLUSIONS), "by_concern": concern is not None,
              "claim_priority": CLAIM_PRIORITY}
    try:
        start = time.perf_counter()
        async with driver.session() as session:
            result = await session.run(query, min_score=float(min_graph_score), **params)
            rows = [dict(record) async for record in result]
            # 폴백: 임계가 결과를 비우면 임계 없이 재조회 (희소 효능 보호)
            if not rows and float(min_graph_score) > 0.0:
                result = await session.run(query, min_score=0.0, **params)
                rows = [dict(record) async for record in result]
            if concern is not None:
                result = await session.run(_CONCERN_EVIDENCE_QUERY, concern=concern, **params)
                rows = _merge_concern_evidence([dict(record) async for record in result], rows,
                                               settings.ingredient_candidate_limit)
            rows = await _add_graph_supported_claims(session, rows)
        _log_query("query_ingredients_by_effects", {"effects": effects, "concern": concern},
                   (time.perf_counter() - start) * 1000, len(rows))
        return rows
    except Exception as exc:
        logger.warning("Neo4j query failed: %s", exc)
        return []


async def query_cautioned_ingredients(concern_codes: list[str]) -> set[str]:
    """주어진 고민(concern_code)에 대해 CAUTION 엣지가 있는 성분(inci_name) 집합을 반환한다.

    스키마: (Ingredient)-[:CAUTION {evidence_type, graph_score, paper_count}]->(Concern)
    근거 기반 금기 오버레이(이슈: 민감성에 자극 성분 추천 방지). CAUTION 엣지가 없으면 빈 집합.
    """
    if not concern_codes:
        return set()
    driver = _get_driver()
    query = """
    MATCH (i:Ingredient)-[:CAUTION]->(c:Concern)
    WHERE c.concern_code IN $codes
    RETURN collect(DISTINCT i.inci_name) AS names
    """
    try:
        async with driver.session() as session:
            result = await session.run(query, codes=concern_codes)
            rec = await result.single()
            return set(rec["names"]) if rec and rec["names"] else set()
    except Exception as exc:
        logger.warning("Neo4j caution query failed: %s", exc)
        return set()


async def query_ingredient_kor_names(inci_names: list[str]) -> dict[str, str]:
    """INCI명 리스트 → {inci_name: kor_name} 맵. 후속 응답에서 '한글 (INCI)' 표기용.
    미존재/장애 시 빈 맵(호출측이 INCI만 표시하도록 폴백)."""
    if not inci_names:
        return {}
    driver = _get_driver()
    query = """
    UNWIND $names AS nm
    MATCH (i:Ingredient {inci_name: nm})
    WHERE i.kor_name IS NOT NULL AND i.kor_name <> ''
    RETURN i.inci_name AS inci, i.kor_name AS kor
    """
    try:
        async with driver.session() as session:
            result = await session.run(query, names=inci_names)
            return {r["inci"]: r["kor"] async for r in result}
    except Exception as exc:
        logger.warning("Neo4j kor_name query failed: %s", exc)
        return {}


async def query_path_by_effects(effects: list[str]) -> list[dict[str, Any]]:
    """Effect → Ingredient → Product 추천 경로를 반환한다."""
    if not effects:
        return []

    driver = _get_driver()
    query = """
    UNWIND $effects AS effect_code
    MATCH (e:Effect {effect_code: effect_code})<-[r:AFFECTS]-(i:Ingredient)<-[:CONTAINS]-(prod:Product)
    RETURN
        e.effect_code    AS effect_code,
        e.effect_name_en AS effect_name,
        i.inci_name      AS ingredient,
        i.kor_name       AS ingredient_kor,
        r.evidence_type  AS evidence_type,
        r.graph_score    AS graph_score,
        prod.product_name AS product_name,
        prod.brand        AS brand
    ORDER BY r.graph_score DESC
    LIMIT 10
    """
    try:
        start = time.perf_counter()
        async with driver.session() as session:
            result = await session.run(query, effects=effects)
            rows = [dict(record) async for record in result]
        paths = [
            {
                "path": [
                    {"node": row["effect_code"],   "label": "Effect",     "name": row["effect_name"]},
                    {"rel":  "AFFECTS", "evidence_type": row["evidence_type"], "graph_score": row["graph_score"]},
                    {"node": row["ingredient"],    "label": "Ingredient", "kor_name": row["ingredient_kor"]},
                    {"rel":  "CONTAINS"},
                    {"node": row["product_name"],  "label": "Product",    "brand": row["brand"]},
                ]
            }
            for row in rows
        ]
        _log_query("query_path_by_effects", {"effects": effects}, (time.perf_counter() - start) * 1000, len(paths))
        return paths
    except Exception as exc:
        logger.warning("Neo4j path query failed: %s", exc)
        return []
