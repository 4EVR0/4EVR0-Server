"""Deterministic 2–3-product comparison on graph-confirmed ingredients."""

import json
import unittest
from unittest import mock

from app.clients import neo4j_client
from app.schemas.recommend import IngredientResult, ProductResult
from app.services import recommend_service as service


def _product(product_id: str, name: str, brand: str, category: str, matched: list[str]) -> dict:
    return {
        "product_id": product_id, "name": name, "brand": brand, "category": category,
        "matched_count": len(matched), "matched_ingredients": matched,
    }


PRODUCTS = [
    _product("p1", "네오젠 진정 토너", "네오젠", "토너", ["PANTHENOL"]),
    _product("p2", "도미나스 앰플", "도미나스", "앰플", ["PANTHENOL", "NIACINAMIDE"]),
    _product("p3", "라운드랩 크림", "라운드랩", "크림", ["CERAMIDE NP"]),
]
INVENTORY = {
    "p1": [
        {"name": "PANTHENOL", "kor_name": "판테놀"},
        {"name": "ALLANTOIN", "kor_name": "알란토인"},
    ],
    "p2": [
        {"name": "PANTHENOL", "kor_name": "판테놀"},
        {"name": "NIACINAMIDE", "kor_name": "나이아신아마이드"},
    ],
    "p3": [
        {"name": "PANTHENOL", "kor_name": "판테놀"},
        {"name": "CERAMIDE NP", "kor_name": "세라마이드엔피"},
    ],
}


class ComparisonSelectionTest(unittest.TestCase):
    def test_unique_brands_select_two_even_when_product_name_contains_concern(self):
        history = [{"user": "진정 추천해줘", "products": PRODUCTS}]
        self.assertEqual("followup", service._heuristic_kind(
            "네오젠 진정 토너와 도미나스 앰플 비교해줘", history,
        ))
        selected, error = service._select_comparison_products("네오젠과 도미나스 비교해줘", PRODUCTS)
        self.assertIsNone(error)
        self.assertEqual(["p1", "p2"], [p["product_id"] for p in selected])

    def test_three_products_and_numbered_selection(self):
        selected, error = service._select_comparison_products("1번, 3번 비교해줘", PRODUCTS)
        self.assertIsNone(error)
        self.assertEqual(["p1", "p3"], [p["product_id"] for p in selected])
        selected, error = service._select_comparison_products("제품 1과 제품 3 비교", PRODUCTS)
        self.assertIsNone(error)
        self.assertEqual(["p1", "p3"], [p["product_id"] for p in selected])
        selected, error = service._select_comparison_products("이 중에서 비교해줘", PRODUCTS)
        self.assertIsNone(error)
        self.assertEqual(3, len(selected))

    def test_more_than_three_requires_explicit_choice(self):
        four = PRODUCTS + [_product("p4", "브랜드 D 세럼", "브랜드 D", "세럼", ["X"])]
        selected, error = service._select_comparison_products("추천 제품 비교해줘", four)
        self.assertEqual([], selected)
        self.assertIn("2~3개", error)
        selected, error = service._select_comparison_products("상위 2개 비교해줘", four)
        self.assertIsNone(error)
        self.assertEqual(["p1", "p2"], [p["product_id"] for p in selected])


class ComparisonContentTest(unittest.TestCase):
    def test_table_uses_only_graph_edges_and_evidence(self):
        products = service._reconstruct_products(PRODUCTS)
        evidence = [IngredientResult(name="PANTHENOL", kor_name="판테놀", claim="soothing",
                                     eligibility_tier="pubmed_evidence")]
        result = service._build_ingredient_comparison(products, INVENTORY, evidence)
        self.assertIsNotNone(result)
        text, supported = result
        self.assertIn("| 성분 | 제품 1 | 제품 2 | 제품 3 | 고민 관련 근거 |", text)
        self.assertIn("| 판테놀 (PANTHENOL) | 확인 | 확인 | 확인 | 피부 진정 · 논문 기반 성분 근거 |", text)
        self.assertIn("| 나이아신아마이드 (NIACINAMIDE) | — | 확인 | — | — |", text)
        self.assertIn("차이점: 제품 2에서만 확인된 표시 성분", text)
        self.assertIn("실제 제품에 없다는 뜻은 아닙니다", text)
        self.assertEqual(["PANTHENOL"], [item.name for item in supported])

    def test_missing_inventory_refuses_comparison(self):
        products = service._reconstruct_products(PRODUCTS[:2])
        self.assertIsNone(service._build_ingredient_comparison(products, {"p1": INVENTORY["p1"]}, []))


class ComparisonFollowupTest(unittest.IsolatedAsyncioTestCase):
    async def test_batch_and_sse_return_same_comparison(self):
        active = {"visible_products": PRODUCTS[:2], "source_products": PRODUCTS[:2],
                  "ingredients": [{"name": "PANTHENOL", "kor_name": "판테놀", "claim": "soothing"}]}
        history = [{"user": "진정 추천", "products": PRODUCTS[:2]}]
        with mock.patch.object(service.conversation_store, "load_recent",
                               mock.AsyncMock(return_value=history)), \
             mock.patch.object(service.conversation_store, "load_active",
                               mock.AsyncMock(return_value=active)), \
             mock.patch.object(service, "query_product_ingredient_inventory",
                               mock.AsyncMock(return_value=INVENTORY)), \
             mock.patch.object(service, "_store_turn", mock.AsyncMock()):
            batch = await service.recommend("s", "이 중에서 비교해줘")
            frames = [frame async for frame in service.recommend_stream("s", "이 중에서 비교해줘")]
        events = {}
        for frame in frames:
            lines = frame.strip().splitlines()
            event = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
            data = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
            events[event] = json.loads(data)
        self.assertEqual(batch.response_text, events["delta"]["text"])
        self.assertEqual([p.product_id for p in batch.products],
                         [p["product_id"] for p in events["meta"]["products"]])
        self.assertEqual("followup_comparison", batch.response_mode)

    async def test_followup_uses_graph_data_without_llm_and_updates_visible_set(self):
        active = {"visible_products": PRODUCTS, "source_products": PRODUCTS,
                  "ingredients": [{"name": "PANTHENOL", "kor_name": "판테놀", "claim": "soothing"}],
                  "base_message": "진정 제품 추천해줘"}
        history = [{"user": active["base_message"], "products": PRODUCTS}]
        with mock.patch.object(service, "query_product_ingredient_inventory",
                               mock.AsyncMock(return_value=INVENTORY)) as query, \
             mock.patch.object(service, "_store_turn", mock.AsyncMock()) as store, \
             mock.patch.object(service, "get_async_llm_client") as llm:
            response = await service._handle_followup(
                "s", "t2", "네오젠과 도미나스 비교해줘", history, active,
            )
        self.assertEqual("followup_comparison", response.response_mode)
        self.assertEqual(["p1", "p2"], [p.product_id for p in response.products])
        self.assertIn("| 성분 |", response.response_text)
        query.assert_awaited_once_with(["p1", "p2"])
        llm.assert_not_called()
        self.assertEqual(["p1", "p2"], [p["product_id"] for p in store.await_args.kwargs["active_state"]["visible_products"]])

    async def test_ambiguous_or_unavailable_does_not_invent_a_table(self):
        four = PRODUCTS + [_product("p4", "브랜드 D 세럼", "브랜드 D", "세럼", ["X"])]
        active = {"visible_products": four, "source_products": four}
        history = [{"user": "추천해줘", "products": four}]
        with mock.patch.object(service, "query_product_ingredient_inventory",
                               mock.AsyncMock()) as query, \
             mock.patch.object(service, "_store_turn", mock.AsyncMock()):
            clarification = await service._handle_followup("s", "t2", "비교해줘", history, active)
        self.assertEqual("followup_comparison_clarification", clarification.response_mode)
        self.assertNotIn("| 성분 |", clarification.response_text)
        query.assert_not_awaited()

        active["visible_products"] = PRODUCTS[:2]
        history[0]["products"] = PRODUCTS[:2]
        with mock.patch.object(service, "query_product_ingredient_inventory",
                               mock.AsyncMock(return_value={"p1": INVENTORY["p1"]})), \
             mock.patch.object(service, "_store_turn", mock.AsyncMock()):
            unavailable = await service._handle_followup("s", "t3", "이 중에서 비교해줘", history, active)
        self.assertEqual("followup_comparison_unavailable", unavailable.response_mode)
        self.assertEqual([], unavailable.products)
        self.assertNotIn("| 성분 |", unavailable.response_text)


class ComparisonGraphQueryTest(unittest.IsolatedAsyncioTestCase):
    async def test_exact_ids_are_deduplicated_before_graph_lookup(self):
        class Result:
            def __aiter__(self):
                self.rows = iter([{"product_id": "p1", "ingredients": INVENTORY["p1"]}])
                return self

            async def __anext__(self):
                try:
                    return next(self.rows)
                except StopIteration:
                    raise StopAsyncIteration from None

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def run(self, query, **params):
                self.query = query
                self.params = params
                return Result()

        session = Session()
        driver = mock.Mock()
        driver.session.return_value = session
        with mock.patch.object(neo4j_client, "_get_driver", return_value=driver):
            result = await neo4j_client.query_product_ingredient_inventory(["p1", "p1"])
        self.assertEqual({"p1": INVENTORY["p1"]}, result)
        self.assertEqual(["p1"], session.params["product_ids"])
        self.assertIn("CONTAINS", session.query)
