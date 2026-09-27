import json

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.clients.neo4j_client import query_path_by_effects
from app.repositories import conversation_repository
from app.api.sessions import COOKIE_NAME
from app.schemas.recommend import PathResponse, PathResult, PathStep, RecommendRequest, RecommendResponse
from app.services.recommend_service import recommend, recommend_stream

router = APIRouter(prefix="/api/v1/recommend", tags=["recommend"])


async def _resolve_session(body: RecommendRequest, request: Request) -> str:
    cookie_id = request.cookies.get(COOKIE_NAME)
    if cookie_id and body.session_id and cookie_id != body.session_id:
        raise HTTPException(status_code=403, detail="session mismatch")
    session_id = cookie_id or body.session_id
    if not session_id:
        raise HTTPException(status_code=401, detail="session required")
    if not await conversation_repository.session_exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    return session_id


async def _browser_stream(stream):
    """Keep the legacy API stream intact while omitting bearer IDs in browser SSE."""
    async for frame in stream:
        if frame.startswith("event: meta\n"):
            event_line, data_line = frame.split("\n", 2)[:2]
            payload = json.loads(data_line.removeprefix("data: "))
            payload.pop("session_id", None)
            yield f"{event_line}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        else:
            yield frame


@router.post("", response_model=RecommendResponse)
async def recommend_endpoint(body: RecommendRequest, request: Request):
    session_id = await _resolve_session(body, request)
    result = await recommend(session_id, body.message)
    if request.cookies.get(COOKIE_NAME):
        return JSONResponse(result.model_dump(exclude={"session_id"}), headers={"Cache-Control": "no-store"})
    return result


@router.post("/stream")
async def recommend_stream_endpoint(body: RecommendRequest, request: Request):
    """SSE 추천 — meta(구조 데이터 즉시) → delta(검증된 본문) → done."""
    session_id = await _resolve_session(body, request)
    stream = recommend_stream(session_id, body.message)
    if request.cookies.get(COOKIE_NAME):
        stream = _browser_stream(stream)
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/path", response_model=PathResponse)
async def recommend_path_endpoint(effects: list[str] = Query(...)):
    raw = await query_path_by_effects(effects)
    return PathResponse(
        effects=effects,
        paths=[PathResult(path=[PathStep(**step) for step in r["path"]]) for r in raw],
    )
