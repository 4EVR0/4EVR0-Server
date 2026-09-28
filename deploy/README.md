# 초대형 베타 컨테이너 배포

이 구성은 EC2 한 대의 앱·Caddy·Redis와 외부의 비공개 PostgreSQL·Neo4j·GPU를 연결한다.
개발용 `docker-compose.yml`과 별개이며 클라우드 리소스나 DNS를 생성하지 않는다.
실제 배포에는 도메인, DB 접근 경로, 백업, 운영 시간과 비용 확정이 먼저 필요하다.

## 사전 조건

- Docker Engine + Compose **2.30 이상** (`env_file.format: raw` 사용).
- EC2 CPU에 맞는 이미지(AMD64/ARM64). 검증한 커밋 SHA 태그 또는 레지스트리 digest로 고정한다.
- 도메인의 DNS가 EC2의 고정 공개 IP를 가리켜야 한다. 인바운드는 80/443만 허용한다.
  SSH는 별도 관리 경로로 제한한다. 8000/6379/5432/7687/18000은 공개하지 않는다.
- EC2뿐 아니라 **앱 컨테이너 내부에서도** PostgreSQL·Neo4j·Tailscale GPU 주소의 DNS/라우팅을 확인한다.
  호스트의 Tailscale 설치만으로 컨테이너의 MagicDNS 동작을 보장하지 않는다.
- `172.30.90.0/28`이 VPC/VPN/Docker 대역과 충돌하지 않아야 한다. 변경할 때
  `PROXY_SUBNET`과 해당 대역 안의 서로 다른 `CADDY_IP`·`APP_IP`를 함께 지정한다.

## 환경 파일

`runtime.env.example`, `proxy.env.example`를 각각 `runtime.env`, `proxy.env`로 복사하고
실제 값으로 교체한다. 권한은 `chmod 600 deploy/runtime.env deploy/proxy.env`로 제한한다.
운영 비밀은 Git·이미지·PR·평가 산출물에 넣지 않는다. 파일은 **따옴표 없는** `KEY=value` 형식이다.
raw 형식이므로 `$` 문자를 Compose 변수로 확장하지 않는다.

`SITE_ADDRESS`에는 `https://` 없이 실제 도메인을 지정한다. Caddy가 인증서를 자동 발급·갱신한다.
`BETA_USER`는 공백 없는 사용자명, `BETA_PASSWORD_HASH`는 bcrypt hash다. 아래 명령은 대화형으로
비밀번호를 받아 hash를 출력한다. 비밀번호를 CLI 인자나 셸 기록에 남기지 않는다.

```bash
docker run --rm -it caddy:2.11.4-alpine@sha256:6aeddd44c3078b0f9a35206472a11420648a79c184603ef95957d0a20044cb2b caddy hash-password
```

현재 Basic 인증은 소규모 초대용 공통 접근 장벽이다. 브라우저 세션 쿠키와 별개이며
사용자별 계정/동의/회수 기능을 대신하지 않는다. 공유 비밀번호를 바꾸면 전체 초대자에게 영향을 준다.
비밀번호/hash를 변경한 뒤에는 Caddy 컨테이너를 재생성한다.

## 실행과 점검

레포 루트에서 환경 변수에 **실제 검증한 이미지**와 절대 경로를 지정한다.

```bash
export APP_IMAGE='registry.example/4evr0-server:REPLACE_WITH_SHA'
export RUNTIME_ENV_FILE="$PWD/deploy/runtime.env"
export PROXY_ENV_FILE="$PWD/deploy/proxy.env"
docker compose --env-file /dev/null -p 4evr0-beta -f compose.beta.yml config --quiet
docker compose --env-file /dev/null -p 4evr0-beta -f compose.beta.yml up -d
docker compose --env-file /dev/null -p 4evr0-beta -f compose.beta.yml ps
```

`config`는 반드시 `--quiet`로 실행한다. 일반 출력에는 비밀 값이 포함될 수 있다.
Uvicorn은 단일 worker이며 전달 헤더는 지정한 Caddy IP만 신뢰한다.
GPU가 꺼져 있어도 `/live`와 웹 화면은 유지된다. **추천 준비 완료와는 다르다.**

| 내부 경로 | 의미 |
|---|---|
| `/live` | 앱 프로세스 응답 확인, 외부 연결 검사 없음. Compose healthcheck에 사용 |
| `/ready` | PostgreSQL·Redis·Neo4j와 설정한 GPU 모델 확인. 모두 정상일 때만 200, 아니면 503 |
| `/health` | 기존 호환 경로. GPU 실패 503, 기타 의존성 실패는 degraded 200 |
| `/metrics` | 운영 관측용. 현재 공개 프록시에서 차단 |

준비 검사는 실제 추론 품질/성능 검증이 아니라 연결 및 모델 목록 검사다.
프록시가 `/ready` 실패 시 UI까지 차단하지 않으며, 운영 시간 외 추천 차단/안내는 후속 작업이다.
내부 상태 확인 예시:

```bash
docker compose --env-file /dev/null -p 4evr0-beta -f compose.beta.yml exec -T app python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=10).read().decode())"
```

외부 HTTPS에서는 상태·문서·메트릭 경로가 404다. 나머지 UI/API는 초대 인증이 필요하다.
Prometheus/Alloy 연결은 비공개 경로를 마련하는 후속 작업이며 메트릭을 공개하지 않는다.
SSE는 Caddy의 기본 event-stream 전달을 이용한다. 운영 GPU를 연결한 후 실제 생성 취소·지연도 별도로 검증한다.

## 데이터, 중단 및 롤백

- Redis는 비공개 내부 네트워크, AOF 볼륨 및 논리 메모리 한도 256MB/noeviction을 사용한다.
  한도 초과 시 쓰기는 실패한다. 프로세스 총 RSS 한도가 아니며 운영 크기/경보는 부하 측정 후 정한다.
- PostgreSQL/Neo4j는 외부 운영 DB다. 이 파일은 생성·마이그레이션·백업하지 않는다.
- Caddy `/data` 볼륨은 인증서·키 저장소다. Redis와 함께 접근/백업 정책을 정해야 한다.
- 중단은 동일 명령의 `stop`, 재개는 `up -d`. **운영에서 `down --volumes`를 쓰지 않는다.**
- 앱 롤백은 이전 검증 이미지로 `APP_IMAGE`를 바꾸고 `up -d --no-deps app`을 실행한 뒤 `/ready`를 확인한다.
  앱 교체 동안 짧은 중단이 생길 수 있다. DB 호환성은 별도로 확인한다.
- 로그는 컨테이너별 10MB × 3개로 회전한다. 요청·응답 수집 동의와 보관 기한은 실제 사용자 초대 전 확정한다.

## 외부 서비스 없는 검증

```bash
python3 scripts/smoke_beta.py YOUR_LOCAL_IMAGE
```

고유 프로젝트에 테스트 PostgreSQL/Redis/Caddy를 띄우며 호스트 포트는 열지 않는다.
테스트 전용 CA만 테스트 클라이언트가 신뢰하며 시스템 인증서 저장소를 변경하지 않는다.
HTTPS, 인증, 세션 쿠키, Origin 검증, SSE 전달, GPU/그래프 미연결 시 readiness를 검사하고
자신이 만든 컨테이너/볼륨만 정리한다. 원격 이미지 다운로드 외에는 외부 DB/GPU/API에 연결하지 않는다.
