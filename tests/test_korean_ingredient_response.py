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
