#!/usr/bin/env bash
#
# Tier 2 트래픽 생성기 — 실제 HTTP 추천 요청을 보내 앱 계측(Phase 2)/로깅(Phase 3)을 깨운다.
#
# test_e2e.py 와 달리 이 스크립트는 HTTP API(/sessions, /recommend)를 친다.
# → /metrics 의 recommend_stage_latency_*, profile_extraction_method_total 등이 실제로 증가하고,
#   각 요청의 trace_id(응답 X-Trace-ID)가 로그/Loki 로 흘러간다.
#
# 사용법:
#   ./generate_traffic.sh                 # localhost:8000 에 기본 케이스 전부
#   BASE_URL=http://localhost:8000 ./generate_traffic.sh
#   ./generate_traffic.sh 3               # 케이스를 3회 반복(부하 늘리기)
#
# 전제: 앱이 떠 있고(Tier 2 가이드 참고), Postgres/Redis/Neo4j/vLLM 연결 가능.

set -uo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
REPEAT="${1:-1}"

# 다양한 피부 고민 → 서로 다른 effect 매핑/Neo4j 결과를 유도
MESSAGES=(
  "모공이 넓고 피지가 많아서 고민이에요"
  "피부가 건조하고 각질이 자주 일어나요"
  "잡티랑 색소침착 때문에 미백 제품 찾고 있어요"
  "눈가 주름이 늘고 탄력이 떨어진 것 같아요"
  "여드름이랑 트러블이 계속 올라와요"
  "민감성이라 자극 없는 진정 제품이 필요해요"
)

red()   { printf "\033[31m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
dim()   { printf "\033[2m%s\033[0m\n" "$*"; }

# ── 0. 헬스 체크 ────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════"
echo "  Tier 2 트래픽 생성  →  $BASE_URL"
echo "═══════════════════════════════════════════════════════════"
if ! curl -s -m 5 -o /dev/null "$BASE_URL/metrics"; then
  red "✗ 앱에 접근 불가: $BASE_URL — 앱이 떠 있는지 확인하세요."
  exit 1
fi
green "✓ 앱 접근 OK"

# ── 1. 세션 생성 ────────────────────────────────────────────────
SID=$(curl -s -m 10 -X POST "$BASE_URL/api/v1/sessions" \
      | python3 -c "import sys,json;print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)
if [ -z "$SID" ]; then
  red "✗ 세션 생성 실패 (Postgres 연결 확인 — .env가 docker 호스트명이면 localhost로 override 필요)"
  exit 1
fi
green "✓ 세션 생성: $SID"
echo ""

# ── 2. 추천 요청 반복 ───────────────────────────────────────────
TRACE_FILE=$(mktemp)
printf "%-3s %-34s %-7s %6s %5s %-18s\n" "#" "메시지" "status" "초" "성분" "trace_id"
echo "───────────────────────────────────────────────────────────────────────────────"

n=0; ok=0; fail=0
for r in $(seq 1 "$REPEAT"); do
  for msg in "${MESSAGES[@]}"; do
    n=$((n+1))
    hdr=$(mktemp); body=$(mktemp)
    payload=$(python3 -c "import json,sys;print(json.dumps({'session_id':sys.argv[1],'message':sys.argv[2]},ensure_ascii=False))" "$SID" "$msg")

    read -r code ttime < <(curl -s -o "$body" -D "$hdr" -w '%{http_code} %{time_total}' \
        -m 120 -X POST "$BASE_URL/api/v1/recommend" \
        -H 'Content-Type: application/json' -d "$payload")

    trace=$(grep -i '^x-trace-id:' "$hdr" | awk '{print $2}' | tr -d '\r')
    ing=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(len(d.get('ingredients',[])))" "$body" 2>/dev/null || echo "-")
    short_msg=$(echo "$msg" | cut -c1-32)

    if [ "$code" = "200" ]; then
      ok=$((ok+1)); echo "$trace" >> "$TRACE_FILE"
      printf "%-3s %-34s \033[32m%-7s\033[0m %6.2f %5s %-18s\n" "$n" "$short_msg" "$code" "$ttime" "$ing" "$trace"
    else
      fail=$((fail+1))
      printf "%-3s %-34s \033[31m%-7s\033[0m %6.2f %5s %-18s\n" "$n" "$short_msg" "$code" "$ttime" "$ing" "$trace"
      dim "      └ $(head -c 160 "$body")"
    fi
    rm -f "$hdr" "$body"
    sleep 0.3
  done
done

echo ""
echo "═══════════════════════════════════════════════════════════"
green "  완료: $ok 성공 / $fail 실패 (총 $n)"
echo "═══════════════════════════════════════════════════════════"

# ── 3. 메트릭 분해 즉시 확인 (Phase 2) ──────────────────────────
echo ""
echo "── /metrics 단계별 분해 (Phase 2) ──"
MFILE=$(mktemp)
curl -s "$BASE_URL/metrics" > "$MFILE"
# 주의: `python3 - <<'PY'` 는 heredoc 이 stdin 이라, 메트릭은 파이프가 아닌 파일 인자로 넘긴다.
python3 - "$MFILE" <<'PY'
import sys, re
sums, counts, methods = {}, {}, {}
for line in open(sys.argv[1]):
    m = re.match(r'recommend_stage_latency_seconds_sum\{stage="([^"]+)"\}\s+([\d.eE+-]+)', line)
    if m: sums[m.group(1)] = float(m.group(2))
    m = re.match(r'recommend_stage_latency_seconds_count\{stage="([^"]+)"\}\s+([\d.eE+-]+)', line)
    if m: counts[m.group(1)] = float(m.group(2))
    m = re.match(r'profile_extraction_method_total\{method="([^"]+)"\}\s+([\d.eE+-]+)', line)
    if m: methods[m.group(1)] = float(m.group(2))
print(f"  {'stage':<14}{'요청수':>6}{'평균(s)':>10}")
for s in ("extract","neo4j","llm_response"):
    c = counts.get(s,0); avg = (sums.get(s,0)/c) if c else 0
    print(f"  {s:<14}{int(c):>6}{avg:>10.3f}")
tot = sum(methods.values())
if tot:
    fb = methods.get('rule_based',0)/tot
    print(f"  폴백률(rule_based): {fb*100:.1f}%   (llm={int(methods.get('llm',0))}, rule_based={int(methods.get('rule_based',0))})")
PY
rm -f "$MFILE"

# ── 4. Loki 조회용 trace_id 안내 (Phase 3) ──────────────────────
echo ""
echo "── Loki 추적용 trace_id (Phase 3) ──"
echo "  Grafana Explore → Loki 에서:"
last_trace=$(tail -1 "$TRACE_FILE" 2>/dev/null)
if [ -n "$last_trace" ]; then
  echo "    {job=\"was-app\"} | trace_id=\"$last_trace\""
fi
echo "  전체 trace_id 목록: $TRACE_FILE"
echo ""
