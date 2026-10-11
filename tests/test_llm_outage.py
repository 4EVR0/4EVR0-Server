"""생성 서버(GPU) 장애: 빠르게 차단하고, 확인된 근거로 안내하며, 그 응답은 캐시하지 않는다."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import openai
import pytest

from app.clients import llm_gate, llm_health
from app.clients.llm_health import LLMUnavailableError
from app.repositories import recommend_cache
from app.services import recommend_service as service


@pytest.fixture(autouse=True)
def _reset_breaker():
    llm_health.reset()
    yield
    llm_health.reset()


def _connection_error():
    return openai.APIConnectionError(request=httpx.Request("POST", "http://gpu/v1/chat/completions"))


def test_connection_failure_opens_breaker_and_success_closes_it():
    llm_health.record_failure(_connection_error())
    assert llm_health.is_down()
    with pytest.raises(LLMUnavailableError):
        llm_health.check()
    llm_health.record_success()
    assert not llm_health.is_down()


def test_other_errors_do_not_open_breaker():
    llm_health.record_failure(ValueError("bad json"))
    assert not llm_health.is_down()


def test_gate_skips_immediately_while_down():
    llm_health.record_failure(_connection_error())

    async def use_slot():
        async with llm_gate.llm_slot():
            raise AssertionError("must not enter")
    with pytest.raises(LLMUnavailableError):
        asyncio.run(use_slot())


def test_gate_opens_breaker_on_connection_error():
    async def use_slot():
        async with llm_gate.llm_slot():
            raise _connection_error()
    with pytest.raises(openai.APIConnectionError):
        asyncio.run(use_slot())
    assert llm_health.is_down()


def test_llm_unavailable_response_is_not_cached():
    client = SimpleNamespace(set=AsyncMock())
    with patch.object(recommend_cache, "_get_client", return_value=client), \
            patch.object(recommend_cache.settings, "recommend_cache_enabled", True):
        asyncio.run(recommend_cache.set("건조해요", None, {"response_mode": "llm_unavailable", "_profile": {}}))
        client.set.assert_not_awaited()
        asyncio.run(recommend_cache.set("건조해요", None, {"response_mode": "generated", "_profile": {}}))
        client.set.assert_awaited_once()


def test_generation_failure_raises_for_caller_fallback():
    llm_health.record_failure(_connection_error())
    with pytest.raises(LLMUnavailableError):
        asyncio.run(service._build_llm_response("건조해요", [], [], "system"))


def test_followup_generation_failure_answers_with_notice():
    from tests.test_followup_quality import _active
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=_connection_error()))))
    patches = [patch.object(service, "get_async_llm_client", return_value=client),
               patch.object(service, "query_ingredient_kor_names", AsyncMock(return_value={})),
               patch.object(service, "query_supported_claims", AsyncMock(return_value={})),
               patch.object(service, "query_product_ingredient_inventory", AsyncMock(return_value={})),
               patch.object(service, "query_product_fragrance_evidence", AsyncMock(return_value=[])),
               patch.object(service, "_store_turn", AsyncMock())]
    for p in patches:
        p.start()
    try:
        response = asyncio.run(service._handle_followup("s", "t2", "왜 이 제품들을 추천했어?", [], _active()))
    finally:
        for p in patches:
            p.stop()
    assert response.response_mode == "followup_llm_unavailable"
    assert response.response_text.startswith(service.LLM_UNAVAILABLE_NOTE)
    assert [p.product_id for p in response.products] == ["t", "c"]
    assert llm_health.is_down()
