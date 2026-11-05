"""수집 안내 이후 완료된 새 턴의 품질 개선용 저장. 과거 대화는 소급하지 않는다.

- 현재 안내 버전이 없는 이전 클라이언트의 원문은 저장하지 않는다.
- 실제로 보여 준 최종 답변(검사·대체를 거친 본문)만 저장한다. 끊긴 스트리밍은 저장하지 않는다.
- 명확한 식별자·진단/치료 정보 패턴을 발견한 원문은 저장하지 않는다. 완전한 비식별화 보장은 아니다.
- 모델 학습에 사용하지 않는다. 피드백은 그 턴을 받은 세션만 남길 수 있다.
- 저장 실패는 채팅을 막지 않는다.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets

from fastapi import Request, Response

from app.core.config import settings
from app.repositories import data_collection_repository as repo
from app.repositories import recommend_cache
from app.services import consent_policy, release_info

logger = logging.getLogger(__name__)

SUBJECT_COOKIE = "4evr0_subject"
SUBJECT_MAX_AGE = 90 * 24 * 3600
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


def current_policy(version: str | None) -> bool:
    return version == consent_policy.POLICY_VERSION


# 원문을 출력하거나 외부 서비스로 보내지 않는 최소 감지. 일반적인 피부 고민은 제외하지 않는다.
_PRIVATE_INPUT = re.compile(
    r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"
    r"|(?<!\d)(?:\+82[-\s]?)?0?1[016789][-\s]?\d{3,4}[-\s]?\d{4}(?!\d)"
    r"|(?<!\d)\d{6}[-\s]?[1-4]\d{6}(?!\d)"
    r"|(?:제|내)\s*이름(?:은|이|:|：)|이름\s*[:：]"
    r"|연락처|전화번호|주민등록|계좌번호|카드번호|(?:집\s*)?주소\s*[:：는]"
    r"|진단|치료\s*(?:받|중|이력)|처방|복용|병력|입원|수술|임신|임산부|수유",
    re.IGNORECASE,
)


def safe_for_quality_storage(text: str) -> bool:
    return _PRIVATE_INPUT.search(text) is None


async def _after_turn(session_id: str, subject_id: str | None, message: str, transport: str, turn: dict,
                      policy_version: str | None = None) -> None:
    turn_id = turn.get("turn_id")
    if not turn_id:
        return
    await _remember_owner(turn_id, session_id)
    if subject_id is None or not current_policy(policy_version):
        return
    answer = turn.get("response_text") or ""
    if not safe_for_quality_storage(message) or not safe_for_quality_storage(answer):
        return
    try:
        await repo.store_turn(
            turn_id=turn_id, subject_id=subject_id, retention_days=consent_policy.RETENTION_DAYS,
            release_id=release_info.current_id(), transport=transport, response_mode=turn.get("response_mode"),
            question=message, answer=answer,
            products=[{k: p.get(k) for k in ("product_id", "product_name", "brand", "category", "matched_ingredients")}
                      for p in turn.get("products") or []],
            ingredients=[{k: i.get(k) for k in ("name", "kor_name", "claim", "eligibility_tier", "paper_ref")}
                         for i in turn.get("ingredients") or []],
            policy_version=consent_policy.POLICY_VERSION, training_use=False)
    except Exception as exc:
        logger.warning("turn record store failed: %s", exc)


def after_turn(session_id: str, subject_id: str | None, message: str, transport: str, turn: dict,
               policy_version: str | None = None) -> None:
    """완료된 턴 처리(백그라운드). turn: turn_id·response_mode·response_text·products·ingredients."""
    task = asyncio.create_task(_after_turn(session_id, subject_id, message, transport, turn, policy_version))
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
