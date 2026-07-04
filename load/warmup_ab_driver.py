"""워밍업 on/off A/B 드라이버 (이슈 #36).

콜드 vLLM 재기동 직후, 앱 readiness가 healthy로 바뀌는 순간 첫 실요청 latency를 잰다.
  - warmup OFF: 워밍업이 없으므로 첫 실요청이 vLLM 컴파일 꼬리를 문다.
  - warmup ON : 워밍업 더미가 꼬리를 먼저 지불 → 첫 실요청은 warm에 근접해야 한다.

사용: python load/warmup_ab_driver.py --app http://127.0.0.1:8100 --label off
"""

import argparse
import json
import time
from datetime import datetime

import httpx


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {m}", flush=True)


def wait_healthy(app: str, timeout: float) -> float:
    """/health의 llm=ok(readiness) 될 때까지 폴링. 경과초 반환. (추론 아님 — /v1/models 핑만)"""
    t0 = time.perf_counter()
    with httpx.Client(timeout=5.0) as c:
        while True:
            el = time.perf_counter() - t0
            if el > timeout:
                raise TimeoutError("readiness timeout")
            try:
                r = c.get(f"{app}/health")
                dep = r.json().get("dependencies", {})
                if dep.get("llm") == "ok":
                    log(f"READY: llm=ok — 드라이버 시작 후 {el:.1f}s")
                    return el
            except Exception:
                pass
            time.sleep(0.3)


def create_session(app: str) -> str:
    with httpx.Client(timeout=10.0) as c:
        r = c.post(f"{app}/api/v1/sessions", json={})
        r.raise_for_status()
        return r.json()["session_id"]


def recommend(app: str, sid: str, msg: str) -> tuple[float, int]:
    with httpx.Client(timeout=180.0) as c:
        t = time.perf_counter()
        r = c.post(f"{app}/api/v1/recommend", json={"session_id": sid, "message": msg})
        dt = time.perf_counter() - t
        return dt, r.status_code


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--app", default="http://127.0.0.1:8100")
    p.add_argument("--label", required=True, help="off | on")
    p.add_argument("--ready-timeout", type=float, default=600)
    args = p.parse_args()

    log(f"드라이버 시작 (label={args.label}) — {args.app}/health 폴링 (지금 콜드 vLLM+앱 기동)")
    ready = wait_healthy(args.app, args.ready_timeout)

    # readiness 직후, 첫 실요청 = 콜드 vLLM에 대한 첫 추론(warmup OFF면 꼬리를 문다)
    sid = create_session(args.app)
    msg = f"피부가 건조하고 각질이 일어나요 ab_{args.label}_{int(time.time())}"
    first_dt, first_sc = recommend(args.app, sid, msg)
    log(f"FIRST /recommend: {first_dt:.2f}s (HTTP {first_sc})")

    # 이어서 워밍 후 기준선 (같은 파이프라인, 다른 고유 메시지 → 캐시 미스 유지)
    warmed = []
    for i in range(3):
        m = f"모공이 넓고 피지가 많아요 ab_{args.label}_{int(time.time())}_{i}"
        dt, sc = recommend(args.app, create_session(args.app), m)
        warmed.append(dt)
        log(f"warm #{i + 1}: {dt:.2f}s (HTTP {sc})")

    warm_avg = sum(warmed) / len(warmed)
    out = {
        "label": args.label,
        "ready_after_driver_start_s": round(ready, 2),
        "first_recommend_s": round(first_dt, 2),
        "warmed_recommend_s": [round(x, 2) for x in warmed],
        "warmed_avg_s": round(warm_avg, 2),
        "first_minus_warm_s": round(first_dt - warm_avg, 2),
    }
    log(f"결과: {json.dumps(out, ensure_ascii=False)}")
    path = f"load/results/warmup_ab_{args.label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(path, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    log(f"저장: {path}")


if __name__ == "__main__":
    main()
