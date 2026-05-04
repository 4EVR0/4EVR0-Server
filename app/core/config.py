from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "4EVR0 Cosmetic Recommendation API"
    app_version: str = "1.0.0"
    debug: bool = False

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"

    postgres_dsn: str = "postgresql://user:password@localhost:5432/cosmetic"

    redis_url: str = "redis://localhost:6379"

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    llm_timeout_seconds: int = 30

    # LLM provider routing
    llm_provider: str = "openai"       # "openai" | "vllm"
    llm_base_url: str = ""             # vLLM RunPod endpoint
    llm_model: str = "gpt-4o-mini"     # primary model
    llm_model_b: str = ""              # A/B second model (optional)
    ab_test_ratio: float = 0.0         # fraction of traffic routed to llm_model_b

    # Conversation
    conversation_history_limit: int = 5  # turns included in LLM context

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
