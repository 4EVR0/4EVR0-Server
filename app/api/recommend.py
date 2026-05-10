from fastapi import APIRouter, HTTPException

from app.repositories import conversation_repository
from app.schemas.recommend import RecommendRequest, RecommendResponse
from app.services.recommend_service import recommend

router = APIRouter(prefix="/api/v1/recommend", tags=["recommend"])


@router.post("", response_model=RecommendResponse)
async def recommend_endpoint(body: RecommendRequest):
    if not await conversation_repository.session_exists(body.session_id):
        raise HTTPException(status_code=404, detail="session not found")
    return await recommend(body.session_id, body.message)
