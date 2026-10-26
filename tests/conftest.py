"""테스트마다 장애 차단기와 요청 장애 기록을 초기화한다(모듈 전역 상태라 테스트끼리 새지 않게)."""

import pytest

from app.clients import dependency_health, llm_health


@pytest.fixture(autouse=True)
def _reset_dependency_breakers():
    for breaker in (dependency_health.REDIS, dependency_health.NEO4J, dependency_health.POSTGRES):
        breaker.reset()
    dependency_health.start_request()
    llm_health.reset()
    llm_health.start_request()
    yield
    for breaker in (dependency_health.REDIS, dependency_health.NEO4J, dependency_health.POSTGRES):
        breaker.reset()
