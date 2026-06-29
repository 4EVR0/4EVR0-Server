"""4EVR0 추천 API 동시 부하 테스트 (Locust).

실제 HTTP 경로(`POST /api/v1/sessions` → `POST /api/v1/recommend`)를 동시 사용자로 두드려
GPU(vLLM) 병목이 부하에서 어떻게 드러나는지 측정한다.

입력 메시지는 `eval/dataset.jsonl`(평가 정답셋)의 현실적인 한국어 피부 고민 문장을
재활용한다. (정답 라벨은 부하 테스트에선 쓰지 않고 message만 샘플링)

실행 (load/README.md 참고):
    # 단계적 램프업(5→10→25→50), 헤드리스 + 리포트 저장
    locust -f load/locustfile.py -f load/staged_shape.py --headless \
           --host http://localhost:8000 --html load/report.html --csv load/result

    # 수동 고정 부하 (동시 25명, 2분)
    locust -f load/locustfile.py --headless --host http://localhost:8000 -u 25 -r 5 -t 2m

    # 웹 UI (브라우저에서 사용자 수 조절)
    locust -f load/locustfile.py --host http://localhost:8000
"""

import json
import random
from pathlib import Path

from locust import HttpUser, between, task

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DATASET = _REPO_ROOT / "eval" / "dataset.jsonl"

# 추천 엔드포인트는 LLM 2회(추출+생성)를 거쳐 수 초~십수 초 걸린다.
# vLLM 타임아웃(기본 60s)보다 넉넉히 잡아, 타임아웃을 '실패'로 관측하되 무한 대기는 막는다.
_REQUEST_TIMEOUT = 120


def _load_messages() -> list[str]:
    """eval/dataset.jsonl 에서 사용자 메시지만 추출."""
    msgs = []
    for line in _DATASET.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            msgs.append(json.loads(line)["message"])
    if not msgs:
        raise RuntimeError(f"부하 입력 메시지가 비었음: {_DATASET}")
    return msgs


MESSAGES = _load_messages()


class RecommendUser(HttpUser):
    """한 명의 가상 사용자: 세션을 한 번 만들고, 반복해서 추천을 요청한다."""

    # 요청 사이 think time(초). 실제 사용자처럼 약간 쉬었다 다시 요청.
    wait_time = between(1, 3)

    def on_start(self):
        """사용자 진입 시 세션 1회 생성 (이후 요청에 재사용)."""
        self.session_id = None
        with self.client.post(
            "/api/v1/sessions",
            name="POST /sessions",
            timeout=_REQUEST_TIMEOUT,
            catch_response=True,
        ) as resp:
            if resp.status_code == 200:
                try:
                    self.session_id = resp.json().get("session_id")
                except ValueError:
                    resp.failure("세션 응답 JSON 파싱 실패")
            else:
                resp.failure(f"세션 생성 실패: HTTP {resp.status_code}")

    @task
    def recommend(self):
        """무작위 피부 고민 메시지로 추천 요청 (핵심 부하 경로)."""
        if not self.session_id:
            return
        payload = {"session_id": self.session_id, "message": random.choice(MESSAGES)}
        with self.client.post(
            "/api/v1/recommend",
            json=payload,
            name="POST /recommend",
            timeout=_REQUEST_TIMEOUT,
            catch_response=True,
        ) as resp:
            if resp.status_code != 200:
                # 타임아웃/5xx는 부하에서 보고 싶은 '무너짐' 신호 → 실패로 명시 집계
                resp.failure(f"HTTP {resp.status_code}")