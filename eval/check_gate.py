"""품질 회귀 게이트 (이슈 #41).

eval 결과 JSON(추출·생성)을 임계값(`gate_config.json`)과 비교해:
  - 전부 통과 → exit 0 + 통과 표
  - 하나라도 미달 → exit 1 + 실패 표 (CI가 머지 차단)
결과는 stdout(사람용) + `--md-out`(PR 코멘트용 마크다운)로 낸다.

러너 전략과 무관한 순수 판정 로직 — GPU 불필요, 실측 JSON만 있으면 됨.

사용:
    python eval/check_gate.py --extraction eval/results/xxx.json \
                              --response   eval/results/resp-xxx.json \
                              [--config eval/gate_config.json] [--md-out gate.md]
    # 둘 중 하나만 줘도 됨 (해당 섹션만 검사).
"""

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG = Path(__file__).resolve().parent / "gate_config.json"


def _load_metrics(path: str) -> dict:
    d = json.loads(Path(path).read_text())
    return d.get("metrics", d)


def _check_section(metrics: dict, rules: dict) -> list[dict]:
    """각 지표를 규칙과 비교. rows: [{label, key, value, op, bound, pass}]."""
    rows = []
    for key, rule in rules.items():
        label = rule.get("label", key)
        value = metrics.get(key)
        if isinstance(value, dict) and "mean" in value:  # retrieval eval의 {mean, ci95, n} 포맷
            value = value["mean"]
        if value is None:
            rows.append({"label": label, "key": key, "value": None,
                         "op": "min" if "min" in rule else "max",
                         "bound": rule.get("min", rule.get("max")), "pass": False,
                         "note": "지표 없음"})
            continue
        if "min" in rule:
            ok, op, bound = value >= rule["min"], "≥", rule["min"]
        else:
            ok, op, bound = value <= rule["max"], "≤", rule["max"]
        rows.append({"label": label, "key": key, "value": value,
                     "op": op, "bound": bound, "pass": ok, "note": ""})
    return rows


def _md_table(title: str, rows: list[dict]) -> str:
    if not rows:
        return ""
    lines = [f"**{title}**", "", "| 지표 | 값 | 기준 | 판정 |", "|---|---|---|---|"]
    for r in rows:
        val = "―" if r["value"] is None else f"{r['value']:.4g}"
        mark = "✅" if r["pass"] else "❌"
        note = f" ({r['note']})" if r.get("note") else ""
        lines.append(f"| {r['label']} | {val} | {r['op']} {r['bound']} | {mark}{note} |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extraction", help="run_eval.py 결과 JSON")
    ap.add_argument("--response", help="run_response_eval.py 결과 JSON")
    ap.add_argument("--retrieval", help="run_retrieval_eval.py 결과 JSON")
    ap.add_argument("--config", default=str(_DEFAULT_CONFIG))
    ap.add_argument("--md-out", help="PR 코멘트용 마크다운 저장 경로")
    args = ap.parse_args()

    if not (args.extraction or args.response or args.retrieval):
        ap.error("--extraction / --response / --retrieval 중 최소 하나 필요")

    config = json.loads(Path(args.config).read_text())
    all_rows: list[dict] = []
    sections: list[str] = []

    if args.extraction:
        rows = _check_section(_load_metrics(args.extraction), config["extraction"])
        all_rows += rows
        sections.append(_md_table("추출 품질 (run_eval)", rows))
    if args.response:
        rows = _check_section(_load_metrics(args.response), config["response"])
        all_rows += rows
        sections.append(_md_table("생성 품질 (LLM-judge)", rows))
    if args.retrieval:
        rows = _check_section(_load_metrics(args.retrieval), config["retrieval"])
        all_rows += rows
        sections.append(_md_table("검색 품질 (RAG precision)", rows))

    failed = [r for r in all_rows if not r["pass"]]
    passed = len(all_rows) - len(failed)
    verdict = "✅ **PASS** — 품질 게이트 통과" if not failed else \
              f"❌ **FAIL** — {len(failed)}개 지표 미달 → 머지 차단"

    md = f"## 품질 회귀 게이트\n\n{verdict}\n\n" + "\n\n".join(s for s in sections if s)
    if failed:
        md += "\n\n**미달 지표:** " + ", ".join(
            f"{r['label']}({'―' if r['value'] is None else f'{r['value']:.4g}'} vs {r['op']} {r['bound']})"
            for r in failed)

    print(md)
    print(f"\n[gate] {passed}/{len(all_rows)} 통과", file=sys.stderr)
    if args.md_out:
        Path(args.md_out).write_text(md)

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
