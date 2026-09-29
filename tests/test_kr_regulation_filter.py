import unittest
from unittest import mock

from app.clients import neo4j_client
from app.schemas.recommend import IngredientResult
from app.services.recommend_service import _compose_user_content, _kr_limit_phrase

ROWS = [{"name": "SALICYLIC ACID", "kor_name": "살리실릭애씨드", "claim": "Acne",
         "eligibility_tier": "pubmed_evidence", "paper_ref": "3", "graph_score": 1.0,
         "kr_reg_status": "restricted", "kr_limit_note": "보존제로서 0.5%"}]


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def __aiter__(self):
        async def gen():
            for row in self._rows:
                yield row
        return gen()


class _Session:
    def __init__(self):
        self.queries = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def run(self, query, **params):
        self.queries.append(query)
        return _Result(ROWS)


class IngredientQueryKrFilterTest(unittest.IsolatedAsyncioTestCase):
    async def test_excludes_banned_before_limit_and_returns_limit_note(self):
        session = _Session()
        driver = mock.Mock()
        driver.session.return_value = session
        with mock.patch.object(neo4j_client, "_get_driver", return_value=driver):
            rows = await neo4j_client.query_ingredients_by_effects(["acne"])
        query = session.queries[0]
        banned_filter = "coalesce(i.kr_reg_status, 'none') <> 'banned'"
        self.assertIn(banned_filter, query)
        # LIMIT 20 이 필터 후 성분 수가 되도록 집계(head(collect)) 전 WHERE 에 있어야 한다.
        self.assertLess(query.index(banned_filter), query.index("head(collect("))
        self.assertIn("i.kr_limit_note       AS kr_limit_note", query)
        self.assertEqual("보존제로서 0.5%", rows[0]["kr_limit_note"])


class KrLimitPhraseTest(unittest.TestCase):
    def test_only_restricted_gets_phrase_and_long_note_is_cut(self):
        self.assertEqual("", _kr_limit_phrase(IngredientResult(name="TALC", kr_reg_status="conditional",
                                                               kr_limit_note="x")))
        self.assertEqual("국내 배합한도가 있는 성분입니다(1%).",
                         _kr_limit_phrase(IngredientResult(name="PHENOXYETHANOL", kr_reg_status="restricted",
                                                           kr_limit_note=" 1% ")))
        phrase = _kr_limit_phrase(IngredientResult(name="X", kr_reg_status="restricted", kr_limit_note="가" * 200))
        self.assertTrue(phrase.endswith("…)."))
        self.assertLess(len(phrase), 120)

    def test_llm_context_carries_full_limit_note(self):
        ingredient = IngredientResult(**{k: v for k, v in ROWS[0].items() if k != "graph_score"})
        content = _compose_user_content("여드름", [ingredient], [])
        self.assertIn("(국내 배합한도: 보존제로서 0.5%)", content)
        plain = _compose_user_content("여드름", [IngredientResult(name="NIACINAMIDE")], [])
        self.assertNotIn("국내 배합한도", plain)


if __name__ == "__main__":
    unittest.main()
