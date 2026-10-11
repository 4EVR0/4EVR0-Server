import asyncio
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
_PROBE_TIMEOUT_SECONDS = 5.0


class DependencyStatus(BaseModel):
    neo4j: str
    postgresql: str
    redis: str
    llm: str


class HealthResponse(BaseModel):
    status: str  # legacy health or readiness status
    dependencies: DependencyStatus
    version: str


async def _check_postgresql() -> str:
    conn = None
    try:
        conn = await asyncpg.connect(settings.postgres_dsn, timeout=3)
        await conn.fetchval("SELECT 1")
        return "ok"
    except Exception as e:
        logger.warning("PostgreSQL ping failed: %s", type(e).__name__)
        return "error"
    finally:
        if conn is not None:
            await conn.close(timeout=1)


async def _check_neo4j() -> str:
    try:
        async with AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            connection_timeout=3,
            connection_acquisition_timeout=3,
        ) as driver:
            async with driver.session() as session:
                result = await session.run("RETURN 1")
                await result.consume()
        return "ok"
    except Exception as e:
        logger.warning("Neo4j ping failed: %s", type(e).__name__)
        return "error"


async def _check_redis() -> str:
    try:
        async with aioredis.from_url(
            settings.redis_url, socket_timeout=3, socket_connect_timeout=3,
        ) as r:
            await r.ping()
        return "ok"
    except Exception as e:
        logger.warning("Redis ping failed: %s", type(e).__name__)
        return "error"


async def _check_llm() -> str:
    """Check the configured model is advertised, without performing inference."""
    url = settings.gpu_server_url.rstrip("/")
    base_url = url if url.endswith("/v1") else f"{url}/v1"
    try:
        # 서빙 앞단에 인증 프록시가 있으면 토큰 없이는 401 → 준비됐는데도 error로 오판한다.
        headers = {"Authorization": f"Bearer {settings.gpu_api_key}"}
        async with httpx.AsyncClient(timeout=settings.llm_health_timeout_seconds) as client:
            resp = await client.get(f"{base_url}/models", headers=headers)
        if resp.status_code == 200:
            models = resp.json().get("data", [])
            if isinstance(models, list) and any(
                isinstance(model, dict) and model.get("id") == settings.gpu_model
                for model in models
            ):
                return "ok"
            logger.warning("Configured GPU model is absent from /v1/models")
            return "error"
        logger.warning("vLLM readiness ping returned HTTP %d", resp.status_code)
        return "error"
    except Exception as e:
        logger.warning("vLLM readiness ping failed: %s", type(e).__name__)
        return "error"


async def _probe(name, check) -> str:
    try:
        return await asyncio.wait_for(check(), timeout=_PROBE_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("Dependency probe failed: %s (%s)", name, type(exc).__name__)
        return "error"


async def _dependencies() -> DependencyStatus:
    checks = {
        "neo4j": _check_neo4j,
        "postgresql": _check_postgresql,
        "redis": _check_redis,
        "llm": _check_llm,
    }
    values = await asyncio.gather(*(_probe(name, check) for name, check in checks.items()))
    return DependencyStatus(**dict(zip(checks, values)))


@router.get("/live")
async def live(response: Response) -> dict[str, str]:
    """Process liveness: does not contact databases or the GPU."""
    response.headers["Cache-Control"] = "no-store"
    return {"status": "alive"}


@router.get("/release")
async def release(response: Response) -> dict:
    """지금 답변을 만드는 버전 묶음(모델·앱·프롬프트·그래프·캐시). 비밀값은 담지 않는다."""
    response.headers["Cache-Control"] = "no-store"
    from app.services import release_info
    return release_info.current() or {"release_id": None, "components": release_info.static_components()}


@router.get("/ready", response_model=HealthResponse)
async def ready(response: Response) -> HealthResponse:
    """Recommendation dependencies must all work, including the configured model."""
    deps = await _dependencies()
    available = all(value == "ok" for value in deps.model_dump().values())
    response.status_code = 200 if available else 503
    response.headers["Cache-Control"] = "no-store"
    return HealthResponse(
        status="ready" if available else "not_ready", dependencies=deps, version=settings.app_version,
    )


@router.get("/health", response_model=HealthResponse)
async def health_check(response: Response) -> HealthResponse:
    deps = await _dependencies()
    response.headers["Cache-Control"] = "no-store"

    # LLM은 추천 품질의 핵심 의존성 — 콜드/다운이면 unhealthy(503)로 LB 라우팅에서 제외.
    # (추출·생성 모두 폴백이 있어 서비스가 죽진 않지만, 품질 저하 상태로 트래픽을 받지 않는다.)
    # 나머지 의존성 문제는 degraded(200) — 부분 기능으로 동작 가능.
    all_ok = all(v == "ok" for v in deps.model_dump().values())
    if deps.llm != "ok":
        status = "unhealthy"
        response.status_code = 503
    else:
        status = "healthy" if all_ok else "degraded"

    return HealthResponse(
        status=status,
        dependencies=deps,
        version=settings.app_version,
    )
