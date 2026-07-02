"""startup 워밍업 — 콜드스타트의 첫-요청 페널티를 기동 시점에 흡수한다 (이슈 #36).

측정된 콜드 비용 두 종류를 실제 사용자 요청이 아니라 여기서 미리 지불한다:
1. 앱 lazy 초기화(~+1.4s): asyncpg 풀·Neo4j 드라이버·Redis 클라이언트·OpenAI 클라이언트가
   전부 첫 요청에서 생성되던 것 → 기동 직후 생성+핑.
2. vLLM 첫-요청 컴파일 꼬리(~+4s, torch.compile/CUDA graph): 재프로비저닝 직후 첫 요청이
   느리던 것 → 더미 completion 1건으로 흡수. vLLM이 아직 모델 로드 중이면(수십초~분)
   재시도하며 기다렸다가 **준비되는 즉시** 워밍한다. /health readiness 게이트(llm 핑)와
   합치면: 콜드 vLLM 동안 unhealthy → 워밍 완료 무렵 healthy 전환 → 첫 실트래픽부터 정상 속도.

전 단계 실패 허용(로그만) — 워밍업은 최적화지 기동 조건이 아니다. lifespan에서
백그라운드 태스크로 돌므로 앱 기동(포트 바인딩)을 막지 않는다.

⚠️ 더미 호출은 recommend 파이프라인을 타지 않는다 — 비즈니스 메트릭(recommend_requests_total,
캐시 히트율)과 응답 캐시를 워밍업 트래픽으로 오염시키지 않기 위해서다(실험 정확도).
"""

import asyncio
import logging
import time

from app.clients import neo4j_client
from app.clients.llm_factory import get_async_llm_client
from app.core.config import settings
from app.core.db import get_pool
from app.repositories import recommend_cache

logger = logging.getLogger(__name__)


async def _warm_postgres() -> None:
    pool = await get_pool()
    await pool.fetchval("SELECT 1")


async def _warm_llm() -> None:
    """vLLM이 준비될 때까지 재시도하며, 준비되는 즉시 더미 1건으로 컴파일 꼬리를 흡수."""
    client = get_async_llm_client()
    attempts = max(settings.warmup_llm_max_attempts, 1)
    for attempt in range(1, attempts + 1):
        try:
            t = time.perf_counter()
            await client.chat.completions.create(
                model=settings.gpu_model,
                messages=[{"role": "user", "content": "안녕"}],
                temperature=0,
                max_tokens=8,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
            logger.info(
                "warmup llm ok: attempt=%d first_completion=%.2fs",
                attempt, time.perf_counter() - t,
            )
            return
        except Exception as exc:
            if attempt == attempts:
                logger.warning(
                    "warmup llm gave up after %d attempts (vLLM 미기동?): %s", attempts, exc,
                )
                return
            logger.info(
                "warmup llm attempt %d/%d failed (%s) — %.0fs 후 재시도",
                attempt, attempts, type(exc).__name__, settings.warmup_llm_retry_seconds,
            )
            await asyncio.sleep(settings.warmup_llm_retry_seconds)


async def run_warmup() -> None:
    """앱 의존성 워밍업. lifespan에서 백그라운드 태스크로 실행된다."""
    if not settings.warmup_enabled:
        logger.info("warmup disabled (WARMUP_ENABLED=false)")
        return

    t_start = time.perf_counter()
    # 인프라 의존성 워밍(각각 수십 ms 수준). 개별 실패는 로그만 남기고 계속.
    for name, coro in (
        ("postgres", _warm_postgres()),
        ("neo4j", neo4j_client.ping()),
        ("redis", recommend_cache.ping()),
    ):
        try:
            await coro
            logger.info("warmup %s ok", name)
        except Exception as exc:
            logger.warning("warmup %s failed: %s", name, exc)

    # LLM은 가장 오래 걸릴 수 있어(모델 로드 대기) 마지막에 — 위 워밍은 이미 끝난 상태.
    await _warm_llm()
    logger.info("warmup done in %.2fs", time.perf_counter() - t_start)
