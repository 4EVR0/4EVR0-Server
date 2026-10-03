"""Fragrance label evidence, fail-closed product selection and conversation parity."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.core.config import settings
from app.clients import neo4j_client
from app.domain.enums import Concern, Constraint
from app.domain.user import UserProfile
from app.repositories import recommend_cache
from app.services import recommend_service as service
from app.services.fragrance_policy import fragrance_decision, fragrance_preference, merge_fragrance_constraint


def evidence(pid="p1"):
    now = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    return {"schema_version": 1, "product_id": pid, "status": "not_listed",
            "present_terms": [], "related_terms": [], "label_sha256": "a" * 64,
            "source_url": "https://example.org/label", "observed_at": now,
            "manufacturer_claim": {"claim": "no_added_fragrance", "product_id": pid,
                "label_sha256": "a" * 64, "label_reviewed_complete": True,
                "source_url": "https://example.org/manufacturer", "reviewed_at": now,
                "quote": "향료 무첨가", "reviewed_by": "test-reviewer"}}


def product(pid="p1"):
    return {"product_id": pid, "product_name": f"테스트 {pid} 토너", "brand": "테스트",
            "category": "토너", "matched_count": 1, "matched_ingredients": ["PANTHENOL"],
            "relevance_score": 1.0}


class FragranceDecisionTest(unittest.TestCase):
    def test_claim_requires_product_specific_recent_label_and_review(self):
        valid = evidence()
        self.assertEqual("verified_claim", fragrance_decision("p1", json.dumps(valid)))
        cases = [None, "bad json", [], {}, {**valid, "product_id": "p2"},
                 {**valid, "manufacturer_claim": None}, {**valid, "status": "unknown"},
                 {**valid, "related_terms": ["LINALOOL"]}, {**valid, "source_url": "http://["},
                 {**valid, "observed_at": "2000-01-01"}, {**valid, "observed_at": "2999-01-01"}]
        for key, value in [("label_sha256", "b" * 64), ("product_id", "p2"),
                           ("label_reviewed_complete", False), ("reviewed_at", "2000-01-01"),
                           ("quote", ""), ("source_url", "javascript:alert(1)")]:
            changed = deepcopy(valid)
            changed["manufacturer_claim"][key] = value
            cases.append(changed)
        for row in cases:
            with self.subTest(row=row):
                self.assertEqual("unknown", fragrance_decision("p1", row))
        self.assertEqual("present", fragrance_decision("p1", {**valid, "present_terms": ["향료"]}))

    def test_constraint_add_remove_and_combination(self):
        for message in ("무향료 제품만", "향료 없는 것", "fragrance-free", "향이 없는 토너"):
            self.assertIs(True, fragrance_preference(message))
        self.assertIs(False, fragrance_preference("향료 있어도 돼"))
        self.assertIs(False, fragrance_preference("무향료 아니어도 돼"))
        self.assertIsNone(fragrance_preference("향료는 무엇인가요?"))
        self.assertEqual([], merge_fragrance_constraint("향료 있어도 돼", [Constraint.FRAGRANCE_FREE]))
        self.assertEqual({Constraint.VEGAN, Constraint.FRAGRANCE_FREE},
                         set(merge_fragrance_constraint("무향이고 비건인 제품", [])))


class FragranceFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_graph_unavailable_returns_unknown_evidence(self):
        with patch.object(neo4j_client, "_get_driver", side_effect=OSError("offline")):
            self.assertEqual({}, await neo4j_client.query_product_fragrance_evidence(["p1"]))

    async def test_explicit_constraint_routes_without_llm_classification(self):
        with (patch.object(service.conversation_store, "load_recent", new=AsyncMock(return_value=[])),
              patch.object(service.conversation_store, "load_active", new=AsyncMock(return_value={"visible_products": []})),
              patch.object(service, "_handle_followup", new=AsyncMock(return_value="handled")) as followup,
              patch.object(service, "_is_followup", new=AsyncMock()) as classify):
            self.assertEqual("handled", await service._resolve_conversation_response("s", "t", "향료는 없는 걸로"))
        followup.assert_awaited_once()
        classify.assert_not_awaited()

    async def test_batch_and_stream_refuse_unknown_labels_without_generator(self):
        with (patch.object(settings, "recommend_cache_enabled", False),
              patch.object(service, "_resolve_conversation_response", new=AsyncMock(return_value=None)),
              patch.object(service, "_store_turn", new=AsyncMock()),
              patch.object(service.conversation_store, "save_active", new=AsyncMock()),
              patch.object(service, "extract_with_fallback", new=AsyncMock(return_value=(UserProfile(), "llm"))),
              patch.object(service, "query_ingredients_by_effects", new=AsyncMock(return_value=[])),
              patch.object(service, "query_products_by_ingredients", new=AsyncMock(return_value=[product()])),
              patch.object(service, "query_product_fragrance_evidence", new=AsyncMock(return_value={})) as lookup,
              patch.object(service, "get_async_llm_client", side_effect=AssertionError("unexpected generation"))):
            result = await service.recommend("s", "무향료 토너 추천")
            frames = [frame async for frame in service.recommend_stream("s", "무향료 토너 추천")]
        self.assertEqual([], result.products)
        self.assertIn("표기에 없더라도", result.response_text)
        deltas = [json.loads(frame.split("data: ", 1)[1])["text"] for frame in frames if frame.startswith("event: delta\n")]
        self.assertEqual([result.response_text], deltas)
        self.assertEqual(2, lookup.await_count)

    async def test_empty_followup_keeps_new_constraint(self):
        with patch.object(service, "_store_turn", new=AsyncMock()) as save:
            await service._handle_followup("s", "t", "무향료로 해줘", [], {"visible_products": [], "source_products": []})
        self.assertEqual(["FRAGRANCE_FREE"], save.await_args.kwargs["active_state"]["profile"]["constraints"])

    async def test_filter_before_final_top_n_and_no_stale_session_trust(self):
        rows = [product(f"p{i}") for i in range(8)]
        with (patch.object(settings, "product_result_limit", 2),
              patch.object(service, "query_products_by_ingredients", new=AsyncMock(return_value=rows)),
              patch.object(service, "query_product_fragrance_evidence",
                           new=AsyncMock(return_value={"p7": evidence("p7")}))):
            selected = await service.select_products("무향료 토너", [], [], constraints=[Constraint.FRAGRANCE_FREE])
        self.assertEqual(["p7"], [p["product_id"] for p in selected])
        with patch.object(service, "query_product_fragrance_evidence", new=AsyncMock(return_value={})):
            self.assertEqual([], await service._filter_products_with_constraints(
                [{**product(), "fragrance_evidence": evidence()}], [Constraint.FRAGRANCE_FREE]))

    async def test_sensitive_rationale_exclusion_does_not_ban_acne_retinoids(self):
        rows = [{"name": name} for name in ("LINALOOL", "FARNESOL", "LIMONENE", "RETINOL")]
        with patch.object(service, "query_cautioned_ingredients", new=AsyncMock(return_value=set())):
            self.assertEqual(rows[-1:], await service.apply_caution_filter(rows, [Concern.IRRITATED_SKIN]))
            self.assertEqual([], await service.apply_caution_filter(rows[:3], [Concern.SENSITIVE_SKIN]))
            # #115: positive fragrance rationales are excluded for acne too;
            # retinoids are not globally banned by this policy.
            self.assertEqual(rows[-1:], await service.apply_caution_filter(rows, [Concern.ACNE]))

    async def test_followup_constraint_persists_and_restore_rechecks_source(self):
        active = {"profile": {"concerns": [], "constraints": []},
                  "visible_products": [product("p1"), product("p2")],
                  "source_products": [product("p1"), product("p2")], "ingredients": []}
        with (patch.object(settings, "product_image_url_mode", "public"),
              patch.object(service, "_store_turn", new=AsyncMock()) as save,
              patch.object(service, "query_product_fragrance_evidence",
                           new=AsyncMock(return_value={"p2": evidence("p2")})) as lookup):
            result = await service._handle_followup("s", "t", "그 중 무향료 제품만", [], active)
            state = save.await_args.kwargs["active_state"]
            self.assertEqual(["FRAGRANCE_FREE"], state["profile"]["constraints"])
            self.assertEqual(["p2"], [p.product_id for p in result.products])
            self.assertEqual("https://example.org/manufacturer", result.products[0].fragrance_free_source_url)
            result = await service._handle_followup("s", "t2", "처음 제품 전체 다시 보여줘", [], state)
            self.assertEqual(["p2"], [p.product_id for p in result.products])
            self.assertEqual(2, lookup.await_count)
            # A newly conflicting label cannot be revived from saved session evidence.
            lookup.return_value = {"p2": {**evidence("p2"), "status": "present"}}
            result = await service._handle_followup("s", "t3", "전체 다시 보여줘", [], state)
            self.assertEqual([], result.products)

    async def test_comparison_does_not_substitute_blocked_numbered_product(self):
        active = {"profile": {"constraints": ["FRAGRANCE_FREE"]},
                  "visible_products": [product("p1"), product("p2"), product("p3")], "ingredients": []}
        with (patch.object(service, "_store_turn", new=AsyncMock()),
              patch.object(service, "query_product_fragrance_evidence",
                           new=AsyncMock(return_value={"p2": evidence("p2"), "p3": evidence("p3")})) as lookup,
              patch.object(service, "query_product_ingredient_inventory", new=AsyncMock()) as inventory):
            result = await service._handle_followup("s", "t", "1번 2번 비교해줘", [], active)
        lookup.assert_awaited_once_with(["p1", "p2"])
        inventory.assert_not_awaited()
        self.assertEqual([], result.products)
        self.assertEqual("followup_constraints", result.response_mode)

    async def test_comparison_keeps_inventory_fact_but_removes_old_soothing_match(self):
        rows = [{**product(pid), "matched_ingredients": ["LINALOOL", "PANTHENOL"]} for pid in ("p1", "p2")]
        active = {"profile": {"concerns": ["IRRITATED_SKIN"]}, "visible_products": rows,
                  "ingredients": [{"name": "LINALOOL", "kor_name": "리날룰", "claim": "Soothing"}]}
        inventory = {pid: [{"name": "LINALOOL", "kor_name": "리날룰"}] for pid in ("p1", "p2")}
        with (patch.object(settings, "product_image_url_mode", "public"),
              patch.object(service, "_store_turn", new=AsyncMock()),
              patch.object(service, "query_product_ingredient_inventory", new=AsyncMock(return_value=inventory))):
            result = await service._handle_followup("s", "t", "1번 2번 비교해줘", [], active)
        self.assertIn("리날룰", result.response_text)
        self.assertNotIn("Soothing", result.response_text)
        self.assertTrue(all("LINALOOL" not in p.matched_ingredients for p in result.products))

    async def test_constrained_response_cache_is_not_reused_or_written(self):
        payload = {"_profile": {"constraints": ["FRAGRANCE_FREE"]}}
        redis = SimpleNamespace(get=AsyncMock(return_value=json.dumps(payload)), set=AsyncMock())
        with (patch.object(settings, "recommend_cache_enabled", True),
              patch.object(recommend_cache, "_get_client", return_value=redis)):
            self.assertIsNone(await recommend_cache.get("무향료", None))
            await recommend_cache.set("무향료", None, payload)
        redis.set.assert_not_awaited()

    async def test_batch_and_stream_guard_hallucinated_fragrance_rationale(self):
        profile = UserProfile(concerns=[Concern.IRRITATED_SKIN])
        rows = [{"name": "PANTHENOL", "kor_name": "판테놀", "claim": "Soothing"}]
        generated = "고민 분석\n따가운 피부이군요.\n성분 설명\n리날룰은 피부 진정에 좋습니다.\n추천 제품\n테스트 p1 토너를 추천합니다."

        async def chunks():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=generated))])

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=chunks()))))
        with (patch.object(settings, "recommend_cache_enabled", False),
              patch.object(settings, "product_image_url_mode", "public"),
              patch.object(service, "_resolve_conversation_response", new=AsyncMock(return_value=None)),
              patch.object(service, "_store_turn", new=AsyncMock()),
              patch.object(service.conversation_store, "save_active", new=AsyncMock()),
              patch.object(service, "extract_with_fallback", new=AsyncMock(return_value=(profile, "llm"))),
              patch.object(service, "query_ingredients_by_effects", new=AsyncMock(return_value=rows)),
              patch.object(service, "query_cautioned_ingredients", new=AsyncMock(return_value=set())),
              patch.object(service, "select_products", new=AsyncMock(return_value=[product()])),
              patch.object(service, "_build_llm_response", new=AsyncMock(return_value=generated)),
              patch.object(service, "get_async_llm_client", return_value=client)):
            result = await service.recommend("s", "피부가 따가워요")
            frames = [frame async for frame in service.recommend_stream("s", "피부가 따가워요")]
        deltas = [json.loads(frame.split("data: ", 1)[1])["text"] for frame in frames if frame.startswith("event: delta\n")]
        self.assertEqual("fragrance_rationale_fallback", result.response_mode)
        self.assertNotIn("리날룰", result.response_text)
        self.assertEqual([result.response_text], deltas)


if __name__ == "__main__":
    unittest.main()
