from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "4EVR0 Cosmetic Recommendation API"
    app_version: str = "1.0.0"
    debug: bool = False

    # 로그 출력 형식: "json"(Loki 수집용, 기본) | "plain"(로컬 가독성)
    log_format: str = "json"
    log_level: str = "INFO"

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"

    postgres_dsn: str = "postgresql://user:password@localhost:5432/cosmetic"

    redis_url: str = "redis://localhost:6379"

    gpu_server_url: str = "http://127.0.0.1:18000"
    gpu_model: str = "Qwen/Qwen3-8B-FP8"
    gpu_timeout_seconds: int = 60
    # 추천 응답 생성 temperature. 프로덕션 기본 0.3, eval 재현성 위해 GEN_TEMPERATURE=0 로 고정 가능.
    gen_temperature: float = 0.3

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
