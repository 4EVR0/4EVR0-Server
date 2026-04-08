from fastapi import APIRouter
from pydantic import BaseModel

from app.clients.llm_fallback import extract_with_fallback
from app.schemas.profile import ProfileExtractResponse

router = APIRouter(prefix="/api/v1/profile", tags=["profile"])


class ExtractRequest(BaseModel):
    message: str


@router.post("/extract", response_model=ProfileExtractResponse)
async def extract_profile_endpoint(body: ExtractRequest):
    profile, method = await extract_with_fallback(body.message)
    return ProfileExtractResponse(**profile.model_dump(), extraction_method=method)
