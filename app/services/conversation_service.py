import json
import logging

import redis.asyncio as aioredis

from app.core.config import settings
from app.repositories.conversation_repository import get_recent_turns, save_turn

logger = logging.getLogger(__name__)

_REDIS_TTL = 1800  # 30분


async def get_history(session_id: str) -> list[dict]:
    """Redis 캐시 우선, 없으면 PostgreSQL에서 최근 N턴 로드."""
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        cached = await r.get(f"history:{session_id}")
        if cached:
            return json.loads(cached)
        turns = await get_recent_turns(session_id, settings.conversation_history_limit)
        await r.set(f"history:{session_id}", json.dumps(turns), ex=_REDIS_TTL)
        return turns
    except Exception:
        logger.warning("Redis 연결 실패, PostgreSQL fallback 사용")
        return await get_recent_turns(session_id, settings.conversation_history_limit)
    finally:
        await r.aclose()


async def add_turn(
    session_id: str,
    role: str,
    content: str,
    graph_ctx: dict | None = None,
) -> int:
    """턴 저장 후 Redis 캐시 무효화."""
    turn_id = await save_turn(session_id, role, content, graph_ctx)
    r = aioredis.from_url(settings.redis_url)
    try:
        await r.delete(f"history:{session_id}")
    except Exception:
        pass
    finally:
        await r.aclose()
    return turn_id
