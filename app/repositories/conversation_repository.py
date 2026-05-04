import json
import uuid
import asyncpg

from app.core.config import settings


async def _get_connection() -> asyncpg.Connection:
    return await asyncpg.connect(settings.postgres_dsn)


async def ensure_table() -> None:
    conn = await _get_connection()
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id  TEXT PRIMARY KEY,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
    finally:
        await conn.close()


async def create_session() -> str:
    session_id = str(uuid.uuid4())
    conn = await _get_connection()
    try:
        await conn.execute(
            "INSERT INTO sessions (session_id) VALUES ($1)",
            session_id,
        )
    finally:
        await conn.close()
    return session_id


async def session_exists(session_id: str) -> bool:
    conn = await _get_connection()
    try:
        row = await conn.fetchrow(
            "SELECT 1 FROM sessions WHERE session_id = $1", session_id
        )
        return row is not None
    finally:
        await conn.close()


async def ensure_turns_table() -> None:
    conn = await _get_connection()
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_turns (
                turn_id     SERIAL PRIMARY KEY,
                session_id  TEXT NOT NULL REFERENCES sessions(session_id),
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                graph_ctx   JSONB,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
    finally:
        await conn.close()


async def save_turn(
    session_id: str,
    role: str,
    content: str,
    graph_ctx: dict | None = None,
) -> int:
    conn = await _get_connection()
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO conversation_turns (session_id, role, content, graph_ctx)
            VALUES ($1, $2, $3, $4)
            RETURNING turn_id
            """,
            session_id, role, content,
            json.dumps(graph_ctx) if graph_ctx else None,
        )
        return row["turn_id"]
    finally:
        await conn.close()


async def get_recent_turns(session_id: str, limit: int) -> list[dict]:
    conn = await _get_connection()
    try:
        rows = await conn.fetch(
            """
            SELECT role, content FROM (
                SELECT role, content, created_at
                FROM conversation_turns
                WHERE session_id = $1
                ORDER BY created_at DESC
                LIMIT $2
            ) sub
            ORDER BY created_at ASC
            """,
            session_id, limit,
        )
        return [{"role": r["role"], "content": r["content"]} for r in rows]
    finally:
        await conn.close()
