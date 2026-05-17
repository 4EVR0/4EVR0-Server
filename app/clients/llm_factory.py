import openai

from app.core.config import settings


def get_async_llm_client() -> openai.AsyncOpenAI:
    url = settings.gpu_server_url.rstrip("/")
    base_url = url if url.endswith("/v1") else f"{url}/v1"
    return openai.AsyncOpenAI(
        api_key=settings.gpu_auth_token,
        base_url=base_url,
        timeout=float(settings.gpu_timeout_seconds),
    )
