"""답변별 버전 추적: 릴리스 식별자와 턴 결과 기록."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from app.schemas.recommend import RecommendResponse
from app.services import recommend_service as service
from app.services import release_info


def test_release_id_changes_with_model_and_graph():
    base = release_info.build({"product_sync": "a", "evidence_for_edges": 884})
    with patch.object(release_info.settings, "gpu_model", "other/model"):
        other_model = release_info.build({"product_sync": "a", "evidence_for_edges": 884})
    other_graph = release_info.build({"product_sync": "b", "evidence_for_edges": 884})
    assert base["release_id"] != other_model["release_id"] != other_graph["release_id"]
    assert release_info.build({"product_sync": "a", "evidence_for_edges": 884})["release_id"] == base["release_id"]
    text = json.dumps(base["components"], ensure_ascii=False)
    assert "password" not in text.lower() and "bolt://" not in text and "http" not in text


def _run(coro, recorded):
    async def fake_record(**kwargs):
        recorded.append(kwargs)

    async def main():
        with patch.object(release_info, "_current", {"release_id": "r1", "components": {}}), \
                patch.object(service.turn_event_repository, "record_turn", fake_record):
            try:
                return await coro()
            finally:
                await asyncio.gather(*list(service._PENDING_RECORDS))
    return asyncio.run(main())


def test_batch_turn_is_recorded_without_text():
    response = RecommendResponse(session_id="s", turn_id="t1", ingredients=[], products=[], response_text="본문",
                                 model_used="m", response_mode="generated")
    recorded = []
    with patch.object(service, "_recommend", AsyncMock(return_value=response)):
        _run(lambda: service.recommend("s", "건조해요"), recorded)
    assert recorded == [{"turn_id": "t1", "release_id": "r1", "transport": "batch", "status": "completed",
                         "response_mode": "generated", "cache_hit": None, "latency_ms": recorded[0]["latency_ms"],
                         "product_ids": []}]


def test_failed_batch_turn_is_recorded():
    recorded = []
    with patch.object(service, "_recommend", AsyncMock(side_effect=RuntimeError("boom"))):
        try:
            _run(lambda: service.recommend("s", "건조해요"), recorded)
        except RuntimeError:
            pass
    assert recorded[0]["status"] == "failed" and recorded[0]["response_mode"] is None


def _frames(*items):
    async def gen(*_args, **_kwargs):
        for event, data in items:
            yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
    return gen


def test_stream_completed_and_cancelled():
    recorded = []
    completed = _frames(("meta", {"turn_id": "t2", "products": [{"product_id": "p1"}]}),
                        ("delta", {"text": "본문"}), ("done", {"response_mode": "generated"}))
    with patch.object(service, "_recommend_stream", completed):
        _run(lambda: _drain(service.recommend_stream("s", "건조해요")), recorded)
    assert recorded[0]["status"] == "completed" and recorded[0]["product_ids"] == ["p1"]
    assert recorded[0]["turn_id"] == "t2" and recorded[0]["transport"] == "stream"

    recorded.clear()
    with patch.object(service, "_recommend_stream", completed):
        _run(lambda: _first_frame(service.recommend_stream("s", "건조해요")), recorded)
    # 끊긴 스트리밍은 완료로 기록하지 않는다.
    assert recorded[0]["status"] == "cancelled" and recorded[0]["response_mode"] is None


def test_not_recorded_without_registered_release():
    response = RecommendResponse(session_id="s", turn_id="t3", ingredients=[], products=[], response_text="본문",
                                 model_used="m", response_mode="generated")
    record = AsyncMock()

    async def main():
        with patch.object(release_info, "_current", None), \
                patch.object(service.turn_event_repository, "record_turn", record), \
                patch.object(service, "_recommend", AsyncMock(return_value=response)):
            await service.recommend("s", "건조해요")
    asyncio.run(main())
    record.assert_not_awaited()


async def _drain(gen):
    return [frame async for frame in gen]


async def _first_frame(gen):
    frame = await gen.__anext__()
    await gen.aclose()
    return frame
