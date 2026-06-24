"""요청 단위 trace_id 미들웨어 (순수 ASGI).

BaseHTTPMiddleware 대신 순수 ASGI 미들웨어로 구현한다. ContextVar 를
`__call__` 과 동일한 코루틴에서 set 하므로 다운스트림 엔드포인트까지
trace_id 가 안전하게 전파된다(BaseHTTPMiddleware 의 contextvar 전파 이슈 회피).
"""

import logging
import time

from app.core.request_context import new_trace_id, reset_trace_id, set_trace_id

logger = logging.getLogger("app.request")

# 들어온 요청에서 trace_id 로 승계할 헤더 (앞선 게이트웨이/클라이언트가 부여했을 수 있음)
_INCOMING_HEADERS = (b"x-request-id", b"x-trace-id")


class TraceIDMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        incoming = next((headers[h] for h in _INCOMING_HEADERS if h in headers), None)
        trace_id = incoming.decode("latin-1") if incoming else new_trace_id()

        token = set_trace_id(trace_id)
        start = time.perf_counter()
        status_code = {"code": 0}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_code["code"] = message["status"]
                message.setdefault("headers", []).append(
                    (b"x-trace-id", trace_id.encode("latin-1"))
                )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            logger.info(
                "request completed",
                extra={
                    "http_method": scope.get("method"),
                    "path": scope.get("path"),
                    "status": status_code["code"],
                    "duration_ms": duration_ms,
                },
            )
            reset_trace_id(token)
