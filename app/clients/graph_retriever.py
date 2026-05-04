from neo4j import AsyncGraphDatabase

from app.core.config import settings

_CYPHER = """
MATCH (i:Ingredient)-[:HAS_CLAIM]->(c:Claim)-[:TARGETS]->(e:Effect)
MATCH (c)-[:SUPPORTED_BY]->(p:Paper)
WHERE e.name IN $effects
  AND c.eligibility_tier IN ['Confirmed', 'Promising']
RETURN i.name        AS ingredient,
       c.claim_text  AS claim,
       c.eligibility_tier AS tier,
       c.confidence  AS confidence,
       p.pmid        AS pmid,
       p.title       AS paper_title
ORDER BY
  CASE c.eligibility_tier WHEN 'Confirmed' THEN 1 WHEN 'Promising' THEN 2 ELSE 3 END,
  c.confidence DESC
LIMIT $limit
"""


async def retrieve_ingredients(effects: list[str], limit: int = 10) -> list[dict]:
    """Neo4j에서 effect 목록에 매핑되는 성분·클레임을 검색한다."""
    if not effects:
        return []

    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    try:
        async with driver.session() as session:
            result = await session.run(_CYPHER, effects=effects, limit=limit)
            return [dict(record) async for record in result]
    finally:
        await driver.close()
