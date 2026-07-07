"""양자화 서빙 벤치마크 (이슈 #37 Q0/Q2) — vLLM 직접 호출, 앱 노이즈 없음.

bf16 vs 양자화(AWQ/FP8) 모델을 같은 방법으로 재서 비교한다:
  - batch=1: TTFT, decode 시간, TPOT(초/토큰), tokens/sec — N회 반복 p50
  - 동시성 스윕: N 동시 스트림의 합산 처리량(tokens/sec) — 배칭 이득 확인
  (VRAM은 박스에서 `nvidia-smi --query-gpu=memory.used` 로 별도 기록)

사용:
  python load/quant_bench.py --url http://<host>:18000 --label bf16
  python load/quant_bench.py --url http://<host>:18000 --label awq --concurrency 1,4,8

출력: stdout + load/results/quant_bench_<label>_<timestamp>.json
"""

import argparse
import asyncio
import datetime
import json
import statistics
import time
from pathlib import Path

import httpx

# 도메인 대표 프롬프트 (calibration/실사용과 같은 결)
_PROMPTS = [
    "피부가 건조하고 각질이 일어나요. 어떤 성분이 좋을까요?",
    "모공이 넓고 피지가 많은 지성 피부인데 진정에 좋은 성분 추천해줘",
    "민감성 피부라 자극 없는 미백 성분을 찾고 있어요",
    "여드름 흉터랑 색소침착이 고민이에요. 어떤 성분을 써야 하나요?",
]


def p50(xs):
    return statistics.median(xs) if xs else 0.0


async def stream_once(client: httpx.AsyncClient, base: str, model: str,
                      prompt: str, max_tokens: int) -> dict:
    """스트리밍 1건 → TTFT/decode/토큰수. usage는 마지막 chunk(stream_options)로 수집."""
    t0 = time.perf_counter()
    ttft = None
    completion_tokens = 0
    async with client.stream(
        "POST", f"{base}/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            "chat_template_kwargs": {"enable_thinking": False},
        },
    ) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            if chunk.get("usage"):
                completion_tokens = chunk["usage"].get("completion_tokens", 0)
            choices = chunk.get("choices") or []
            if choices and (choices[0].get("delta") or {}).get("content"):
                if ttft is None:
                    ttft = time.perf_counter() - t0
    total = time.perf_counter() - t0
    ttft = ttft if ttft is not None else total
    decode = total - ttft
    tpot = decode / max(completion_tokens - 1, 1)
    return {"ttft": ttft, "decode": decode, "total": total,
            "tokens": completion_tokens, "tpot": tpot,
            "tok_per_sec": (completion_tokens - 1) / decode if decode > 0 else 0.0}


async def bench_batch1(base: str, model: str, n: int, max_tokens: int) -> dict:
    rows = []
    async with httpx.AsyncClient(timeout=180.0) as client:
        # 워밍 1건(측정 제외) — 콜드 꼬리가 batch1 수치를 오염시키지 않게
        await stream_once(client, base, model, _PROMPTS[0], max_tokens)
        for i in range(n):
            r = await stream_once(client, base, model, _PROMPTS[i % len(_PROMPTS)], max_tokens)
            rows.append(r)
            print(f"  [batch1 #{i+1}/{n}] ttft={r['ttft']:.3f}s decode={r['decode']:.2f}s "
                  f"tokens={r['tokens']} tok/s={r['tok_per_sec']:.1f}", flush=True)
    return {
        "n": n,
        "ttft_p50_s": round(p50([r["ttft"] for r in rows]), 3),
        "decode_p50_s": round(p50([r["decode"] for r in rows]), 2),
        "tokens_p50": p50([r["tokens"] for r in rows]),
        "tpot_p50_ms": round(p50([r["tpot"] for r in rows]) * 1000, 1),
        "tok_per_sec_p50": round(p50([r["tok_per_sec"] for r in rows]), 1),
    }


async def bench_concurrency(base: str, model: str, conc: int, max_tokens: int) -> dict:
    async with httpx.AsyncClient(timeout=300.0) as client:
        t0 = time.perf_counter()
        rows = await asyncio.gather(*[
            stream_once(client, base, model, _PROMPTS[i % len(_PROMPTS)], max_tokens)
            for i in range(conc)
        ])
        wall = time.perf_counter() - t0
    total_tokens = sum(r["tokens"] for r in rows)
    agg = total_tokens / wall if wall > 0 else 0.0
    print(f"  [conc={conc}] wall={wall:.2f}s total_tokens={total_tokens} "
          f"aggregate={agg:.1f} tok/s per-req_decode_p50={p50([r['decode'] for r in rows]):.2f}s", flush=True)
    return {"concurrency": conc, "wall_s": round(wall, 2), "total_tokens": total_tokens,
            "aggregate_tok_per_sec": round(agg, 1),
            "per_req_decode_p50_s": round(p50([r["decode"] for r in rows]), 2)}


async def main_async(args) -> None:
    base = args.url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{base}/v1/models")
        resp.raise_for_status()
        model = resp.json()["data"][0]["id"]
    print(f"모델: {model} — label={args.label}", flush=True)

    results = {
        "label": args.label,
        "model": model,
        "base_url": base,
        "max_tokens": args.max_tokens,
        "started_at": datetime.datetime.now().isoformat(),
    }
    print(f"--- batch=1 x{args.n} ---", flush=True)
    results["batch1"] = await bench_batch1(base, model, args.n, args.max_tokens)

    conc_levels = [int(c) for c in args.concurrency.split(",") if int(c) > 1]
    if conc_levels:
        print(f"--- 동시성 스윕 {conc_levels} ---", flush=True)
        results["concurrency_sweep"] = [
            await bench_concurrency(base, model, c, args.max_tokens) for c in conc_levels
        ]

    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"quant_bench_{args.label}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\n요약: {json.dumps(results['batch1'], ensure_ascii=False)}")
    print(f"저장: {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="vLLM base URL")
    ap.add_argument("--label", required=True, help="bf16 | awq | fp8 등")
    ap.add_argument("--n", type=int, default=8, help="batch=1 반복 횟수")
    ap.add_argument("--max-tokens", type=int, default=256)
    ap.add_argument("--concurrency", default="4,8", help="동시성 레벨 CSV (예: 4,8)")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
