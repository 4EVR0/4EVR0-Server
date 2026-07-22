"""테스트용 자연어 시나리오.

단일 concern 질문 26개 대신, eval/dataset.jsonl(4EVR0-Server 프로필 추출 평가용
정답셋 — 여러 고민이 한 문장에 섞인 실제 사용자 발화 스타일)을 그대로 재사용한다.
이 파일이 4EVR0-Server 안에 있어서 상대 경로로 바로 읽을 수 있다 (cross-repo 아님).

concerns가 비어 있는 시나리오(예: "그냥 무난한 보습 제품 추천해줘")는 이 실험이
재는 게 "concern -> effect -> ingredient 경로 품질"이라 effect로 변환할 게
없으면 채점 대상이 아니므로 제외한다.
"""

import json
from pathlib import Path

_DATASET_PATH = Path(__file__).resolve().parent.parent / "eval" / "dataset.jsonl"


def load_scenarios() -> list[dict]:
    """[{"id":, "message":, "concerns": [...]}, ...] concerns 비어있지 않은 것만."""
    scenarios = []
    for line in _DATASET_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row["concerns"]:
            scenarios.append({"id": row["id"], "message": row["message"], "concerns": row["concerns"]})
    return scenarios


SCENARIOS: list[dict] = load_scenarios()
