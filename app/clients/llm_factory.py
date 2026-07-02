import openai

from app.core.config import settings

# 프로세스 싱글턴 — AsyncOpenAI는 내부에 httpx 커넥션 풀을 가진다. 기존처럼 요청마다
# 새로 만들면 vLLM으로의 TCP 연결을 매번 새로 맺는다(콜드스타트 이슈 #36 "커넥션 풀 부재").
_client: openai.AsyncOpenAI | None = None


def get_async_llm_client() -> openai.AsyncOpenAI:
    global _client
    if _client is None:
        url = settings.gpu_server_url.rstrip("/")
        base_url = url if url.endswith("/v1") else f"{url}/v1"
        _client = openai.AsyncOpenAI(
            api_key="EMPTY",
            base_url=base_url,
            timeout=float(settings.gpu_timeout_seconds),
        )
    return _client


async def close_llm_client() -> None:
    """앱 종료 시 커넥션 정리 (lifespan shutdown에서 호출)."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None
