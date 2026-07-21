"""추천 응답 캐시 (Redis).

추천 1건은 GPU를 2번(추출+생성) 친다 — 부하의 거의 전부가 GPU다
(review/2026-06-30-load-test.md: 병목 = llm_response). 같은 고민 문장이 반복되면
매번 GPU를 다시 칠 이유가 없다. 이 캐시는 **추출 이전**에 조회해, 히트 시
extract·neo4j·generate를 통째로 건너뛴다 → 그 요청의 GPU 비용 = 0.

v1은 **정규화된 메시지 exact-match**(공백·대소문자 정규화) 키다. 임베딩 유사도 기반의
진짜 '시맨틱' 매칭(다른 표현도 같은 캐시로)은 후속 과제(임베딩 모델 의존성 필요).

데이터(성분·제품 그래프)는 거의 변하지 않으므로 TTL을 길게 둘 수 있다. Redis 장애 시엔
조용히 미스 처리(캐시는 부가기능, 본 경로를 막지 않는다).
"""

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager

import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "reccache:v3:"
_client: aioredis.Redis | None = None


def _get_client() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(settings.redis_url, decode_responses=True, socket_timeout=3)
    return _client


async def ping() -> None:
    """클라이언트 싱글턴 생성 + 연결 확인 (startup 워밍업용). 실패 시 예외 전파."""
    await _get_client().ping()


async def close_client() -> None:
    """앱 종료 시 커넥션 정리 (lifespan shutdown에서 호출)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _key(message: str, gen_prompt_name: str | None) -> str:
    # 공백 정규화 + 소문자화 → "건조해요 "와 "건조해요"가 같은 키. 응답 프롬프트가 다르면
    # 결과도 다르므로 키에 포함(실험용 프롬프트 교체와 캐시 충돌 방지).
    norm = " ".join(message.strip().split()).lower()
    raw = f"{gen_prompt_name or 'default'}|{norm}"
    return _KEY_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def get(message: str, gen_prompt_name: str | None) -> dict | None:
    """캐시된 추천 콘텐츠(dict)를 반환. 미스/비활성/장애 시 None."""
    if not settings.recommend_cache_enabled:
        return None
    try:
        raw = await _get_client().get(_key(message, gen_prompt_name))
    except Exception as exc:  # Redis 장애는 본 경로를 막지 않는다
        logger.warning("recommend cache get failed: %s", exc)
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        logger.warning("recommend cache decode failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# single-flight: 같은 키의 동시 미스를 1건의 GPU 계산으로 합친다(캐시 스탬피드 제거).
# 콜드 시작 직후 동일 질의가 몰리면 전부 미스로 GPU를 중복 타격했다
# (이슈 #36: 실측 miss 69 > 고유 50). per-key asyncio.Lock으로 리더 1건만 계산하고,
# 나머지는 대기 후 캐시를 재조회해 히트로 처리한다.
# ⚠️ 프로세스 내 코루틴 간에만 유효 — 멀티 워커/멀티 인스턴스 간 스탬피드는 못 막는다
# (그건 Redis 분산 락이 필요한 별도 과제. 단일 uvicorn 프로세스인 현 구조엔 이걸로 충분).
# ---------------------------------------------------------------------------

class _Flight:
    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.refs = 0


_flights: dict[str, _Flight] = {}


@asynccontextmanager
async def single_flight(message: str, gen_prompt_name: str | None):
    """캐시 미스 후 계산 구간을 감싼다. 같은 키는 한 번에 하나만 안으로 들어온다.

    사용 패턴: 미스 → single_flight 진입 → **캐시 재조회**(리더가 채웠으면 히트) → 계산.
    리더가 실패하면 락이 풀리고 다음 대기자가 리더가 되어 재시도한다.
    """
    # 캐시 비활성 시엔 대기자가 결과를 받을 곳이 없어 직렬화 손해만 남는다 → 통과.
    if not settings.recommend_cache_enabled:
        yield
        return

    key = _key(message, gen_prompt_name)
    flight = _flights.get(key)
    if flight is None:
        flight = _flights[key] = _Flight()
    flight.refs += 1
    try:
        async with flight.lock:
            yield
    finally:
        flight.refs -= 1
        if flight.refs == 0:
            _flights.pop(key, None)


async def set(message: str, gen_prompt_name: str | None, payload: dict) -> None:
    """추천 콘텐츠(dict)를 TTL과 함께 저장. 장애 시 조용히 무시."""
    if not settings.recommend_cache_enabled:
        return
    try:
        await _get_client().set(
            _key(message, gen_prompt_name),
            json.dumps(payload, ensure_ascii=False),
            ex=settings.recommend_cache_ttl_seconds,
        )
    except Exception as exc:
        logger.warning("recommend cache set failed: %s", exc)
