"""콜드스타트 개선(이슈 #36) 유닛 테스트.

- /health readiness 게이트: llm 상태 → unhealthy(503) 매핑
- 캐시 single-flight: 동시 미스 합치기, 리더 실패 시 승계, 비활성 시 통과
- LLM 클라이언트 싱글턴

외부 인프라(vLLM/PG/Neo4j/Redis) 없이 돈다 — 의존성 체크 함수는 mock.
"""

import asyncio
import unittest
from unittest import mock

from fastapi import Response

from app.api import health
from app.clients import llm_factory
from app.repositories import recommend_cache


def _mock_checks(neo4j="ok", pg="ok", redis="ok", llm="ok"):
    return (
        mock.patch.object(health, "_check_neo4j", mock.AsyncMock(return_value=neo4j)),
        mock.patch.object(health, "_check_postgresql", mock.AsyncMock(return_value=pg)),
        mock.patch.object(health, "_check_redis", mock.AsyncMock(return_value=redis)),
        mock.patch.object(health, "_check_llm", mock.AsyncMock(return_value=llm)),
    )


class HealthReadinessGateTest(unittest.IsolatedAsyncioTestCase):
    async def _call(self, **kwargs):
        response = Response()
        patches = _mock_checks(**kwargs)
        for p in patches:
            p.start()
        try:
            result = await health.health_check(response)
        finally:
            for p in patches:
                p.stop()
        return result, response

    async def test_all_ok_is_healthy(self):
        result, response = await self._call()
        self.assertEqual("healthy", result.status)
        self.assertEqual(200, response.status_code)

    async def test_llm_down_is_unhealthy_503(self):
        # 콜드 vLLM(모델 로드 중)이면 LB가 라우팅에서 빼도록 503이어야 한다.
        result, response = await self._call(llm="error")
        self.assertEqual("unhealthy", result.status)
        self.assertEqual("error", result.dependencies.llm)
        self.assertEqual(503, response.status_code)

    async def test_other_dep_down_is_degraded_200(self):
        result, response = await self._call(redis="error")
        self.assertEqual("degraded", result.status)
        self.assertEqual(200, response.status_code)


class SingleFlightTest(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_misses_coalesce_to_one_compute(self):
        store: dict[str, str] = {}
        computes = 0

        async def worker():
            nonlocal computes
            async with recommend_cache.single_flight("지성 피부 고민", None):
                if "k" in store:
                    return store["k"]  # coalesced hit
                computes += 1
                await asyncio.sleep(0.01)  # GPU 계산 흉내
                store["k"] = "결과"
                return store["k"]

        results = await asyncio.gather(*[worker() for _ in range(10)])
        self.assertEqual(1, computes)
        self.assertEqual(["결과"] * 10, results)
        # 전원 종료 후 락 딕셔너리가 비워져야 한다(누수 방지)
        self.assertEqual({}, recommend_cache._flights)

    async def test_leader_failure_promotes_next_waiter(self):
        computes = 0

        async def failing_leader():
            async with recommend_cache.single_flight("m", None):
                await asyncio.sleep(0.01)
                raise RuntimeError("GPU 실패")

        async def follower():
            nonlocal computes
            await asyncio.sleep(0.005)  # 리더가 먼저 락을 잡게
            async with recommend_cache.single_flight("m", None):
                computes += 1

        results = await asyncio.gather(failing_leader(), follower(), return_exceptions=True)
        self.assertIsInstance(results[0], RuntimeError)
        self.assertEqual(1, computes)
        self.assertEqual({}, recommend_cache._flights)

    async def test_different_keys_do_not_serialize(self):
        inside = asyncio.Event()

        async def a():
            async with recommend_cache.single_flight("a", None):
                inside.set()
                await asyncio.sleep(0.05)

        async def b():
            async with recommend_cache.single_flight("b", None):
                # a가 락을 쥔 동안에도 b는 들어와야 한다
                await asyncio.wait_for(inside.wait(), timeout=0.01)

        await asyncio.gather(a(), b())

    async def test_disabled_cache_passes_through(self):
        with mock.patch.object(recommend_cache.settings, "recommend_cache_enabled", False):
            overlap = asyncio.Event()

            async def a():
                async with recommend_cache.single_flight("m", None):
                    overlap.set()
                    await asyncio.sleep(0.05)

            async def b():
                await asyncio.sleep(0.005)
                async with recommend_cache.single_flight("m", None):
                    # 캐시 비활성이면 같은 키도 직렬화하지 않는다
                    self.assertTrue(overlap.is_set())

            await asyncio.gather(a(), b())
        self.assertEqual({}, recommend_cache._flights)


class LLMClientSingletonTest(unittest.IsolatedAsyncioTestCase):
    async def test_client_is_reused_and_resettable(self):
        c1 = llm_factory.get_async_llm_client()
        c2 = llm_factory.get_async_llm_client()
        self.assertIs(c1, c2)
        await llm_factory.close_llm_client()
        c3 = llm_factory.get_async_llm_client()
        self.assertIsNot(c1, c3)
        await llm_factory.close_llm_client()


if __name__ == "__main__":
    unittest.main()
