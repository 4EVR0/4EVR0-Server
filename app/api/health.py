import logging
from fastapi import APIRouter
from pydantic import BaseModel

import asyncpg
from neo4j import AsyncGraphDatabase
import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


class DependencyStatus(BaseModel):
    neo4j: str
    postgresql: str
    redis: str
    llm: str


class HealthResponse(BaseModel):
    status: str  # "healthy" | "degraded" | "unhealthy"
    dependencies: DependencyStatus
    version: str


async def _check_postgresql() -> str:
    try:
        conn = await asyncpg.connect(settings.postgres_dsn, timeout=3)
        await conn.close()
        return "ok"
    except Exception as e:
        logger.warning("PostgreSQL ping failed: %s", e)
        return "error"


async def _check_neo4j() -> str:
    try:
        driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
        async with driver.session() as session:
            await session.run("RETURN 1")
        await driver.close()
        return "ok"
    except Exception as e:
        logger.warning("Neo4j ping failed: %s", e)
        return "error"


async def _check_redis() -> str:
    try:
        r = aioredis.from_url(settings.redis_url, socket_timeout=3)
        await r.ping()
        await r.aclose()
        return "ok"
    except Exception as e:
        logger.warning("Redis ping failed: %s", e)
        return "error"


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    neo4j_status = await _check_neo4j()
    pg_status = await _check_postgresql()
    redis_status = await _check_redis()

    deps = DependencyStatus(
        neo4j=neo4j_status,
        postgresql=pg_status,
        redis=redis_status,
        llm="ok",  # LLM 클라이언트는 추후 구현
    )

    all_ok = all(v == "ok" for v in deps.model_dump().values())
    status = "healthy" if all_ok else "degraded"

    return HealthResponse(
        status=status,
        dependencies=deps,
        version=settings.app_version,
    )
