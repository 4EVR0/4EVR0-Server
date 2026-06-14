from fastapi import APIRouter, HTTPException, Query

from app.clients.neo4j_client import query_path_by_effects
from app.repositories import conversation_repository
from app.schemas.recommend import PathResponse, PathResult, PathStep, RecommendRequest, RecommendResponse
from app.services.recommend_service import recommend

router = APIRouter(prefix="/api/v1/recommend", tags=["recommend"])


@router.post("", response_model=RecommendResponse)
async def recommend_endpoint(body: RecommendRequest):
    if not await conversation_repository.session_exists(body.session_id):
        raise HTTPException(status_code=404, detail="session not found")
    return await recommend(body.session_id, body.message)


@router.get("/path", response_model=PathResponse)
async def recommend_path_endpoint(effects: list[str] = Query(...)):
    raw = await query_path_by_effects(effects)
    return PathResponse(
        effects=effects,
        paths=[PathResult(path=[PathStep(**step) for step in r["path"]]) for r in raw],
    )
