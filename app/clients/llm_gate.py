"""GPU(vLLM) 호출 동시성 게이트.

추천 1건은 같은 단일 GPU를 추출+생성으로 2번 친다. 동시 요청이 늘면 vLLM 큐가 무한정
쌓여 생성 지연이 폭증하고 끝내 타임아웃 캐스케이드로 무너진다
(review/2026-06-30-load-test.md baseline: 50명에서 p95 120s·실패 8.7%).

이 게이트는 GPU로 나가는 동시 호출 수를 `settings.llm_max_concurrency` 로 제한한다:
- reject_over_capacity=False(기본): 한도 초과분은 큐에서 대기 → 처리량 천장은 같되
  GPU 스래싱이 줄어 p95/p99·에러율이 안정된다.
- reject_over_capacity=True: 빈 슬롯이 없으면 대기하지 않고 즉시 LLMOverCapacityError →
  API에서 HTTP 429로 빠르게 거절("느리게 다 실패" 대신 "초과분은 빠르게 거절").

⚠️ 동시성 제한은 RPS 천장 자체를 올리지 못한다(안전벨트). 천장은 GPU 최적화로 올린다
   (같은 문서 6절: 프리픽스 캐싱·max_tokens 적정화·시맨틱 캐시·FP8 등).
"""

import asyncio
import contextvars
import time
from contextlib import asynccontextmanager

from prometheus_client import Counter, Gauge

from app.core.config import settings


class LLMOverCapacityError(Exception):
    """GPU 동시성 한도 초과로 요청을 거절했음을 나타낸다(→ HTTP 429)."""


# 요청당 세마포어 큐 대기 누적(초). 한 요청은 슬롯을 2번(추출+생성) 확보하므로 그 대기의 합.
# 부하 시 이 값이 extract/generate 안에 섞여 보이던 큐 대기를 분리 계측하기 위함.
_gate_wait_seconds: contextvars.ContextVar[float] = contextvars.ContextVar("gate_wait_seconds", default=0.0)


def reset_gate_wait() -> None:
    """요청 시작 시 호출 — 이 요청의 큐 대기 누적을 0으로."""
    _gate_wait_seconds.set(0.0)


def get_gate_wait_seconds() -> float:
    """이번 요청에서 슬롯 확보에 대기한 총 시간(초)."""
    return _gate_wait_seconds.get()


# 현재 GPU로 처리 중인 동시 호출 수 (포화 모니터링용)
llm_inflight_requests = Gauge(
    "llm_inflight_requests",
    "현재 GPU(vLLM)로 처리 중인 동시 호출 수",
)
# 동시성 한도 초과로 즉시 거절된 호출 수 (reject 모드에서만 증가)
llm_rejected_total = Counter(
    "llm_rejected_total",
    "동시성 한도 초과로 거절된 GPU 호출 수",
)

# 한도 ≤ 0 이면 게이트 비활성(무제한, 기존 동작). 양수면 그 수로 동시 호출 제한.
_enabled = settings.llm_max_concurrency > 0
_semaphore = asyncio.Semaphore(settings.llm_max_concurrency) if _enabled else None


@asynccontextmanager
async def llm_slot():
    """GPU 호출 1건의 슬롯을 확보한다.

    게이트 비활성 시 그대로 통과. reject 모드에서 빈 슬롯이 없으면 LLMOverCapacityError.
    """
    if not _enabled:
        yield
        return

    # asyncio는 단일 스레드라, 동기 locked() 체크와 곧이은 acquire() 사이에 다른 코루틴이
    # 끼어들지 않는다(빈 슬롯이면 acquire가 suspend 없이 즉시 성공) → 아래 분기에 race 없음.
    if settings.llm_reject_over_capacity and _semaphore.locked():
        llm_rejected_total.inc()
        raise LLMOverCapacityError()

    _wait_start = time.perf_counter()
    await _semaphore.acquire()
    _gate_wait_seconds.set(_gate_wait_seconds.get() + (time.perf_counter() - _wait_start))
    llm_inflight_requests.inc()
    try:
        yield
    finally:
        llm_inflight_requests.dec()
        _semaphore.release()
