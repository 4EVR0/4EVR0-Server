"""구조화(JSON) 로깅 설정.

모든 로그 라인에 현재 요청의 `trace_id` 를 붙여 출력한다(미들웨어가 설정).
JSON 한 줄 = 한 로그 이벤트 → Loki/Promtail 이 그대로 수집·검색 가능.
외부 의존성 없이 표준 logging 으로 구현.

`log_format="plain"` 이면 로컬 가독성용 평문 포맷으로 대체된다.
"""

import json
import logging
import sys
from datetime import datetime, timezone

from app.core.config import settings
from app.core.request_context import get_trace_id

# LogRecord 의 표준 속성 — 이걸 제외한 나머지가 `extra=` 로 들어온 커스텀 필드.
_RESERVED = set(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime", "taskName"}


class _TraceIdFilter(logging.Filter):
    """모든 레코드에 현재 요청의 trace_id 를 주입."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_trace_id()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": getattr(record, "trace_id", ""),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # extra= 로 넘어온 커스텀 필드(http_method, path, status, duration_ms 등)
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_TraceIdFilter())

    if settings.log_format == "plain":
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s [trace=%(trace_id)s] - %(message)s"
            )
        )
    else:
        handler.setFormatter(_JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # uvicorn 로거도 같은 핸들러로 통합(중복 출력 방지 위해 자체 핸들러 제거 후 전파).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
