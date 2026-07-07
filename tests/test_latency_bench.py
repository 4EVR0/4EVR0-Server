"""latency_bench 백분위 계산 유닛 테스트 (이슈 #34).

기존 `int(q*len)`는 nearest-rank보다 한 칸 높게 잡아(n=10 p90 → 최댓값) p50/p90을 왜곡했다.
`_percentile`은 nearest-rank(rank=ceil(q*n), 0-index=rank-1)를 쓴다.
"""

import unittest

from load.latency_bench import _percentile


class PercentileTest(unittest.TestCase):
    def test_n10_nearest_rank(self):
        xs = list(range(1, 11))  # 1..10
        # nearest-rank: p50 → ceil(5)=5 → 5번째=5, p90 → ceil(9)=9 → 9번째=9
        self.assertEqual(_percentile(xs, 0.5), 5)
        self.assertEqual(_percentile(xs, 0.9), 9)
        # 기존 버그(int(q*n))였다면 p50=6, p90=10(최댓값)이 나왔음 — 회귀 방지
        self.assertNotEqual(_percentile(xs, 0.9), 10)

    def test_n2(self):
        xs = [10, 20]
        self.assertEqual(_percentile(xs, 0.5), 10)  # ceil(1)=1 → 1번째
        self.assertEqual(_percentile(xs, 0.9), 20)  # ceil(1.8)=2 → 2번째
        self.assertEqual(_percentile(xs, 1.0), 20)

    def test_n1(self):
        self.assertEqual(_percentile([42], 0.5), 42)
        self.assertEqual(_percentile([42], 0.9), 42)

    def test_unsorted_input(self):
        self.assertEqual(_percentile([9, 1, 5, 3, 7], 0.5), 5)  # 정렬 후 1,3,5,7,9 → ceil(2.5)=3 → 3번째=5

    def test_q_zero_and_edges(self):
        xs = [1, 2, 3, 4]
        self.assertEqual(_percentile(xs, 0.0), 1)   # ceil(0)=0 → max(0,-1)=0 → 첫 번째
        self.assertEqual(_percentile(xs, 1.0), 4)   # ceil(4)=4 → 4번째=마지막

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            _percentile([], 0.5)


if __name__ == "__main__":
    unittest.main()
