"""대화 이력(P1) 순수 로직 유닛 테스트 — Redis 불필요."""

import unittest

from app.repositories.conversation_store import _key
from app.services.recommend_service import _slim_products


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


if __name__ == "__main__":
    unittest.main()
