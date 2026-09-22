"""사람 블라인드 라벨링 도구 — judge 점수를 검증할 정답 라벨을 만든다.

LLM judge 점수를 신뢰하려면 judge가 사람과 얼마나 일치하는지 알아야 한다. 이 도구는
평가 리포트를 읽어, **judge 점수·코멘트를 감춘 채** 같은 근거 컨텍스트와 같은 루브릭으로
사람이 직접 채점하게 하고, `run_response_eval.py --human-labels` 가 받는 JSONL을 만든다.

블라인드 원칙(LABELING.md): 모델 점수를 보고 라벨을 맞추면 일치도가 부풀려진다.
이 도구는 judge 점수를 화면에 절대 출력하지 않는다.

사용:
    # 40건 무작위 표본 라벨링(중단해도 같은 명령으로 이어서 진행)
    python eval/label_responses.py --report eval/results/<report>.json --labeler hyeokjun --sample 40

    # 두 사람의 라벨 일치도 — judge에 기대할 수 있는 일치도의 상한선
    python eval/label_responses.py --agreement eval/labels/hyeokjun.jsonl eval/labels/friend.jsonl

    # 라벨을 judge와 비교
    python eval/label_responses.py --calibrate eval/results/<report>.json eval/labels/hyeokjun.jsonl
"""

import argparse
import json
import random
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from eval.eval_utils import pearson_correlation, spearman_correlation  # noqa: E402
from eval.run_response_eval import (  # noqa: E402
    DIMS,
    JUDGE_PROMPT_NAME,
    PRIMARY_DIMS,
    calibrate_against_humans,
    load_human_scores,
)
from app.prompts import load_prompt, prompt_version  # noqa: E402

DEFAULT_LABEL_DIR = _REPO_ROOT / "eval" / "labels"

# 화면 표시용 한글 설명. 판정 기준 자체는 judge 프롬프트(response_judge.txt)를 그대로
# 보여줘 사람과 judge가 같은 루브릭을 쓰게 한다.
DIM_KOR = {
    "concern_fit": "고민 적합성 — 사용자가 말한 피부 고민을 실제로 다루는가",
    "grounding": "근거성 — 추천 제품·주장이 [제공된 데이터] 안에 있는가 (없는 것을 말하면 감점)",
    "conciseness": "간결성 — 장황하지 않고 실용적인가",
    "korean_quality": "한국어 품질 — 번역투·외국어 누출 없이 자연스러운가",
    "format_adherence": "형식 준수 — 고민분석 → 제품추천+이유 → (선택)사용팁, 끝에 성분명 나열 금지",
}


def extract_rubric(judge_prompt: str) -> str:
    """judge 프롬프트에서 Dimensions 절만 뽑는다(사람에게 같은 기준을 보여주기 위해)."""
    lines = judge_prompt.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "Dimensions:")
    except StopIteration:
        return judge_prompt.strip()
    body = []
    for line in lines[start + 1:]:
        if line.strip().startswith("Return JSON"):
            break
        body.append(line)
    return "\n".join(body).strip()


def load_report_cases(report_path: Path) -> tuple[list[dict], dict]:
    """리포트에서 채점 가능한 케이스와 run 정보를 반환."""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    cases = [
        case for case in report.get("cases", [])
        if case.get("response") and not case.get("error")
    ]
    if not cases:
        raise ValueError(f"{report_path}: 채점할 응답이 없습니다")
    return cases, report.get("run", {})


def load_existing_labels(path: Path) -> dict:
    """이미 라벨한 케이스(id → row). 중단 후 이어서 진행하기 위함."""
    if not path.exists():
        return {}
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row["id"]] = row
    return rows


def select_cases(cases: list[dict], sample: int | None, seed: int) -> list[dict]:
    """표본 선정 후 제시 순서를 섞는다.

    순서를 섞는 이유: 앞뒤 응답을 비교하며 점수가 끌려가는 순서 효과를 줄인다.
    표본은 judge 점수를 보지 않고 무작위로 뽑는다 — judge 점수로 고르면 일치도 추정이 편향된다.
    """
    rng = random.Random(seed)
    chosen = list(cases)
    rng.shuffle(chosen)
    if sample:
        chosen = chosen[:sample]
    return chosen


def render_case(case: dict, position: int, total: int) -> str:
    evidence = case.get("evidence") or {}
    ing = evidence.get("ingredients")
    prod = evidence.get("products")
    if ing is None or prod is None:
        ing = ing or f"(리포트에 근거 컨텍스트 없음 — 성분 {case.get('n_ingredients', '?')}개)"
        prod = prod or f"(리포트에 근거 컨텍스트 없음 — 제품 {case.get('n_products', '?')}개)"
    return (
        f"\n{'━' * 72}\n"
        f"  [{position}/{total}]  case id={case['id']}  ({case.get('label', '')})\n"
        f"{'━' * 72}\n\n"
        f"[사용자 메시지]\n{case['message']}\n\n"
        f"[제공된 성분]\n{ing}\n\n"
        f"[제공된 제품]\n{prod}\n\n"
        f"{'─' * 72}\n"
        f"[어시스턴트 응답]\n{case['response']}\n"
        f"{'─' * 72}"
    )


def prompt_scores(case: dict) -> dict | None:
    """한 케이스의 5개 차원 점수를 입력받는다. None이면 사용자가 중단을 선택."""
    scores = {}
    for dim in DIMS:
        while True:
            raw = input(f"  {DIM_KOR[dim]}\n    {dim} [1-5, q=저장 후 종료]: ").strip().lower()
            if raw == "q":
                return None
            if raw.isdigit() and 1 <= int(raw) <= 5:
                scores[dim] = int(raw)
                break
            print("    → 1~5 사이 정수를 입력하세요 (또는 q).")
    note = input("  메모(선택, Enter로 건너뜀): ").strip()
    return {"scores": scores, "note": note}


def run_labeling(args) -> int:
    report_path = Path(args.report)
    cases, run_info = load_report_cases(report_path)
    out_path = Path(args.out) if args.out else DEFAULT_LABEL_DIR / f"{args.labeler}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    existing = load_existing_labels(out_path)
    selected = select_cases(cases, args.sample, args.seed)
    todo = [case for case in selected if case["id"] not in existing]

    judge_prompt = load_prompt(JUDGE_PROMPT_NAME)
    print("=" * 72)
    print("  블라인드 응답 채점 — judge 점수는 표시되지 않습니다")
    print("=" * 72)
    print(f"  리포트   : {report_path.name}")
    print(f"  생성 프롬프트: {run_info.get('gen_prompt')} ({run_info.get('gen_prompt_version')})")
    print(f"  라벨러   : {args.labeler}")
    print(f"  저장 위치: {out_path}")
    print(f"  진행     : 표본 {len(selected)}건 중 {len(existing)}건 완료, {len(todo)}건 남음")
    print("─" * 72)
    print("  채점 기준 (LLM judge와 동일한 루브릭):")
    print()
    print(extract_rubric(judge_prompt))
    print()
    print("  1=매우 나쁨 … 5=매우 좋음. 기본값으로 5를 주지 말고 전 구간을 쓰세요.")
    print("=" * 72)

    if not todo:
        print("\n표본을 모두 라벨했습니다. --sample 을 늘리거나 --agreement 로 비교하세요.")
        return 0

    done = len(existing)
    with out_path.open("a", encoding="utf-8") as fp:
        for case in todo:
            print(render_case(case, done + 1, len(selected)))
            result = prompt_scores(case)
            if result is None:
                print(f"\n중단했습니다. {done}건 저장됨 → {out_path}")
                print("같은 명령을 다시 실행하면 이어서 진행합니다.")
                return 0
            row = {
                "id": case["id"],
                "scores": result["scores"],
                "note": result["note"],
                "labeler": args.labeler,
                "labeled_at": datetime.now(timezone.utc).isoformat(),
                "source_report": report_path.name,
                "judge_prompt_version": prompt_version(JUDGE_PROMPT_NAME),
            }
            fp.write(json.dumps(row, ensure_ascii=False) + "\n")
            fp.flush()  # 중단해도 직전까지 보존
            done += 1

    print(f"\n완료 — {done}건 저장 → {out_path}")
    print("\n다음 단계:")
    print(f"  python eval/label_responses.py --calibrate {report_path} {out_path}")
    return 0


def run_agreement(paths: list[str]) -> int:
    """두 라벨러의 일치도 — judge에 기대할 수 있는 일치도의 현실적 상한선."""
    left_path, right_path = Path(paths[0]), Path(paths[1])
    left = load_existing_labels(left_path)
    right = load_existing_labels(right_path)
    shared = sorted(set(left) & set(right), key=str)
    if not shared:
        print("겹치는 케이스가 없습니다. 두 사람이 같은 표본(--sample·--seed 동일)을 채점해야 합니다.")
        return 1

    print("=" * 72)
    print(f"  라벨러 간 일치도  ({left_path.name} vs {right_path.name})")
    print("=" * 72)
    print(f"  공통 케이스: {len(shared)}건")
    print("─" * 72)
    print(f"  {'차원':<20} {'MAE':>7} {'Pearson':>9} {'Spearman':>9} {'완전일치':>9}")
    print("─" * 72)

    all_left, all_right = [], []
    for dim in DIMS:
        a = [left[i]["scores"][dim] for i in shared]
        b = [right[i]["scores"][dim] for i in shared]
        all_left.extend(a)
        all_right.extend(b)
        mae = statistics.mean(abs(x - y) for x, y in zip(a, b))
        exact = sum(1 for x, y in zip(a, b) if x == y) / len(a)
        print(f"  {dim:<20} {mae:>7.3f} {str(pearson_correlation(a, b)):>9} "
              f"{str(spearman_correlation(a, b)):>9} {exact:>8.0%}")

    print("─" * 72)
    overall_mae = statistics.mean(abs(x - y) for x, y in zip(all_left, all_right))
    overall_exact = sum(1 for x, y in zip(all_left, all_right) if x == y) / len(all_left)
    print(f"  {'전체':<20} {overall_mae:>7.3f} {str(pearson_correlation(all_left, all_right)):>9} "
          f"{str(spearman_correlation(all_left, all_right)):>9} {overall_exact:>8.0%}")
    print("=" * 72)
    print("  해석: judge-vs-human 일치도가 이 값에 가까우면 judge는 사람만큼 일관적이다.")
    print("        사람끼리도 낮다면 루브릭이 모호한 것이므로 judge부터 탓할 일이 아니다.")
    return 0


def run_calibration(paths: list[str], out: str | None = None) -> int:
    """채점한 바로 그 응답의 judge 점수와 사람 라벨을 비교한다.

    새 응답을 생성하면 temperature=0에서도 문구가 달라질 수 있으므로,
    라벨을 만든 source report 자체를 재사용해야 한다.
    """
    report_path, labels_path = Path(paths[0]), Path(paths[1])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    label_rows = load_existing_labels(labels_path)
    mismatched_sources = sorted({
        row.get("source_report")
        for row in label_rows.values()
        if row.get("source_report") and row.get("source_report") != report_path.name
    })
    if mismatched_sources:
        print(
            f"라벨의 source_report({', '.join(mismatched_sources)})와 "
            f"비교 리포트({report_path.name})가 다릅니다."
        )
        return 1

    calibration = calibrate_against_humans(
        report.get("cases", []),
        load_human_scores(labels_path),
    )
    print("=" * 72)
    print(f"  JUDGE VS HUMAN  ({report_path.name} vs {labels_path.name})")
    print("=" * 72)
    print(f"  공통 케이스: {calibration['n_cases']}건")
    print("─" * 72)
    print(f"  {'차원':<20} {'MAE':>7} {'Pearson':>9} {'Spearman':>9}")
    print("─" * 72)
    for dim in DIMS:
        values = calibration["dimensions"][dim]
        print(
            f"  {dim:<20} {values['mae']:>7.3f} {str(values['pearson']):>9} "
            f"{str(values['spearman']):>9}"
        )
    print("─" * 72)
    primary = calibration["primary"]
    print(
        f"  {'핵심 3축 평균':<20} {primary['mae']:>7.3f} {str(primary['pearson']):>9} "
        f"{str(primary['spearman']):>9}"
    )
    print(
        f"  핵심 축: {', '.join(PRIMARY_DIMS)} / "
        f"judge 평균 {primary['judge_mean']:.3f}, 사람 평균 {primary['human_mean']:.3f}, "
        f"편향 {primary['bias']:+.3f}"
    )
    overall = calibration["overall"]
    print(
        f"  참고(5축 평탄화)     {overall['mae']:>7.3f} {str(overall['pearson']):>9} "
        f"{str(overall['spearman']):>9}"
    )
    print("=" * 72)
    if out:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(calibration, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  저장: {out_path}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="응답 품질 블라인드 사람 채점")
    ap.add_argument("--report", help="채점할 응답 평가 리포트 JSON")
    ap.add_argument("--labeler", help="라벨러 식별자(파일명에 사용)")
    ap.add_argument("--sample", type=int, default=40,
                    help="채점할 표본 수 (기본 40, 0이면 전체)")
    ap.add_argument("--seed", type=int, default=23,
                    help="표본·순서 시드. 두 라벨러가 같은 표본을 보려면 같은 값 사용")
    ap.add_argument("--out", default=None, help="라벨 저장 경로 (기본 eval/labels/<labeler>.jsonl)")
    ap.add_argument("--agreement", nargs=2, metavar=("A.jsonl", "B.jsonl"),
                    help="두 라벨 파일의 일치도만 계산하고 종료")
    ap.add_argument("--calibrate", nargs=2, metavar=("REPORT.json", "LABELS.jsonl"),
                    help="라벨의 source report에 저장된 judge 점수와 사람 점수 비교")
    ap.add_argument("--calibration-out", default=None, help="judge-vs-human 결과 JSON 저장 경로")
    args = ap.parse_args()

    if args.agreement:
        return run_agreement(args.agreement)
    if args.calibrate:
        return run_calibration(args.calibrate, args.calibration_out)
    if not args.report or not args.labeler:
        ap.error("--report 와 --labeler 가 필요합니다 (또는 --agreement/--calibrate 사용)")
    if args.sample == 0:
        args.sample = None
    try:
        return run_labeling(args)
    except (ValueError, FileNotFoundError) as exc:
        ap.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
