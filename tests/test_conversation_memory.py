"""대화 이력(P1) 순수 로직 유닛 테스트 — Redis 불필요."""

import unittest

from app.repositories.conversation_store import _key
from app.services.recommend_service import (
    _extract_ranking,
    _heuristic_kind,
    _reorder_by_ranking,
    _slim_products,
)


class ConversationKeyTest(unittest.TestCase):
    def test_key_prefix(self):
        self.assertEqual("conv:v1:abc", _key("abc"))


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

    def test_concern_cue_is_new(self):
        self.assertEqual("new", _heuristic_kind("민감성 피부에 좋은거 있어?", self._HIST))
        self.assertEqual("new", _heuristic_kind("여드름 때문에 고민이야", self._HIST))

    def test_ambiguous_returns_none(self):
        # 후속 큐도 고민 큐도 없으면 None(→ LLM 위임)
        self.assertIsNone(_heuristic_kind("이 제품들 사용 순서 알려줘", self._HIST))


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
