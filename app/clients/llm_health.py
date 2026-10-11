"""생성 서버(vLLM) 연결 장애 차단기.

GPU 서버에 연결되지 않으면(연결 실패·시간 초과) 일정 시간 LLM 호출을 바로 건너뛰게 한다.
그동안 추출은 규칙 기반, 생성은 서버가 쓰는 근거 기반 안내로 빠르게 답한다.
쿨다운이 지나면 다음 요청이 다시 시도하고, 성공하면 차단을 푼다.
"""

import time
from contextlib import asynccontextmanager

import openai
from prometheus_client import Counter

from app.core.config import settings


class LLMUnavailableError(Exception):
    """생성 서버 연결 장애로 LLM 호출을 건너뛰었음을 나타낸다."""


llm_unavailable_total = Counter(
    "llm_unavailable_total",
    "생성 서버 연결 장애로 실패했거나 건너뛴 LLM 호출 수",
    ["reason"],
)

_down_until = 0.0


def is_down() -> bool:
    return time.monotonic() < _down_until


def check() -> None:
    if is_down():
        llm_unavailable_total.labels(reason="skipped").inc()
        raise LLMUnavailableError("generation server unavailable (circuit open)")


def record_failure(exc: BaseException) -> None:
    global _down_until
    if isinstance(exc, openai.APIConnectionError):  # APITimeoutError 포함
        _down_until = time.monotonic() + settings.llm_down_cooldown_seconds
        llm_unavailable_total.labels(reason="connection").inc()


def record_success() -> None:
    global _down_until
    _down_until = 0.0


def reset() -> None:
    record_success()


@asynccontextmanager
async def guard():
    """LLM 호출 1건을 감싼다: 차단 중이면 바로 LLMUnavailableError, 연결 장애면 차단을 연다."""
    check()
    try:
        yield
    except Exception as exc:
        record_failure(exc)
        raise
    record_success()
