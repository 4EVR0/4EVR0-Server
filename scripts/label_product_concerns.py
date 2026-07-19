"""제품 → 타겟 고민(concern) LLM 라벨링 (이슈 #56 P1).

neo4j의 제품(name + category + ingredients)을 gpt-4o-mini로 라벨해 각 제품이 실제로 겨냥하는
고민(Concern enum) 목록을 만든다. 성분 포함이 아니라 "제품 목적"을 잡는 데이터.

출력: scripts/out/product_concerns.jsonl  (한 줄 = {product_id, product_name, concerns})
      재실행하면 이미 라벨된 product_id는 건너뛴다(resumable, 크래시 안전).

사용:
    JUDGE_API_KEY=<openai key> python scripts/label_product_concerns.py [--limit N] [--concurrency 12]
"""

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

import openai

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
from app.domain.enums import Concern  # noqa: E402
from app.clients.neo4j_client import _get_driver, close_driver  # noqa: E402

_OUT = _REPO / "scripts" / "out" / "product_concerns.jsonl"

# 26 concern code → 한글 gloss (LLM 판단 정확도용)
_CONCERN_GLOSS = {
    "ACNE": "여드름", "COMEDONES": "면포/좁쌀", "PORE_CONGESTION": "모공 막힘",
    "ENLARGED_PORES": "넓은 모공", "OILY_SKIN": "지성/피지", "SENSITIVE_SKIN": "민감성",
    "REDNESS": "붉은기", "IRRITATED_SKIN": "자극/트러블", "ATOPIC_PRONE": "아토피 경향",
    "ROSACEA_PRONE": "주사 경향", "DRY_SKIN": "건성", "DEHYDRATED_SKIN": "수분부족",
    "FLAKY_SKIN": "각질/일어남", "ROUGH_TEXTURE": "거친 결", "BARRIER_DAMAGE": "장벽 손상",
    "HYPERPIGMENTATION": "색소침착/기미", "DULLNESS": "칙칙함/톤저하", "UNEVEN_SKIN_TONE": "고르지 않은 톤",
    "BLEMISHES": "잡티", "POST_ACNE_MARKS": "여드름 자국", "DARK_CIRCLES": "다크서클",
    "SUNBURN": "자외선/선번", "AGING_SIGNS": "노화", "WRINKLES": "주름",
    "LOSS_OF_ELASTICITY": "탄력 저하", "SAGGING_SKIN": "처짐",
}
_VALID = {c.value for c in Concern}
_CONCERN_LIST = "\n".join(f"- {c}: {g}" for c, g in _CONCERN_GLOSS.items())


def _prompt(name: str, category: str, ings: list[str]) -> str:
    return f"""화장품이 **실제로 겨냥하는 피부 고민**을 라벨링하세요.
제품명·카테고리·주요 성분으로 판단하되, "성분이 들었다"가 아니라 "이 제품의 목적"으로.
(예: 기미앰플에 나이아신아마이드가 있어도 목적은 색소침착이지 여드름이 아님.)

제품명: {name}
카테고리: {category}
주요 성분: {', '.join(x for x in ings if x)}

가능한 고민(이 코드 중에서만 선택):
{_CONCERN_LIST}

이 제품이 실제로 겨냥하는 고민 코드만 골라 **JSON 배열**로 출력. 1~4개 권장. 예: ["HYPERPIGMENTATION","DULLNESS"]"""


def _parse(text: str) -> list[str]:
    m = re.search(r"\[.*?\]", text, re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return [c for c in arr if isinstance(c, str) and c in _VALID]


async def main_async(args) -> None:
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    if _OUT.exists():
        for line in _OUT.read_text().splitlines():
            try:
                done.add(json.loads(line)["product_id"])
            except Exception:
                pass
    print(f"이미 라벨됨: {len(done)}개 (스킵)")

    driver = _get_driver()
    async with driver.session() as s:
        r = await s.run("""
            MATCH (p:Product)
            OPTIONAL MATCH (p)-[:CONTAINS]->(i:Ingredient)
            WITH p, collect(coalesce(i.kor_name, i.inci_name))[..15] AS ings
            RETURN toString(p.product_id) AS pid, p.product_name AS name,
                   p.category AS category, ings
        """)
        products = [dict(x) async for x in r]
    todo = [p for p in products if p["pid"] not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"라벨 대상: {len(todo)}개")

    # max_retries: 429(TPM 레이트리밋) 자동 백오프 재시도. 동시성은 TPM(200k/min) 안 넘게 낮게.
    client = openai.AsyncOpenAI(api_key=os.environ["JUDGE_API_KEY"], timeout=60.0, max_retries=8)
    sem = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()
    counter = {"ok": 0, "empty": 0, "err": 0}

    async def _label(p):
        async with sem:
            try:
                resp = await client.chat.completions.create(
                    model="gpt-4o-mini", temperature=0,
                    messages=[{"role": "user", "content": _prompt(p["name"], p["category"], p["ings"])}],
                )
                concerns = _parse(resp.choices[0].message.content)
            except Exception as exc:
                counter["err"] += 1
                if counter["err"] <= 5:
                    print(f"  err {p['name'][:20]}: {exc}")
                return
            rec = {"product_id": p["pid"], "product_name": p["name"], "concerns": concerns}
            async with lock:
                with _OUT.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                counter["ok" if concerns else "empty"] += 1
                tot = counter["ok"] + counter["empty"] + counter["err"]
                if tot % 100 == 0:
                    print(f"  진행 {tot}/{len(todo)}  ok={counter['ok']} empty={counter['empty']} err={counter['err']}", flush=True)

    await asyncio.gather(*[_label(p) for p in todo])
    await close_driver()
    print(f"\n완료: ok={counter['ok']} empty(라벨0)={counter['empty']} err={counter['err']}")
    print(f"출력: {_OUT}  (총 {len(done) + counter['ok'] + counter['empty']}줄)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=5)
    args = ap.parse_args()
    if not os.environ.get("JUDGE_API_KEY"):
        sys.exit("JUDGE_API_KEY 필요")
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
