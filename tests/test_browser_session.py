"""Browser refresh/new-chat boundaries without PostgreSQL, Redis, or GPU."""

import asyncio
from functools import wraps

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import recommend, sessions
from app.schemas.recommend import RecommendResponse


def async_test(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return run


@pytest.fixture
def session_backend(monkeypatch):
    live = set()
    turns = {}
    active = {}
    next_id = 0

    async def ensure_table():
        pass

    async def create_session():
        nonlocal next_id
        next_id += 1
        sid = f"opaque-session-{next_id}"
        live.add(sid)
        return sid

    async def session_exists(sid):
        return sid in live

    async def delete_session(sid):
        live.discard(sid)

    async def load_recent(sid):
        return turns.get(sid, [])

    async def load_active(sid):
        return active.get(sid)

    async def clear(sid):
        turns.pop(sid, None)
        active.pop(sid, None)

    monkeypatch.setattr(sessions.conversation_repository, "ensure_table", ensure_table)
    monkeypatch.setattr(sessions.conversation_repository, "create_session", create_session)
    monkeypatch.setattr(sessions.conversation_repository, "session_exists", session_exists)
    monkeypatch.setattr(sessions.conversation_repository, "delete_session", delete_session)
    monkeypatch.setattr(sessions.conversation_store, "load_recent", load_recent)
    monkeypatch.setattr(sessions.conversation_store, "load_active", load_active)
    monkeypatch.setattr(sessions.conversation_store, "clear", clear)
    return live, turns, active


@pytest.fixture
def app():
    api = FastAPI()
    api.include_router(sessions.router)
    api.include_router(recommend.router)
    return api


@async_test
async def test_refresh_restores_only_own_turns(app, session_backend):
    _, turns, active = session_backend
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as first:
        created = await first.post("/api/v1/sessions")
        assert created.status_code == 201
        sid = created.json()["session_id"]
        cookie = created.headers["set-cookie"]
        assert "httponly" in cookie.lower()
        assert "samesite=strict" in cookie.lower()
        assert "Max-Age=7200" in cookie
        assert "Secure" not in cookie  # local HTTP only

        turns[sid] = [{"user": "토너만 추천해줘", "assistant": "이 토너가 맞아요.", "products": [], "concerns": []}]
        active[sid] = {"ingredients": [{"name": "NIACINAMIDE"}]}
        restored = await first.get("/api/v1/sessions/current")
        assert restored.status_code == 200
        assert restored.json() == {"turns": [{
            "user": "토너만 추천해줘", "assistant": "이 토너가 맞아요.",
            "products": [], "ingredients": [{"name": "NIACINAMIDE"}],
        }]}
        assert "session_id" not in restored.json()

        browser_created = await first.post("/api/v1/sessions/browser")
        assert browser_created.status_code == 201
        assert browser_created.text == ""
        assert "4evr0_session=" in browser_created.headers["set-cookie"]

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as second:
            assert (await second.get("/api/v1/sessions/current")).status_code == 404
            other = await second.post("/api/v1/sessions")
            assert other.json()["session_id"] != sid
            assert (await second.get("/api/v1/sessions/current")).json() == {"turns": []}


@async_test
async def test_new_chat_deletes_old_context(app, session_backend):
    live, turns, active = session_backend
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        sid = (await client.post("/api/v1/sessions")).json()["session_id"]
        turns[sid] = [{"user": "old", "assistant": "old", "products": []}]
        active[sid] = {"visible_products": ["old"]}
        deleted = await client.delete("/api/v1/sessions/current")
        assert deleted.status_code == 204
        assert sid not in live and sid not in turns and sid not in active
        assert (await client.get("/api/v1/sessions/current")).status_code == 404
        new_sid = (await client.post("/api/v1/sessions")).json()["session_id"]
        assert new_sid != sid
        assert (await client.get("/api/v1/sessions/current")).json() == {"turns": []}


@async_test
async def test_cookie_cannot_be_overridden_by_body(app, session_backend, monkeypatch):
    async def fake_recommend(sid, message):
        return RecommendResponse(session_id=sid, turn_id="1", ingredients=[], products=[],
                                 response_text=message, model_used="test")

    monkeypatch.setattr(recommend, "recommend", fake_recommend)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        sid = (await client.post("/api/v1/sessions")).json()["session_id"]
        mismatch = await client.post("/api/v1/recommend", json={"session_id": "another", "message": "hi"})
        assert mismatch.status_code == 403
        okay = await client.post("/api/v1/recommend", json={"message": "hi"})
        assert okay.status_code == 200
        assert "session_id" not in okay.json()
        assert okay.json()["response_text"] == "hi"


@async_test
async def test_missing_or_expired_session_is_not_reused(app, session_backend):
    live, _, _ = session_backend
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        sid = (await client.post("/api/v1/sessions")).json()["session_id"]
        live.discard(sid)
        assert (await client.get("/api/v1/sessions/current")).status_code == 404
        assert (await client.post("/api/v1/recommend", json={"message": "그 중에서"})).status_code == 404


@async_test
async def test_cross_origin_session_write_is_rejected(app, session_backend):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        result = await client.post("/api/v1/sessions", headers={"Origin": "https://attacker.example"})
        assert result.status_code == 403


@async_test
async def test_browser_stream_hides_session_id_and_api_keeps_legacy_id(app, session_backend, monkeypatch):
    async def fake_stream(sid, message):
        yield f'event: meta\ndata: {{"session_id":"{sid}","products":[]}}\n\n'
        yield 'event: done\ndata: {}\n\n'

    monkeypatch.setattr(recommend, "recommend_stream", fake_stream)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        await client.post("/api/v1/sessions/browser")
        browser = await client.post("/api/v1/recommend/stream", json={"message": "hi"})
        assert browser.status_code == 200
        assert "session_id" not in browser.text
        assert "event: done" in browser.text

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://example.com") as client:
        created = await client.post("/api/v1/sessions")
        assert "Secure" in created.headers["set-cookie"]
        sid = created.json()["session_id"]
        client.cookies.clear()
        legacy = await client.post("/api/v1/recommend/stream", json={"session_id": sid, "message": "hi"})
        assert legacy.status_code == 200
        assert f'"session_id":"{sid}"' in legacy.text
