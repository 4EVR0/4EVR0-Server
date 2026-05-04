from fastapi import APIRouter, HTTPException

from app.repositories.conversation_repository import session_exists
from app.schemas.recommendation import (
    IngredientResult,
    RecommendRequest,
    RecommendResponse,
)
from app.services.recommendation_service import recommend

router = APIRouter(prefix="/api/v1/recommend", tags=["recommend"])


@router.post("", response_model=RecommendResponse)
async def recommend_ingredients(req: RecommendRequest) -> RecommendResponse:
    if not await session_exists(req.session_id):
        raise HTTPException(status_code=404, detail="Session not found")

    result = await recommend(req.session_id, req.message)

    ingredients = [
        IngredientResult(
            name=r["ingredient"],
            claim=r["claim"],
            eligibility_tier=r["tier"],
            paper_ref=f"PMID:{r['pmid']}" if r.get("pmid") else None,
        )
        for r in result["ingredients"]
    ]

    return RecommendResponse(
        session_id=req.session_id,
        turn_id=result["turn_id"],
        ingredients=ingredients,
        response_text=result["response_text"],
        model_used=result["model_used"],
    )
