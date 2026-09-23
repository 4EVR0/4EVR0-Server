"""응답 품질 평가 (LLM-as-judge).

추출 평가(run_eval.py)가 못 보는 **생성된 추천문 자체의 품질**을 측정한다.
각 메시지에 대해 실제 추천 파이프라인(recommend_service.recommend)을 돌려 응답을 만들고,
그 응답을 LLM 심판(judge)이 루브릭으로 1~5점 채점한다.

전제: Neo4j(EC2) + vLLM 가동. (HTTP/세션 불필요 — 서비스 함수 직접 호출)

사용:
    JUDGE_MODEL=<external-model> JUDGE_API_KEY=<key> python eval/run_response_eval.py
    python eval/run_response_eval.py --judge-model <external-model> --limit 5
    python eval/run_response_eval.py --generate-only --out eval/results/local-generation.json

기본 동작은 생성기와 다른 외부 judge를 요구하고, 생성 temperature를 0으로 고정한다.
--generate-only는 같은 응답·근거·결정론적 검사를 저장하고 외부 judge는 호출하지 않는다.
저장된 응답은 eval/rejudge.py로 나중에 채점할 수 있다.
self-judge는 편향을 명시적으로 감수하는 --allow-self-judge 없이는 실행되지 않는다.

세션 격리(중요): 케이스마다 고유 session_id를 쓰고 실행 전후로 대화 이력을 비운다.
공유 세션이면 recommend()가 앞 케이스의 이력을 읽어 후속 질문으로 오판할 수 있고, 그때는
검색을 건너뛴 채 **앞 케이스의 제품**으로 답을 만든다(recommend_service._handle_followup).
응답 캐시를 꺼도 대화 이력은 Redis에 따로 남으므로 캐시 OFF만으로는 격리되지 않는다.
--session-mode shared 는 격리 도입 이전 동작을 재현해 오염 영향을 측정할 때만 쓴다.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import openai

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from app.core.config import settings  # noqa: E402
from app.prompts import load_prompt, prompt_version  # noqa: E402
from app.repositories import conversation_store  # noqa: E402
from app.services.recommend_service import (  # noqa: E402
    _evidence_label,
    _ingredient_display_name,
    _product_display_name,
    recommend,
)
from eval.eval_utils import (  # noqa: E402
    bootstrap_mean_ci,
    file_sha256,
    git_code_sha,
    load_dataset,
    pearson_correlation,
    spearman_correlation,
)
from eval.hard_checks import HANJA_PATTERN, check_response, summarize_hard_failures  # noqa: E402

JUDGE_PROMPT_NAME = "response_judge"  # 기본 루브릭(--judge-prompt 로 과거 버전 지정 가능)
# 평가 대상 응답 생성 프롬프트. 기준선은 **운영이 실제로 쓰는 프롬프트**를 측정해야 하므로
# settings.gen_prompt_name을 따른다(고정 문자열로 두면 운영 기본이 바뀌어도 평가가 따라가지
# 않는다). 과거 버전과 비교할 때만 --gen-prompt로 명시 지정한다.
DEFAULT_GEN_PROMPT = settings.gen_prompt_name
DIMS = ["concern_fit", "grounding", "conciseness", "korean_quality", "format_adherence"]
# 제품 품질의 핵심 축. conciseness/format은 스타일 선호와 루브릭 해석 차이가 커서
# 별도 진단값으로 유지하되, 사람 교정의 주 판정에는 포함하지 않는다.
PRIMARY_DIMS = ["concern_fit", "grounding", "korean_quality"]
DEFAULT_JUDGE_BASE_URL = "https://api.openai.com/v1"

# 세션 격리 모드.
#   isolated — 케이스마다 고유 session_id. 독립 평가의 기본값.
#   shared   — 전 케이스가 한 session_id 공유(격리 도입 이전 동작). 오염 영향 측정용으로만 사용.
SESSION_MODES = ("isolated", "shared")
LEGACY_SHARED_SESSION_ID = "eval-response"  # shared 모드가 재현하는 기존 세션 ID

# 한자(Hanja/漢字) 누출 — 응답은 순한글이어야 한다.
# LLM judge에 맡기지 않는 이유: 루브릭에 한자 감점 조항을 넣어 측정해 봤더니 정작 누출된
# 케이스의 korean_quality는 오르고(+0.33) 멀쩡한 케이스가 내려갔다(-0.16). judge는 이걸
# 신뢰성 있게 못 잡는다. 정규식은 100% 정확하므로 결정적 검사로 분리한다.
def find_hanja(text: str | None) -> list[str]:
    """응답에 섞인 한자 목록(중복 제거·정렬). 없으면 빈 리스트."""
    return sorted(set(HANJA_PATTERN.findall(text or "")))


@dataclass(frozen=True)
class JudgeConfig:
    model: str
    base_url: str
    api_key: str
    timeout_seconds: float


def _normalize_base_url(url: str) -> str:
    value = url.strip().rstrip("/")
    if not value:
        raise ValueError("judge base URL must not be empty")
    parts = urlsplit(value)
    path = parts.path.rstrip("/")
    if not path.endswith("/v1"):
        path = f"{path}/v1"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def build_judge_config(
    *,
    model: str | None,
    base_url: str | None,
    api_key_env: str,
    timeout_seconds: float,
    allow_self_judge: bool,
) -> JudgeConfig:
    judge_model = (model or os.environ.get("JUDGE_MODEL", "")).strip()
    if not judge_model:
        raise ValueError("external judge model is required: set JUDGE_MODEL or --judge-model")

    judge_base_url = _normalize_base_url(
        base_url or os.environ.get("JUDGE_BASE_URL", DEFAULT_JUDGE_BASE_URL)
    )
    api_key = os.environ.get(api_key_env, "")
    if not api_key:
        raise ValueError(f"judge API key is required: set {api_key_env}")

    same_model = judge_model == settings.gpu_model
    if same_model and not allow_self_judge:
        raise ValueError(
            "judge resolves to the generator model; configure a different external model "
            "or pass --allow-self-judge to acknowledge bias"
        )
    return JudgeConfig(judge_model, judge_base_url, api_key, timeout_seconds)


def build_judge_client(config: JudgeConfig) -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        timeout=config.timeout_seconds,
    )


def render_evidence_context(ingredients, products) -> dict[str, str]:
    """채점자에게 보여줄 근거 컨텍스트(제공된 성분·제품)를 문자열로 조립.

    심판에게 생성기와 '동일한' 근거 컨텍스트(근거 수준·제품 핵심성분)를 줘야 grounding을
    공정하게 채점한다. 안 주면 응답의 '논문 근거 N건' 인용을 검증 못 해 hallucination으로 오판한다.
    잘림 규칙(성분 10개·제품별 핵심성분 3개)은 생성기(_compose_user_content)와 맞춘다.

    LLM judge와 사람 라벨러가 **같은 근거**를 보도록 리포트에도 이 결과를 저장한다
    (judge-vs-human 비교가 성립하려면 채점 입력이 같아야 한다).
    """
    ingredient_by_name = {ingredient.name: ingredient for ingredient in ingredients}
    ing_lines = "\n".join(
        f"- {_ingredient_display_name(i)}: {i.claim or '효능 데이터 없음'} "
        f"[{_evidence_label(i.eligibility_tier, i.paper_ref)}]"
        for i in ingredients[:10]
    ) or "(없음)"

    def _annotate(names: list[str]) -> str:
        annotated = []
        for name in names[:3]:
            ingredient = ingredient_by_name.get(name)
            if ingredient:
                annotated.append(
                    f"{_ingredient_display_name(ingredient)} "
                    f"[{_evidence_label(ingredient.eligibility_tier, ingredient.paper_ref)}]"
                )
            else:
                annotated.append(f"{name} [근거 미상]")
        return ", ".join(annotated)

    prod_lines = "\n".join(
        f"- [{p.category}] {_product_display_name(p.brand, p.product_name)} "
        f"(핵심성분: {_annotate(p.matched_ingredients)})"
        for p in products
    ) or "(없음)"
    return {"ingredients": ing_lines, "products": prod_lines}


async def judge_with_evidence(client, model, message, evidence, response, judge_prompt) -> dict:
    """이미 조립된 근거 컨텍스트로 채점.

    리포트에 저장된 evidence만으로 재채점할 수 있어야 루브릭 변경 효과를 생성 비결정성과
    분리해 측정할 수 있다(같은 응답을 다른 루브릭으로 다시 채점 — eval/rejudge.py).
    """
    content = (
        f"[User message]\n{message}\n\n"
        f"[Provided ingredients]\n{evidence['ingredients']}\n\n"
        f"[Provided products]\n{evidence['products']}\n\n"
        f"[Assistant response]\n{response}"
    )
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": judge_prompt},
            {"role": "user", "content": content},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    # 1~5 범위로 클램프 + 누락 방지
    scores = {}
    for d in DIMS:
        try:
            scores[d] = max(1, min(5, int(round(float(data.get(d))))))
        except (TypeError, ValueError):
            scores[d] = None
    missing_scores = [dim for dim in DIMS if scores[dim] is None]
    if missing_scores:
        raise ValueError(f"judge response is missing numeric scores: {missing_scores}")
    scores["comment"] = str(data.get("comment", ""))[:200]
    return scores


async def judge_response(client, model, message, ingredients, products, response, judge_prompt) -> dict:
    """추천 결과 객체로부터 근거 컨텍스트를 조립해 채점."""
    return await judge_with_evidence(
        client, model, message,
        render_evidence_context(ingredients, products),
        response, judge_prompt,
    )


def load_human_scores(path: Path) -> dict[int | str, dict[str, float]]:
    rows: dict[int | str, dict[str, float]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        case_id = row.get("id")
        if case_id is None or case_id in rows:
            raise ValueError(f"{path}:{line_number}: missing or duplicate id")
        raw_scores = row.get("scores", {})
        scores: dict[str, float] = {}
        unknown = set(raw_scores) - set(DIMS)
        if unknown:
            raise ValueError(f"{path}:{line_number}: unknown score dimensions: {sorted(unknown)}")
        for dim in PRIMARY_DIMS:
            value = raw_scores.get(dim)
            if not isinstance(value, (int, float)) or not 1 <= float(value) <= 5:
                raise ValueError(f"{path}:{line_number}: {dim} must be a number from 1 to 5")
            scores[dim] = float(value)
        for dim in set(DIMS) - set(PRIMARY_DIMS):
            if dim not in raw_scores:
                continue
            value = raw_scores[dim]
            if not isinstance(value, (int, float)) or not 1 <= float(value) <= 5:
                raise ValueError(f"{path}:{line_number}: {dim} must be a number from 1 to 5")
            scores[dim] = float(value)
        rows[case_id] = scores
    if not rows:
        raise ValueError(f"{path}: human label file is empty")
    return rows


def calibrate_against_humans(results: list[dict], human_scores: dict) -> dict:
    judged = {row["id"]: row["scores"] for row in results if "scores" in row}
    shared_ids = sorted(set(judged) & set(human_scores), key=str)
    if not shared_ids:
        raise ValueError("human labels do not overlap with successfully judged case IDs")
    dimensions: dict[str, dict] = {}
    all_judge: list[float] = []
    all_human: list[float] = []
    for dim in DIMS:
        dimension_ids = [case_id for case_id in shared_ids if dim in human_scores[case_id]]
        if not dimension_ids:
            continue
        judge_values = [float(judged[case_id][dim]) for case_id in dimension_ids]
        human_values = [float(human_scores[case_id][dim]) for case_id in dimension_ids]
        all_judge.extend(judge_values)
        all_human.extend(human_values)
        dimensions[dim] = {
            "judge_mean": round(statistics.mean(judge_values), 4),
            "human_mean": round(statistics.mean(human_values), 4),
            "bias": round(statistics.mean(a - b for a, b in zip(judge_values, human_values)), 4),
            "mae": round(statistics.mean(abs(a - b) for a, b in zip(judge_values, human_values)), 4)
            if judge_values else None,
            "pearson": pearson_correlation(judge_values, human_values),
            "spearman": spearman_correlation(judge_values, human_values),
            "exact_rate": round(
                sum(a == b for a, b in zip(judge_values, human_values)) / len(judge_values), 4
            ),
            "within_one_rate": round(
                sum(abs(a - b) <= 1 for a, b in zip(judge_values, human_values)) / len(judge_values),
                4,
            ),
        }

    # 한 케이스의 핵심 3축 평균끼리 비교한다. 서로 다른 차원의 관측치를 한 배열에
    # 평탄화하면 차원별 분포 차이가 상관계수를 왜곡하므로 주 판정에는 쓰지 않는다.
    primary_judge = [
        statistics.mean(judged[case_id][dim] for dim in PRIMARY_DIMS)
        for case_id in shared_ids
    ]
    primary_human = [
        statistics.mean(human_scores[case_id][dim] for dim in PRIMARY_DIMS)
        for case_id in shared_ids
    ]
    return {
        "n_cases": len(shared_ids),
        "case_ids": shared_ids,
        "dimensions": dimensions,
        "primary": {
            "dimensions": PRIMARY_DIMS,
            "judge_mean": round(statistics.mean(primary_judge), 4),
            "human_mean": round(statistics.mean(primary_human), 4),
            "bias": round(statistics.mean(a - b for a, b in zip(primary_judge, primary_human)), 4),
            "mae": round(statistics.mean(abs(a - b) for a, b in zip(primary_judge, primary_human)), 4),
            "pearson": pearson_correlation(primary_judge, primary_human),
            "spearman": spearman_correlation(primary_judge, primary_human),
        },
        "overall": {
            "scope": ("all_dimensions_flattened" if all(
                dim in human_scores[case_id] for case_id in shared_ids for dim in DIMS
            )
                      else "available_dimensions_flattened"),
            "mae": round(statistics.mean(abs(a - b) for a, b in zip(all_judge, all_human)), 4)
            if all_judge else None,
            "pearson": pearson_correlation(all_judge, all_human),
            "spearman": spearman_correlation(all_judge, all_human),
        },
    }


async def run(
    dataset_path: Path,
    limit: int | None,
    gen_prompt: str,
    judge_config: JudgeConfig | None,
    *,
    judge_repeats: int = 1,
    gen_temperature: float = 0.0,
    bootstrap_samples: int = 2_000,
    seed: int = 23,
    human_labels_path: Path | None = None,
    session_mode: str = "isolated",
    judge_prompt_name: str = JUDGE_PROMPT_NAME,
) -> dict:
    cases = load_dataset(dataset_path)
    if limit:
        cases = cases[:limit]
    if judge_repeats < 1:
        raise ValueError("judge_repeats must be at least 1")
    if bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be at least 1")
    if session_mode not in SESSION_MODES:
        raise ValueError(f"session_mode must be one of {SESSION_MODES}")
    if human_labels_path and judge_config is None:
        raise ValueError("human-label calibration requires a judged report")
    settings.gen_temperature = gen_temperature
    client = build_judge_client(judge_config) if judge_config else None
    judge_prompt = load_prompt(judge_prompt_name) if judge_config else None

    # 실행 ID — isolated 모드의 session_id 네임스페이스. 이력 TTL(기본 2h) 안에 같은 평가를
    # 여러 번 돌려도 실행 간 이력이 섞이지 않도록 실행마다 새로 만든다.
    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    results = []
    dim_scores = {d: [] for d in DIMS}
    errors = 0
    gen_latencies = []
    repeat_stddevs: list[float] = []
    contaminated = 0  # 실행 시점에 이전 이력이 남아 있던 케이스 수(오염 노출)

    for case in cases:
        row = {"id": case["id"], "label": case.get("label", ""), "message": case["message"]}
        session_id = (
            LEGACY_SHARED_SESSION_ID if session_mode == "shared"
            else f"eval-{run_id}-{case['id']}"
        )
        # 격리 모드: 남은 이력(키 충돌·이전 중단 실행)을 지우고 깨끗한 상태에서 시작.
        if session_mode == "isolated":
            await conversation_store.clear(session_id)
        # 이 케이스가 '이전 대화가 있는 상태'로 실행됐는지 기록 — 오염을 사후에 증명하는 근거.
        history_before = len(await conversation_store.load_recent(session_id))
        row["session_id"] = session_id
        row["history_len_before"] = history_before
        if history_before:
            contaminated += 1
        try:
            t0 = time.perf_counter()
            rec = await recommend(session_id, case["message"], gen_prompt)  # 실제 파이프라인 (Neo4j+vLLM)
            gen_latencies.append(time.perf_counter() - t0)
            hard_failures = [failure.as_dict() for failure in check_response(case, rec)]
            scores = None
            if judge_config:
                score_runs = [
                    await judge_response(
                        client,
                        judge_config.model,
                        case["message"],
                        rec.ingredients,
                        rec.products,
                        rec.response_text,
                        judge_prompt,
                    )
                    for _ in range(judge_repeats)
                ]
                scores = {
                    dim: round(statistics.mean(run[dim] for run in score_runs if run[dim] is not None), 3)
                    if any(run[dim] is not None for run in score_runs) else None
                    for dim in DIMS
                }
                scores["comment"] = score_runs[0]["comment"]
                if judge_repeats > 1:
                    repeat_stddevs.extend(
                        statistics.pstdev([run[dim] for run in score_runs if run[dim] is not None])
                        for dim in DIMS
                        if any(run[dim] is not None for run in score_runs)
                    )
        except Exception as exc:
            errors += 1
            row["error"] = f"{type(exc).__name__}: {exc}"
            results.append(row)
            print(f"  [id {case['id']:>2}] ERROR {type(exc).__name__}: {exc}")
            continue
        finally:
            # 이 케이스가 남긴 이력은 다음 케이스로 넘기지 않는다(성공·실패 무관).
            if session_mode == "isolated":
                await conversation_store.clear(session_id)

        valid = [scores[d] for d in DIMS if scores[d] is not None] if scores else []
        if scores:
            for d in DIMS:
                if scores[d] is not None:
                    dim_scores[d].append(scores[d])
        overall = round(statistics.mean(valid), 2) if valid else None
        row.update({
            "n_products": len(rec.products), "n_ingredients": len(rec.ingredients),
            "response": rec.response_text,
            # 사람 라벨러가 judge와 같은 근거를 보고 채점할 수 있도록 함께 저장.
            "evidence": render_evidence_context(rec.ingredients, rec.products),
            # 결정적 검사 — judge 점수와 독립적으로 집계한다.
            "hanja": find_hanja(rec.response_text),
            "hard_pass": not hard_failures,
            "hard_failures": hard_failures,
        })
        if scores:
            row.update({
                "scores": {d: scores[d] for d in DIMS},
                "overall": overall,
                "comment": scores["comment"],
            })
        results.append(row)
        if scores:
            _abbr = {"concern_fit": "fit", "grounding": "grnd", "conciseness": "concise",
                     "korean_quality": "kor", "format_adherence": "fmt"}
            print(f"  [id {case['id']:>2}] overall={overall}  " +
                  " ".join(f"{_abbr[d]}={scores[d]}" for d in DIMS))
        else:
            print(f"  [id {case['id']:>2}] generated  hard_failures={len(hard_failures)}")

    scored = len(cases) - errors
    metrics = {f"resp_{d}": round(statistics.mean(dim_scores[d]), 3) for d in DIMS if dim_scores[d]}
    for index, dim in enumerate(DIMS):
        ci = bootstrap_mean_ci(dim_scores[dim], samples=bootstrap_samples, seed=seed + index)
        if ci:
            metrics[f"resp_{dim}_ci95_low"], metrics[f"resp_{dim}_ci95_high"] = ci
    all_means = [statistics.mean(dim_scores[d]) for d in DIMS if dim_scores[d]]
    metrics["resp_overall"] = round(statistics.mean(all_means), 3) if all_means else None
    case_overalls = [
        row["overall"] for row in results if row.get("overall") is not None
    ]
    overall_ci = bootstrap_mean_ci(case_overalls, samples=bootstrap_samples, seed=seed)
    if overall_ci:
        metrics["resp_overall_ci95_low"], metrics["resp_overall_ci95_high"] = overall_ci
    metrics["error_rate"] = round(errors / len(cases), 4) if cases else 0.0
    metrics["gen_latency_p50"] = round(statistics.median(gen_latencies), 2) if gen_latencies else None
    metrics["judge_repeat_stddev"] = (
        round(statistics.mean(repeat_stddevs), 4) if repeat_stddevs else 0.0
    )
    # 이전 대화 이력이 남은 채로 실행된 케이스 — isolated 모드에서는 0이어야 한다.
    hanja_cases = [row for row in results if row.get("hanja")]
    metrics["hanja_leak_cases"] = len(hanja_cases)
    metrics["hanja_leak_rate"] = round(len(hanja_cases) / scored, 4) if scored else 0.0
    metrics["contaminated_cases"] = contaminated
    metrics["contamination_rate"] = round(contaminated / len(cases), 4) if cases else 0.0
    metrics.update(summarize_hard_failures(results, scored))

    run_info = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "code_sha": git_code_sha(),
        "generator_model": settings.gpu_model,
        "generator_base_url": _normalize_base_url(settings.gpu_server_url),
        "generator_temperature": gen_temperature,
        "gen_prompt": gen_prompt,
        "gen_prompt_version": prompt_version(gen_prompt),
        # 운영 기본 프롬프트와 같은 것을 평가했는지 — 다르면 이 리포트는 기준선이 아니다.
        "service_gen_prompt": settings.gen_prompt_name,
        "service_gen_prompt_version": prompt_version(settings.gen_prompt_name),
        "matches_service_prompt": gen_prompt == settings.gen_prompt_name,
        "judge_model": judge_config.model if judge_config else None,
        "judge_base_url": judge_config.base_url if judge_config else None,
        "judge_temperature": 0 if judge_config else None,
        "judge_repeats": judge_repeats if judge_config else 0,
        "judge_prompt": judge_prompt_name if judge_config else None,
        "judge_prompt_version": prompt_version(judge_prompt_name) if judge_config else None,
        "dataset": str(dataset_path),
        "dataset_sha256": file_sha256(dataset_path),
        "n_cases": len(cases),
        "n_scored": scored,
        "n_judged": scored if judge_config else 0,
        "bootstrap_samples": bootstrap_samples,
        "bootstrap_seed": seed,
        # 평가 조건을 리포트만 보고 재현·해석할 수 있도록 세션 격리 상태를 함께 남긴다.
        "run_id": run_id,
        "session_mode": session_mode,
        "conversation_enabled": settings.conversation_enabled,
        "conversation_ttl_seconds": settings.conversation_ttl_seconds,
    }
    report = {"run": run_info, "metrics": metrics, "cases": results}
    if human_labels_path:
        report["human_calibration"] = calibrate_against_humans(
            results, load_human_scores(human_labels_path)
        )
    return report


def print_summary(report: dict) -> None:
    r, m = report["run"], report["metrics"]
    print("\n" + "═" * 60)
    print("  응답 품질 평가 (LLM-judge)" if r["judge_model"] else "  응답 생성 및 결정론적 검사")
    print("═" * 60)
    print(
        f"  generator={r['generator_model']}  judge={r['judge_model']}  "
        f"gen_prompt={r['gen_prompt_version']}  judge_prompt={r['judge_prompt_version']}"
    )
    print(f"  n={r['n_cases']} (scored {r['n_scored']})")
    session_note = "" if r.get("session_mode") == "isolated" else "  ⚠️ 케이스 간 이력 공유"
    print(f"  session_mode={r.get('session_mode')}  run_id={r.get('run_id')}{session_note}")
    if r.get("matches_service_prompt") is False:
        print(f"  ⚠️ 운영 기본 프롬프트가 아님 — 운영={r.get('service_gen_prompt')} "
              f"({r.get('service_gen_prompt_version')}), 평가={r.get('gen_prompt')} "
              f"({r.get('gen_prompt_version')})")
    print("─" * 60)
    for d in DIMS:
        if f"resp_{d}" in m:
            low = m.get(f"resp_{d}_ci95_low")
            high = m.get(f"resp_{d}_ci95_high")
            ci_text = f" (95% CI {low:.2f}–{high:.2f})" if low is not None and high is not None else ""
            print(f"  {d:<20} {m['resp_' + d]:.2f} / 5{ci_text}")
    print("─" * 60)
    overall_ci = (
        f" (95% CI {m['resp_overall_ci95_low']:.2f}–{m['resp_overall_ci95_high']:.2f})"
        if "resp_overall_ci95_low" in m else ""
    )
    print(f"  {'OVERALL':<20} {m.get('resp_overall')} / 5{overall_ci}")
    print(f"  {'에러율':<20} {m['error_rate']}")
    print(f"  {'응답생성 p50':<20} {m.get('gen_latency_p50')}s")
    print(f"  {'judge 반복 표준편차':<20} {m.get('judge_repeat_stddev')}")
    leaks = m.get("hanja_leak_cases", 0)
    leak_flag = "" if not leaks else f"  ⚠️ 순한글 위반({m.get('hanja_leak_rate')})"
    print(f"  {'한자 누출':<20} {leaks}{leak_flag}")
    hard_failures = m.get("hard_failure_count", 0)
    hard_flag = "" if not hard_failures else f"  ⚠️ {m.get('hard_failure_counts', {})}"
    print(f"  {'Hard gate 실패':<20} {hard_failures}{hard_flag}")
    contaminated = m.get("contaminated_cases", 0)
    flag = "" if not contaminated else f"  ⚠️ 이전 이력 노출({m.get('contamination_rate')})"
    print(f"  {'오염 케이스':<20} {contaminated}{flag}")
    if "human_calibration" in report:
        calibration = report["human_calibration"]
        print(
            f"  {'human 보정':<20} n={calibration['n_cases']} "
            f"MAE={calibration['overall']['mae']} "
            f"Spearman={calibration['overall']['spearman']}"
        )
    print("═" * 60)


def log_to_mlflow(report: dict, artifact_path: Path | None) -> None:
    try:
        import mlflow
    except ImportError:
        print("  (mlflow 미설치 — 기록 건너뜀)")
        return
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", f"sqlite:///{_REPO_ROOT / 'eval' / 'mlflow.db'}")
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment("4evr0-response-quality")
    run, metrics = report["run"], report["metrics"]
    with mlflow.start_run(run_name=run["timestamp"]):
        parameter_names = (
            "code_sha", "generator_model", "generator_base_url", "generator_temperature",
            "gen_prompt", "gen_prompt_version", "judge_model", "judge_base_url",
            "judge_temperature", "judge_repeats", "judge_prompt_version",
            "dataset_sha256", "n_cases", "n_scored", "bootstrap_samples", "bootstrap_seed",
            "run_id", "session_mode", "conversation_enabled",
        )
        mlflow.log_params({key: run[key] for key in parameter_names})
        mlflow.log_metrics({k: v for k, v in metrics.items() if isinstance(v, (int, float))})
        if "human_calibration" in report:
            calibration = report["human_calibration"]
            mlflow.log_metrics({
                f"human_{key}": value
                for key, value in calibration["overall"].items()
                if isinstance(value, (int, float))
            })
            mlflow.log_param("human_n_cases", calibration["n_cases"])
        if artifact_path:
            mlflow.log_artifact(str(artifact_path))
    print(f"  MLflow 기록: experiment='4evr0-response-quality' @ {tracking_uri}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(_REPO_ROOT / "eval" / "dataset.jsonl"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--gen-prompt", default=DEFAULT_GEN_PROMPT,
                    help=f"응답 생성 프롬프트 이름 (기본: 운영과 동일한 {DEFAULT_GEN_PROMPT})")
    ap.add_argument("--gen-temperature", type=float, default=0.0,
                    help="재현성을 위해 기본 0.0 (운영값 재현 시 명시적으로 변경)")
    ap.add_argument("--judge-model", default=None,
                    help="외부 judge 모델 (기본: JUDGE_MODEL)")
    ap.add_argument("--judge-base-url", default=None,
                    help=f"OpenAI 호환 judge URL (기본: JUDGE_BASE_URL 또는 {DEFAULT_JUDGE_BASE_URL})")
    ap.add_argument("--judge-api-key-env", default="JUDGE_API_KEY",
                    help="judge API 키를 읽을 환경변수 이름")
    ap.add_argument("--judge-timeout", type=float, default=120.0)
    ap.add_argument("--judge-repeats", type=int, default=1,
                    help="동일 응답 반복 채점 횟수; 분산 확인 시 3 이상 권장")
    ap.add_argument("--allow-self-judge", action="store_true",
                    help="생성기와 동일한 judge 사용을 명시적으로 허용")
    ap.add_argument("--human-labels", default=None,
                    help="전문가 점수 JSONL; judge-vs-human MAE/상관 산출")
    ap.add_argument("--judge-prompt", default=JUDGE_PROMPT_NAME,
                    help=f"채점 루브릭 이름 (기본 {JUDGE_PROMPT_NAME}; 과거 루브릭 비교 시 response_judge.v1)")
    ap.add_argument("--session-mode", choices=SESSION_MODES, default="isolated",
                    help="isolated=케이스별 세션 격리(기본), shared=격리 이전 동작 재현(오염 영향 측정용)")
    ap.add_argument("--bootstrap-samples", type=int, default=2_000)
    ap.add_argument("--seed", type=int, default=23)
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-mlflow", action="store_true")
    ap.add_argument("--generate-only", action="store_true",
                    help="외부 judge 없이 응답·근거·결정론적 검사만 저장")
    args = ap.parse_args()

    try:
        judge_config = None if args.generate_only else build_judge_config(
            model=args.judge_model,
            base_url=args.judge_base_url,
            api_key_env=args.judge_api_key_env,
            timeout_seconds=args.judge_timeout,
            allow_self_judge=args.allow_self_judge,
        )
        report = asyncio.run(
            run(
                Path(args.dataset),
                args.limit,
                args.gen_prompt,
                judge_config,
                judge_repeats=args.judge_repeats,
                gen_temperature=args.gen_temperature,
                bootstrap_samples=args.bootstrap_samples,
                seed=args.seed,
                human_labels_path=Path(args.human_labels) if args.human_labels else None,
                session_mode=args.session_mode,
                judge_prompt_name=args.judge_prompt,
            )
        )
    except ValueError as exc:
        ap.error(str(exc))
    print_summary(report)

    out = Path(args.out) if args.out else _REPO_ROOT / "eval" / "results" / f"resp-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n결과 저장: {out}")

    if not args.no_mlflow:
        log_to_mlflow(report, out)


if __name__ == "__main__":
    main()
