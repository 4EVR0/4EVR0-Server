import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.core.config import settings
from app.schemas.recommend import IngredientResult, ProductResult
from app.services.recommend_service import (
    _SYSTEM_PROMPT,
    _build_llm_response,
    _ingredient_display_name,
    recommend,
    recommend_stream,
)
from app.services.product_image_service import build_product_image_url
from eval.run_response_eval import judge_response


class IngredientDisplayNameTest(unittest.TestCase):
    def test_v4_is_the_concise_production_prompt(self):
        self.assertIn("at most 3 products", _SYSTEM_PROMPT)
        self.assertIn("한글명 (INCI)", _SYSTEM_PROMPT)
        self.assertIn("Never end with a dangling", _SYSTEM_PROMPT)

    def test_prefers_korean_name_and_keeps_inci(self):
        ingredient = IngredientResult(name="NIACINAMIDE", kor_name="나이아신아마이드")

        self.assertEqual("나이아신아마이드 (NIACINAMIDE)", _ingredient_display_name(ingredient))

    def test_falls_back_to_inci_for_missing_or_duplicate_korean_name(self):
        self.assertEqual("UREA", _ingredient_display_name(IngredientResult(name="UREA")))
        self.assertEqual(
            "UREA",
            _ingredient_display_name(IngredientResult(name="UREA", kor_name=" urea ")),
        )


class ProductImageUrlTest(unittest.TestCase):
    def test_builds_oliveyoung_image_url_from_goods_no(self):
        with patch.object(settings, "product_image_url_mode", "public"):
            self.assertEqual(
                "https://oliveyoung-crawl-data.s3.amazonaws.com/oliveyoung_images/goodsNo=A%201/main.jpg",
                build_product_image_url(" A 1 "),
            )


class RecommendKoreanNameTest(unittest.IsolatedAsyncioTestCase):
    async def test_rosacea_study_template_matches_batch_stream_and_can_rollback(self):
        from app.domain.enums import Concern

        profile = SimpleNamespace(effects=[], concerns=[Concern.ROSACEA_PRONE], constraints=[])
        ingredient_rows = [{
            "name": "NIACINAMIDE", "kor_name": "나이아신아마이드", "claim": "Soothing",
            "eligibility_tier": "pubmed_evidence", "paper_ref": "1",
        }]
        product_rows = [{
            "product_id": "p1", "product_name": "테스트 레드 세럼", "brand": "테스트",
            "category": "세럼", "matched_count": 1,
            "matched_ingredients": ["NIACINAMIDE"],
        }, {
            "product_id": "p2", "product_name": "다른 레드 세럼", "brand": "테스트",
            "category": "세럼", "matched_count": 1,
            "matched_ingredients": ["NIACINAMIDE"],
        }]
        with (
            patch.object(settings, "recommend_cache_enabled", False),
            patch("app.services.recommend_service._resolve_conversation_response", new=AsyncMock(return_value=None)),
            patch("app.services.recommend_service._store_turn", new=AsyncMock()),
            patch("app.services.recommend_service.extract_with_fallback", new=AsyncMock(return_value=(profile, "llm"))),
            patch("app.services.recommend_service.query_ingredients_by_effects", new=AsyncMock(return_value=ingredient_rows)),
            patch("app.services.recommend_service.query_cautioned_ingredients", new=AsyncMock(return_value=set())),
            patch("app.services.recommend_service.select_products", new=AsyncMock(return_value=product_rows)),
            patch("app.services.recommend_service.get_async_llm_client", side_effect=AssertionError("generator called")),
        ):
            batch = await recommend("rosacea-study-batch", "로사케아 경향에 맞는 제품")
            frames = [frame async for frame in recommend_stream(
                "rosacea-study-stream", "로사케아 경향에 맞는 제품",
            )]
            with patch.object(settings, "verified_study_response_enabled", False):
                rollback = await recommend("rosacea-study-off", "로사케아 경향에 맞는 제품")

        self.assertEqual("redness_verified_study_template", batch.response_mode)
        self.assertEqual(["p1"], [product.product_id for product in batch.products])
        self.assertIn("[연구 보기](https://pubmed.ncbi.nlm.nih.gov/16209160/)", batch.response_text)
        meta = next(frame for frame in frames if frame.startswith("event: meta\n"))
        self.assertEqual(1, len(json.loads(meta.split("data: ", 1)[1])["products"]))
        deltas = [json.loads(frame.split("data: ", 1)[1])["text"] for frame in frames
                  if frame.startswith("event: delta\n")]
        self.assertEqual([batch.response_text], deltas)
        self.assertIn('"response_mode": "redness_verified_study_template"', frames[-1])
        self.assertEqual("redness_evidence_template", rollback.response_mode)
        self.assertNotIn("[연구 보기]", rollback.response_text)

    async def test_verified_study_toggle_off_uses_previous_generation_path(self):
        from app.domain.enums import Concern

        profile = SimpleNamespace(effects=[], concerns=[Concern.WRINKLES], constraints=[])
        ingredient_rows = [{
            "name": "RETINOL", "kor_name": "레티놀", "claim": "Anti-aging",
            "eligibility_tier": "pubmed_evidence", "paper_ref": "2",
        }]
        product_rows = [{
            "product_id": "p1", "product_name": "테스트 주름 세럼", "brand": "테스트",
            "category": "세럼", "matched_count": 1, "matched_ingredients": ["RETINOL"],
        }]
        generated = (
            "고민 분석\n입가 잔주름이 고민이군요.\n\n성분 설명\n"
            "- 레티놀: 제품 데이터의 매칭 성분입니다.\n\n추천 제품\n"
            "- [세럼] 테스트 주름 세럼: 레티놀이 매칭 성분으로 확인됩니다."
        )
        generator = AsyncMock(return_value=generated)
        with (
            patch.object(settings, "recommend_cache_enabled", False),
            patch.object(settings, "verified_study_response_enabled", False),
            patch("app.services.recommend_service._resolve_conversation_response", new=AsyncMock(return_value=None)),
            patch("app.services.recommend_service._store_turn", new=AsyncMock()),
            patch("app.services.recommend_service.extract_with_fallback", new=AsyncMock(return_value=(profile, "llm"))),
            patch("app.services.recommend_service.query_ingredients_by_effects", new=AsyncMock(return_value=ingredient_rows)),
            patch("app.services.recommend_service.select_products", new=AsyncMock(return_value=product_rows)),
            patch("app.services.recommend_service._build_llm_response", new=generator),
        ):
            result = await recommend("verified-off", "입가 잔주름이 고민이에요.")

        generator.assert_awaited_once()
        self.assertEqual("generated", result.response_mode)
        self.assertNotIn("연구 보기", result.response_text)

    async def test_verified_retinol_study_uses_grounded_template_in_batch_and_stream(self):
        from app.domain.enums import Concern

        profile = SimpleNamespace(effects=[], concerns=[Concern.WRINKLES], constraints=[])
        ingredient_rows = [{
            "name": "RETINOL", "kor_name": "레티놀", "claim": "Anti-aging",
            "eligibility_tier": "pubmed_evidence", "paper_ref": "2",
        }]
        product_rows = [{
            "product_id": "p1", "product_name": "테스트 주름 세럼", "brand": "테스트",
            "category": "세럼", "matched_count": 1,
            "matched_ingredients": ["RETINOL"],
        }]
        cache_set = AsyncMock()
        with (
            patch.object(settings, "recommend_cache_enabled", False),
            patch("app.services.recommend_service._resolve_conversation_response", new=AsyncMock(return_value=None)),
            patch("app.services.recommend_service._store_turn", new=AsyncMock()),
            patch("app.services.recommend_service.extract_with_fallback", new=AsyncMock(return_value=(profile, "llm"))),
            patch("app.services.recommend_service.query_ingredients_by_effects", new=AsyncMock(return_value=ingredient_rows)),
            patch("app.services.recommend_service.select_products", new=AsyncMock(return_value=product_rows)),
            patch("app.services.recommend_service._build_llm_response", new=AsyncMock(side_effect=AssertionError("generator called"))),
            patch("app.services.recommend_service.get_async_llm_client", side_effect=AssertionError("generator called")),
            patch("app.services.recommend_service.recommend_cache.set", new=cache_set),
        ):
            batch = await recommend("verified-batch", "입가 잔주름이 고민이에요.")
            frames = [frame async for frame in recommend_stream("verified-stream", "입가 잔주름이 고민이에요.")]

        self.assertEqual("verified_study_template", batch.response_mode)
        self.assertIn("0.1% 안정화 레티놀 제형을 비교한 6개 연구", batch.response_text)
        self.assertIn("이 연구는 입가 주름을 따로 평가하지 않았고", batch.response_text)
        self.assertIn("[연구 보기](https://pubmed.ncbi.nlm.nih.gov/38564380/)", batch.response_text)
        self.assertNotIn("24아마이드", batch.response_text)
        deltas = [json.loads(frame.split("data: ", 1)[1])["text"] for frame in frames
                  if frame.startswith("event: delta\n")]
        self.assertEqual([batch.response_text], deltas)
        self.assertEqual("verified_study_template", cache_set.await_args.args[2]["response_mode"])

    async def test_batch_and_stream_replace_corrupted_generation_before_delivery(self):
        profile = SimpleNamespace(effects=[], concerns=[], constraints=[])
        ingredient_rows = [{
            "name": "RETINOL", "kor_name": "레티놀", "claim": "Hydrating",
            "eligibility_tier": "pubmed_evidence", "paper_ref": "2",
        }]
        product_rows = [{
            "product_id": "p1", "product_name": "테스트 크림", "brand": "테스트",
            "category": "크림", "matched_count": 1,
            "matched_ingredients": ["RETINOL"],
        }]
        corrupted = (
            "고민 분석\n주름을 살핍니다.\n성분 설명\n"
            "- 레티놀 (RETINOL): 피부 세포 세포 세포 세포 세포 CELLULAR 재생.\n"
            "추천 제품\n- 테스트 크림: 레티놀이 확인됩니다."
        )

        async def chunks():
            for piece in (corrupted[:35], corrupted[35:]):
                yield SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=piece),
                )])

        completion = AsyncMock(return_value=chunks())
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
        cache_set = AsyncMock()
        with (
            patch.object(settings, "recommend_cache_enabled", False),
            patch("app.services.recommend_service._resolve_conversation_response", new=AsyncMock(return_value=None)),
            patch("app.services.recommend_service._store_turn", new=AsyncMock()),
            patch("app.services.recommend_service.extract_with_fallback", new=AsyncMock(return_value=(profile, "llm"))),
            patch("app.services.recommend_service.query_ingredients_by_effects", new=AsyncMock(return_value=ingredient_rows)),
            patch("app.services.recommend_service.select_products", new=AsyncMock(return_value=product_rows)),
            patch("app.services.recommend_service._build_llm_response", new=AsyncMock(return_value=corrupted)),
            patch("app.services.recommend_service.get_async_llm_client", return_value=client),
            patch("app.services.recommend_service.recommend_cache.set", new=cache_set),
        ):
            batch = await recommend("batch-session", "입가 주름이 고민이에요.")
            frames = [frame async for frame in recommend_stream("stream-session", "입가 주름이 고민이에요.")]

        deltas = [
            json.loads(frame.split("data: ", 1)[1])["text"]
            for frame in frames if frame.startswith("event: delta\n")
        ]
        self.assertEqual([batch.response_text], deltas)
        self.assertEqual("quality_fallback", batch.response_mode)
        done = [json.loads(frame.split("data: ", 1)[1]) for frame in frames
                if frame.startswith("event: done\n")]
        self.assertEqual("quality_fallback", done[-1]["response_mode"])
        self.assertIn("추천 이유는", batch.response_text)
        self.assertNotIn("세포 세포", batch.response_text)
        self.assertNotIn("CELLULAR", batch.response_text)
        self.assertEqual(2, cache_set.await_count)
        self.assertEqual(batch.response_text, cache_set.await_args.args[2]["response_text"])

    async def test_recommend_preserves_korean_name_from_graph_result(self):
        profile = SimpleNamespace(effects=[], concerns=[])
        ingredient_rows = [{
            "name": "NIACINAMIDE",
            "kor_name": "나이아신아마이드",
            "claim": "Depigmenting",
            "eligibility_tier": "pubmed_evidence",
            "paper_ref": "2",
        }]

        with (
            patch.object(settings, "product_image_url_mode", "public"),
            patch(
                "app.services.recommend_service.extract_with_fallback",
                new=AsyncMock(return_value=(profile, "llm")),
            ),
            patch(
                "app.services.recommend_service.query_ingredients_by_effects",
                new=AsyncMock(return_value=ingredient_rows),
            ),
            patch(
                "app.services.recommend_service.query_products_by_ingredients",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.recommend_service._build_llm_response",
                new=AsyncMock(return_value="완결된 추천 응답입니다."),
            ),
        ):
            response = await recommend("session", "칙칙해요")

        self.assertEqual("나이아신아마이드", response.ingredients[0].kor_name)

    async def test_recommend_includes_product_goods_no_and_image_url(self):
        profile = SimpleNamespace(effects=[], concerns=[])
        product_rows = [{
            "product_id": "prod-1",
            "goods_no": "123456789",
            "product_name": "테스트 앰플",
            "brand": "테스트",
            "category": "앰플",
            "matched_count": 1,
            "matched_ingredients": ["NIACINAMIDE"],
        }]

        with (
            patch.object(settings, "product_image_url_mode", "public"),
            patch(
                "app.services.recommend_service.extract_with_fallback",
                new=AsyncMock(return_value=(profile, "llm")),
            ),
            patch(
                "app.services.recommend_service.query_ingredients_by_effects",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.services.recommend_service.query_products_by_ingredients",
                new=AsyncMock(return_value=product_rows),
            ),
            patch(
                "app.services.recommend_service._build_llm_response",
                new=AsyncMock(return_value="완결된 추천 응답입니다."),
            ),
        ):
            response = await recommend("session", "앰플 추천해줘")

        product = response.products[0]
        self.assertEqual("123456789", product.goods_no)
        self.assertEqual(
            "https://oliveyoung-crawl-data.s3.amazonaws.com/oliveyoung_images/goodsNo=123456789/main.jpg",
            product.image_url,
        )

    async def test_generation_context_uses_korean_names_and_output_budget(self):
        ingredient = IngredientResult(
            name="NIACINAMIDE",
            kor_name="나이아신아마이드",
            claim="Depigmenting",
            eligibility_tier="pubmed_evidence",
            paper_ref="2",
        )
        product = ProductResult(
            product_id="1",
            product_name="테스트 앰플",
            brand="테스트",
            category="앰플",
            matched_count=1,
            matched_ingredients=["NIACINAMIDE"],
        )
        completion = AsyncMock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="완결된 추천 응답입니다."))]
            )
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))

        with patch("app.services.recommend_service.get_async_llm_client", return_value=client):
            result = await _build_llm_response(
                "칙칙해요",
                [ingredient],
                [product],
                "system prompt",
            )

        self.assertEqual("완결된 추천 응답입니다.", result)
        request = completion.await_args.kwargs
        self.assertEqual(settings.gen_max_tokens, request["max_tokens"])
        user_content = request["messages"][1]["content"]
        self.assertIn("나이아신아마이드 (NIACINAMIDE)", user_content)
        self.assertIn("나이아신아마이드 (NIACINAMIDE) [논문 근거 2건]", user_content)

    async def test_judge_receives_same_korean_evidence_context(self):
        ingredient = IngredientResult(
            name="UREA",
            kor_name="우레아",
            claim="Hydrating",
            eligibility_tier="pubmed_evidence",
            paper_ref="4",
        )
        product = ProductResult(
            product_id="1",
            product_name="보습 크림",
            brand="테스트",
            category="크림",
            matched_count=1,
            matched_ingredients=["UREA"],
        )
        judge_payload = {
            "concern_fit": 5,
            "grounding": 5,
            "conciseness": 5,
            "korean_quality": 5,
            "format_adherence": 5,
            "comment": "ok",
        }
        completion = AsyncMock(
            return_value=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=json.dumps(judge_payload))
                    )
                ]
            )
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))

        await judge_response(
            client,
            "external/judge",
            "건조해요",
            [ingredient],
            [product],
            "우레아를 추천합니다.",
            "judge prompt",
        )

        judge_content = completion.await_args.kwargs["messages"][1]["content"]
        self.assertIn("우레아 (UREA)", judge_content)
        self.assertIn("우레아 (UREA) [논문 근거 4건]", judge_content)

    def test_web_card_prefers_korean_name(self):
        html = (
            Path(__file__).resolve().parent.parent / "app" / "static" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn("ing.kor_name", html)
        self.assertIn("escHtml(ing.kor_name.trim())", html)
        self.assertIn("product.image_url", html)
        self.assertIn("product.product_name", html)


if __name__ == "__main__":
    unittest.main()
