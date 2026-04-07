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
