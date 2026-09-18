"""taxonomy(concern→effect, concern→카테고리) 저장소 — 그래프가 단일 진실 원천.

기동(warmup) 시 Neo4j에서 RELATES_TO/appropriate_categories를 한 번 읽어
메모리에 캐시한다. 이후 조회(get_effects_for 등)는 전부 동기·인메모리.

Neo4j를 읽지 못하면 번들된 taxonomy_snapshot.json(생성기 산출물, 그래프와
같은 taxonomy.yaml에서 나옴)으로 폴백한다 — 폴백 동작은 기존 하드코딩
dict와 동일한 내용이므로 안전하다.

taxonomy 수정은 GraphRAG_Pipeline/db/seed/taxonomy.yaml에서만 하고
generate_taxonomy_artifacts.py로 재생성할 것 (이 모듈에 상수 추가 금지).
"""

import json
import logging
from pathlib import Path

from app.clients import neo4j_client
from app.domain.enums import Concern, Effect

logger = logging.getLogger(__name__)

_SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "domain" / "taxonomy_snapshot.json"

_TAXONOMY_QUERY = """
MATCH (c:Concern)
OPTIONAL MATCH (e:Effect)-[r:RELATES_TO]->(c)
WITH c, e, r ORDER BY r.rank
RETURN
    c.concern_code AS concern_code,
    c.appropriate_categories AS categories,
    [x IN collect(e.effect_code) WHERE x IS NOT NULL] AS effects
"""

_DEFAULT_CATEGORIES_QUERY = """
MATCH (m:TaxonomyConfig {key: 'default_categories'})
RETURN m.values AS values
"""

# 프로세스 캐시. {"concerns": {code: {"effects": [...], "categories": [...]}}, "default_categories": [...]}
_cache: dict | None = None
_source: str = "unloaded"


def _load_snapshot() -> dict:
    data = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    return {
        "concerns": data["concerns"],
        "default_categories": data["default_categories"],
    }


def _get() -> dict:
    """캐시를 반환한다. 미적재 상태면 스냅샷으로 즉시 폴백 (동기 경로 보장)."""
    global _cache, _source
    if _cache is None:
        _cache = _load_snapshot()
        _source = "snapshot"
        logger.warning("taxonomy가 그래프에서 로드되지 않아 snapshot 폴백 사용")
    return _cache


async def load_taxonomy() -> None:
    """Neo4j에서 taxonomy를 읽어 캐시한다. warmup에서 호출. 실패 시 스냅샷 폴백.

    그래프와 스냅샷이 다르면 배포 불일치(스냅샷 재생성 누락 또는 그래프 마이그레이션
    미적용) 신호이므로 ERROR 로그를 남긴다.
    """
    global _cache, _source
    driver = neo4j_client._get_driver()
    try:
        async with driver.session() as session:
            result = await session.run(_TAXONOMY_QUERY)
            concerns = {
                row["concern_code"]: {
                    "effects": row["effects"],
                    "categories": row["categories"],
                }
                async for row in result
            }
            result = await session.run(_DEFAULT_CATEGORIES_QUERY)
            record = await result.single()
            default_categories = record["values"] if record else None
    except Exception as exc:
        logger.warning("taxonomy 그래프 로드 실패, snapshot 폴백: %s", exc)
        _cache = _load_snapshot()
        _source = "snapshot"
        return

    if not concerns or not default_categories:
        logger.error(
            "taxonomy 그래프가 비어 있음(concerns=%d, default=%s) — "
            "migrate.py 미적용 의심, snapshot 폴백", len(concerns), default_categories,
        )
        _cache = _load_snapshot()
        _source = "snapshot"
        return

    graph = {"concerns": concerns, "default_categories": default_categories}
    snapshot = _load_snapshot()
    if graph != snapshot:
        graph_only = set(concerns) - set(snapshot["concerns"])
        snap_only = set(snapshot["concerns"]) - set(concerns)
        diff_effects = [
            code
            for code in set(concerns) & set(snapshot["concerns"])
            if concerns[code] != snapshot["concerns"][code]
        ]
        logger.error(
            "taxonomy 드리프트: 그래프 != snapshot (graph_only=%s snapshot_only=%s diff=%s) "
            "— taxonomy.yaml 수정 후 생성기/마이그레이션 중 한쪽만 반영된 상태. 그래프를 우선 사용.",
            sorted(graph_only), sorted(snap_only), sorted(diff_effects),
        )
    _cache = graph
    _source = "graph"
    logger.info("taxonomy 로드 완료: concerns=%d source=graph", len(concerns))


def source() -> str:
    """현재 taxonomy 출처 ('graph' | 'snapshot' | 'unloaded') — 로깅/헬스 노출용."""
    return _source


def get_effects_for(concern: Concern) -> list[Effect]:
    entry = _get()["concerns"].get(concern.value)
    if entry is None:
        logger.warning("taxonomy에 없는 concern: %s", concern.value)
        return []
    effects = []
    for code in entry["effects"]:
        try:
            effects.append(Effect(code))
        except ValueError:
            logger.warning("taxonomy의 effect %s 가 Effect enum에 없음 (무시)", code)
    return effects


def get_default_categories() -> list[str]:
    return list(_get()["default_categories"])


def get_categories_for(concern: Concern) -> list[str]:
    entry = _get()["concerns"].get(concern.value)
    if entry is None or not entry.get("categories"):
        return get_default_categories()
    return list(entry["categories"])
