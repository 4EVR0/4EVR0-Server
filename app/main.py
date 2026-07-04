import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_fastapi_instrumentator import Instrumentator

from app.core.config import settings
from app.core.db import close_pool
from app.core.logging import setup_logging
from app.core.middleware import TraceIDMiddleware
from app.core.exception_handler import global_exception_handler, llm_over_capacity_handler
from app.core.warmup import run_warmup
from app.clients import neo4j_client
from app.clients.llm_factory import close_llm_client
from app.clients.llm_gate import LLMOverCapacityError
from app.api import health, sessions, profile, recommend
from app.repositories import recommend_cache
# app.core.metrics 를 import 해 커스텀 메트릭을 기본 레지스트리에 등록한다.
from app.core import metrics as _metrics  # noqa: F401

_STATIC_DIR = Path(__file__).parent / "static"

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 워밍업은 백그라운드로 — 포트 바인딩(기동)을 막지 않는다. vLLM 준비 대기·더미 호출로
    # 첫-요청 페널티를 흡수한다(이슈 #36). 트래픽 라우팅은 /health readiness 게이트가 관리.
    warmup_task = asyncio.create_task(run_warmup())
    yield
    warmup_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await warmup_task
    # 프로세스 싱글턴 커넥션들 정리 (graceful shutdown)
    await close_pool()
    await neo4j_client.close_driver()
    await recommend_cache.close_client()
    await close_llm_client()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    debug=settings.debug,
    lifespan=lifespan,
)

app.add_exception_handler(Exception, global_exception_handler)
# 동시성 한도 초과 거절은 500이 아니라 429로 (구체 핸들러가 우선 매칭됨)
app.add_exception_handler(LLMOverCapacityError, llm_over_capacity_handler)

# 요청마다 trace_id 부여 → 그 요청의 모든 로그가 같은 trace_id 로 묶임
app.add_middleware(TraceIDMiddleware)

app.include_router(health.router)
app.include_router(sessions.router)
app.include_router(profile.router)
app.include_router(recommend.router)

# 기본 HTTP 메트릭 계측 + GET /metrics 노출 (커스텀 비즈니스 메트릭도 함께 실림)
Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")
