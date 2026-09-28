import asyncio
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from app.api import health


class HealthProbeTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, path):
        app = FastAPI()
        app.include_router(health.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(path)

    async def test_live_does_not_probe(self):
        with patch.object(health, "_dependencies", new_callable=AsyncMock) as probe:
            response = await self.request("/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        probe.assert_not_called()

    async def test_ready_requires_every_dependency(self):
        names = ("neo4j", "postgresql", "redis", "llm")
        for failed in (None, *names):
            with self.subTest(failed=failed), ExitStack() as stack:
                for name in names:
                    stack.enter_context(patch.object(health, f"_check_{name}", AsyncMock(return_value="error" if failed == name else "ok")))
                response = await self.request("/ready")
            self.assertEqual(response.status_code, 503 if failed else 200)
            self.assertEqual(response.json()["status"], "not_ready" if failed else "ready")
            self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_probe_timeout_and_exception_are_errors(self):
        async def slow():
            await asyncio.sleep(1)
        with patch.object(health, "_PROBE_TIMEOUT_SECONDS", 0.01):
            self.assertEqual(await health._probe("slow", slow), "error")
        self.assertEqual(await health._probe("broken", AsyncMock(side_effect=RuntimeError())), "error")

    async def test_configured_model_required(self):
        cases = [({"data": [{"id": health.settings.gpu_model}]}, "ok"),
                 ({"data": [{"id": "wrong-model"}]}, "error"),
                 ({"data": []}, "error"), ({"data": None}, "error"), ([], "error")]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                client = AsyncMock()
                client.__aenter__.return_value = client
                client.get.return_value = httpx.Response(200, json=payload)
                with patch.object(health.httpx, "AsyncClient", return_value=client):
                    self.assertEqual(await health._check_llm(), expected)

    async def test_legacy_degraded_status_preserved(self):
        deps = health.DependencyStatus(neo4j="error", postgresql="ok", redis="ok", llm="ok")
        with patch.object(health, "_dependencies", AsyncMock(return_value=deps)):
            response = await self.request("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "degraded")

    async def test_postgresql_connection_closed_after_query_failure(self):
        connection = AsyncMock()
        connection.fetchval.side_effect = RuntimeError("query failed")
        with patch.object(health.asyncpg, "connect", AsyncMock(return_value=connection)):
            self.assertEqual(await health._check_postgresql(), "error")
        connection.close.assert_awaited_once_with(timeout=1)

    async def test_dependencies_probe_concurrently(self):
        started = 0
        all_started = asyncio.Event()

        async def check():
            nonlocal started
            started += 1
            if started == 4:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=1)
            return "ok"

        with ExitStack() as stack:
            for name in ("neo4j", "postgresql", "redis", "llm"):
                stack.enter_context(patch.object(health, f"_check_{name}", check))
            result = await health._dependencies()
        self.assertTrue(all(value == "ok" for value in result.model_dump().values()))
