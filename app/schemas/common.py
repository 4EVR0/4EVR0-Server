from pydantic import BaseModel


class ErrorResponse(BaseModel):
    error_code: str
    message: str
    detail: str = ""
    request_id: str = ""
