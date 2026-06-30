#!/usr/bin/env bash
#
# 부하 테스트 1회 오케스트레이션:
#   서버측 메트릭 스냅샷(before) → Locust 단계적 램프업 부하 → 스냅샷(after) → before/after 비교
#
# 산출물(load/results/<tag>/):
#   - report.html      : Locust 클라이언트측 리포트(응답시간 분포·RPS·실패)
#   - result_*.csv     : Locust 통계 CSV
#   - metrics_*.json   : 서버측 /metrics 스냅샷(before/after)
#
# 사용:
#   ./load/run_load_test.sh                 # tag=baseline, localhost:8000
#   TAG=after-semaphore ./load/run_load_test.sh
#   BASE_URL=http://localhost:8000 TAG=baseline ./load/run_load_test.sh
#
# 전제: 앱이 떠 있고(Postgres/Redis/Neo4j/vLLM 연결 가능), locust 설치됨(pip install -r load/requirements.txt)

set -uo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
TAG="${TAG:-baseline}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$HERE/results/$TAG"
mkdir -p "$OUT"

green() { printf "\033[32m%s\033[0m\n" "$*"; }
red()   { printf "\033[31m%s\033[0m\n" "$*"; }

echo "═══════════════════════════════════════════════════════════"
echo "  부하 테스트  tag=$TAG  →  $BASE_URL"
echo "  결과 디렉토리: $OUT"
echo "═══════════════════════════════════════════════════════════"

# 0) 헬스 체크
if ! curl -s -m 5 -o /dev/null "$BASE_URL/metrics"; then
  red "✗ 앱 접근 불가: $BASE_URL (앱이 떠 있는지 확인)"
  exit 1
fi
green "✓ 앱 접근 OK"

# 1) before 스냅샷
echo ""; echo "── before 스냅샷 ──"
python3 "$HERE/capture_metrics.py" "$BASE_URL" --out "$OUT/metrics_before.json"

# 2) Locust 단계적 램프업 (헤드리스)
echo ""; echo "── Locust 부하 시작 (5→10→25→50, 약 7분) ──"
locust -f "$HERE/locustfile.py,$HERE/staged_shape.py" \
       --headless --host "$BASE_URL" \
       --html "$OUT/report.html" --csv "$OUT/result"

# 3) after 스냅샷
echo ""; echo "── after 스냅샷 ──"
python3 "$HERE/capture_metrics.py" "$BASE_URL" --out "$OUT/metrics_after.json"

# 4) 서버측 before/after 비교
python3 "$HERE/capture_metrics.py" --diff "$OUT/metrics_before.json" "$OUT/metrics_after.json"

echo ""
green "완료 → $OUT (report.html 을 브라우저로 열어 p95/p99·RPS·실패 확인)"
