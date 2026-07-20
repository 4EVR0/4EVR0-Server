import logging
import time
from typing import Any

from neo4j import AsyncGraphDatabase

from app.core.config import settings

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
) -> list[dict[str, Any]]:
    """고민-관련도가 높은 성분을 가진 제품을 관련도 순으로 반환한다.

    ingredient_scores: [{"name": inci_name, "weight": float}, ...]
        weight = 그 성분의 이 고민에 대한 관련도(graph_score). 제품 점수 = 매칭 성분 weight 합.
        → 단순 "성분 개수"가 아니라 관련도 가중이라, 제너럴리스트 성분(예: 나이아신아마이드)만
          겹치는 목적-불일치 제품이 위로 못 올라온다.
    appropriate_categories: 허용 카테고리(포맷) — recommend_service에서 concern 기반 결정.
    min_relevance_ratio: 최고 점수 대비 이 비율 미만 제품은 컷(0=컷 없음, 가중 랭킹만).
    min_matched_count: 최소 매칭 성분 수 — 제너럴리스트 성분 1개만 겹치는 목적-불일치 제품 컷(1=컷 없음).
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
    }

    query = """
    UNWIND $ingredient_scores AS isc
    MATCH (i:Ingredient {inci_name: isc.name})<-[:CONTAINS]-(prod:Product)
    WHERE prod.category IN $appropriate_categories
    WITH prod,
         COUNT(DISTINCT i.inci_name)   AS matched_count,
         COLLECT(DISTINCT i.inci_name) AS matched_ingredients,
         SUM(coalesce(isc.weight, 1.0)) AS relevance_score
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
        row.relevance_score     AS relevance_score
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


async def query_ingredients_by_effects(
    effects: list[str],
    min_graph_score: float = 0.0,
) -> list[dict[str, Any]]:
    """효능에 관련된 성분을 근거·관련도 순으로 반환한다.

    min_graph_score: AFFECTS 엣지 graph_score 임계 — 이 미만 엣지는 무시(노이즈 컷).
        스키마: (Ingredient)-[:AFFECTS {graph_score, evidence_type, paper_count}]->(Effect)
        evidence_type: "pubmed_evidence"(논문, score 0.1~1.2) > "cosing_function"(성분기능, 0~0.15).
        cosing 엣지(5천+개, 저품질)가 pubmed 부족한 효능에서 노이즈로 상위를 채우는 문제 →
        임계로 약한 엣지를 걷어낸다. 결과가 비면(희소 효능) 임계 없이 폴백.
    """
    if not effects:
        return []

    driver = _get_driver()
    # head(collect()) 패턴으로 성분당 최강 근거 1건만 남김 → LIMIT 20 = distinct 성분 20개 보장
    query = """
    UNWIND $effects AS effect_code
    MATCH (e:Effect {effect_code: effect_code})<-[r:AFFECTS]-(i:Ingredient)
    WHERE r.graph_score >= $min_score
    WITH i, e, r,
         CASE r.evidence_type WHEN 'pubmed_evidence' THEN 0 ELSE 1 END AS ev_rank
    ORDER BY ev_rank, r.graph_score DESC
    WITH i,
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
        best.graph_score      AS graph_score
    ORDER BY best.ev_rank, best.graph_score DESC, i.inci_name
    LIMIT 20
    """
    try:
        start = time.perf_counter()
        async with driver.session() as session:
            result = await session.run(query, effects=effects, min_score=float(min_graph_score))
            rows = [dict(record) async for record in result]
            # 폴백: 임계가 결과를 비우면 임계 없이 재조회 (희소 효능 보호)
            if not rows and float(min_graph_score) > 0.0:
                result = await session.run(query, effects=effects, min_score=0.0)
                rows = [dict(record) async for record in result]
        _log_query("query_ingredients_by_effects", {"effects": effects}, (time.perf_counter() - start) * 1000, len(rows))
        return rows
    except Exception as exc:
        logger.warning("Neo4j query failed: %s", exc)
        return []


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
