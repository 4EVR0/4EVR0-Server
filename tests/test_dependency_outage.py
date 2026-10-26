"""저장소 의존성(Redis·Neo4j·PostgreSQL) 장애: 빨리 실패하고, 사실과 다른 안내를 하지 않고, 캐시하지 않는다."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.clients import dependency_health
from app.clients.dependency_health import DependencyUnavailableError, NEO4J, POSTGRES, REDIS
from app.repositories import conversation_store, recommend_cache
from app.services import recommend_service as service


def test_breaker_opens_on_failure_and_marks_request():
    dependency_health.start_request()
    REDIS.failed(ConnectionError("down"))
    assert REDIS.is_open() and dependency_health.degraded("redis")
    with pytest.raises(DependencyUnavailableError):
        REDIS.check()
    REDIS.reset()
    assert not REDIS.is_open()


def test_skipped_call_does_not_extend_cooldown():
    REDIS.failed(ConnectionError("down"))
    until = REDIS._until
    REDIS.failed(DependencyUnavailableError("redis"))
    assert REDIS._until == until


def test_redis_calls_are_skipped_while_open():
    client = SimpleNamespace(get=AsyncMock(), lrange=AsyncMock())
    REDIS.failed(ConnectionError("down"))
    with patch.object(recommend_cache, "_get_client", return_value=client), \
            patch.object(recommend_cache.settings, "recommend_cache_enabled", True), \
            patch.object(conversation_store.settings, "conversation_enabled", True):
        assert asyncio.run(recommend_cache.get("건조해요", None)) is None
        assert asyncio.run(conversation_store.load_recent("s")) == []
    client.get.assert_not_awaited()
    client.lrange.assert_not_awaited()


def test_neo4j_query_fails_fast_while_open():
    from app.clients import neo4j_client
    NEO4J.failed(ConnectionError("down"))
    assert asyncio.run(neo4j_client.query_ingredient_kor_names(["UREA"])) in ({}, [])
    assert dependency_health.degraded("neo4j")


def test_degraded_request_is_not_cached():
    client = SimpleNamespace(set=AsyncMock())

    async def run():
        dependency_health.start_request()
        NEO4J.failed(ConnectionError("down"))
        await recommend_cache.set("건조해요", None, {"response_mode": "generated", "_profile": {}})
    with patch.object(recommend_cache, "_get_client", return_value=client), \
            patch.object(recommend_cache.settings, "recommend_cache_enabled", True):
        asyncio.run(run())
    client.set.assert_not_awaited()


def test_graph_unavailable_response_is_not_no_products():
    with patch.object(service, "_store_turn", AsyncMock()):
        response = asyncio.run(service._graph_unavailable_response("s", "t", "건조해요"))
    assert response.response_mode == "graph_unavailable" and response.products == []
    assert "조건에 맞는" not in response.response_text


def test_history_unavailable_when_redis_down():
    async def run():
        dependency_health.start_request()
        REDIS.failed(ConnectionError("down"))
        with patch.object(service.conversation_store, "load_recent", AsyncMock(return_value=[])), \
                patch.object(service.conversation_store, "load_active", AsyncMock(return_value=None)):
            return await service._resolve_conversation_response("s", "t", "1번 제품 어때?")
    response = asyncio.run(run())
    assert response.response_text == service.HISTORY_UNAVAILABLE_TEXT
    assert response.response_mode == "history_unavailable"


def test_session_create_returns_503_when_postgres_down():
    from app.api import sessions
    POSTGRES.failed(ConnectionError("down"))
    with pytest.raises(HTTPException) as err:
        asyncio.run(sessions._create_session_or_503())
    assert err.value.status_code == 503 and "대화를 시작할 수 없어요" in err.value.detail


def test_followup_without_products_during_graph_outage_says_unavailable():
    NEO4J.failed(ConnectionError("down"))
    with patch.object(service, "_store_turn", AsyncMock()):
        response = asyncio.run(service._handle_followup(
            "s", "t", "닥터지 레드 블레미쉬 수딩크림 어때?", [{"user": "건조해요", "products": []}], None))
    assert response.response_mode == "graph_unavailable"


def test_query_errors_do_not_open_breaker():
    import neo4j.exceptions
    dependency_health.start_request()
    NEO4J.failed(neo4j.exceptions.CypherSyntaxError("bad query"))
    NEO4J.failed(ValueError("bad row"))
    assert not NEO4J.is_open() and not dependency_health.degraded("neo4j")
    NEO4J.failed(neo4j.exceptions.ServiceUnavailable("down"))
    assert NEO4J.is_open() and dependency_health.degraded("neo4j")
