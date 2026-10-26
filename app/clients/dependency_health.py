"""저장소 의존성(Redis·Neo4j·PostgreSQL) 장애 차단기.

연결에 실패하면 일정 시간 그 의존성 호출을 바로 건너뛴다. 응답 대기 시간(연결 제한)마다
요청이 수 초~수십 초씩 늘어나는 것을 막는다. 쿨다운이 지나면 다음 호출이 다시 시도한다.
요청마다 어떤 의존성이 실패했는지 기록해, 그 요청의 결과를 캐시하지 않거나 장애 안내로 바꾼다.
"""

import time
from contextvars import ContextVar

from prometheus_client import Counter

from app.core.config import settings


class DependencyUnavailableError(Exception):
    """차단 중이라 의존성 호출을 건너뛰었음을 나타낸다."""


dependency_unavailable_total = Counter(
    "dependency_unavailable_total",
    "저장소 의존성 장애로 실패했거나 건너뛴 호출 수",
    ["dependency", "reason"],
)

_degraded: ContextVar[frozenset] = ContextVar("dependency_degraded", default=frozenset())


def start_request() -> None:
    _degraded.set(frozenset())


def degraded(name: str | None = None) -> bool:
    """이번 요청에서 의존성(name 없으면 아무 의존성) 장애가 있었는가."""
    names = _degraded.get()
    return bool(names) if name is None else name in names


def _mark(name: str) -> None:
    _degraded.set(_degraded.get() | {name})


class Breaker:
    def __init__(self, name: str):
        self.name = name
        self._until = 0.0

    def is_open(self) -> bool:
        return time.monotonic() < self._until

    def check(self) -> None:
        """차단 중이면 DependencyUnavailableError."""
        if self.is_open():
            _mark(self.name)
            dependency_unavailable_total.labels(dependency=self.name, reason="skipped").inc()
            raise DependencyUnavailableError(f"{self.name} unavailable (circuit open)")

    def failed(self, exc: BaseException) -> None:
        """호출 실패를 기록한다. 차단 중이라 건너뛴 경우는 쿨다운을 늘리지 않는다."""
        _mark(self.name)
        if isinstance(exc, DependencyUnavailableError):
            return
        self._until = time.monotonic() + settings.dependency_down_cooldown_seconds
        dependency_unavailable_total.labels(dependency=self.name, reason="failure").inc()

    def reset(self) -> None:
        self._until = 0.0


REDIS = Breaker("redis")
NEO4J = Breaker("neo4j")
POSTGRES = Breaker("postgres")
