import secrets

from app.core.config import settings
from app.core.db import get_pool


async def ensure_table() -> None:
    pool = await get_pool()
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id  TEXT PRIMARY KEY,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


async def create_session() -> str:
    session_id = secrets.token_urlsafe(32)
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO sessions (session_id) VALUES ($1)",
        session_id,
    )
    return session_id


async def session_exists(session_id: str) -> bool:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT 1 FROM sessions WHERE session_id = $1 "
        "AND created_at > NOW() - ($2::integer * INTERVAL '1 second')",
        session_id,
        settings.conversation_ttl_seconds,
    )
    return row is not None


async def delete_session(session_id: str) -> None:
    pool = await get_pool()
    await pool.execute("DELETE FROM sessions WHERE session_id = $1", session_id)
