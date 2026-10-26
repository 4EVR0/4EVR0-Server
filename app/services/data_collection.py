"""동의 기반 수집: 턴 소유 확인, 동의한 턴 원문 저장, 피드백, 내 데이터 삭제.

원칙(배포 계획서 4장)
- 동의하지 않은 원문은 저장하지 않는다. 채팅은 동의 없이도 쓸 수 있다.
- 실제로 보여 준 최종 답변(검사·대체를 거친 본문)만 저장한다. 끊긴 스트리밍은 저장하지 않는다.
- 피드백은 그 턴을 받은 세션만 남길 수 있다. 자유 메모는 품질 개선 저장 동의가 있을 때만 저장한다.
- 저장 실패는 채팅을 막지 않는다.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets

from fastapi import Request, Response

from app.core.config import settings
from app.repositories import data_collection_repository as repo
from app.repositories import recommend_cache
from app.services import consent_policy, release_info

logger = logging.getLogger(__name__)

SUBJECT_COOKIE = "4evr0_subject"
SUBJECT_MAX_AGE = 90 * 24 * 3600
QUALITY, TRAINING = "quality_storage", "training_use"
_OWNER_PREFIX = "turnown:v1:"
_pending: set[asyncio.Task] = set()


def subject_from(request: Request) -> str | None:
    value = request.cookies.get(SUBJECT_COOKIE)
    return value if value and len(value) == 32 and value.isalnum() else None


def ensure_subject(request: Request, response: Response) -> str:
    """세션 토큰과 별개인 가명 ID. 없으면 만들어 쿠키로 준다."""
    subject = subject_from(request)
    if subject is None:
        subject = secrets.token_hex(16)
        local_http = request.url.scheme == "http" and request.url.hostname in {"localhost", "127.0.0.1"}
        response.set_cookie(SUBJECT_COOKIE, subject, max_age=SUBJECT_MAX_AGE, httponly=True,
                            secure=not local_http, samesite="strict", path="/")
    return subject


def _session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


async def _remember_owner(turn_id: str, session_id: str) -> None:
    try:
        await recommend_cache._redis().set(_OWNER_PREFIX + turn_id, _session_hash(session_id),
                                           ex=settings.conversation_ttl_seconds)
    except Exception as exc:
        logger.warning("turn owner record failed: %s", exc)


async def owns_turn(turn_id: str, session_id: str | None) -> bool:
    if not session_id:
        return False
    try:
        return await recommend_cache._redis().get(_OWNER_PREFIX + turn_id) == _session_hash(session_id)
    except Exception as exc:
        logger.warning("turn owner check failed: %s", exc)
        return False


async def consents(subject_id: str | None) -> dict[str, bool]:
    if subject_id is None:
        return {}
    return await repo.current_consents(subject_id)


async def _after_turn(session_id: str, subject_id: str | None, message: str, transport: str, turn: dict) -> None:
    turn_id = turn.get("turn_id")
    if not turn_id:
        return
    await _remember_owner(turn_id, session_id)
    if subject_id is None:
        return
    try:
        granted = await repo.current_consents(subject_id)
        if not granted.get(QUALITY):
            return
        await repo.store_turn(
            turn_id=turn_id, subject_id=subject_id, retention_days=consent_policy.RETENTION_DAYS,
            release_id=release_info.current_id(), transport=transport, response_mode=turn.get("response_mode"),
            question=message, answer=turn.get("response_text") or "",
            products=[{k: p.get(k) for k in ("product_id", "product_name", "brand", "category", "matched_ingredients")}
                      for p in turn.get("products") or []],
            ingredients=[{k: i.get(k) for k in ("name", "kor_name", "claim", "eligibility_tier", "paper_ref")}
                         for i in turn.get("ingredients") or []],
            policy_version=consent_policy.POLICY_VERSION, training_use=bool(granted.get(TRAINING)))
    except Exception as exc:
        logger.warning("turn record store failed: %s", exc)


def after_turn(session_id: str, subject_id: str | None, message: str, transport: str, turn: dict) -> None:
    """완료된 턴 처리(백그라운드). turn: turn_id·response_mode·response_text·products·ingredients."""
    task = asyncio.create_task(_after_turn(session_id, subject_id, message, transport, turn))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def maintenance_loop(interval_seconds: float = 3600.0) -> None:
    """테이블을 준비하고, 보관 기한이 지난 데이터를 주기적으로 지운다. 실패해도 서비스는 계속한다."""
    while True:
        try:
            await repo.ensure_tables()
            purged = await repo.purge_expired()
            if any(purged.values()):
                logger.info("data retention purge: %s", purged)
        except Exception as exc:
            logger.warning("data maintenance failed: %s", exc)
        await asyncio.sleep(interval_seconds)
