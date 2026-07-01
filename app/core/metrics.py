"""추천 파이프라인의 비즈니스 메트릭 정의.

GPU(vLLM) 메트릭만으로는 "추천 1건이 왜 N초 걸렸나"가 안 보인다.
여기서 단계별 latency / 폴백률 / 성분 수 / 요청 결과를 노출해
Prometheus가 스크레이프하고 Grafana에서 파이프라인 내부를 분해해 본다.

`prometheus_client` 기본 레지스트리에 등록되므로, main.py의
Instrumentator().expose(app) 가 노출하는 `/metrics` 에 함께 실린다.
"""

import time
from contextlib import contextmanager

from prometheus_client import Counter, Histogram

# 추천 파이프라인 단계별 처리시간 (초)
#   stage = extract | neo4j | llm_response
#   "추천 1건 5초"가 추출/Neo4j/응답으로 분해되어 보이게 한다.
recommend_stage_latency_seconds = Histogram(
    "recommend_stage_latency_seconds",
    "추천 파이프라인 단계별 처리시간(초)",
    ["stage"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0),
)

# 프로필 추출에 실제 사용된 방식 (폴백률 = LLM 장애 조기경보)
#   method = llm | rule_based
profile_extraction_method_total = Counter(
    "profile_extraction_method_total",
    "프로필 추출에 사용된 방식 (rule_based 비율 상승 = LLM 장애 신호)",
    ["method"],
)

# 추천 요청 처리 결과
#   status = ok | error
recommend_requests_total = Counter(
    "recommend_requests_total",
    "추천 요청 처리 결과",
    ["status"],
)

# 요청당 latency 트레이스 — 파이프라인 단계별 소요시간(초).
#   span = cache_lookup | extract | retrieval | gate_wait | generate | overhead | total
#   (P1 스트리밍 후 generate를 generate_ttft/generate_decode로 분리)
#   latency 최적화 실험에서 "어디서 시간이 가나"를 단건 단위로 분해한다.
recommend_latency_span_seconds = Histogram(
    "recommend_latency_span_seconds",
    "추천 요청의 단계별 latency(초) — latency 최적화 트레이스",
    ["span"],
    buckets=(0.005, 0.05, 0.2, 0.5, 1, 2, 4, 8, 16, 32, 64, 120),
)

# 추천 응답 캐시 조회 결과 (hit = GPU 호출 0으로 처리 → 유효 처리량↑)
#   result = hit | miss
recommend_cache_total = Counter(
    "recommend_cache_total",
    "추천 응답 캐시 조회 결과 (hit = GPU 호출 없이 처리)",
    ["result"],
)

# 추천 1건에서 Neo4j가 반환한 성분 수 (0개 = 데이터/쿼리 회귀 신호)
recommend_ingredients_found = Histogram(
    "recommend_ingredients_found",
    "추천 1건에서 Neo4j가 반환한 성분 수",
    buckets=(0, 1, 3, 5, 10, 20, 50),
)


@contextmanager
def track_stage(stage: str):
    """`with track_stage("neo4j"):` 블록의 wall-clock 시간을 해당 stage에 기록.

    블록 안에서 예외가 나도(즉 단계가 실패해도) 거기까지 걸린 시간은 기록된다.
    async 호출을 `with` 블록 안에 두고 써도 측정에 문제 없다(벽시계 측정).
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        recommend_stage_latency_seconds.labels(stage=stage).observe(
            time.perf_counter() - start
        )
