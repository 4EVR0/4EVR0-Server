"""요청 단위 trace_id 를 담는 ContextVar.

미들웨어가 요청마다 trace_id 를 set 하면, 같은 요청에서 실행되는 모든 코드
(서비스/클라이언트/예외 핸들러)가 `get_trace_id()` 로 동일한 값을 읽는다.
→ 추천 한 건의 모든 로그가 같은 trace_id 로 묶여 Loki/Grafana에서 추적 가능.
"""

import uuid
from contextvars import ContextVar

_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def set_trace_id(trace_id: str):
    """trace_id 를 설정하고 reset 용 토큰을 반환한다."""
    return _trace_id_var.set(trace_id)


def reset_trace_id(token) -> None:
    _trace_id_var.reset(token)


def get_trace_id() -> str:
    """현재 요청의 trace_id. 요청 컨텍스트 밖이면 빈 문자열."""
    return _trace_id_var.get()
