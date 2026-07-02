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
    # asyncpg 풀 크기(app/core/db.py). 세션 API는 가벼워 크게 필요 없다.
    pg_pool_min_size: int = 1
    pg_pool_max_size: int = 10

    redis_url: str = "redis://localhost:6379"
    # 추천 응답 캐시(app/repositories/recommend_cache.py). 같은 고민 문장 반복 시 GPU를 건너뛴다.
    # 데이터가 거의 안 변해 TTL을 길게(기본 24h). 부하 테스트 on/off 비교용으로 토글 가능.
    recommend_cache_enabled: bool = True
    recommend_cache_ttl_seconds: int = 86400

    gpu_server_url: str = "http://127.0.0.1:18000"
    gpu_model: str = "Qwen/Qwen3-8B-FP8"
    gpu_timeout_seconds: int = 60
    # /health의 llm 상태 판정용 vLLM /v1/models 핑 타임아웃(초). LB 헬스체크 주기 안에
    # 끝나야 하므로 요청 타임아웃(gpu_timeout_seconds)보다 훨씬 짧게.
    llm_health_timeout_seconds: float = 3.0
    # startup 워밍업(app/core/warmup.py). vLLM 모델 로드(수십초~분)를 기다렸다가 준비되는
    # 즉시 더미 호출로 첫-요청 컴파일 꼬리를 흡수한다. 재시도 간격×횟수가 커버 범위.
    warmup_enabled: bool = True
    warmup_llm_max_attempts: int = 30
    warmup_llm_retry_seconds: float = 10.0
    # 추출(LLM #1)에 guided decoding(JSON 스키마 강제) 사용 여부. 유효 JSON·유효 enum 값을 보장해
    # 파싱 실패·규칙기반 폴백을 줄인다(신뢰성). P4 실험용 토글(EXTRACT_GUIDED_DECODING).
    extract_guided_decoding: bool = False
    # GPU(vLLM) 동시 호출 한도. 단일 GPU 서빙 보호용 — 동시 요청이 몰릴 때 vLLM 큐 폭증·
    # 타임아웃 캐스케이드를 막는다(app/clients/llm_gate.py). 0 이하면 비활성(무제한, 기존 동작).
    # 부하 테스트로 적정값 탐색(baseline은 동시 10→25 구간에서 열화 시작).
    llm_max_concurrency: int = 8
    # True면 동시성 한도 초과 시 대기 대신 즉시 거절(HTTP 429). False면 한도 내로 직렬화하되
    # 초과분은 큐에서 대기(처리량 동일, latency 안정).
    llm_reject_over_capacity: bool = False
    # 생성 프롬프트 버전(app/prompts/<name>.txt). latency/품질 실험용으로 GEN_PROMPT_NAME 로 교체.
    # v6: P3 스윗스팟 — v4 대비 total −30%(출력 토큰↓)이면서 품질 유지(OVERALL 4.52→4.46, grounding 동일).
    gen_prompt_name: str = "recommend_response.v6"
    # 추천 응답 생성 temperature. 프로덕션 기본 0.3, eval 재현성 위해 GEN_TEMPERATURE=0 로 고정 가능.
    gen_temperature: float = 0.3
    # 간결한 응답을 유도하되 문장 중간 잘림을 막을 수 있는 충분한 출력 여유.
    gen_max_tokens: int = 1200

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
