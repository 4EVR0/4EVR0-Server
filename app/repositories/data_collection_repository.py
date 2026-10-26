"""동의·동의한 턴 원문·피드백 저장(PostgreSQL). 배포 계획서 4장 '베타 전에 구현할 최소 수집 구조'.

- subject_id는 세션 토큰과 다른 가명 ID다(세션 bearer token을 분석용 ID로 쓰지 않는다).
- 원문(turn_records)은 품질 개선 저장 동의가 있을 때만, 보관 기한(expires_at)과 함께 저장한다.
"""

import json

from app.core.db import get_pool


async def ensure_tables() -> None:
    pool = await get_pool()
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS consents (
            id              BIGSERIAL PRIMARY KEY,
            subject_id      TEXT NOT NULL,
            kind            TEXT NOT NULL,
            granted         BOOLEAN NOT NULL,
            policy_version  TEXT NOT NULL,
            decided_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    await pool.execute("CREATE INDEX IF NOT EXISTS consents_subject_idx ON consents (subject_id, kind, decided_at DESC)")
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS turn_records (
            turn_id         TEXT PRIMARY KEY,
            subject_id      TEXT NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at      TIMESTAMPTZ NOT NULL,
            release_id      TEXT,
            transport       TEXT NOT NULL,
            response_mode   TEXT,
            question        TEXT NOT NULL,
            answer          TEXT NOT NULL,
            products        JSONB NOT NULL DEFAULT '[]',
            ingredients     JSONB NOT NULL DEFAULT '[]',
            policy_version  TEXT NOT NULL,
            training_use    BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)
    await pool.execute("CREATE INDEX IF NOT EXISTS turn_records_subject_idx ON turn_records (subject_id)")
    await pool.execute("CREATE INDEX IF NOT EXISTS turn_records_expires_idx ON turn_records (expires_at)")
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            turn_id         TEXT NOT NULL,
            subject_id      TEXT NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            helpful         BOOLEAN NOT NULL,
            issues          TEXT[] NOT NULL DEFAULT '{}',
            note            TEXT,
            note_expires_at TIMESTAMPTZ,
            source          TEXT NOT NULL DEFAULT 'user',
            PRIMARY KEY (turn_id, subject_id)
        )
    """)


async def current_consents(subject_id: str) -> dict[str, bool]:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT DISTINCT ON (kind) kind, granted FROM consents WHERE subject_id = $1 "
        "ORDER BY kind, decided_at DESC, id DESC", subject_id)
    return {row["kind"]: row["granted"] for row in rows}


async def record_consents(subject_id: str, decisions: dict[str, bool], policy_version: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        for kind, granted in decisions.items():
            await conn.execute(
                "INSERT INTO consents (subject_id, kind, granted, policy_version) VALUES ($1, $2, $3, $4)",
                subject_id, kind, granted, policy_version)


async def store_turn(*, turn_id: str, subject_id: str, retention_days: int, release_id: str | None,
                     transport: str, response_mode: str | None, question: str, answer: str,
                     products: list, ingredients: list, policy_version: str, training_use: bool) -> None:
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO turn_records (turn_id, subject_id, expires_at, release_id, transport, response_mode, "
        "question, answer, products, ingredients, policy_version, training_use) "
        "VALUES ($1, $2, NOW() + ($3::integer * INTERVAL '1 day'), $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11, $12) "
        "ON CONFLICT (turn_id) DO NOTHING",
        turn_id, subject_id, retention_days, release_id, transport, response_mode, question, answer,
        json.dumps(products, ensure_ascii=False, default=str), json.dumps(ingredients, ensure_ascii=False, default=str),
        policy_version, training_use)


async def upsert_feedback(*, turn_id: str, subject_id: str, helpful: bool, issues: list[str],
                          note: str | None, retention_days: int) -> None:
    pool = await get_pool()
    await pool.execute(
        "INSERT INTO feedback (turn_id, subject_id, helpful, issues, note, note_expires_at) "
        "VALUES ($1, $2, $3, $4, $5, CASE WHEN $5::text IS NULL THEN NULL "
        "ELSE NOW() + ($6::integer * INTERVAL '1 day') END) "
        "ON CONFLICT (turn_id, subject_id) DO UPDATE SET helpful = EXCLUDED.helpful, issues = EXCLUDED.issues, "
        "note = EXCLUDED.note, note_expires_at = EXCLUDED.note_expires_at, created_at = NOW()",
        turn_id, subject_id, helpful, issues, note, retention_days)


async def delete_subject_data(subject_id: str) -> dict[str, int]:
    """저장된 대화 원문과 피드백을 지운다(동의 기록은 남긴다 — 언제 무엇에 동의·철회했는지 증빙)."""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.transaction():
        turns = await conn.execute("DELETE FROM turn_records WHERE subject_id = $1", subject_id)
        feedback = await conn.execute("DELETE FROM feedback WHERE subject_id = $1", subject_id)
    return {"turn_records": int(turns.split()[-1]), "feedback": int(feedback.split()[-1])}


async def withdraw_training_use(subject_id: str) -> None:
    pool = await get_pool()
    await pool.execute("UPDATE turn_records SET training_use = FALSE WHERE subject_id = $1", subject_id)


async def purge_expired() -> dict[str, int]:
    """보관 기한이 지난 원문·메모를 지우고, 만료된 세션 행도 정리한다."""
    from app.core.config import settings

    pool = await get_pool()
    turns = await pool.execute("DELETE FROM turn_records WHERE expires_at < NOW()")
    notes = await pool.execute(
        "UPDATE feedback SET note = NULL, note_expires_at = NULL WHERE note_expires_at < NOW()")
    sessions = await pool.execute(
        "DELETE FROM sessions WHERE created_at < NOW() - ($1::integer * INTERVAL '1 second')",
        settings.conversation_ttl_seconds)
    return {"turn_records": int(turns.split()[-1]), "notes": int(notes.split()[-1]),
            "sessions": int(sessions.split()[-1])}
