# 4EVR0-Server

화장품 성분·제품 추천 API 서버. 사용자 자연어 입력 → GPU 서버(vLLM) 프로필 추출 → Neo4j 성분·제품 쿼리 → 추천 응답 생성. 단일 정적 웹 UI(챗봇)도 같은 서버가 함께 서빙한다.

## 현재 상태 (2026-11-19)

- **공개 베타 운영 종료.** 2026-11-04 ~ 11-17에 `https://4evr0.duckdns.org`로 공개 베타를 운영했고,
  비용 때문에 AWS 리소스(베타 앱 EC2·RDS·모니터링·GraphDB·Airflow EC2, S3, ECR)를 정리하기로 했다.
  EC2는 모두 중지되어 지금은 접속할 수 없다.
- **데이터는 팀 로컬 백업으로 보관.** 베타 RDS 전체(질문·답변 원문, 답변 메타데이터, 릴리스, 피드백, 의견),
  모니터링(Prometheus·Grafana·Loki·MLflow), S3 원본, Neo4j 덤프, Airflow 기록을 정리 직전에 내려받았다.
  질문 원문이 포함되어 있어 외부 공유 금지.
- 베타 기간 응답: `turn_events` 568건(점검 트래픽 포함), 수집 원문 `turn_records` 192건.
- 다시 운영하려면: 인프라는 `Terraform_Infra`, 앱 배포는 [배포 가이드](deploy/README.md)와 Terraform_Infra
  `docs/beta-runtime-deployment.md`(실제 배포 기록), DB는 백업 덤프를 `pg_restore`로 복원한다.

---

## 동작 흐름

```
[사용자] ──자연어 고민──▶ [앱 서버 · FastAPI]
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
        [GPU 서버]       [Neo4j 서버]      [PostgreSQL(RDS) / Redis]
        Vast.ai · vLLM    AWS EC2           세션·대화·수집 기록
        (Qwen3.5-9B bf16) Graph DB
              │               │
   ① 프로필 추출       ② 효능→성분 조회
   (자연어→JSON,        ③ 성분→제품 조회
    실패 시 규칙 폴백)
              │               │
              └───────┬───────┘
                      ▼
       ④ LLM 추천 응답 생성 (성분 설명 → 제품 추천, 한글 성분명 + 근거 수준 인용)
                      ▼
       ⑤ 서버 출력 검사·정리 (아래) → 실패 시 근거 기반 고정 답변
                      ▼
                  [사용자]
```

생성 답변은 그대로 내보내지 않고 서버가 검사·정리한다.

- **근거 검사:** 그래프에 없는 성분·효능·제품 연결, 근거 없는 안전·함량 단정, 한자 섞임을 찾으면 근거 기반 고정 답변으로 바꾼다.
- **고시 원문 제거:** 식약처 배합한도·사용금지 원문은 생성 입력·답변·성분 카드 어디에도 내보내지 않는다.
- **복합 고민:** 고민이 둘 이상이면 고민별로 성분을 찾고, 고민마다 근거 성분을 보장한다. 모델이 고민을 못 뽑으면 키워드 사전으로 보완한다.
- **성분 설명:** 고민별 논문 근거 수와 연구에서 다룬 역할 목록, 고민과 역할을 잇는 일반 설명을 생성 입력으로 준다. 고민 분석에 질문과 무관한 고민이 나오면 그 질문의 연결 설명으로 바꾼다.
- **안전 안내:** 처방 치료를 화장품으로 대신하려는 질문에는 답변 맨 앞에 치료 대체 불가 안내를 붙이고, 처방 중단을 받아 주는 문장은 지운다.
- **추천하지 않는 종류:** 선크림(데이터 없음)과 클렌징 제품(씻어 내는 제품)은 다른 제품을 그 종류처럼 보여 주지 않고 없다고 안내한다.
- **이름으로 비교:** "A와 B의 차이가 뭐야?"는 생성 모델 없이 그래프 성분으로 요약·효능별 성분·비교표를 만든다.

모든 서버는 **Tailscale VPN + MagicDNS**로 연결된다. `.env`에서 IP 대신 MagicDNS 호스트명을 쓰므로 인스턴스를 교체해도 설정 수정이 불필요하다.
아래는 베타 운영 당시 구성이다(AWS 쪽은 정리 대상).

| 서버 | MagicDNS 호스트명 | 역할 |
|------|------------------|------|
| 베타 앱 (EC2 t4g.medium, ARM64) | `evr0-beta-app.tailb70036.ts.net` | FastAPI + 웹 UI + Caddy(HTTPS), 공개 도메인 `4evr0.duckdns.org` |
| 로컬 개발 (Mac) | `macbook-pro-3.tailb70036.ts.net` | 개발·평가 |
| GPU 서버 (Vast.ai, RTX 3090) | `vast-gpu-server-2.tailb70036.ts.net:18000` | vLLM (`Qwen3.5-9B` bf16) |
| Neo4j 서버 (EC2) | `ip-172-31-56-102.tailb70036.ts.net:7687` | Graph DB |
| PostgreSQL (RDS 16) | 비공개(앱 EC2에서만 접근, TLS verify-full) | 세션·대화·수집 기록 |
| 모니터링 서버 (EC2) | `monitoring-server-1.tailb70036.ts.net` | Prometheus + Grafana + Loki + MLflow |

> GPU 서버를 다시 빌릴 때 Tailscale에 `vast-gpu-server-2-1`처럼 다른 이름으로 가입되면 앱이 GPU를 찾지 못한다.
> Tailscale 관리 콘솔에서 이름을 `vast-gpu-server-2`로 맞춘다.

---

## 성능·품질 엔지니어링 (측정 기반 최적화)

> 단일 GPU(RTX 3090) · `Qwen3.5-9B` · vLLM 서빙에서 **측정으로 병목을 찾고 → 레버를 걸고 →
> 결과를 품질 게이트로 검증**하는 LLMOps 루프. 상세 근거·원자료는 `review/` 문서.

### 1. 서빙 지연(latency) 최적화 — decode가 병목
단건 latency를 span별로 분해하니 **generate/decode가 총 시간의 76~87%**. 병목을 정조준:

| 레버 | 효과 | 성격 |
|---|---|---|
| **SSE 선행 표시** | 구조 데이터 TTFT **10s → 2.7s (~4.3×)** | 성분·제품 카드 즉시 + 근거 검증 후 본문 |
| **간결 프롬프트(v6)** | total **−30%** (12s → 8.35s), 품질 유지(judge OVERALL 4.52→4.46, grounding 동일) | 출력 토큰↓ |
| **동시성 제어(세마포어+429)** | 과부하 시 실패율 **8.7% → 0%** | admission control(붕괴 방지) |
| **프리픽스 캐싱** | 처리량 **+12%**, p95 **−11%** | vLLM 엔진 튜닝 |

부하 테스트로 **처리량 천장 ≈ 0.76 RPS**(단일 8B GPU 한계)를 확인 → 근본 해결은 서빙 레버(양자화)로.

### 2. 양자화 (AWQ int4) — 비용↓ 하되 품질 게이트로 검증 ⭐
`Qwen3.5-9B` bf16 → AWQ int4 A/B. **"빠르게 만들되 품질 회귀를 eval로 막는다"** 규율로 채택 결정:

| 지표 | bf16 | AWQ int4 | Δ |
|---|---|---|---|
| decode 속도(batch1) | 46 tok/s | **111 tok/s** | **2.4×** |
| 동시 처리량(8스트림) | 254 tok/s | **572 tok/s** | **2.25×** |
| 가중치 VRAM | 17.7GB | 5.3GB | −70% |
| 최대 동시성 @32K ctx | ~2.3× | **10.3×** | KV 캐시 4.4× |
| 당시 자동 Judge OVERALL(참고) | 4.46 | **4.58** (grounding 유지) | 사람 교정 전 측정 |

→ **서빙 효율 기준으로 채택.** 다만 이후 사람 블라인드 40건에서 핵심 3축의 케이스별
평균 Pearson은 `0.4835`였고 Judge가 평균 `+0.753` 과대평가해, 위 자동 점수는
절대 품질 근거로 사용하지 않는다.
트레이드오프는 TTFT +19%(전체에 묻힘)와 추출 precision 소폭 하락(recall은 상승)이다.

> **2026-10-11 bf16으로 되돌림.** 실제 응답의 오류 건수(근거 없는 표현·데이터 언급·대체 응답)로 다시 비교하니
> AWQ가 프롬프트 금지 표현을 더 자주 어겼다(생성 원문 기준 근거 없는 표현: 첫 추천 9 → 0, 후속 4 → 0).
> 베타 전까지 정확성을 우선해 bf16을 쓰고, decode 속도(약 2.4배 느림)와 동시 처리량은 부하 측정으로 다시 확인한다.

### 3. 콜드스타트 — 진단하고 앱·인프라 양면에서 해결 (실측 검증) ⭐
신규 GPU 대여 시 서버 준비까지의 비용을 실측 분해하고, 두 축으로 해결·검증:

- **앱 사이드(구현·검증):** readiness 게이트(콜드 vLLM 라우팅 차단) + startup 워밍업 + 커넥션 풀링 + 캐시 single-flight.
  - 워밍업 A/B(콜드 compile): 첫 요청 컴파일 꼬리 **+4.42s → +1.25s** (워밍업 더미가 대신 지불, **−72%**).
- **인프라(영구 볼륨) — 실측 검증됨:** `/workspace`에 영구 볼륨 마운트 → 같은 볼륨 재부팅 A/B:

  | 단계 | 1차(빈 볼륨) | 2차(볼륨 유지) | 효과 |
  |---|---|---|---|
  | 가중치 다운로드 | 30.1s | **0s** | ✅ 볼륨이 재다운로드 제거 |
  | torch.compile | 45.9s | **12.6s** | ✅ compile 캐시 재사용(−73%) |
  | 서버 준비 총 | ~165s | **~110s** | −33% |

  > (측정: RTX 3090, AWQ int4 + HF_TOKEN. 원 진단의 "다운로드 216s"는 bf16+무토큰 기준 — 채택 구성에선
  > 다운로드가 애초에 30s이고 볼륨이 이를 0으로 만든다. 남는 최대 비용 = 매 부팅 cudagraph/KV 캡처 ~94s.)

### 4. 품질 회귀 게이트 (eval-in-CI) ⭐
프롬프트·모델·검색 변경이 품질을 떨어뜨리면 **CI가 자동 차단**. 유닛테스트가 "버그=머지 금지"라면 이건 "**품질 저하=머지 금지**":

- **오프라인 eval 하네스:** 추출 정확도(concern F1·skin_type accuracy) + 생성 품질(LLM-as-judge — OVERALL·grounding·format, 외부 `gpt-4o-mini`, bootstrap 95% CI).
- **게이트:** 결과를 임계값과 비교 → 미달 시 CI 실패 + PR 코멘트 점수 표(self-hosted 러너 + 라벨 트리거).
- **검증:** 채택 AWQ 통과 / 실제 회귀 사례(프롬프트 v5, grounding **4.55→3.80**)를 **차단** 확인.

> **방법론 — judge 게이트 규율:** 모든 채택 결정(양자화·프롬프트)은 "느낌"이 아니라
> *judge OVERALL이 baseline CI 안 + grounding 무회귀*라는 동일 규율로 판정하고, 이를 §4에서 CI로 자동화했다.

---

## 웹 UI

별도 프론트엔드 레포·빌드 단계 없이, 앱 서버가 단일 정적 파일을 직접 서빙한다.

- 파일: `app/static/index.html` + `app/static/olive.css`
- 경로: `GET /` → `index.html` 반환, `/static`에 정적 마운트
- 특징: 올리브 톤 챗봇 UI, 마크다운 렌더링, 상품 사진, 성분 카드(한글 성분명 + 한국어 효능, 영문 효능명·고시 원문은 표시하지 않음)
- 사이드바: 새 상담, 관심 제품(브라우저 저장), 사용 가이드, 개인정보 처리방침, **개발자에게 의견 보내기**(오류 신고·개선 제안), 내 데이터 관리(저장된 대화·의견 삭제)
- 피부 프로필(피부 타입·고민)은 질문과 별도로 보내며, 민감 프로필은 주의 성분이 든 후보를 제외하는 데만 쓴다.
- 답변마다 👍/👎 피드백(불편 항목·메모 선택)
- 브라우저 대화는 HttpOnly 쿠키로 2시간 유지한다. 새로고침 시 Redis의 최근 대화(최대 8턴)를 복원하고, **새 대화** 버튼은 이전 세션·맥락을 삭제한다. 만료된 세션의 후속 질문은 자동으로 다른 대화에 붙이지 않는다.
- 이전 추천 중 2~3개 제품을 지정해 비교하면 그래프에 INCI로 매핑된 성분의 공통점·차이를 Markdown 표로 보여준다. 그래프에서 확인되지 않은 성분은 제품에 없다고 단정하지 않는다. 4개 이상이거나 제품 선택이 모호하면 2~3개를 지정해 달라고 묻는다.
- 브라우저 호출 API: `POST /api/v1/sessions/browser` → `GET /api/v1/sessions/current` → `POST /api/v1/recommend/stream` (스트리밍 불가 시 일괄 경로)
- 비브라우저 API 클라이언트는 기존 `POST /api/v1/sessions`의 `session_id` 응답과 추천 요청의 `session_id` 필드를 계속 사용할 수 있다.

---

## API 엔드포인트

| 메서드 | 경로 | 설명 |
|--------|------|------|
| `GET`  | `/` | 웹 UI(챗봇) |
| `GET`  | `/live` | 앱 프로세스 생존(외부 연결 검사 없음) |
| `GET`  | `/ready` | PostgreSQL·Redis·Neo4j·GPU 모델 준비. 모두 정상일 때만 200 |
| `GET`  | `/health` | 기존 호환 상태 경로 |
| `GET`  | `/release` | 현재 릴리스(코드·모델·프롬프트·캐시·그래프 지문) |
| `GET`  | `/docs` | Swagger UI |
| `GET`  | `/metrics` | Prometheus 메트릭 |
| `POST` | `/api/v1/sessions` | 세션 생성 |
| `POST` | `/api/v1/sessions/browser` | 브라우저 쿠키 세션 생성(본문에 ID 없음) |
| `GET` | `/api/v1/sessions/current` | 현재 브라우저 세션의 최근 대화 복원 |
| `DELETE` | `/api/v1/sessions/current` | 현재 세션·대화 맥락 삭제 |
| `POST` | `/api/v1/recommend` | 추천 (성분 + 제품 + 자연어 응답) |
| `POST` | `/api/v1/recommend/stream` | 추천 SSE(meta → 검증된 본문 delta → done) |
| `GET`  | `/api/v1/recommend/path` | 효능→성분→제품 그래프 경로 조회 |
| `POST` | `/api/v1/profile/extract` | 자연어 → 피부 프로필 추출 |
| `GET`  | `/api/v1/data-policy` | 수집 안내·보관 기간·피드백 항목·의견 종류 |
| `POST` | `/api/v1/feedback` | 답변 피드백(👍/👎·불편 항목·메모) |
| `POST` | `/api/v1/developer-reports` | 개발자에게 의견 보내기(오류 신고·개선 제안) |
| `DELETE` | `/api/v1/my-data` | 이 브라우저의 저장된 대화·의견 삭제 |

공개 프록시(Caddy)는 `/live`·`/ready`·`/health`·`/docs`·`/metrics`를 404로 막는다.

## 데이터 수집과 보관

- 수집 안내(입력창 아래·개인정보 처리방침)가 표시된 브라우저의 질문 원문과 실제로 보여 준 최종 답변·추천 제품·근거를
  `turn_records`에 30일 보관한다. 학습 데이터로 쓰지 않는다(`training_use=false`).
- 이메일·전화번호·주민번호·이름이나 진단·치료·처방 같은 민감정보가 있으면 원문을 저장하지 않는다.
- 모든 답변의 메타데이터(완료·실패·취소, 응답 방식, 캐시 여부, 지연 시간, 제품 ID)는 원문 없이 `turn_events`에,
  어떤 코드·모델·프롬프트로 답했는지는 `release_versions`에 남긴다.
- 피드백·개발자 의견도 30일 보관하며, 사용자는 "내 데이터 관리"에서 지울 수 있다. 만료 데이터는 매시간 정리한다.
- 운영 DB 계정은 DML만 가진다(`DB_AUTO_CREATE_SCHEMA=false`). 스키마 추가는 별도 migration 계정으로 `python -m app.core.migrate`.

---

## 프로젝트 구조

```
app/
  main.py              # FastAPI 진입점 (라우터·미들웨어·정적 서빙)
  api/                 # 라우터: health, sessions, profile, recommend, data_collection
  services/            # 비즈니스 로직 (recommend, 프로필 추출, taxonomy 정규화, 출력 검사, 제품 설명·비교, 수집)
  clients/             # 외부 연동 (vLLM/LLM, Neo4j, 폴백)
  repositories/        # 대화·세션·수집 기록·릴리스 저장 (PostgreSQL), 응답 캐시 (Redis)
  prompts/             # 버전 관리되는 프롬프트 (*.txt + 로더)
  schemas/ domain/     # Pydantic 스키마, 도메인 enum
  core/                # 설정, 로깅, 메트릭, 예외 처리, 미들웨어
  static/index.html    # 웹 UI
eval/                  # 프로필 추출 / 응답·검색 평가 / 사람 Judge 교정 (MLflow 기록)
scripts/               # 컨테이너 smoke, 개발자 의견 조회 등 운영 보조 스크립트
deploy/                # 공개 베타 배포 가이드·Caddyfile
tests/                 # pytest 단위 테스트
```

프롬프트는 코드에 하드코딩하지 않고 `app/prompts/*.txt`로 분리·버전 관리한다. 추천 응답 프롬프트는 현재 `recommend_response.v14`가 기본이다.
응답 캐시 키에는 프롬프트·모델·설정이 들어가고, 출력 규칙을 바꿀 때마다 캐시 네임스페이스를 올린다(현재 `reccache:v41`).

---

## 환경 변수 (`.env`)

```env
APP_NAME=4EVR0 Cosmetic Recommendation API
APP_VERSION=1.0.0
DEBUG=false

POSTGRES_DSN=postgresql://cosmetic_user:cosmetic_pass@postgresql:5432/cosmetic_db
REDIS_URL=redis://redis:6379

NEO4J_URI=bolt://ip-172-31-56-102.tailb70036.ts.net:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<비밀번호>

GPU_SERVER_URL=http://vast-gpu-server-2.tailb70036.ts.net:18000
GPU_MODEL=Qwen/Qwen3.5-9B          # vLLM이 실제 서빙하는 모델명과 반드시 일치
GPU_TIMEOUT_SECONDS=60

GEN_PROMPT_NAME=recommend_response.v14  # 추천 응답 프롬프트(기본값과 같음)
GEN_TEMPERATURE=0.3                # 추천 응답 생성 온도 (eval 재현 시 0)
GEN_MAX_TOKENS=1200                # 응답 잘림 방지용 출력 여유
VERIFIED_STUDY_RESPONSE_ENABLED=true  # 검증 연구의 짧은 설명; false면 기존 생성 경로로 복귀
REDNESS_VERIFIED_STUDY_RESPONSE_ENABLED=true  # 홍조·로사케아 연구 설명만 별도로 켜고 끄기
DICTIONARY_EXPLANATIONS_ENABLED=false  # 보습 성분 사전 설명 파일럿; 품질 확인 전 기본 OFF
```

> ⚠️ `GPU_MODEL`이 vLLM 실제 서빙 모델과 불일치하면 404 → 규칙 기반 폴백으로 동작한다.

검증 연구 설명이 운영 응답에서 과하면 `VERIFIED_STUDY_RESPONSE_ENABLED=false`로
설정하고 API 서버를 재시작한다. 이 토글은 연구 템플릿만 끄며 성분명 교정과
제품 사용 부위 필터는 유지한다. 설정별 캐시 키가 달라 이전 답변이 다시
노출되지 않는다. 다시 켜려면 `true`로 설정하고 재시작한다.

홍조·로사케아의 연구 설명만 롤백하려면
`REDNESS_VERIFIED_STUDY_RESPONSE_ENABLED=false`로 설정하고 API 서버를
재시작한다. 그러면 이전의 보수적 `redness_evidence_template` 응답으로 돌아가고,
다른 검증 연구 응답은 유지된다. 이 스위치도 캐시 키에 포함돼 롤백 전
답변이 재사용되지 않는다. 다시 켜려면 `true`로 설정하고 재시작한다.

성분 사전 설명 파일럿은 `DICTIONARY_EXPLANATIONS_ENABLED=true`로 설정하고
API 서버를 재시작하면 활성화된다. 건조·수분 부족 고민에서 선정된 제품의
`CONTAINS` 성분 중 글리세린·소듐하이알루로네이트가 확인될 때만 짧은 일반
역할 설명을 제공한다. 검색 성분·점수·제품 순위는 바꾸지 않으며, 그래프에
없는 성분을 '미함유'로 단정하지 않는다. 기본값은 품질 검증 전 `false`다.
롤백은 `false`로 변경 후 재시작하며, 카드 내용 해시가 캐시 키에 포함되어
ON/OFF 및 카드 변경 전후 답변이 섞이지 않는다. 후속 질문은 선택 제품의
함유 근거를 다시 조회하며 세션에 설명 카드를 영구 복제하지 않는다.

카드가 제공된 요청에는 검토된 일반 보습 역할을 추천 이유에 쓰는 추가
시스템 지침을 적용한다. 카드가 없으면 기존 프롬프트를 유지한다.
추가 지침의 해시도 ON 캐시 키와 실험 메타데이터에 포함한다.

파일럿 설명 출처: 김기연 외, 《화장품성분학 사전》, 현문사(2011),
ISBN 9788966300891, 글리세린 p.25·소듐하이알루로네이트 p.93.
제공 자료의 해당 항목을 검토해 화장품 보습 역할만 짧게 바꿔 썼다.
원문·스캔은 배포하지 않으며 제품 임상 효과, 저자극, 알레르기 안전성,
피부 깊은 침투를 보장하는 근거로 사용하지 않는다. 제공 스캔의 쇄는 미확인이다.

사전 설명 A/B는 저장된 검색 스냅샷에 대해 동일 제품·순서를 고정하고
GPU에서 ON/OFF 응답을 다시 생성한다. 외부 Judge를 호출하거나 그래프를 쓰지 않는다.
추가 함유 근거와 설명의 결합 효과를 보는 탐색 파일럿이며, 설명만의 효과를
분리한 실험이나 통계적 유의성 검증은 아니다.

```bash
python eval/run_dictionary_pilot.py \
  --snapshot-report eval/results/dictionary-pilot-20260929-results.json \
  --output eval/results/dictionary-product-pilot-20260929-results.json
```

추적 파일이 깨끗한 커밋 SHA에서만 실행하며 GPU 준비 실패 시 평가를 시작하지 않는다.
완료 시 실제 초안·최종 응답·생성/평가 근거·함유 스냅샷·노출 여부·A/B 표시 순서를
로컬 JSON과 MLflow에 남긴다. 입력/결과 파일은 로컬 평가 산출물이다.

샘플은 `.env.example` 참고.

---

## 실행

### 배포용 앱 이미지

웹 UI·프롬프트·추천 근거 JSON을 포함한 앱 이미지를 빌드한다. DB와 GPU는 실행 시
환경변수로 연결한다. `.dockerignore`는 `app/`과 빌드 입력만 허용해 `.env`, 평가
결과, 로컬 리뷰, Git 이력을 빌드 컨텍스트에서 제외한다.

```bash
APP_SHA=$(git rev-parse HEAD)
docker buildx build --load --platform linux/amd64 \
  --build-arg VCS_REF="$APP_SHA" --tag "4evr0-server:$APP_SHA" .
python3 scripts/smoke_container.py "4evr0-server:$APP_SHA" \
  --platform linux/amd64 --expected-revision "$APP_SHA"
```

- 배포 이미지는 커밋된 소스에서 빌드하고 SHA 태그와 image digest를 기록한다.
  Python 베이스 이미지는 버전/digest를 고정했으며 업데이트 시 다시 검증한다.
  직접 의존성은 `requirements.txt`에 고정되어 있지만 전이 의존성 전체 lock은 아직 없다.
- x86 EC2는 `linux/amd64`, Graviton EC2/Apple Silicon의 네이티브 검증은
  `linux/arm64`를 사용한다. 두 아키텍처를 로컬에 함께 보관하려면 태그에
  `-amd64`/`-arm64`를 구분해 붙인다.
- UID/GID `10001:10001`로 실행하며 Python bytecode 쓰기를 끈다.
  소스 파일은 이미지에 포함된 읽기 전용 입력으로 사용하고 로그는 stdout으로 출력한다.
- smoke 검증은 네트워크/호스트 포트 없이, 읽기 전용 파일시스템에서 기동·필수 파일·웹 UI·
  API 스키마·메트릭·정상 종료를 확인한다. GPU/DB 없이 실행할 수 있으며 추천 품질 평가는 별도다.
- PR과 main의 관련 변경은 `container-smoke` CI에서 Linux AMD64로 빌드·검증한다.

외부 DB/GPU 연결 값을 준비한 뒤 앱만 실행하는 예:

```bash
docker run --name 4evr0-app --detach \
  --env-file /absolute/path/to/runtime.env \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --publish 127.0.0.1:8000:8000 \
  "4evr0-server:$APP_SHA"
```

`runtime.env`에는 `.env.example`을 참고해 `POSTGRES_DSN`, `REDIS_URL`, `NEO4J_URI`,
`NEO4J_USER`, `NEO4J_PASSWORD`, `GPU_SERVER_URL`, `GPU_MODEL` 및 필요한 인증 정보를
설정한다. 컨테이너의 `localhost`는 컨테이너 자신이므로 DB/GPU의 실제 접근 가능한
주소를 사용한다. Tailscale 호스트명은 컨테이너에서도 DNS/통신이 되는지 확인해야 한다.
위 포트는 호스트 내부 확인용이다. 공개 베타 구성은 [배포 가이드](deploy/README.md)를 참고한다.
생존 검사는 외부 연결 없는 `/live`, 의존성 준비 검사는 `/ready`로 구분한다.
기존 `/health`는 GPU/DB 없는 이미지 smoke 테스트의 생존 검사로 사용하지 않는다.

### Docker Compose (로컬 개발)

```bash
docker compose up
```
앱(8000) + PostgreSQL + Neo4j + Redis + promtail 컨테이너가 함께 뜬다.
이 Compose의 개발용 비밀번호·호스트 공개 포트·DB 저장 설정은 운영에 그대로 사용하지 않는다.

### 로컬 개발 (hot reload)
맥 로컬 PostgreSQL이 5432를 점유하면 Docker PostgreSQL을 5433으로 우회:
```bash
docker run -d --name 4evr0-postgresql \
  -e POSTGRES_USER=cosmetic_user -e POSTGRES_PASSWORD=cosmetic_pass \
  -e POSTGRES_DB=cosmetic_db -p 5433:5432 postgres:16
docker compose up -d redis

POSTGRES_DSN="postgresql://cosmetic_user:cosmetic_pass@localhost:5433/cosmetic_db" \
REDIS_URL="redis://localhost:6379" \
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

- 웹 UI: `http://localhost:8000/`
- API 문서: `http://localhost:8000/docs`
- 헬스체크: `http://localhost:8000/health`

> 정적 UI(`index.html`)는 요청마다 디스크에서 읽으므로 재시작 없이 새로고침으로 반영된다. 프롬프트·서비스·설정 변경은 서버 재시작이 필요하다(`--reload` 미사용 시).

---

## 테스트 & 평가

```bash
# 단위 테스트
pytest tests/

# 프로필 추출 평가 (라벨 50건)
python eval/run_eval.py --no-mlflow

# 추천 응답 품질 평가 (외부 LLM judge 필요 — 생성기와 다른 모델)
JUDGE_MODEL=<external-model> JUDGE_API_KEY=<key> \
  python eval/run_response_eval.py --gen-temperature 0

# 품질 회귀 게이트 (임계 미달 시 exit 1 → CI 머지 차단)
python eval/check_gate.py --extraction <run_eval.json> --response <run_response_eval.json>
```

응답 평가는 자기 채점을 거부하며(외부 judge 강제), grounding·conciseness 등 5개 축을
1~5점으로 기록한다. 현재 Judge는 사람 교정을 통과하지 못했으므로 이 의미 점수는 관측용이다.
CI의 응답 게이트는 에러·한자 누출·세션 오염처럼 결정론적으로 측정되는 지표만 사용한다.
평가 결과는 MLflow에 기록한다. 기본은 로컬 `eval/mlflow.db`이고, 공용 서버를 쓰려면
`MLFLOW_TRACKING_URI`를 지정한다(베타 기간에는 모니터링 서버 `:5000`, 정리 대상).
품질 게이트는 PR에 `run-eval` 라벨을 붙이면 self-hosted 러너에서 자동 실행된다
(`.github/workflows/eval-gate.yml` — 프롬프트·모델·검색 변경의 품질 회귀를 머지 전 차단).
자세한 내용은 `eval/README.md`, `.github/workflows/README-eval-gate.md` 참고.

---

## 인프라 (별도 레포)

공개 베타의 EC2 컨테이너 구성, HTTPS, 비공개 의존성 연결과 롤백 절차는
[배포 가이드](deploy/README.md)와 `compose.beta.yml`을 참고한다. 실제 베타 배포 기록(EC2·RDS·ECR·Secrets Manager·릴리스 교체 절차)은
Terraform_Infra `docs/beta-runtime-deployment.md`에 있다. 릴리스는 커밋 SHA로 ARM64 이미지를 빌드해 ECR에 올리고,
EC2에서 설정 검증(stage) 뒤 앱만 교체(activate)하며 준비 확인 실패 시 이전 이미지로 자동 복원했다.

| 대상 | 레포 |
|------|------|
| AWS 인프라(EC2·RDS·보안 그룹·IAM) | [`Terraform_Infra`](https://github.com/4EVR0/Terraform_Infra) |
| GPU(vLLM) 서버 프로비저닝 (`setup_tailscale.sh` + vast.ai 템플릿) | [`GPU_Serving_Infra`](https://github.com/4EVR0/GPU_Serving_Infra) |
| 모니터링 스택 (Prometheus/Grafana/Loki/MLflow) | [`Monitoring_Infra`](https://github.com/4EVR0/Monitoring_Infra) |

GPU 인스턴스를 같은 호스트명(`vast-gpu-server-2`)으로 다시 등록하면 MagicDNS 주소가 유지되어 `.env` 수정이 불필요하다.

### 모니터링

| 서비스 | URL |
|--------|-----|
| Grafana | `http://monitoring-server-1.tailb70036.ts.net:3000` |
| Prometheus | `http://monitoring-server-1.tailb70036.ts.net:9090` |
| MLflow | `http://monitoring-server-1.tailb70036.ts.net:5000` |
| vLLM 메트릭 | `http://vast-gpu-server-2.tailb70036.ts.net:18000/metrics` |

(베타 운영 당시 주소. 모니터링 서버는 중지·정리 대상이며, 지표·로그는 로컬 백업에서 Prometheus를 띄워 볼 수 있다.)
베타 앱 EC2의 메트릭·로그는 Alloy가 모니터링 서버로 push했다(`Monitoring_Infra/agents/beta-app`).
GPU 하드웨어 지표(사용률·VRAM·온도)는 GPU 서버에 dcgm-exporter를 두지 않아 수집하지 않았다.

핵심 Grafana 쿼리:
```promql
vllm:num_requests_running                                                    # 처리 중 요청
vllm:num_requests_waiting                                                    # 대기 요청 (병목 감지)
rate(vllm:generation_tokens_total[1m])                                       # 초당 생성 토큰
histogram_quantile(0.99, rate(vllm:e2e_request_latency_seconds_bucket[5m]))  # P99 응답시간
vllm:kv_cache_usage_perc                                                     # KV 캐시 사용률(V1 엔진 이름)
rate(vllm:prefix_cache_hits_total[5m]) / rate(vllm:prefix_cache_queries_total[5m])  # 프리픽스 캐시 적중률
sum by (result) (rate(recommend_cache_total[1h]))                            # 앱 응답 캐시 hit/miss
```
