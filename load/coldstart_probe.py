"""콜드스타트 측정 프로브 (이슈 #36).

vLLM 기동과 동시에(또는 그 전에) 실행해 두면:
  1. /v1/models 폴링 → 최초 200 시각 = 모델 로드 완료 (프로브 시작 기준 경과초)
  2. 준비 직후 첫 completion latency = torch.compile/CUDA graph 컴파일 꼬리
  3. 이어지는 N건 = 워밍 후 기준선 (첫 요청과의 차이가 꼬리 크기)

⚠️ 콜드 수치는 부팅당 1회만 잡힌다 — 프로브가 결과를 찍기 전에는 수동 테스트 요청이나
   앱(startup 워밍업이 더미를 쏨)을 띄우지 말 것.

사용:
  python load/coldstart_probe.py --url http://<tailscale-host>:18000 [--warm-count 4] [--max-tokens 64]

출력: stdout + load/results/coldstart_<timestamp>.json
"""

import argparse
import datetime
import json
import time
from pathlib import Path

import httpx

POLL_INTERVAL = 0.5


def log(msg: str) -> None:
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}", flush=True)


def wait_until_ready(base_url: str, timeout: float) -> tuple[float, str]:
    """모델 로드 완료(/v1/models 200)까지 폴링. (경과초, 모델ID) 반환."""
    t0 = time.perf_counter()
    last_err = ""
    with httpx.Client(timeout=3.0) as client:
        while True:
            elapsed = time.perf_counter() - t0
            if elapsed > timeout:
                raise TimeoutError(f"{timeout}s 내에 vLLM이 준비되지 않음 (마지막: {last_err})")
            try:
                resp = client.get(f"{base_url}/v1/models")
                if resp.status_code == 200:
                    model_id = resp.json()["data"][0]["id"]
                    log(f"READY: /v1/models 200 — 프로브 시작 후 {elapsed:.1f}s, model={model_id}")
                    return elapsed, model_id
                last_err = f"HTTP {resp.status_code}"
            except Exception as exc:
                last_err = type(exc).__name__
            time.sleep(POLL_INTERVAL)


def completion(client: httpx.Client, base_url: str, model: str, max_tokens: int) -> float:
    t = time.perf_counter()
    resp = client.post(
        f"{base_url}/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": "피부가 건조하고 각질이 일어나요. 어떤 성분이 좋을까요?"}],
            "temperature": 0,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )
    resp.raise_for_status()
    return time.perf_counter() - t


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="vLLM base URL (예: http://host:18000)")
    parser.add_argument("--ready-timeout", type=float, default=1800, help="모델 로드 대기 한도(초)")
    parser.add_argument("--warm-count", type=int, default=4, help="워밍 후 기준선 측정 건수")
    parser.add_argument("--max-tokens", type=int, default=64)
    args = parser.parse_args()
    base_url = args.url.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[: -len("/v1")]

    log(f"프로브 시작 — {base_url} 폴링 (vLLM 기동 전이면 지금 .sh를 실행하세요)")
    ready_elapsed, model = wait_until_ready(base_url, args.ready_timeout)

    results: dict = {
        "probe_started_at": datetime.datetime.now().isoformat(),
        "base_url": base_url,
        "model": model,
        "max_tokens": args.max_tokens,
        "ready_after_probe_start_s": round(ready_elapsed, 2),
    }

    with httpx.Client(timeout=120.0) as client:
        first = completion(client, base_url, model, args.max_tokens)
        log(f"FIRST completion (컴파일 꼬리 포함): {first:.2f}s")
        warmed = []
        for i in range(args.warm_count):
            sec = completion(client, base_url, model, args.max_tokens)
            warmed.append(sec)
            log(f"warm #{i + 1}: {sec:.2f}s")

    warm_avg = sum(warmed) / len(warmed) if warmed else 0.0
    results.update({
        "first_completion_s": round(first, 2),
        "warmed_completions_s": [round(s, 2) for s in warmed],
        "warmed_avg_s": round(warm_avg, 2),
        "compile_tail_s": round(first - warm_avg, 2),
    })
    log(f"컴파일 꼬리 ≈ {results['compile_tail_s']}s (first {first:.2f}s − warm avg {warm_avg:.2f}s)")

    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"coldstart_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    log(f"저장: {out}")


if __name__ == "__main__":
    main()
