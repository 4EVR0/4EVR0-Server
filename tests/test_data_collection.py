"""동의·피드백·내 데이터 API와 턴 원문 수집."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.services import data_collection as collection

SUBJECT = "a" * 32


def client():
    c = TestClient(app)
    c.cookies.set(collection.SUBJECT_COOKIE, SUBJECT)
    c.cookies.set("4evr0_session", "sess-1")
    return c


def test_get_consents_returns_texts_and_state():
    with patch.object(collection.repo, "current_consents", AsyncMock(return_value={"quality_storage": True})):
        data = client().get("/api/v1/consents").json()
    assert data["decided"] and data["quality_storage"] and not data["training_use"]
    assert data["retention_days"] == 30 and "quality_storage" in data["texts"]


def test_training_requires_quality_storage():
    response = client().put("/api/v1/consents", json={"quality_storage": False, "training_use": True})
    assert response.status_code == 422


def test_put_consents_records_both_kinds_and_sets_subject_cookie():
    record = AsyncMock()
    c = TestClient(app)
    with patch.object(collection.repo, "record_consents", record), \
            patch.object(collection.repo, "withdraw_training_use", AsyncMock()):
        response = c.put("/api/v1/consents", json={"quality_storage": True, "training_use": False})
    assert response.status_code == 200
    subject, decisions, version = record.await_args.args
    assert decisions == {"quality_storage": True, "training_use": False} and len(subject) == 32
    assert collection.SUBJECT_COOKIE in response.cookies


def test_feedback_requires_turn_owner():
    with patch.object(collection, "owns_turn", AsyncMock(return_value=False)):
        response = client().post("/api/v1/feedback", json={"turn_id": "t1", "helpful": True})
    assert response.status_code == 404


def test_feedback_note_only_stored_with_quality_consent():
    upsert = AsyncMock()
    body = {"turn_id": "t1", "helpful": False, "issues": ["wrong_info"], "note": "제품 성분이 달라요"}
    for granted, expected_note in (({}, None), ({"quality_storage": True}, "제품 성분이 달라요")):
        with patch.object(collection, "owns_turn", AsyncMock(return_value=True)), \
                patch.object(collection.repo, "current_consents", AsyncMock(return_value=granted)), \
                patch.object(collection.repo, "upsert_feedback", upsert):
            assert client().post("/api/v1/feedback", json=body).status_code == 204
        kwargs = upsert.await_args.kwargs
        assert kwargs["note"] == expected_note and kwargs["issues"] == ["wrong_info"]


def test_unknown_feedback_issue_rejected():
    with patch.object(collection, "owns_turn", AsyncMock(return_value=True)):
        response = client().post("/api/v1/feedback", json={"turn_id": "t1", "helpful": False, "issues": ["nope"]})
    assert response.status_code == 422


def test_delete_my_data_withdraws_and_deletes():
    record, delete = AsyncMock(), AsyncMock(return_value={"turn_records": 2, "feedback": 1})
    with patch.object(collection.repo, "record_consents", record), \
            patch.object(collection.repo, "delete_subject_data", delete):
        data = client().delete("/api/v1/my-data").json()
    assert data["deleted"] == {"turn_records": 2, "feedback": 1}
    assert record.await_args.args[1] == {"quality_storage": False, "training_use": False}


def _run_after_turn(granted):
    store = AsyncMock()

    async def main():
        with patch.object(collection, "_remember_owner", AsyncMock()), \
                patch.object(collection.repo, "current_consents", AsyncMock(return_value=granted)), \
                patch.object(collection.repo, "store_turn", store):
            collection.after_turn("sess-1", SUBJECT, "건조해요", "batch",
                                  {"turn_id": "t1", "response_mode": "generated", "response_text": "답변",
                                   "products": [{"product_id": "p1", "product_name": "크림", "extra": "x"}],
                                   "ingredients": []})
            await asyncio.gather(*list(collection._pending))
    asyncio.run(main())
    return store


def test_turn_text_stored_only_with_consent():
    assert not _run_after_turn({}).await_count
    store = _run_after_turn({"quality_storage": True, "training_use": True})
    kwargs = store.await_args.kwargs
    assert kwargs["question"] == "건조해요" and kwargs["answer"] == "답변" and kwargs["training_use"] is True
    assert kwargs["products"] == [{"product_id": "p1", "product_name": "크림", "brand": None, "category": None,
                                   "matched_ingredients": None}]


def test_stream_collects_only_completed_turns():
    from app.api import recommend as api
    seen = []

    def frames(*events):
        async def gen():
            for event, data in events:
                yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        return gen()

    async def drain(stream, close_early=False):
        out = []
        async for frame in stream:
            out.append(frame)
            if close_early:
                break
        return out

    with patch.object(api.collection, "after_turn", lambda *a: seen.append(a)):
        asyncio.run(drain(api._collect_stream(frames(("meta", {"turn_id": "t2", "products": [], "ingredients": []}),
                                                     ("delta", {"text": "본"}), ("delta", {"text": "문"}),
                                                     ("done", {"response_mode": "generated"})), "s", SUBJECT, "질문")))
        assert seen[-1][4]["response_text"] == "본문" and seen[-1][3] == "stream"
        seen.clear()
        asyncio.run(drain(api._collect_stream(frames(("meta", {"turn_id": "t3"}), ("delta", {"text": "본"})),
                                              "s", SUBJECT, "질문")))
        assert seen == []
