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


async def query_products_by_ingredients(ingredient_names: list[str]) -> list[dict[str, Any]]:
    """핵심 성분을 가장 많이 포함한 제품을 순서대로 반환한다."""
    if not ingredient_names:
        return []

    driver = _get_driver()
    # 추천 성분을 많이 포함할수록 상위 노출, 동점 시 제품명 알파벳순
    query = """
    UNWIND $ingredient_names AS ing_name
    MATCH (prod:Product)-[:CONTAINS]->(i:Ingredient {inci_name: ing_name})
    WITH prod,
         COUNT(DISTINCT i.inci_name) AS matched_count,
         COLLECT(DISTINCT i.inci_name) AS matched_ingredients
    ORDER BY matched_count DESC, prod.product_name
    LIMIT 5
    RETURN
        prod.product_id   AS product_id,
        prod.product_name AS product_name,
        prod.brand        AS brand,
        prod.category     AS category,
        matched_count     AS matched_count,
        matched_ingredients AS matched_ingredients
    """
    try:
        start = time.perf_counter()
        async with driver.session() as session:
            result = await session.run(query, ingredient_names=ingredient_names)
            rows = [dict(record) async for record in result]
        _log_query("query_products_by_ingredients", {"count": len(ingredient_names)}, (time.perf_counter() - start) * 1000, len(rows))
        return rows
    except Exception as exc:
        logger.warning("Neo4j product query failed: %s", exc)
        return []


async def query_ingredients_by_effects(effects: list[str]) -> list[dict[str, Any]]:
    if not effects:
        return []

    driver = _get_driver()
    # 실제 스키마: (Ingredient)-[:AFFECTS {graph_score, evidence_type, paper_count}]->(Effect {effect_code})
    # evidence_type: "pubmed_evidence" (논문 근거) > "cosing_function" (성분 기능 근거)
    # head(collect()) 패턴으로 성분당 최강 근거 1건만 남김 → LIMIT 20 = distinct 성분 20개 보장
    query = """
    UNWIND $effects AS effect_code
    MATCH (i:Ingredient)-[r:AFFECTS]->(e:Effect {effect_code: effect_code})
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
            result = await session.run(query, effects=effects)
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
    MATCH (prod:Product)-[:CONTAINS]->(i:Ingredient)-[r:AFFECTS]->(e:Effect {effect_code: effect_code})
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
