import unittest
from unittest.mock import AsyncMock, patch

from app.domain.enums import Concern
from eval.run_retrieval_eval import (
    _JUDGE_PROMPT,
    _unexpected_product_zero_rate,
    eval_case,
)


def test_unexpected_zero_rate_excludes_intentional_refusals():
    cases = [
        {"expects_products": True, "n_products": 0},
        {"expects_products": True, "n_products": 2},
        {"expects_products": False, "n_products": 0},
        {"expects_products": False, "n_products": 0},
    ]

    assert _unexpected_product_zero_rate(cases) == 0.5


def test_multiconcern_judge_allows_partial_product_relevance():
    assert "제품 하나가 모든 고민을 동시에 해결할 필요는 없습니다" in _JUDGE_PROMPT


class RetrievalServiceParityTest(unittest.IsolatedAsyncioTestCase):
    async def test_eval_case_reuses_caution_and_service_product_selection(self):
        case = {
            "id": 37,
            "message": "새 화장품을 쓴 뒤 피부가 따갑고 화끈거려요.",
            "concerns": ["IRRITATED_SKIN"],
            "constraints": [],
        }
        raw_ingredients = [
            {"name": "LINALOOL", "graph_score": 1.0, "eligibility_tier": "pubmed_evidence"},
            {"name": "PANTHENOL", "graph_score": 0.8, "eligibility_tier": "pubmed_evidence"},
        ]
        safe_ingredients = [raw_ingredients[1]]
        products = [{
            "product_id": "p1",
            "product_name": "진정 크림",
            "category": "크림",
            "matched_ingredients": ["PANTHENOL"],
        }]

        with (
            patch(
                "eval.run_retrieval_eval.query_ingredients_by_effects",
                new=AsyncMock(return_value=raw_ingredients),
            ),
            patch(
                "eval.run_retrieval_eval.apply_caution_filter",
                new=AsyncMock(return_value=safe_ingredients),
            ) as caution,
            patch(
                "eval.run_retrieval_eval.select_products",
                new=AsyncMock(return_value=products),
            ) as select,
            patch(
                "eval.run_retrieval_eval._judge",
                new=AsyncMock(return_value={"ingredients": [1], "products": [1]}),
            ),
        ):
            result = await eval_case(case, object(), "judge", 10)

        caution.assert_awaited_once_with(raw_ingredients, [Concern.IRRITATED_SKIN])
        select.assert_awaited_once_with(
            case["message"],
            [Concern.IRRITATED_SKIN],
            [{"name": "PANTHENOL", "weight": 0.8}],
        )
        self.assertEqual(1, result["n_products"])
        self.assertEqual(1.0, result["product_precision"])
