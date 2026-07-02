import logging
from fastapi import APIRouter, Response
from pydantic import BaseModel

import asyncpg
import httpx
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


async def _check_llm() -> str:
    """vLLM readiness 핑 — /v1/models 는 모델 로드 완료 후에만 200을 준다.

    임시 GPU 재프로비저닝 중(모델 로드 수십초~분, 이슈 #36)에는 연결 거부/타임아웃
    → "error" → /health가 unhealthy(503)를 반환해 LB가 콜드 vLLM으로 라우팅하지 않는다.
    """
    url = settings.gpu_server_url.rstrip("/")
    base_url = url if url.endswith("/v1") else f"{url}/v1"
    try:
        async with httpx.AsyncClient(timeout=settings.llm_health_timeout_seconds) as client:
            resp = await client.get(f"{base_url}/models")
        if resp.status_code == 200:
            return "ok"
        logger.warning("vLLM readiness ping returned HTTP %d", resp.status_code)
        return "error"
    except Exception as e:
        logger.warning("vLLM readiness ping failed: %s", e)
        return "error"


@router.get("/health", response_model=HealthResponse)
async def health_check(response: Response) -> HealthResponse:
    neo4j_status = await _check_neo4j()
    pg_status = await _check_postgresql()
    redis_status = await _check_redis()
    llm_status = await _check_llm()

    deps = DependencyStatus(
        neo4j=neo4j_status,
        postgresql=pg_status,
        redis=redis_status,
        llm=llm_status,
    )

    # LLM은 추천 품질의 핵심 의존성 — 콜드/다운이면 unhealthy(503)로 LB 라우팅에서 제외.
    # (추출·생성 모두 폴백이 있어 서비스가 죽진 않지만, 품질 저하 상태로 트래픽을 받지 않는다.)
    # 나머지 의존성 문제는 degraded(200) — 부분 기능으로 동작 가능.
    all_ok = all(v == "ok" for v in deps.model_dump().values())
    if llm_status != "ok":
        status = "unhealthy"
        response.status_code = 503
    else:
        status = "healthy" if all_ok else "degraded"

    return HealthResponse(
        status=status,
        dependencies=deps,
        version=settings.app_version,
    )
