"""대화 이력 저장소 (Redis LIST) — 멀티턴 맥락.

추천이 턴마다 stateless라 "그 중에서 비교해줘" 같은 후속 질문이 이전 추천을 못 본다.
세션별 대화를 Redis LIST(`conv:{session_id}`)에 쌓아, 후속 턴이 이전 턴(제품·고민)을
참조할 수 있게 한다.

- 턴 엔트리: {user, assistant, products:[{name,brand,category,rating}], concerns:[...], ts}
- LTRIM으로 최근 N턴만 유지(프롬프트 크기 상한), 턴마다 TTL 갱신(대화는 휘발성).
- best-effort: Redis 장애/비활성 시 조용히 무이력(본 경로를 막지 않음).

Redis 클라이언트·워밍업·종료는 recommend_cache의 것을 재사용(단일 커넥션).
저장(append)은 P1, 로드(load_recent)는 P2(후속 감지/생성)에서 사용.
"""

import json
import logging
import time

from app.core.config import settings
from app.repositories import recommend_cache

logger = logging.getLogger(__name__)

_KEY_PREFIX = "conv:v1:"


def _key(session_id: str) -> str:
    return _KEY_PREFIX + session_id


async def append_turn(
    session_id: str,
    user: str,
    assistant: str,
    products: list[dict] | None = None,
    concerns: list[str] | None = None,
) -> None:
    """한 턴(사용자 발화 + 어시스턴트 응답 + 제품/고민)을 대화 이력에 추가.

    best-effort — 실패해도 요청을 막지 않는다.
    """
    if not settings.conversation_enabled or not session_id:
        return
    entry = {
        "user": user,
        "assistant": assistant,
        "products": products or [],
        "concerns": concerns or [],
        "ts": time.time(),
    }
    try:
        client = recommend_cache._get_client()
        key = _key(session_id)
        await client.rpush(key, json.dumps(entry, ensure_ascii=False))
        # 최근 N턴만 유지 + TTL 갱신
        await client.ltrim(key, -settings.conversation_max_turns, -1)
        await client.expire(key, settings.conversation_ttl_seconds)
    except Exception as exc:  # noqa: BLE001 — 이력은 부가기능
        logger.warning("conversation append failed (session=%s): %s", session_id, exc)


async def load_recent(session_id: str, limit: int | None = None) -> list[dict]:
    """최근 대화 턴을 오래된→최신 순으로 반환. 미존재/장애/비활성 시 빈 리스트."""
    if not settings.conversation_enabled or not session_id:
        return []
    n = limit or settings.conversation_max_turns
    try:
        client = recommend_cache._get_client()
        raw = await client.lrange(_key(session_id), -n, -1)
        out = []
        for item in raw:
            try:
                out.append(json.loads(item))
            except json.JSONDecodeError:
                continue
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("conversation load failed (session=%s): %s", session_id, exc)
        return []


async def clear(session_id: str) -> None:
    """세션 대화 이력 삭제(테스트/리셋용)."""
    if not session_id:
        return
    try:
        await recommend_cache._get_client().delete(_key(session_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("conversation clear failed (session=%s): %s", session_id, exc)
