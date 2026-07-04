"""asyncpg 커넥션 풀 (프로세스 싱글턴).

기존에는 요청마다 asyncpg.connect()/close()로 TCP+인증 핸드셰이크를 새로 했다
(콜드스타트 이슈 #36의 "커넥션 풀 부재"). 풀을 재사용하면 첫 요청 페널티와
요청당 커넥션 비용이 사라진다. 풀 생성은 lazy — 첫 사용(또는 startup 워밍업) 시 1회.
"""

import asyncio

import asyncpg

from app.core.config import settings

_pool: asyncpg.Pool | None = None
# 동시 첫-요청이 풀을 중복 생성하지 않도록 생성 구간만 잠근다.
_pool_lock = asyncio.Lock()


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                _pool = await asyncpg.create_pool(
                    settings.postgres_dsn,
                    min_size=settings.pg_pool_min_size,
                    max_size=settings.pg_pool_max_size,
                )
    return _pool


async def close_pool() -> None:
    """앱 종료 시 풀 정리 (lifespan shutdown에서 호출)."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
