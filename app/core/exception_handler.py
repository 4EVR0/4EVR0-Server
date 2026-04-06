import uuid
import logging
from fastapi import Request
from fastapi.responses import JSONResponse

from app.schemas.common import ErrorResponse

logger = logging.getLogger(__name__)


async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = str(uuid.uuid4())[:8]
    logger.exception("Unhandled exception [request_id=%s]", request_id)
    body = ErrorResponse(
        error_code="INTERNAL_SERVER_ERROR",
        message="서버 내부 오류가 발생했습니다.",
        detail=str(exc),
        request_id=request_id,
    )
    return JSONResponse(status_code=500, content=body.model_dump())
