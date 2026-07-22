"""대화 이력(P1) 순수 로직 유닛 테스트 — Redis 불필요."""

import unittest

from app.repositories.conversation_store import _key
from app.services.recommend_service import _heuristic_kind, _slim_products


class ConversationKeyTest(unittest.TestCase):
    def test_key_prefix(self):
        self.assertEqual("conv:v1:abc", _key("abc"))


class SlimProductsTest(unittest.TestCase):
    def test_from_dicts(self):
        rows = [{"product_name": "토너A", "brand": "브랜드", "category": "토너", "rating": 4.7, "x": 1}]
        self.assertEqual(
            [{"name": "토너A", "brand": "브랜드", "category": "토너", "rating": 4.7}],
            _slim_products(rows),
        )

    def test_from_objects(self):
        class P:
            product_name, brand, category, rating = "크림B", "B", "크림", 4.2
        self.assertEqual(
            [{"name": "크림B", "brand": "B", "category": "크림", "rating": 4.2}],
            _slim_products([P()]),
        )

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


if __name__ == "__main__":
    unittest.main()
