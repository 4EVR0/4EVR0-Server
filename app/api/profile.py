from fastapi import APIRouter
from pydantic import BaseModel

from app.domain.user import UserProfile
from app.services.user_profile_extraction_service import extract_profile

router = APIRouter(prefix="/api/v1/profile", tags=["profile"])


class ExtractRequest(BaseModel):
    message: str


@router.post("/extract", response_model=UserProfile)
async def extract_profile_endpoint(body: ExtractRequest):
    return extract_profile(body.message)
