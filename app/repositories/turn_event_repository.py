"""릴리스 버전과 턴 결과 메타데이터 저장(PostgreSQL).

동의 기능 전이므로 질문·답변 원문과 세션 ID는 저장하지 않는다(배포 계획서 4장).
저장 실패는 응답을 막지 않는다(호출 쪽에서 best-effort로 처리).
"""

import json

from app.core.db import get_pool


async def ensure_tables() -> None:
    pool = await get_pool()
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS release_versions (
            release_id     TEXT PRIMARY KEY,
            components     JSONB NOT NULL,
            first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS turn_events (
            turn_id        TEXT PRIMARY KEY,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            release_id     TEXT,
            transport      TEXT NOT NULL,
            status         TEXT NOT NULL,
            response_mode  TEXT,
            cache_hit      BOOLEAN,
            latency_ms     INTEGER,
            product_ids    TEXT[] NOT NULL DEFAULT '{}'
        )
    """)


async def upsert_release(release_id: str, components: dict) -> None:
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO release_versions (release_id, components) VALUES ($1, $2::jsonb) "
        "ON CONFLICT (release_id) DO NOTHING",
        release_id, json.dumps(components, ensure_ascii=False, default=str),
    )


async def record_turn(*, turn_id: str, release_id: str | None, transport: str, status: str,
                      response_mode: str | None, cache_hit: bool | None, latency_ms: int,
                      product_ids: list[str]) -> None:
    """같은 turn_id는 한 번만 저장한다(재시도 멱등)."""
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO turn_events (turn_id, release_id, transport, status, response_mode, cache_hit, "
        "latency_ms, product_ids) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT (turn_id) DO NOTHING",
        turn_id, release_id, transport, status, response_mode, cache_hit, latency_ms, product_ids,
    )
