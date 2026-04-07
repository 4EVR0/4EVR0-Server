from fastapi import APIRouter
from pydantic import BaseModel

from app.repositories import conversation_repository

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


class SessionResponse(BaseModel):
    session_id: str


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session():
    await conversation_repository.ensure_table()
    session_id = await conversation_repository.create_session()
    return SessionResponse(session_id=session_id)
