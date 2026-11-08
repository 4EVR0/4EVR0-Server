import unittest
from unittest import mock

from app.clients import neo4j_client
from app.schemas.recommend import IngredientResult
from app.services.recommend_service import _compose_user_content, _normalize_response_text
from app.services.regulatory_text import strip_regulatory_text

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


USER_REPORTED = ("<보존제> ∙ 살리실릭애씨드로서 0.5% ∙ 영유아용 제품류 또는 만 13세 이하 어린이가 사용할 수 있음을 "
                 "특정하여 표시하는 제품에는 사용금지(다만, 샴푸는 제외) <기타배합한도> ∙ 인체세정용 제품류에 "
                 "살리실릭애씨드로서 2% ∙ 사용 후 씻어내는 두발용 제품류에 살리실릭애씨드로서 3% ∙ 기능성화장품의 "
                 "유효성분으로 사용하는 경우에 한하며 기타 제품에는 사용금지")


class RegulatoryTextNotShownTest(unittest.TestCase):
    def test_llm_context_has_no_limit_note(self):
        ingredient = IngredientResult(**{k: v for k, v in ROWS[0].items() if k != "graph_score"})
        content = _compose_user_content("여드름", [ingredient], [])
        self.assertNotIn("배합한도", content)
        self.assertNotIn("0.5%", content)

    def test_strips_reported_notice_but_keeps_explanation(self):
        text = ("성분 설명\n"
                f"- 살리실산 (SALICYLIC ACID): 각질과 피지 관리에 도움이 된다는 연구가 있어요. (국내 배합한도: {USER_REPORTED})\n"
                f"{USER_REPORTED}\n"
                "- 나이아신아마이드: 피지 조절과 관련된 연구가 있어요.")
        cleaned, removed = strip_regulatory_text(text)
        self.assertTrue(removed)
        for word in ("배합한도", "사용금지", "0.5%", "13세", "유효성분으로"):
            self.assertNotIn(word, cleaned)
        self.assertIn("각질과 피지 관리에 도움이 된다는 연구가 있어요.", cleaned)
        self.assertIn("- 나이아신아마이드: 피지 조절과 관련된 연구가 있어요.", cleaned)

    def test_sentence_level_removal_and_untouched_text(self):
        cleaned, removed = strip_regulatory_text("좋은 성분이에요. 국내 배합한도가 있는 성분입니다. 꾸준히 써 보세요.")
        self.assertTrue(removed)
        self.assertEqual("좋은 성분이에요. 꾸준히 써 보세요.", cleaned)
        plain = "함량·배합도 확인되지 않아요."
        self.assertEqual((plain, False), strip_regulatory_text(plain))

    def test_response_normalizer_applies_strip(self):
        text, _ = _normalize_response_text("추천해요.\n" + USER_REPORTED, [])
        self.assertEqual("추천해요.", text)


if __name__ == "__main__":
    unittest.main()


def test_web_ui_and_api_do_not_carry_limit_note():
    # 베타(2026-11-08): 올리브 UI 성분 카드가 API의 kr_limit_note(고시 원문)를 그대로 그림
    from pathlib import Path
    import inspect
    from app.services import recommend_service
    html = Path("app/static/index.html").read_text(encoding="utf-8")
    assert "ing.kr_limit_note" not in html
    assert 'kr_limit_note=row.get("kr_limit_note")' not in inspect.getsource(recommend_service)
