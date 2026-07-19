"""서버측 `/metrics` 스냅샷 + before/after 비교 도구.

Locust 가 보는 건 '클라이언트 측'(응답시간·에러율)이다. 이 스크립트는 '서버 측' Prometheus
메트릭(`recommend_latency_span_seconds`, 폴백률, 요청 결과)을 떠서, 부하 전/후를 비교한다.
→ "느려진 게 어느 단계 때문인가(extract/retrieval/generate)"를 단계별로 분해해 본다.

사용:
    # 스냅샷 저장
    python load/capture_metrics.py http://localhost:8000 --out load/metrics_before.json

    # 두 스냅샷 비교(부하 구간 동안 늘어난 요청만으로 평균 latency 산출)
    python load/capture_metrics.py --diff load/metrics_before.json load/metrics_after.json
"""

import argparse
import json
import re
import urllib.request
from datetime import datetime, timezone

# recommend_latency_span_seconds 의 주요 span (단계별 분해). 전체 span 목록은 app/core/metrics.py 참고.
_SPANS = ("extract", "retrieval", "generate", "total")


def scrape(base_url: str) -> dict:
    """/metrics 를 긁어 관심 메트릭만 추출."""
    url = base_url.rstrip("/") + "/metrics"
    with urllib.request.urlopen(url, timeout=10) as resp:
        text = resp.read().decode("utf-8")

    span_sum, span_count, methods, requests = {}, {}, {}, {}
    for line in text.splitlines():
        m = re.match(r'recommend_latency_span_seconds_sum\{span="([^"]+)"\}\s+([\d.eE+-]+)', line)
        if m:
            span_sum[m.group(1)] = float(m.group(2))
        m = re.match(r'recommend_latency_span_seconds_count\{span="([^"]+)"\}\s+([\d.eE+-]+)', line)
        if m:
            span_count[m.group(1)] = float(m.group(2))
        m = re.match(r'profile_extraction_method_total\{method="([^"]+)"\}\s+([\d.eE+-]+)', line)
        if m:
            methods[m.group(1)] = float(m.group(2))
        m = re.match(r'recommend_requests_total\{status="([^"]+)"\}\s+([\d.eE+-]+)', line)
        if m:
            requests[m.group(1)] = float(m.group(2))

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "span_sum": span_sum,
        "span_count": span_count,
        "methods": methods,
        "requests": requests,
    }


def _print_snapshot(snap: dict) -> None:
    print(f"  스냅샷 @ {snap['timestamp']}  ({snap['base_url']})")
    print(f"  {'span':<14}{'누적요청':>8}{'평균(s)':>10}")
    for s in _SPANS:
        c = snap["span_count"].get(s, 0)
        avg = (snap["span_sum"].get(s, 0) / c) if c else 0
        print(f"  {s:<14}{int(c):>8}{avg:>10.3f}")
    ok = snap["requests"].get("ok", 0)
    err = snap["requests"].get("error", 0)
    print(f"  요청 결과: ok={int(ok)}  error={int(err)}")


def diff(before: dict, after: dict) -> None:
    """두 스냅샷 사이에 '추가된' 요청만으로 단계별 평균 latency·에러율을 산출."""
    print("\n" + "═" * 60)
    print("  서버측 메트릭 before/after 비교 (부하 구간 증분)")
    print("═" * 60)
    print(f"  {'span':<14}{'요청수Δ':>8}{'평균(s)':>12}")
    for s in _SPANS:
        dc = after["span_count"].get(s, 0) - before["span_count"].get(s, 0)
        ds = after["span_sum"].get(s, 0) - before["span_sum"].get(s, 0)
        avg = (ds / dc) if dc else 0
        print(f"  {s:<14}{int(dc):>8}{avg:>12.3f}")

    d_ok = after["requests"].get("ok", 0) - before["requests"].get("ok", 0)
    d_err = after["requests"].get("error", 0) - before["requests"].get("error", 0)
    total = d_ok + d_err
    err_rate = (d_err / total * 100) if total else 0
    print("─" * 60)
    print(f"  요청 결과 Δ: ok={int(d_ok)}  error={int(d_err)}  에러율={err_rate:.1f}%")

    d_llm = after["methods"].get("llm", 0) - before["methods"].get("llm", 0)
    d_rule = after["methods"].get("rule_based", 0) - before["methods"].get("rule_based", 0)
    m_total = d_llm + d_rule
    fb_rate = (d_rule / m_total * 100) if m_total else 0
    print(f"  추출 폴백률 Δ: rule_based={fb_rate:.1f}%  (llm={int(d_llm)}, rule_based={int(d_rule)})")
    print("═" * 60)
    print("  ※ 폴백률↑ = 부하로 LLM 추출이 타임아웃돼 규칙기반으로 떨어진 신호")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url", nargs="?", help="앱 주소 (예: http://localhost:8000)")
    ap.add_argument("--out", help="스냅샷 저장 경로(JSON)")
    ap.add_argument("--diff", nargs=2, metavar=("BEFORE", "AFTER"), help="두 스냅샷 비교")
    args = ap.parse_args()

    if args.diff:
        before = json.loads(open(args.diff[0], encoding="utf-8").read())
        after = json.loads(open(args.diff[1], encoding="utf-8").read())
        diff(before, after)
        return

    if not args.base_url:
        ap.error("base_url 또는 --diff 중 하나가 필요합니다.")

    snap = scrape(args.base_url)
    _print_snapshot(snap)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
        print(f"\n  저장: {args.out}")


if __name__ == "__main__":
    main()
