"""대화 이력(P1) 순수 로직 유닛 테스트 — Redis 불필요."""

import json
import unittest
from unittest import mock
from types import SimpleNamespace

from app.repositories import conversation_store
from app.repositories.conversation_store import _active_key, _key
from app.domain.enums import Concern, Effect
from app.domain.user import UserProfile
from app.schemas.recommend import IngredientResult, ProductResult, RecommendResponse
from app.services import recommend_service
from app.services.recommend_service import (
    _extract_ranking,
    _followup_context,
    _has_followup_cue,
    _has_missing_history_cue,
    _heuristic_kind,
    _is_deictic,
    _reorder_by_ranking,
    _slim_products,
)


class ConversationKeyTest(unittest.TestCase):
    def test_key_prefix(self):
        self.assertEqual("conv:v1:abc", _key("abc"))
        self.assertEqual("conv:active:v1:abc", _active_key("abc"))


class ActiveStateStoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_active_state_round_trip_and_clear(self):
        values = {}
        client = mock.Mock()

        async def set_value(key, value, ex):
            values[key] = value

        async def get_value(key):
            return values.get(key)

        async def delete_values(*keys):
            for key in keys:
                values.pop(key, None)

        client.set = mock.AsyncMock(side_effect=set_value)
        client.get = mock.AsyncMock(side_effect=get_value)
        client.delete = mock.AsyncMock(side_effect=delete_values)
        state = {"profile": {"concerns": ["DRY_SKIN"]}, "visible_products": []}
        with mock.patch.object(conversation_store.settings, "conversation_enabled", True), \
                mock.patch.object(conversation_store.recommend_cache, "_get_client", return_value=client):
            await conversation_store.save_active("abc", state)
            self.assertEqual(state, await conversation_store.load_active("abc"))
            await conversation_store.clear("abc")
            self.assertIsNone(await conversation_store.load_active("abc"))
        self.assertEqual(conversation_store.settings.conversation_ttl_seconds,
                         client.set.call_args.kwargs["ex"])


class SlimProductsTest(unittest.TestCase):
    def test_from_dicts(self):
        rows = [{"product_name": "토너A", "brand": "브랜드", "category": "토너", "rating": 4.7,
                 "goods_no": "A1", "matched_count": 3, "matched_ingredients": ["X"]}]
        s = _slim_products(rows)[0]
        self.assertEqual("토너A", s["name"])
        self.assertEqual("A1", s["goods_no"])
        self.assertEqual(3, s["matched_count"])
        self.assertEqual(["X"], s["matched_ingredients"])

    def test_from_objects(self):
        class P:
            product_id, product_name, brand, category = "id1", "크림B", "B", "크림"
            goods_no, product_url, rating, review_count, review_stats = "A2", "u", 4.2, 10, None
            matched_count, matched_ingredients = 2, ["Y"]
        s = _slim_products([P()])[0]
        self.assertEqual("크림B", s["name"])
        self.assertEqual("A2", s["goods_no"])
        self.assertEqual(4.2, s["rating"])

    def test_empty(self):
        self.assertEqual([], _slim_products(None))
        self.assertEqual([], _slim_products([]))


class HeuristicClassifyTest(unittest.TestCase):
    _HIST = [{"products": [{"name": "x"}]}]  # 이력 있음

    def test_no_history_is_new(self):
        self.assertEqual("new", _heuristic_kind("그 중에서 비교해줘", []))

    def test_followup_cue(self):
        self.assertEqual("followup", _heuristic_kind("그 중에서 비교해줘", self._HIST))
        self.assertEqual("followup", _heuristic_kind("이거 장단점 알려줘", self._HIST))
        self.assertEqual("followup", _heuristic_kind("추천해준 제품 중 하나만 골라줘", self._HIST))

    def test_generic_comparison_words_are_not_previous_recommendation_refs(self):
        for message in (
            "입 주변과 이마의 피부톤 차이가 고민이에요.",
            "따끔거리는 피부에 알코올 없는 제품만 골라주세요.",
            "비건 제품 중에서 무향인 것만 보여주세요.",
            "친구가 추천한 제품들을 비교해줘.",
        ):
            with self.subTest(message=message):
                self.assertFalse(_has_followup_cue(message))
                self.assertEqual("new", _heuristic_kind(message, []))

    def test_explicit_previous_recommendation_refs(self):
        for message in (
            "그 중에서 비교해줘",
            "이거 장단점 알려줘",
            "방금 추천한 제품 중 뭐가 나아?",
            "추천한 제품들 차이를 비교해줘",
            "추천한 제품들을 리뷰는 보조로만 쓰서 비교해줘",
        ):
            with self.subTest(message=message):
                self.assertTrue(_has_followup_cue(message))
                self.assertEqual("followup", _heuristic_kind(message, self._HIST))
        self.assertTrue(_has_missing_history_cue("그 중에서 비교해줘"))
        self.assertTrue(_has_missing_history_cue("방금 추천한 제품 중 뭐가 나아?"))
        self.assertTrue(_has_missing_history_cue("추천한 제품들을 비교해줘"))
        self.assertFalse(_has_missing_history_cue("친구가 추천한 제품들을 비교해줘"))
        self.assertFalse(_has_missing_history_cue("이 제품 추천해줘"))
        self.assertFalse(_has_missing_history_cue("이중 세안 제품 추천해줘"))

    def test_concern_cue_is_new(self):
        self.assertEqual("new", _heuristic_kind("민감성 피부에 좋은거 있어?", self._HIST))
        self.assertEqual("new", _heuristic_kind("여드름 때문에 고민이야", self._HIST))

    def test_ambiguous_returns_none(self):
        # 후속 큐도 고민 큐도 없으면 None(→ LLM 위임)
        self.assertIsNone(_heuristic_kind("사용 순서 알려줘", self._HIST))


class DeicticContextTest(unittest.TestCase):
    _HISTORY = [
        {
            "user": "건조한 피부에 맞는 크림 추천해줘",
            "assistant": "건조 피부용 추천입니다.",
            "products": [{"name": "크림 A", "brand": "브랜드 A", "category": "크림"}],
        },
        {
            "user": "지성 피부에 맞는 로션 추천해줘",
            "assistant": "지성 피부용 추천입니다.",
            "products": [{"name": "로션 B", "brand": "브랜드 B", "category": "로션"}],
        },
    ]

    def test_deictic_cue_detection(self):
        self.assertTrue(_is_deictic("이 중에서 가장 산뜻한 거"))
        self.assertTrue(_is_deictic("그것들 차이를 알려줘"))
        self.assertFalse(_is_deictic("세 제품을 전반적으로 비교해줘"))
        self.assertFalse(_is_deictic("비건 제품 중에서 무향인 것만 보여주세요"))

    def test_deictic_context_keeps_only_latest_recommendation_turn(self):
        context = _followup_context(self._HISTORY, {}, deictic=True)
        self.assertIn("지성 피부", context)
        self.assertIn("로션 B", context)
        self.assertNotIn("건조한 피부", context)
        self.assertNotIn("크림 A", context)

    def test_general_followup_context_keeps_recent_turns(self):
        context = _followup_context(self._HISTORY, {}, deictic=False)
        self.assertIn("건조한 피부", context)
        self.assertIn("지성 피부", context)


class ConversationTransportParityTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _active_state():
        profile = UserProfile(concerns=[Concern.DRY_SKIN], effects=[Effect.HYDRATING])
        return recommend_service._active_recommendation(profile, "건조한 피부 진정 제품 추천해줘", [
            {"product_id": "t", "product_name": "토너 A", "brand": "A", "category": "토너",
             "matched_count": 1, "matched_ingredients": ["NIACINAMIDE"]},
            {"product_id": "c", "product_name": "크림 B", "brand": "B", "category": "크림",
             "matched_count": 1, "matched_ingredients": ["CERAMIDE"]},
        ], "turn-1")

    @staticmethod
    def _conversation_response() -> RecommendResponse:
        return RecommendResponse(
            session_id="session-1",
            turn_id="turn-1",
            ingredients=[IngredientResult(name="NIACINAMIDE", kor_name="나이아신아마이드")],
            products=[ProductResult(
                product_id="p1",
                product_name="로션 B",
                brand="브랜드 B",
                category="로션",
                matched_count=1,
                matched_ingredients=["NIACINAMIDE"],
            )],
            response_text="**로션 B**가 더 산뜻해요.",
            model_used="test-model",
        )

    @staticmethod
    def _parse_frame(frame: str) -> tuple[str, dict]:
        lines = frame.strip().splitlines()
        event = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
        data = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        return event, json.loads(data)

    async def test_batch_and_sse_share_conversation_resolution(self):
        resolved = self._conversation_response()
        resolver = mock.AsyncMock(return_value=resolved)
        with mock.patch.object(recommend_service, "_resolve_conversation_response", resolver):
            batch = await recommend_service.recommend("session-1", "이 중에서 산뜻한 거")
            frames = [frame async for frame in recommend_service.recommend_stream(
                "session-1", "이 중에서 산뜻한 거"
            )]

        parsed = [self._parse_frame(frame) for frame in frames]
        self.assertEqual(["meta", "delta", "done"], [event for event, _ in parsed])
        self.assertEqual(batch.response_text, parsed[1][1]["text"])
        self.assertEqual(
            [product.product_name for product in batch.products],
            [product["product_name"] for product in parsed[0][1]["products"]],
        )
        self.assertEqual("conversation", parsed[2][1]["finish_reason"])
        self.assertEqual(2, resolver.await_count)

    async def test_missing_history_followup_is_same_graceful_response(self):
        with mock.patch.object(
            recommend_service.conversation_store,
            "load_recent",
            mock.AsyncMock(return_value=[]),
        ):
            response = await recommend_service._resolve_conversation_response(
                "expired-session", "turn-1", "이 중에서 비교해줘"
            )

        self.assertIsNotNone(response)
        self.assertEqual([], response.products)
        self.assertIn("이전 추천 내역을 찾지 못했어요", response.response_text)

    async def test_first_turn_with_generic_words_continues_to_recommendation(self):
        with mock.patch.object(
            recommend_service.conversation_store,
            "load_recent",
            mock.AsyncMock(return_value=[]),
        ):
            for message in (
                "피부톤 차이가 고민입니다.",
                "알코올 없는 제품만 골라주세요.",
                "비건 제품 중에서 무향인 것만 보여주세요.",
                "이 제품 추천해줘.",
                "이중 세안 제품 추천해줘.",
            ):
                with self.subTest(message=message):
                    response = await recommend_service._resolve_conversation_response(
                        "new-session", "turn-1", message
                    )
                    self.assertIsNone(response)

    async def test_followup_after_zero_product_result_is_deterministic(self):
        history = [{
            "user": "민감 피부 제품 추천해줘",
            "assistant": "조건에 맞는 제품이 없습니다.",
            "products": [],
        }]
        with mock.patch.object(
            recommend_service,
            "get_async_llm_client",
        ) as llm_client, mock.patch.object(
            recommend_service,
            "_store_turn",
            mock.AsyncMock(),
        ):
            response = await recommend_service._handle_followup(
                "session-1", "turn-2", "추천 제품을 비교해줘", history
            )

        llm_client.assert_not_called()
        self.assertEqual([], response.products)
        self.assertEqual([], response.ingredients)
        self.assertIn("비교할 제품이 없습니다", response.response_text)

    async def test_recommended_products_followup_after_zero_products_skips_classifier(self):
        history = [{
            "user": "수부지인데 모공도 신경 쓰여",
            "assistant": "조건에 맞는 제품이 없습니다.",
            "products": [],
        }]
        with mock.patch.object(
            recommend_service.conversation_store,
            "load_recent",
            mock.AsyncMock(return_value=history),
        ), mock.patch.object(
            recommend_service,
            "_llm_classify",
            mock.AsyncMock(),
        ) as classifier, mock.patch.object(
            recommend_service,
            "_store_turn",
            mock.AsyncMock(),
        ):
            response = await recommend_service._resolve_conversation_response(
                "session-1", "turn-2", "추천한 제품들을 리뷰는 보조로만 쓰서 비교해줘"
            )

        classifier.assert_not_awaited()
        self.assertIsNotNone(response)
        self.assertEqual([], response.products)
        self.assertIn("비교할 제품이 없습니다", response.response_text)

    async def test_category_followup_filters_cards_and_generation_context(self):
        active = self._active_state()
        history = [{"user": active["base_message"], "assistant": "토너 A와 크림 B를 추천합니다.",
                    "products": active["source_products"]}]
        completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="**토너 A**를 추천합니다."))])
        client = mock.Mock()
        client.chat.completions.create = mock.AsyncMock(return_value=completion)
        store = mock.AsyncMock()
        with mock.patch.object(recommend_service, "get_async_llm_client", return_value=client), \
                mock.patch.object(recommend_service, "query_ingredient_kor_names", mock.AsyncMock(return_value={})), \
                mock.patch.object(recommend_service, "_store_turn", store):
            for message in ("그중 토너만", "토너만"):
                response = await recommend_service._handle_followup(
                    "session-1", "turn-2", message, history, active,
                )
                self.assertEqual(["t"], [p.product_id for p in response.products])
                self.assertEqual("followup_filtered", response.response_mode)
                self.assertEqual(["t"], [p["product_id"] for p in store.await_args.kwargs["active_state"]["visible_products"]])
                prompt = client.chat.completions.create.await_args.kwargs["messages"][1]["content"]
                self.assertNotIn("크림 B", prompt)

    async def test_corrupted_usage_order_followup_uses_safe_response(self):
        active = self._active_state()
        history = [{"user": active["base_message"], "products": active["source_products"]}]
        corrupted = "**크림 B**의 세포 세포 세포 세포 세포 재생을 위해 먼저 쓰세요."
        completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=corrupted))])
        client = mock.Mock()
        client.chat.completions.create = mock.AsyncMock(return_value=completion)
        with mock.patch.object(recommend_service, "get_async_llm_client", return_value=client), \
                mock.patch.object(recommend_service, "query_ingredient_kor_names", mock.AsyncMock(return_value={})), \
                mock.patch.object(recommend_service, "_store_turn", mock.AsyncMock()):
            response = await recommend_service._handle_followup(
                "session-1", "turn-2", "추천한 제품들을 어떤 순서로 써야 해?", history, active,
            )
        self.assertEqual("followup_quality_fallback", response.response_mode)
        self.assertEqual(["t", "c"], [p.product_id for p in response.products])
        self.assertIn("일반적인 제품 제형 순서", response.response_text)
        self.assertNotIn("세포 세포", response.response_text)
        self.assertNotIn("재생", response.response_text)
        self.assertEqual([], recommend_service.find_response_integrity_issues(
            response.response_text, response.ingredients, response.products,
        ))

    async def test_excluded_product_in_filtered_answer_uses_safe_response(self):
        active = self._active_state()
        history = [{"user": active["base_message"], "products": active["source_products"]}]
        completion = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="**크림 B**를 먼저 쓰고 **토너 A**를 쓰세요."),
        )])
        client = mock.Mock()
        client.chat.completions.create = mock.AsyncMock(return_value=completion)
        with mock.patch.object(recommend_service, "get_async_llm_client", return_value=client), \
                mock.patch.object(recommend_service, "query_ingredient_kor_names", mock.AsyncMock(return_value={})), \
                mock.patch.object(recommend_service, "_store_turn", mock.AsyncMock()):
            response = await recommend_service._handle_followup(
                "session-1", "turn-2", "그중 토너만 보여줘", history, active,
            )
        self.assertEqual("followup_quality_fallback", response.response_mode)
        self.assertEqual(["t"], [p.product_id for p in response.products])
        self.assertNotIn("크림 B", response.response_text)

    async def test_missing_category_asks_before_new_search_and_confirmation_reuses_concern(self):
        active = self._active_state()
        active["visible_products"] = [active["source_products"][1]]
        history = [{"user": active["base_message"], "products": active["visible_products"]}]
        store = mock.AsyncMock()
        with mock.patch.object(recommend_service, "get_async_llm_client") as llm, \
                mock.patch.object(recommend_service, "_store_turn", store):
            response = await recommend_service._handle_followup(
                "session-1", "turn-2", "그중 토너만", history, active,
            )
        self.assertEqual([], response.products)
        self.assertIn("새로 찾아볼까요", response.response_text)
        self.assertEqual(["토너"], store.await_args.kwargs["active_state"]["pending_categories"])
        llm.assert_not_called()
        pending_state = store.await_args.kwargs["active_state"]
        with mock.patch.object(recommend_service.conversation_store, "load_recent", mock.AsyncMock(return_value=history)), \
                mock.patch.object(recommend_service.conversation_store, "load_active", mock.AsyncMock(return_value=pending_state)):
            resolution = await recommend_service._resolve_conversation_response(
                "session-1", "turn-3", "응 새로 찾아줘",
            )
        self.assertIsInstance(resolution, recommend_service.ContextualSearch)
        self.assertEqual({"토너"}, resolution.categories)
        self.assertEqual([Concern.DRY_SKIN], resolution.profile.concerns)

    async def test_explicit_new_concern_does_not_reuse_previous_profile(self):
        active = self._active_state()
        with mock.patch.object(recommend_service.conversation_store, "load_recent", mock.AsyncMock(return_value=[])), \
                mock.patch.object(recommend_service.conversation_store, "load_active", mock.AsyncMock(return_value=active)):
            resolution = await recommend_service._resolve_conversation_response(
                "session-1", "turn-2", "이번에는 여드름에 좋은 토너로 다시 추천해줘",
            )
        self.assertIsNone(resolution)

    async def test_explicit_empty_visible_set_does_not_restore_old_cards(self):
        active = self._active_state()
        active["visible_products"] = []
        history = [{"user": active["base_message"], "products": active["source_products"]}]
        with mock.patch.object(recommend_service, "get_async_llm_client") as llm, \
                mock.patch.object(recommend_service, "_store_turn", mock.AsyncMock()):
            response = await recommend_service._handle_followup(
                "session-1", "turn-2", "이 중에서 비교해줘", history, active,
            )
        self.assertEqual([], response.products)
        llm.assert_not_called()

    async def test_cache_hit_keeps_profile_for_later_category_search(self):
        active = self._active_state()
        cached = {
            "_profile": active["profile"],
            "ingredients": [],
            "products": [{"product_id": "t", "product_name": "토너 A", "brand": "A",
                          "category": "토너", "matched_count": 1,
                          "matched_ingredients": ["NIACINAMIDE"]}],
            "response_text": "토너 A를 추천합니다.",
            "model_used": "test-model",
            "response_mode": "generated",
        }
        store = mock.AsyncMock()
        with mock.patch.object(recommend_service, "_resolve_conversation_response", mock.AsyncMock(return_value=None)), \
                mock.patch.object(recommend_service.recommend_cache, "get", mock.AsyncMock(return_value=cached)), \
                mock.patch.object(recommend_service, "_store_turn", store):
            response = await recommend_service.recommend("session-1", "건조한 피부 진정 제품 추천해줘")
        self.assertEqual(["t"], [p.product_id for p in response.products])
        saved = store.await_args.kwargs["active_state"]
        self.assertEqual(active["profile"], saved["profile"])
        self.assertEqual(["t"], [p["product_id"] for p in saved["visible_products"]])

    async def test_contextual_search_cache_key_includes_saved_concern(self):
        first = recommend_service.ContextualSearch(
            UserProfile(concerns=[Concern.DRY_SKIN]), "건조한 피부", {"토너"},
        )
        second = recommend_service.ContextualSearch(
            UserProfile(concerns=[Concern.ACNE]), "여드름 피부", {"토너"},
        )
        first_key, first_prompt = recommend_service._contextual_messages("토너로 다시 찾아줘", first)
        second_key, _ = recommend_service._contextual_messages("토너로 다시 찾아줘", second)
        self.assertNotEqual(first_key, second_key)
        self.assertIn("검색할 제품 유형: 토너", first_prompt)


class _Prod:
    def __init__(self, name):
        self.product_name = name


class RankingTest(unittest.TestCase):
    def test_extract_marker(self):
        text = "가장 순한 건 A입니다.\n[추천순위] 제품A | 제품B"
        clean, ranking = _extract_ranking(text)
        self.assertEqual("가장 순한 건 A입니다.", clean)
        self.assertEqual(["제품A", "제품B"], ranking)

    def test_no_marker(self):
        clean, ranking = _extract_ranking("그냥 비교 답변입니다.")
        self.assertEqual("그냥 비교 답변입니다.", clean)
        self.assertEqual([], ranking)

    def test_reorder_matched_first(self):
        prods = [_Prod("미샤 잡티 앰플"), _Prod("네오젠 세럼"), _Prod("동아 크림")]
        out = _reorder_by_ranking(prods, ["네오젠 세럼", "동아 크림"])
        self.assertEqual(["네오젠 세럼", "동아 크림", "미샤 잡티 앰플"], [p.product_name for p in out])

    def test_reorder_empty_ranking_keeps_order(self):
        prods = [_Prod("A"), _Prod("B")]
        out = _reorder_by_ranking(prods, [])
        self.assertEqual(["A", "B"], [p.product_name for p in out])

    def test_reorder_by_mention_fallback(self):
        # 마커 없으면 응답 내 첫 언급 순으로 정렬(B가 먼저 언급 → 먼저)
        prods = [_Prod("미샤 앰플"), _Prod("네오젠 세럼")]
        text = "지성피부엔 네오젠 세럼이 가볍고 좋습니다. 미샤 앰플도 괜찮습니다."
        out = _reorder_by_ranking(prods, [], text)
        self.assertEqual(["네오젠 세럼", "미샤 앰플"], [p.product_name for p in out])


if __name__ == "__main__":
    unittest.main()
