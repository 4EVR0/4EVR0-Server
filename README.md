# 4EVR0-Server

화장품 성분 플랫폼의 LLM 서빙 백엔드. 사용자의 피부 고민을 입력받아 Neo4j GraphRAG로 관련 성분과 근거 논문을 검색하고, LLM이 추천 응답을 생성한다.

---

## 기술 스택

- **FastAPI** + **uvicorn** — 비동기 API 서버
- **PostgreSQL** (asyncpg) — 세션 및 대화 히스토리 영속화
- **Neo4j** — 성분·클레임·논문 그래프 DB (GraphRAG)
- **Redis** — 활성 세션 히스토리 캐시
- **OpenAI SDK** — Phase 1 (OpenAI API) / Phase 2 (vLLM) 공통 클라이언트

---

## 작동 방식

### 전체 흐름

```
클라이언트
    │
    │  POST /api/v1/recommend
    │  { session_id, message: "건조한 피부에 좋은 성분 추천해줘" }
    ▼
[recommend.py]  세션 유효성 확인 → recommendation_service.recommend() 호출
    │
    ├─ 1. 프로필 추출 (rule-based, 즉시)
    │      "건조한" → concerns: [DRY_SKIN] → effects: [MOISTURIZING, BARRIER_REPAIR]
    │
    ├─ 2. GraphRAG 검색 (Neo4j)
    │      effects 리스트로 Cypher 쿼리 실행
    │      → [{ingredient: "Hyaluronic Acid", claim: "보습 증가", tier: "Confirmed", pmid: "12345"}]
    │
    ├─ 3. 대화 히스토리 로드
    │      Redis 캐시 확인 → 없으면 PostgreSQL에서 최근 5턴 로드
    │
    ├─ 4. LLM 메시지 구성
    │      system: "당신은 화장품 성분 전문가..."
    │      system: "관련 성분 데이터: ..."  ← graph context 주입
    │      [이전 대화 히스토리]
    │      user: "건조한 피부에 좋은 성분 추천해줘"
    │
    ├─ 5. LLM 호출 (provider 자동 분기)
    │      Phase 1: OpenAI API
    │      Phase 2: vLLM on RunPod  ← .env 한 줄로 전환
    │
    └─ 6. 턴 저장 + 응답 반환
           PostgreSQL conversation_turns INSERT
           Redis 캐시 무효화
```

### 핵심 설계 포인트

#### 1. LLM Provider 추상화 — 코드 변경 없이 전환

`make_llm_client()`가 `.env` 설정에 따라 클라이언트를 분기한다. vLLM이 OpenAI API 스펙을 동일하게 구현하기 때문에 호출 코드는 완전히 동일하다.

```python
# Phase 1 (.env: LLM_PROVIDER=openai)
openai.AsyncOpenAI(api_key="sk-...")

# Phase 2 (.env: LLM_PROVIDER=vllm)
openai.AsyncOpenAI(api_key="not-needed", base_url="https://<runpod>/v1")
```

#### 2. GraphRAG — 검색 후 생성 (RAG) 패턴

LLM이 성분 지식을 직접 생성하는 것이 아니라, Neo4j에서 근거 있는 데이터를 먼저 검색해 system 메시지로 주입한다. LLM은 해당 데이터를 자연어로 설명하는 역할만 수행한다.

- 환각(hallucination) 방지
- 논문 PMID 인용 가능
- eligibility_tier(Confirmed > Promising) 기준 정렬로 신뢰도 높은 성분 우선 제공

```cypher
MATCH (i:Ingredient)-[:HAS_CLAIM]->(c:Claim)-[:TARGETS]->(e:Effect)
MATCH (c)-[:SUPPORTED_BY]->(p:Paper)
WHERE e.name IN $effects
  AND c.eligibility_tier IN ['Confirmed', 'Promising']
RETURN i.name, c.claim_text, c.eligibility_tier, p.pmid
ORDER BY CASE c.eligibility_tier WHEN 'Confirmed' THEN 1 ELSE 2 END, c.confidence DESC
LIMIT 10
```

#### 3. 멀티턴 대화 — Redis + PostgreSQL 이중 레이어

| 레이어 | 역할 | TTL |
|--------|------|-----|
| Redis | 활성 세션 히스토리 캐시 — 매 요청 DB 조회 방지 | 30분 |
| PostgreSQL | 영속 저장 — 세션 만료 후 분석 가능 | 영구 |

Redis 장애 시 PostgreSQL로 자동 fallback.

#### 4. A/B 모델 비교

```env
LLM_MODEL=exaone          # 80% 트래픽
LLM_MODEL_B=qwen          # 20% 트래픽
AB_TEST_RATIO=0.2
```

응답에 `model_used` 필드가 포함되어 어떤 모델이 응답했는지 추적 가능.

---

## 환경 설정

### .env 예시

```env
# Database
POSTGRES_DSN=postgresql://user:password@localhost:5432/cosmetic
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=password
REDIS_URL=redis://localhost:6379

# LLM — Phase 1 (OpenAI)
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini

# LLM — Phase 2 (vLLM, 주석 해제 후 전환)
# LLM_PROVIDER=vllm
# LLM_BASE_URL=https://<runpod-endpoint>/v1
# LLM_MODEL=exaone
# LLM_MODEL_B=qwen
# AB_TEST_RATIO=0.2

# Conversation
CONVERSATION_HISTORY_LIMIT=5
```

### Phase 2 vLLM 전환 (RunPod)

RunPod에서 vLLM 컨테이너 기동:

```bash
# EXAONE-3.5-7.8B (RTX 4090 × 1, ~$0.44/hr)
docker run --gpus all vllm/vllm-openai:latest \
  --model LGAI-EXAONE/EXAONE-3.5-7.8B-Instruct \
  --served-model-name exaone \
  --dtype bfloat16 --max-model-len 8192

# Qwen2.5-7B (RTX 4090 × 1, ~$0.44/hr)
docker run --gpus all vllm/vllm-openai:latest \
  --model Qwen/Qwen2.5-7B-Instruct \
  --served-model-name qwen \
  --dtype bfloat16 --max-model-len 8192
```

`.env`의 `LLM_PROVIDER=vllm`, `LLM_BASE_URL=<RunPod URL>/v1` 으로 변경하면 전환 완료.

---

## API

### `POST /api/v1/sessions`
새 대화 세션 생성.

```json
// Response
{"session_id": "uuid"}
```

### `POST /api/v1/recommend`
피부 고민 기반 성분 추천 (멀티턴).

```json
// Request
{
  "session_id": "uuid",
  "message": "건조한 피부에 좋은 성분 추천해줘",
  "category": "스킨케어"
}

// Response
{
  "session_id": "uuid",
  "turn_id": 3,
  "ingredients": [
    {
      "name": "Hyaluronic Acid",
      "claim": "피부 수분 보유량 증가",
      "eligibility_tier": "Confirmed",
      "paper_ref": "PMID:12345678"
    }
  ],
  "response_text": "건조한 피부를 위해 히알루론산을 추천합니다...",
  "model_used": "gpt-4o-mini"
}
```

### `POST /api/v1/profile/extract`
텍스트에서 피부 프로필 추출 (LLM + rule-based fallback).

### `GET /health`
서버 및 의존성 상태 확인 (PostgreSQL, Neo4j, Redis, LLM).

---

## 로컬 실행

```bash
docker compose up -d
uvicorn app.main:app --reload --port 8000
```
