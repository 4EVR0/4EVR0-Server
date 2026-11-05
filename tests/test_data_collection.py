"""수집 안내 이후 저장·학습 제외·개인정보 감지·삭제 경계."""
import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.recommend import RecommendResponse
from app.services import consent_policy, data_collection as collection

SUBJECT = "a" * 32
VERSION = consent_policy.POLICY_VERSION


def client():
    c = TestClient(app)
    c.cookies.set(collection.SUBJECT_COOKIE, SUBJECT)
    c.cookies.set("4evr0_session", "sess-1")
    return c


def test_policy_available_without_database_or_cookie():
    with patch.object(collection.repo, "current_consents", AsyncMock(side_effect=RuntimeError)) as old:
        r = TestClient(app).get("/api/v1/data-policy")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "set-cookie" not in r.headers
    d = r.json()
    assert d["policy_version"] == VERSION and d["retention_days"] == 30
    assert d["collection_mode"] == "notice" and d["training_use"] is False
    assert "[선택]" not in json.dumps(d, ensure_ascii=False)
    assert "학습 데이터로 사용하지 않습니다" in d["texts"]["training_use"]
    old.assert_not_called()


def test_retired_consent_api_does_not_fabricate_grants():
    with patch.object(collection.repo, "record_consents", AsyncMock()) as record:
        assert client().get("/api/v1/consents").status_code == 410
        assert client().put("/api/v1/consents", json={"quality_storage": True, "training_use": True}).status_code == 410
    record.assert_not_called()


def test_feedback_requires_owner():
    with patch.object(collection, "owns_turn", AsyncMock(return_value=False)):
        assert client().post("/api/v1/feedback", json={"turn_id": "t1", "helpful": True}).status_code == 404


@pytest.mark.parametrize("version,note,expected", [
    (VERSION, "제품 설명이 달라요", "제품 설명이 달라요"),
    (None, "제품 설명이 달라요", None),
    (VERSION, "연락처 010-1234-5678", None),
    (VERSION, "병원에서 진단받았어요", None),
])
def test_feedback_note_respects_notice_and_private_input(version, note, expected):
    upsert = AsyncMock()
    with patch.object(collection, "owns_turn", AsyncMock(return_value=True)), \
            patch.object(collection.repo, "upsert_feedback", upsert):
        r = client().post("/api/v1/feedback", json={"turn_id": "t1", "helpful": False,
            "issues": ["wrong_info"], "note": note, "collection_policy_version": version})
    assert r.status_code == 204
    assert upsert.await_args.kwargs["note"] == expected
    assert upsert.await_args.kwargs["retention_days"] == 30


def test_unknown_feedback_rejected():
    assert client().post("/api/v1/feedback", json={"turn_id": "t1", "helpful": False, "issues": ["nope"]}).status_code == 422


def test_delete_does_not_create_consent_decision():
    with patch.object(collection.repo, "record_consents", AsyncMock()) as record, \
            patch.object(collection.repo, "delete_subject_data", AsyncMock(return_value={"turn_records": 2, "feedback": 1})):
        r = client().delete("/api/v1/my-data")
    assert r.json()["deleted"] == {"turn_records": 2, "feedback": 1}
    record.assert_not_called()


def run_after_turn(version=VERSION, message="건조해서 보습 제품을 찾고 있어요", answer="보습 성분을 확인하세요"):
    store = AsyncMock()
    async def run():
        with patch.object(collection, "_remember_owner", AsyncMock()), \
                patch.object(collection.repo, "current_consents", AsyncMock(side_effect=RuntimeError)), \
                patch.object(collection.repo, "store_turn", store):
            collection.after_turn("sess-1", SUBJECT, message, "batch", {
                "turn_id": "t1", "response_mode": "generated", "response_text": answer,
                "products": [], "ingredients": []}, version)
            await asyncio.gather(*list(collection._pending))
    asyncio.run(run())
    return store


def test_notice_collects_new_turns_without_consent_lookup_and_never_for_training():
    store = run_after_turn()
    assert store.await_count == 1
    d = store.await_args.kwargs
    assert d["policy_version"] == VERSION and d["retention_days"] == 30
    assert d["training_use"] is False


@pytest.mark.parametrize("version", [None, "2026-10-26", "unknown"])
def test_old_or_unknown_notice_does_not_silently_collect(version):
    assert run_after_turn(version).await_count == 0


@pytest.mark.parametrize("text", ["hello@example.com", "010-1234-5678", "01012345678",
    "900101-1234567", "제 이름은 홍길동이에요", "주소: 서울시", "피부과에서 진단받았어요",
    "약을 복용 중이에요", "치료 이력이 있어요", "임신 중이에요"])
def test_detected_private_or_medical_text_not_stored(text):
    assert run_after_turn(message=text).await_count == 0
    assert run_after_turn(answer=text).await_count == 0


@pytest.mark.parametrize("transport", ["batch", "stream"])
def test_current_notice_gets_cookie_and_reaches_collection(transport):
    from app.api import recommend as api
    seen = Mock()
    result = RecommendResponse(session_id="sess-1", turn_id="t1", ingredients=[], products=[],
                               response_text="보습 성분 안내", model_used="test")
    async def stream(*args, **kwargs):
        yield 'event: meta\ndata: {"turn_id":"t1","products":[],"ingredients":[]}\n\n'
        yield 'event: delta\ndata: {"text":"보습 성분 안내"}\n\n'
        yield 'event: done\ndata: {"response_mode":"generated"}\n\n'
    c = TestClient(app, base_url="https://testserver")
    c.cookies.set("4evr0_session", "sess-1")
    with patch.object(api.conversation_repository, "session_exists", AsyncMock(return_value=True)), \
            patch.object(api, "recommend", AsyncMock(return_value=result)), \
            patch.object(api, "recommend_stream", stream), patch.object(collection, "after_turn", seen):
        r = c.post("/api/v1/recommend" + ("/stream" if transport == "stream" else ""),
                   json={"message": "보습", "collection_policy_version": VERSION})
    assert r.status_code == 200
    assert all(v in r.headers["set-cookie"] for v in ["4evr0_subject=", "Secure", "HttpOnly", "SameSite=strict"])
    assert len(seen.call_args.args[1]) == 32 and seen.call_args.args[-1] == VERSION


def test_stream_only_collects_after_done():
    from app.api import recommend as api
    async def frames(complete):
        yield 'event: meta\ndata: {"turn_id":"t1","products":[],"ingredients":[]}\n\n'
        yield 'event: delta\ndata: {"text":"답변"}\n\n'
        if complete:
            yield 'event: done\ndata: {"response_mode":"generated"}\n\n'
    async def drain(complete):
        async for _ in api._collect_stream(frames(complete), "s", SUBJECT, "질문", VERSION):
            pass
    with patch.object(collection, "after_turn", Mock()) as seen:
        asyncio.run(drain(False))
        seen.assert_not_called()
        asyncio.run(drain(True))
        assert seen.call_args.args[4]["response_text"] == "답변"
        assert seen.call_args.args[-1] == VERSION
