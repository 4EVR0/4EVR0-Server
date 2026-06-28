import logging
from typing import Any

from neo4j import AsyncGraphDatabase

from app.core.config import settings

logger = logging.getLogger(__name__)

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
        async with driver.session() as session:
            result = await session.run(query, ingredient_names=ingredient_names)
            return [dict(record) async for record in result]
    except Exception as exc:
        logger.warning("Neo4j product query failed: %s", exc)
        return []


async def query_ingredients_by_effects(effects: list[str]) -> list[dict[str, Any]]:
    if not effects:
        return []

    driver = _get_driver()
    # 실제 스키마: (Ingredient)-[:AFFECTS {graph_score, evidence_type, paper_count}]->(Effect {effect_code})
    # evidence_type: "pubmed_evidence" (논문 근거) > "cosing_function" (성분 기능 근거)
    # 성분당 1행으로 집계 후, "요청한 효능을 몇 개나 만족하는지(effect_match)"를 1순위로 랭킹.
    # → 광범위 효능 하나만 타고 온 off-target 성분(예: ANTI_INFLAMMATORY만 맞는 RETINOL)이 밀려나고,
    #   여러 효능을 동시에 만족하는 on-target 성분이 우대됨. score 합으로 score=0 노이즈가 가라앉음.
    # display 필드(claim/근거/논문수)는 매칭 효능 중 graph_score 최고 엣지에서 가져온다.
    query = """
    UNWIND $effects AS effect_code
    MATCH (i:Ingredient)-[r:AFFECTS]->(e:Effect {effect_code: effect_code})
    WITH i,
         count(DISTINCT e) AS effect_match,
         sum(coalesce(r.graph_score, 0.0)) AS total_score,
         max(CASE WHEN r.evidence_type = 'pubmed_evidence' THEN 1 ELSE 0 END) AS has_pubmed,
         collect({score: coalesce(r.graph_score, 0.0), ev: r.evidence_type,
                  effect: e.effect_name_en, papers: r.paper_count}) AS ms
    WITH i, effect_match, total_score, has_pubmed,
         reduce(best = null, m IN ms |
             CASE WHEN best IS NULL OR m.score > best.score THEN m ELSE best END) AS best
    RETURN
        i.inci_name           AS name,
        i.kor_name            AS kor_name,
        best.effect           AS claim,
        best.ev               AS eligibility_tier,
        toString(best.papers) AS paper_ref,
        total_score           AS graph_score
    ORDER BY effect_match DESC, total_score DESC, has_pubmed DESC, i.inci_name
    LIMIT 20
    """
    try:
        async with driver.session() as session:
            result = await session.run(query, effects=effects)
            return [dict(record) async for record in result]
    except Exception as exc:
        logger.warning("Neo4j query failed: %s", exc)
        return []
