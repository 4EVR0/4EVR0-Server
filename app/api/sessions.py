"""Anonymous, same-browser chat sessions; conversation contents live in Redis."""

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from app.core.config import settings
from app.repositories import conversation_repository, conversation_store
from app.services.recommend_service import _reconstruct_products

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])
COOKIE_NAME = "4evr0_session"


class SessionResponse(BaseModel):
    session_id: str


class RestoredTurn(BaseModel):
    user: str
    assistant: str
    products: list[dict]
    ingredients: list[dict]


class CurrentSessionResponse(BaseModel):
    turns: list[RestoredTurn]


def _check_origin(request: Request) -> None:
    """Reject cross-origin browser writes without blocking origin-less API clients."""
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
        raise HTTPException(status_code=403, detail="cross-origin request")


def set_session_cookie(response: Response, request: Request, session_id: str) -> None:
    # An insecure cookie is limited to local HTTP development.
    local_http = request.url.scheme == "http" and request.url.hostname in {"localhost", "127.0.0.1"}
    response.set_cookie(
        COOKIE_NAME,
        session_id,
        max_age=settings.conversation_ttl_seconds,
        httponly=True,
        secure=not local_http,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"


@router.post("", response_model=SessionResponse, status_code=201)
async def create_session(request: Request, response: Response):
    _check_origin(request)
    await conversation_repository.ensure_table()
    session_id = await conversation_repository.create_session()
    set_session_cookie(response, request, session_id)
    return SessionResponse(session_id=session_id)


@router.post("/browser", status_code=201)
async def create_browser_session(request: Request, response: Response):
    """Create a cookie session without exposing its bearer token to JavaScript."""
    _check_origin(request)
    await conversation_repository.ensure_table()
    session_id = await conversation_repository.create_session()
    set_session_cookie(response, request, session_id)
    response.status_code = 201
    return response


@router.get("/current", response_model=CurrentSessionResponse)
async def current_session(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    session_id = request.cookies.get(COOKIE_NAME)
    if not session_id or not await conversation_repository.session_exists(session_id):
        raise HTTPException(status_code=404, detail="session not found")

    history = await conversation_store.load_recent(session_id)
    active = await conversation_store.load_active(session_id)
    turns = []
    for index, entry in enumerate(history):
        products = [p.model_dump() for p in _reconstruct_products(entry.get("products") or [])]
        ingredients = (
            active.get("ingredients") or []
            if index == len(history) - 1 and isinstance(active, dict)
            else []
        )
        turns.append(RestoredTurn(
            user=entry.get("user") or "",
            assistant=entry.get("assistant") or "",
            products=products,
            ingredients=ingredients,
        ))
    return CurrentSessionResponse(turns=turns)


@router.delete("/current", status_code=204)
async def delete_current_session(request: Request, response: Response):
    _check_origin(request)
    session_id = request.cookies.get(COOKIE_NAME)
    if session_id:
        await conversation_repository.delete_session(session_id)
        await conversation_store.clear(session_id)
    response.delete_cookie(COOKIE_NAME, path="/")
    response.headers["Cache-Control"] = "no-store"
