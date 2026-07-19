"""추천 파이프라인의 비즈니스 메트릭 정의.

GPU(vLLM) 메트릭만으로는 "추천 1건이 왜 N초 걸렸나"가 안 보인다.
여기서 요청당 단계별 latency(span) / 폴백률 / 성분 수 / 요청 결과를 `/metrics`에 노출한다.

소비처: 중앙 모니터링(EC2 Prometheus)은 vLLM만 스크레이프한다 — 앱은 로컬 개발 머신이라
상시 스크레이프 대상이 아니다. 앱 `/metrics`는 **로컬 최적화 도구**가 직접 긁어 소비한다:
  - `load/latency_bench.py` — 단건 span 트레이스 (recommend_latency_span_seconds)
  - `load/capture_metrics.py` — 부하 전/후 비교 (같은 span + 요청 결과)
(운영 배포 시엔 이 `/metrics`를 Prometheus 타깃에 추가하면 그대로 대시보드화된다.)

`prometheus_client` 기본 레지스트리에 등록되므로, main.py의
Instrumentator().expose(app) 가 노출하는 `/metrics` 에 함께 실린다.
"""

from prometheus_client import Counter, Histogram

# 프로필 추출에 실제 사용된 방식 (폴백률 = LLM 장애 조기경보)
#   method = llm | rule_based
profile_extraction_method_total = Counter(
    "profile_extraction_method_total",
    "프로필 추출에 사용된 방식 (rule_based 비율 상승 = LLM 장애 신호)",
    ["method"],
)

# 추천 요청 처리 결과
#   status = ok | error | rejected
recommend_requests_total = Counter(
    "recommend_requests_total",
    "추천 요청 처리 결과",
    ["status"],
)

# 요청당 latency 트레이스 — 파이프라인 단계별 소요시간(초). **단건 latency 분해의 단일 소스.**
#   span = cache_lookup | flight_wait | extract | retrieval | gate_wait
#          | generate | generate_ttft | generate_decode | overhead | total
#   (P1 스트리밍 후 generate를 generate_ttft/generate_decode로 분리.
#    flight_wait = single-flight 락 대기 — 스탬피드 시 coalesced 요청이 여기서 기다린다.
#    retrieval = Neo4j 조회(효능→성분→제품), generate = LLM 응답 생성.)
recommend_latency_span_seconds = Histogram(
    "recommend_latency_span_seconds",
    "추천 요청의 단계별 latency(초) — latency 최적화 트레이스",
    ["span"],
    buckets=(0.005, 0.05, 0.2, 0.5, 1, 2, 4, 8, 16, 32, 64, 120),
)

# 추천 응답 캐시 조회 결과 (hit = GPU 호출 0으로 처리 → 유효 처리량↑)
#   result = hit | miss | coalesced
#   coalesced = 미스였지만 single-flight 대기 후 리더의 결과를 받아 GPU를 건너뛴 요청
#   (스탬피드 제거 효과 = coalesced 수. miss만 실제로 GPU를 쳤다.)
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
