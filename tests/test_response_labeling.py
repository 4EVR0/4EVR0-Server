"""사람 블라인드 라벨링 도구(eval/label_responses.py) 검증.

핵심 불변식 두 가지:
  - 블라인드: 화면에 judge 점수·코멘트가 절대 나오지 않는다.
  - 호환성: 출력 JSONL을 run_response_eval.load_human_scores 가 그대로 읽는다.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import eval.label_responses as labeling
from eval.run_response_eval import DIMS, load_human_scores


def _case(case_id: int, **overrides) -> dict:
    case = {
        "id": case_id,
        "label": f"case{case_id}",
        "message": f"고민 {case_id}",
        "response": f"추천 응답 {case_id}",
        "scores": {dim: 5 for dim in DIMS},   # judge 점수 — 절대 노출되면 안 됨
        "overall": 5.0,
        "comment": "judge 가 남긴 코멘트",     # 이것도 노출 금지
        "n_products": 3,
        "n_ingredients": 7,
        "evidence": {
            "ingredients": f"- 성분A (INGREDIENT_A): 효능 {case_id} [논문 2건]",
            "products": f"- [세럼] 브랜드 제품{case_id} (핵심성분: 성분A)",
        },
    }
    case.update(overrides)
    return case


def _write_report(path: Path, n: int, **run_extra) -> Path:
    report = {
        "run": {"gen_prompt": "recommend_response.v7", "gen_prompt_version": "47f37c55", **run_extra},
        "metrics": {},
        "cases": [_case(i) for i in range(1, n + 1)],
    }
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return path


# --- 표본 선정 -------------------------------------------------------------

def test_same_seed_gives_two_labelers_the_same_sample():
    cases = [_case(i) for i in range(1, 21)]

    left = labeling.select_cases(cases, sample=5, seed=23)
    right = labeling.select_cases(cases, sample=5, seed=23)

    assert [c["id"] for c in left] == [c["id"] for c in right]
    assert len(left) == 5


def test_different_seed_changes_sample_or_order():
    cases = [_case(i) for i in range(1, 21)]

    a = [c["id"] for c in labeling.select_cases(cases, sample=10, seed=1)]
    b = [c["id"] for c in labeling.select_cases(cases, sample=10, seed=2)]

    assert a != b


def test_sample_none_keeps_every_case():
    cases = [_case(i) for i in range(1, 8)]

    chosen = labeling.select_cases(cases, sample=None, seed=23)

    assert sorted(c["id"] for c in chosen) == list(range(1, 8))


# --- 블라인드 ---------------------------------------------------------------

def test_rendered_case_hides_judge_scores_and_comment():
    rendered = labeling.render_case(_case(1), position=1, total=1)

    assert "judge 가 남긴 코멘트" not in rendered
    for dim in DIMS:
        assert f"{dim}: 5" not in rendered
    assert "overall" not in rendered
    # 채점에 필요한 것은 모두 보여야 한다.
    assert "고민 1" in rendered
    assert "추천 응답 1" in rendered
    assert "INGREDIENT_A" in rendered
    assert "브랜드 제품1" in rendered


def test_rendered_case_flags_missing_evidence_context():
    """근거 컨텍스트가 없는 옛 리포트는 grounding을 채점할 수 없음을 알려야 한다."""
    rendered = labeling.render_case(_case(1, evidence=None), position=1, total=1)

    assert "근거 컨텍스트 없음" in rendered


def test_rubric_comes_from_the_judge_prompt():
    from app.prompts import load_prompt

    rubric = labeling.extract_rubric(load_prompt("response_judge"))

    for dim in DIMS:
        assert dim in rubric
    assert "Return JSON" not in rubric


# --- 라벨링 실행 ------------------------------------------------------------

def _run(tmp_path, monkeypatch, answers, *, sample=2, out_name="labeler.jsonl", n_cases=3):
    report = _write_report(tmp_path / "report.json", n_cases)
    out = tmp_path / out_name
    supplied = iter(answers)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(supplied))
    args = SimpleNamespace(report=str(report), labeler="labeler", sample=sample,
                           seed=23, out=str(out))
    labeling.run_labeling(args)
    return out


def test_labels_are_readable_by_the_calibration_loader(tmp_path, monkeypatch):
    answers = ["4", "5", "3", "5", "4", "메모", "2", "1", "4", "5", "3", ""]

    out = _run(tmp_path, monkeypatch, answers)

    scores = load_human_scores(out)
    assert len(scores) == 2
    assert all(set(v) == set(DIMS) for v in scores.values())
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["scores"]["concern_fit"] == 4
    assert rows[0]["note"] == "메모"
    assert rows[0]["labeler"] == "labeler"
    assert rows[0]["source_report"] == "report.json"


def test_quitting_saves_completed_cases_and_resumes(tmp_path, monkeypatch):
    # 1건 채점 후 두 번째 케이스에서 q
    out = _run(tmp_path, monkeypatch, ["4", "4", "4", "4", "4", "", "q"])
    assert len(load_human_scores(out)) == 1
    first_id = json.loads(out.read_text(encoding="utf-8").splitlines()[0])["id"]

    # 같은 명령 재실행 → 남은 1건만 묻는다(입력을 1건분만 준다)
    report = _write_report(tmp_path / "report.json", 3)
    supplied = iter(["3", "3", "3", "3", "3", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(supplied))
    labeling.run_labeling(SimpleNamespace(report=str(report), labeler="labeler",
                                          sample=2, seed=23, out=str(out)))

    scores = load_human_scores(out)
    assert len(scores) == 2
    assert first_id in scores  # 앞서 채점한 건이 덮어써지지 않았다


def test_invalid_score_is_rejected_until_valid(tmp_path, monkeypatch):
    # 0, 6, 'x' 는 거부되고 재입력을 받아야 한다
    answers = ["0", "6", "x", "5", "5", "5", "5", "5", "", "q"]

    out = _run(tmp_path, monkeypatch, answers, sample=1)

    scores = load_human_scores(out)
    assert list(scores.values())[0]["concern_fit"] == 5


def test_labeling_rejects_report_without_scorable_responses(tmp_path, monkeypatch):
    report = tmp_path / "empty.json"
    report.write_text(json.dumps({"run": {}, "cases": [
        {"id": 1, "message": "x", "error": "boom"},
    ]}), encoding="utf-8")

    with pytest.raises(ValueError, match="채점할 응답이 없습니다"):
        labeling.load_report_cases(report)


# --- 라벨러 간 일치도 --------------------------------------------------------

def test_agreement_reports_perfect_match(tmp_path, capsys):
    rows = [{"id": i, "scores": {dim: 4 for dim in DIMS}} for i in (1, 2, 3)]
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    for path in (a, b):
        path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    assert labeling.run_agreement([str(a), str(b)]) == 0
    out = capsys.readouterr().out
    assert "공통 케이스: 3건" in out
    assert "100%" in out


def test_agreement_requires_overlapping_cases(tmp_path, capsys):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    a.write_text(json.dumps({"id": 1, "scores": {d: 4 for d in DIMS}}), encoding="utf-8")
    b.write_text(json.dumps({"id": 2, "scores": {d: 4 for d in DIMS}}), encoding="utf-8")

    assert labeling.run_agreement([str(a), str(b)]) == 1
    assert "겹치는 케이스가 없습니다" in capsys.readouterr().out


# --- judge-vs-human 동일 리포트 보정 -----------------------------------

def test_calibration_reuses_the_labeled_source_report(tmp_path, capsys):
    report = _write_report(tmp_path / "report.json", 3)
    labels = tmp_path / "labels.jsonl"
    rows = [{
        "id": i,
        "scores": {dim: 5 for dim in DIMS},
        "source_report": "report.json",
    } for i in (1, 2, 3)]
    labels.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    out = tmp_path / "calibration.json"

    assert labeling.run_calibration([str(report), str(labels)], str(out)) == 0
    calibration = json.loads(out.read_text(encoding="utf-8"))
    assert calibration["n_cases"] == 3
    assert calibration["overall"]["mae"] == 0
    assert "공통 케이스: 3건" in capsys.readouterr().out


def test_calibration_rejects_labels_from_another_report(tmp_path, capsys):
    report = _write_report(tmp_path / "report.json", 1)
    labels = tmp_path / "labels.jsonl"
    labels.write_text(json.dumps({
        "id": 1,
        "scores": {dim: 5 for dim in DIMS},
        "source_report": "other.json",
    }), encoding="utf-8")

    assert labeling.run_calibration([str(report), str(labels)]) == 1
    assert "source_report" in capsys.readouterr().out
