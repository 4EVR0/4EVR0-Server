"""제품 선정 로직 유닛 테스트 (이슈 #40 후속: 요청 카테고리 존중 + 카테고리 다양성).

순수 함수만 검증 — Neo4j/LLM 불필요.
  - _requested_categories: 메시지에서 명시 요청 카테고리 추출(+ '스킨' 오탐 방지)
  - _appropriate_categories: 요청 카테고리 우선 존중
  - _diversify: 한 카테고리 상위 독식 방지
"""

import unittest

from app.domain.enums import Concern
from app.services.recommend_service import (
    _appropriate_categories,
    _diversify,
    _is_sensitivity_query,
    _requested_categories,
)


class RequestedCategoriesTest(unittest.TestCase):
    def test_explicit_toner(self):
        self.assertEqual({"토너"}, _requested_categories("여드름 토너 추천해줘"))

    def test_skin_synonym_maps_to_toner(self):
        self.assertEqual({"토너"}, _requested_categories("여드름 나는데 스킨/토너 추천"))

    def test_skin_compounds_not_matched(self):
        # '스킨타입'·'스킨케어' 등 합성어는 카테고리 요청이 아니다(오탐 방지).
        self.assertEqual(set(), _requested_categories("내 스킨타입에 맞는거 추천"))
        self.assertEqual(set(), _requested_categories("스킨케어 루틴 알려줘"))

    def test_no_category_word(self):
        self.assertEqual(set(), _requested_categories("여드름이 고민이에요"))

    def test_serum(self):
        self.assertEqual({"세럼"}, _requested_categories("미백 세럼 추천"))

    def test_empty(self):
        self.assertEqual(set(), _requested_categories(""))


class AppropriateCategoriesTest(unittest.TestCase):
    def test_no_request_returns_concern_base(self):
        cats = _appropriate_categories([Concern.ACNE])
        self.assertIn("토너", cats)
        self.assertNotIn("크림", cats)  # ACNE는 크림 제외

    def test_request_narrows_to_intersection(self):
        # 토너 요청 + ACNE 적합 카테고리에 토너 있음 → 토너로 좁혀짐
        self.assertEqual(["토너"], _appropriate_categories([Concern.ACNE], {"토너"}))

    def test_request_overrides_when_disjoint(self):
        # ACNE 적합엔 크림 없지만, 사용자가 크림을 콕 집으면 존중(사용자 의도 우선)
        self.assertEqual(["크림"], _appropriate_categories([Concern.ACNE], {"크림"}))


class DiversifyTest(unittest.TestCase):
    @staticmethod
    def _p(cat, name):
        return {"category": cat, "product_name": name}

    def test_caps_per_category(self):
        # 카테고리가 충분하면 상한(2)이 유지되고 다른 카테고리가 진입한다.
        prods = ([self._p("앰플", f"a{i}") for i in range(5)]
                 + [self._p("토너", "t1"), self._p("토너", "t2")]
                 + [self._p("세럼", "s1"), self._p("세럼", "s2")])
        out = _diversify(prods, per_category=2, total=6)
        cats = [p["category"] for p in out]
        self.assertEqual(2, cats.count("앰플"))  # 앰플은 상한 2로 제한
        self.assertIn("토너", cats)  # 다른 카테고리 진입 보장
        self.assertIn("세럼", cats)

    def test_backfills_when_cap_underfills(self):
        # 카테고리가 하나뿐이면 상한 무시하고 total까지 채운다(제품 수 유지)
        prods = [self._p("앰플", f"a{i}") for i in range(6)]
        out = _diversify(prods, per_category=2, total=6)
        self.assertEqual(6, len(out))

    def test_preserves_ranking_order(self):
        prods = [self._p("앰플", "a1"), self._p("토너", "t1"), self._p("앰플", "a2")]
        out = _diversify(prods, per_category=2, total=3)
        self.assertEqual(["a1", "t1", "a2"], [p["product_name"] for p in out])


class SensitivityQueryTest(unittest.TestCase):
    def test_sensitivity_concerns_trigger(self):
        self.assertTrue(_is_sensitivity_query([Concern.SENSITIVE_SKIN]))
        self.assertTrue(_is_sensitivity_query([Concern.REDNESS]))
        self.assertTrue(_is_sensitivity_query([Concern.ROSACEA_PRONE]))
        self.assertTrue(_is_sensitivity_query([Concern.ACNE, Concern.IRRITATED_SKIN]))

    def test_non_sensitivity_does_not_trigger(self):
        # 여드름·색소·노화 단독은 CAUTION 필터 대상 아님(레티놀·산이 정답인 케이스)
        self.assertFalse(_is_sensitivity_query([Concern.ACNE]))
        self.assertFalse(_is_sensitivity_query([Concern.HYPERPIGMENTATION]))
        self.assertFalse(_is_sensitivity_query([Concern.AGING_SIGNS]))
        self.assertFalse(_is_sensitivity_query([]))


if __name__ == "__main__":
    unittest.main()
