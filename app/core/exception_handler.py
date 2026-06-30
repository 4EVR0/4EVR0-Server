import logging
from fastapi import Request
from fastapi.responses import JSONResponse

from app.clients.llm_gate import LLMOverCapacityError
from app.core.request_context import get_trace_id, new_trace_id
from app.schemas.common import ErrorResponse

logger = logging.getLogger(__name__)


async def llm_over_capacity_handler(request: Request, exc: LLMOverCapacityError) -> JSONResponse:
    # GPU 동시성 한도 초과 → 빠른 거절(429). 클라이언트는 잠시 후 재시도하면 된다.
    trace_id = get_trace_id() or new_trace_id()
    body = ErrorResponse(
        error_code="LLM_OVER_CAPACITY",
        message="요청이 많아 처리 용량을 초과했습니다. 잠시 후 다시 시도해 주세요.",
        detail="GPU 동시 처리 한도 초과",
        request_id=trace_id,
    )
    return JSONResponse(status_code=429, content=body.model_dump())


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
