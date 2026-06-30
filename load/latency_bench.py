"""단건 latency 벤치 — 추천 1건의 latency를 단계별(span)로 분해해 본다.

부하 테스트(동시성)와 별개로, **저부하 단건 반복**으로 "한 요청이 어디서 시간을 쓰나"를 잰다.
서버가 노출하는 `recommend_latency_span_seconds{span}` 히스토그램의 before/after 증분으로
span별 평균을 산출하고, 클라이언트 측 total의 분포(p50/p90)를 함께 본다.

사용:
    # 캐시 미스(매번 신규 문장) — 실제 GPU 경로 latency 분해 (baseline)
    python load/latency_bench.py --n 10 --mode miss

    # 캐시 히트(같은 문장 반복) — 히트 경로(=Redis) latency
    python load/latency_bench.py --n 10 --mode hit

span: cache_lookup / extract / retrieval / gate_wait / generate / overhead / total
(P1 스트리밍 후 generate → generate_ttft/generate_decode 로 분리 예정)
"""

import argparse
import json
import re
import statistics
import time
import urllib.request
import uuid

_SPANS = ["cache_lookup", "extract", "retrieval", "gate_wait", "generate", "overhead", "total"]
_BASE_MSG = "피부가 건조하고 각질이 일어나서 보습 잘 되는 화장품 추천해줘"


def _post(base, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else b""
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=130) as r:
        return json.loads(r.read().decode())


def _span_snapshot(base):
    """recommend_latency_span_seconds 의 span별 (sum, count) 스냅샷."""
    with urllib.request.urlopen(base.rstrip("/") + "/metrics", timeout=10) as r:
        text = r.read().decode()
    sums, counts = {}, {}
    for line in text.splitlines():
        m = re.match(r'recommend_latency_span_seconds_sum\{span="([^"]+)"\}\s+([\d.eE+-]+)', line)
        if m:
            sums[m.group(1)] = float(m.group(2))
        m = re.match(r'recommend_latency_span_seconds_count\{span="([^"]+)"\}\s+([\d.eE+-]+)', line)
        if m:
            counts[m.group(1)] = float(m.group(2))
    return sums, counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--n", type=int, default=10, help="요청 수")
    ap.add_argument("--mode", choices=["miss", "hit"], default="miss",
                    help="miss=매번 신규 문장(GPU 경로) / hit=같은 문장 반복(캐시 경로)")
    args = ap.parse_args()

    sid = _post(args.base, "/api/v1/sessions")["session_id"]
    # 워밍업 1건(분포에서 제외)
    _post(args.base, "/api/v1/recommend", {"session_id": sid, "message": "워밍업 " + uuid.uuid4().hex})

    s0, c0 = _span_snapshot(args.base)
    totals = []
    fixed_msg = f"{_BASE_MSG} {uuid.uuid4().hex}"  # hit 모드용 고정 문장
    print(f"실행: n={args.n} mode={args.mode}  →  {args.base}")
    for i in range(args.n):
        msg = f"{_BASE_MSG} {uuid.uuid4().hex}" if args.mode == "miss" else fixed_msg
        t = time.time()
        _post(args.base, "/api/v1/recommend", {"session_id": sid, "message": msg})
        dt = time.time() - t
        totals.append(dt)
        print(f"  req {i + 1:>2}: {dt:>7.3f}s")
    s1, c1 = _span_snapshot(args.base)

    print("\n── span별 평균 (서버 측, 이번 구간 증분) ──")
    print(f"  {'span':<14}{'평균(ms)':>10}{'요청수':>7}")
    for span in _SPANS:
        dc = c1.get(span, 0) - c0.get(span, 0)
        ds = s1.get(span, 0) - s0.get(span, 0)
        avg = (ds / dc * 1000) if dc else 0
        print(f"  {span:<14}{avg:>10.1f}{int(dc):>7}")
    print("  (gate_wait는 extract/generate 안에 포함된 '대기' 성분 — overhead엔 미포함)")

    ts = sorted(totals)
    p = lambda q: ts[min(len(ts) - 1, int(q * len(ts)))]
    print("\n── 클라이언트 측 total 분포 ──")
    print(f"  n={len(ts)}  mean={statistics.mean(ts):.3f}s  p50={p(0.5):.3f}s  p90={p(0.9):.3f}s  max={ts[-1]:.3f}s")


if __name__ == "__main__":
    main()
