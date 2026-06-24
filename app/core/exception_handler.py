import logging
from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.request_context import get_trace_id, new_trace_id
from app.schemas.common import ErrorResponse

logger = logging.getLogger(__name__)


async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # 요청 미들웨어가 부여한 trace_id 로 응답·로그를 묶는다(없으면 새로 발급).
    trace_id = get_trace_id() or new_trace_id()
    logger.exception("Unhandled exception")
    body = ErrorResponse(
        error_code="INTERNAL_SERVER_ERROR",
        message="서버 내부 오류가 발생했습니다.",
        detail=str(exc),
        request_id=trace_id,
    )
    return JSONResponse(status_code=500, content=body.model_dump())
