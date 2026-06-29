"""단계적 램프업 부하 프로파일 (LoadTestShape).

동시 사용자를 5 → 10 → 25 → 50 으로 한 단계씩 올리며 각 단계를 일정 시간 유지한다.
포화점(어느 동시성에서 p95 latency·에러율이 무너지는지)을 한 번의 실행으로 찾기 위함.

locustfile 과 함께 로드하면 Locust 가 자동으로 이 프로파일을 따른다:
    locust -f load/locustfile.py -f load/staged_shape.py --headless --host http://localhost:8000

이 파일을 빼고 실행하면 -u/-r 로 직접 동시성을 지정하는 수동 모드가 된다.
"""

from locust import LoadTestShape


class StagedRamp(LoadTestShape):
    # (단계 종료 시각(초, 누적), 목표 동시 사용자, 초당 생성 속도)
    # GPU 8B 단일 서빙이라 포화점은 비교적 일찍 올 것 — 그게 병목을 또렷이 보여주는 지점이다.
    stages = [
        {"end": 60, "users": 5, "spawn_rate": 5},
        {"end": 150, "users": 10, "spawn_rate": 5},
        {"end": 270, "users": 25, "spawn_rate": 5},
        {"end": 420, "users": 50, "spawn_rate": 10},
    ]

    def tick(self):
        run_time = self.get_run_time()
        for stage in self.stages:
            if run_time < stage["end"]:
                return (stage["users"], stage["spawn_rate"])
        return None  # 모든 단계 종료 → 테스트 정지
