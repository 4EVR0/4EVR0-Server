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
import math
import re
import statistics
import time
import urllib.request
import uuid

_SPANS = ["cache_lookup", "extract", "retrieval", "gate_wait",
          "generate", "generate_ttft", "generate_decode", "overhead", "total"]
_BASE_MSG = "피부가 건조하고 각질이 일어나서 보습 잘 되는 화장품 추천해줘"


def _percentile(values, q):
    """nearest-rank 백분위. q∈[0,1]. rank=ceil(q*n)(1-index) → 0-index=rank-1.

    기존 `int(q*len)`는 n=10의 p90을 index 9(=최댓값)로 잡아 한 칸 높게 왜곡됐다.
    """
    if not values:
        raise ValueError("empty sequence")
    ss = sorted(values)
    idx = math.ceil(q * len(ss)) - 1
    return ss[max(0, min(idx, len(ss) - 1))]


def _post(base, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else b""
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=130) as r:
        return json.loads(r.read().decode())


def _stream(base, payload):
    """SSE 스트리밍 요청 → (TTFT, total). TTFT = 첫 delta 이벤트 수신까지."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(base + "/api/v1/recommend/stream", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    ttft = None
    cur = None
    with urllib.request.urlopen(req, timeout=130) as r:
        for raw in r:
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith("event:"):
                cur = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and cur == "delta" and ttft is None:
                ttft = time.time() - t0
    return ttft, time.time() - t0


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
    ap.add_argument("--mode", choices=["miss", "hit", "stream"], default="miss",
                    help="miss=신규 문장(GPU) / hit=같은 문장 반복(캐시) / stream=SSE 스트리밍 엔드포인트(신규 문장, TTFT 측정)")
    args = ap.parse_args()

    sid = _post(args.base, "/api/v1/sessions")["session_id"]
    fixed_msg = f"{_BASE_MSG} {uuid.uuid4().hex}"  # hit 모드용 고정 문장
    # 워밍업 1건(분포에서 제외) — 서버/GPU 워밍
    _post(args.base, "/api/v1/recommend", {"session_id": sid, "message": "워밍업 " + uuid.uuid4().hex})
    # hit 모드: 측정 전에 고정 문장으로 캐시를 채운다 → 첫 측정 요청이 miss(GPU)가 되지 않게
    if args.mode == "hit":
        _post(args.base, "/api/v1/recommend", {"session_id": sid, "message": fixed_msg})

    s0, c0 = _span_snapshot(args.base)
    totals, ttfts = [], []
    print(f"실행: n={args.n} mode={args.mode}  →  {args.base}")
    for i in range(args.n):
        if args.mode == "stream":
            ttft, dt = _stream(args.base, {"session_id": sid, "message": f"{_BASE_MSG} {uuid.uuid4().hex}"})
            ttfts.append(ttft if ttft is not None else dt)
            print(f"  req {i + 1:>2}: TTFT {ttft:>6.3f}s  total {dt:>7.3f}s")
        else:
            msg = f"{_BASE_MSG} {uuid.uuid4().hex}" if args.mode == "miss" else fixed_msg
            t = time.time()
            _post(args.base, "/api/v1/recommend", {"session_id": sid, "message": msg})
            dt = time.time() - t
            print(f"  req {i + 1:>2}: {dt:>7.3f}s")
        totals.append(dt)
    s1, c1 = _span_snapshot(args.base)

    print("\n── span별 평균 (서버 측, 이번 구간 증분) ──")
    print(f"  {'span':<16}{'평균(ms)':>10}{'요청수':>7}")
    for span in _SPANS:
        dc = c1.get(span, 0) - c0.get(span, 0)
        if dc <= 0:
            continue
        ds = s1.get(span, 0) - s0.get(span, 0)
        print(f"  {span:<16}{ds / dc * 1000:>10.1f}{int(dc):>7}")
    print("  (gate_wait는 extract/generate 안 '대기' 성분 — overhead엔 미포함)")

    def _dist(label, xs):
        ss = sorted(xs)
        print(f"  {label}: n={len(ss)} mean={statistics.mean(ss):.3f}s "
              f"p50={_percentile(ss, 0.5):.3f}s p90={_percentile(ss, 0.9):.3f}s max={ss[-1]:.3f}s")

    print("\n── 클라이언트 측 분포 ──")
    if ttfts:
        _dist("TTFT ", ttfts)
    _dist("total", totals)


if __name__ == "__main__":
    main()
