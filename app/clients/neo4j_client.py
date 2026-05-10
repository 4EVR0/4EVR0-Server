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


async def query_ingredients_by_effects(effects: list[str]) -> list[dict[str, Any]]:
    if not effects:
        return []

    driver = _get_driver()
    query = """
    UNWIND $effects AS effect_name
    MATCH (e:Effect {name: effect_name})<-[:HAS_EFFECT]-(i:Ingredient)-[:HAS_CLAIM]->(c:Claim)-[:FROM_PAPER]->(p:Paper)
    WHERE c.eligibility_tier IN ['strict_graph', 'soft_graph']
    RETURN DISTINCT
        i.name        AS name,
        c.claim       AS claim,
        c.eligibility_tier AS eligibility_tier,
        p.paper_ref   AS paper_ref
    ORDER BY
        CASE c.eligibility_tier WHEN 'strict_graph' THEN 0 ELSE 1 END,
        i.name
    LIMIT 20
    """
    try:
        async with driver.session() as session:
            result = await session.run(query, effects=effects)
            return [dict(record) async for record in result]
    except Exception as exc:
        logger.warning("Neo4j query failed: %s", exc)
        return []
