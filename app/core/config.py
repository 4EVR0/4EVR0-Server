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

    llm_timeout_seconds: int = 5

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
