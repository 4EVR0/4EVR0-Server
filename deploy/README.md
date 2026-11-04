# 공개 베타 컨테이너 배포

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

## DuckDNS 주소 연결

2026-11-04 사용자 확인: 유료 독립 도메인 대신 DuckDNS의 무료 `이름.duckdns.org` 주소를 사용한다.
`4evr0.duckdns.org`를 DuckDNS 계정에 확보했다. 처음에는 사용자 승인에 따라 A 레코드를 `0.0.0.0`으로 두었고,
2026-11-04 공개 전환 승인 후 앱 EIP에 연결했다. 정상 HTTPS·세션·Origin·요청 상한·내부 경로 차단을 실제 주소에서 검증했다.
현재 운영 상태와 GPU 최종 확인 여부는 인프라 저장소의 `docs/beta-runtime-deployment.md`를 따른다.
주소를 새 서버로 옮길 때는 아래 절차로 연결한다.

1. DuckDNS 계정에서 확보된 `4evr0.duckdns.org`를 확인한다.
2. 앱 EC2에 EIP를 연결한 뒤 DuckDNS의 IPv4 레코드를 해당 EIP로 설정한다.
   IPv6를 운영하지 않으면 접속 불가능한 AAAA 레코드를 남기지 않는다.
3. DNS가 앱 IP를 가리키는지 확인한 뒤 `proxy.env`에 실제 주소를 적는다.
   예: `SITE_ADDRESS=확보한이름.duckdns.org` (`https://`와 포트는 제외).
4. 앱 EC2의 80/443과 Caddy 인증서 저장 볼륨을 준비하고, 실제 주소로 HTTPS·세션·SSE를 검증한다.

EIP를 유지하는 동안에는 IP 자동 감지용 주기 작업이 필요하지 않다. EIP를 교체하면 DuckDNS를 갱신한다.
단일 호스트의 공개 80/443 검증으로 인증서를 발급하므로, 이 배포 구성에는 DuckDNS 전용 Caddy 플러그인이나 DNS 토큰 주입이 필요하지 않다.
주소 갱신을 자동화하게 되면 DuckDNS 토큰은 별도 비밀로 관리하고 Git·명령 인자·로그에 남기지 않는다.
DuckDNS 주소는 무료지만 AWS 서버·공인 IPv4·GPU 비용은 별도로 발생한다.

근거: [DuckDNS 안내](https://www.duckdns.org/why.jsp), [갱신 명세](https://www.duckdns.org/spec.jsp), [Caddy 자동 HTTPS](https://caddyserver.com/docs/automatic-https).

## 환경 파일

`runtime.env.example`, `proxy.env.example`를 각각 `runtime.env`, `proxy.env`로 복사하고
실제 값으로 교체한다. 권한은 `chmod 600 deploy/runtime.env deploy/proxy.env`로 제한한다.
운영 비밀은 Git·이미지·PR·평가 산출물에 넣지 않는다. 파일은 **따옴표 없는** `KEY=value` 형식이다.
raw 형식이므로 `$` 문자를 Compose 변수로 확장하지 않는다.

### RDS와 실행 권한

운영 RDS는 `compose.rds.yml`을 추가하고 `POSTGRES_CA_FILE`에 AWS 공식 RDS CA 번들의
절대 경로를 지정한다. 번들은 앱의 `/run/4evr0/rds-ca.pem`에 읽기 전용으로 마운트한다.
`POSTGRES_DSN`에는 실제 RDS endpoint와 `sslmode=verify-full&sslrootcert=/run/4evr0/rds-ca.pem`을
설정한다. IP 주소로 대체하거나 인증서 검증을 끄지 않는다.

DB master는 RDS 관리형 Secrets Manager 비밀로 관리하며 앱에 주입하지 않는다.
별도의 migration 계정으로 `python -m app.core.migrate`를 한 번 실행한 뒤,
런타임 계정에는 테이블 SELECT/INSERT/UPDATE/DELETE와 시퀀스 USAGE/SELECT만 부여한다.
런타임은 `DB_AUTO_CREATE_SCHEMA=false`로 실행해 요청·기동 때 DDL을 실행하지 않는다.
마이그레이션 환경 파일은 런타임 환경 파일과 분리하며 Git·이미지·SSM 명령 본문에 넣지 않는다.

RDS 운영 명령은 아래 예시의 `-f compose.beta.yml` 뒤에 `-f compose.rds.yml`을 추가한다.
이미지 pull과 런타임 비밀 조회는 호스트 역할로만 수행하고 앱 컨테이너에는 AWS 키를 전달하지 않는다.

`SITE_ADDRESS`에는 `https://` 없이 실제 도메인을 지정한다. Caddy가 인증서를 자동 발급·갱신한다.

2026-11-04 결정에 따라 초대 인증과 로그인은 사용하지 않는다. 방문자는 HTTPS로 바로 UI와
사용자 API를 이용하며, 대화는 기존 HttpOnly 세션 쿠키로 분리한다. 운영 상태·문서·메트릭 경로는
공개 프록시에서 계속 404로 차단한다. 프록시 요청 본문 상한은 16KB다. 이는 요청 속도 제한이나
GPU 비용 상한을 대신하지 않으며, 공개 전 별도로 확정·검증한다.

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

외부 HTTPS에서는 상태·문서·메트릭 경로가 404다. 나머지 UI/API는 로그인 없이 접근한다.

## 관측 (Alloy → 모니터링 서버)

`alloy`가 내부망에서 앱 `/metrics`와 `node-exporter`를 수집하고, Docker 로그(app·caddy·redis)를
모니터링 서버로 push한다. 호스트 포트·공개 경로는 열지 않는다.

- EC2가 모니터링 서버와 **같은 tailnet**(`tailb70036`)에 가입돼 있어야 한다.
- 목적지는 Tailscale **IP**로 적는다. 컨테이너에서 MagicDNS(`*.ts.net`)가 풀린다고 가정하지 않는다.
- `observe.env.example`을 `observe.env`로 복사하고 `export OBSERVE_ENV_FILE="$PWD/deploy/observe.env"`를 추가한다.
- Alloy는 `docker.sock`(읽기 전용 마운트)으로 로그를 읽는다. 소켓 API 자체는 제한되지 않으므로
  Alloy 이미지·설정 변경은 리뷰를 거친다.

연결 확인(EC2 호스트):

```bash
curl -fsS http://100.105.250.65:9090/-/ready && curl -fsS http://100.105.250.65:3100/ready
docker compose --env-file /dev/null -p 4evr0-beta -f compose.beta.yml logs --tail 50 alloy
```

Grafana에서 `up{host="beta-app"}`(job `beta-app`·`beta-node`)과 Loki `{job="beta-app"}`이 보이면 정상이다.
`-p` 프로젝트 이름을 바꾸면 `COMPOSE_PROJECT_NAME`도 같은 값으로 지정한다(로그 수집 대상 필터).
SSE는 Caddy의 기본 event-stream 전달을 이용한다. 운영 GPU를 연결한 후 실제 생성 취소·지연도 별도로 검증한다.

## 데이터, 중단 및 롤백

- Redis는 비공개 내부 네트워크, AOF 볼륨 및 논리 메모리 한도 256MB/noeviction을 사용한다.
  한도 초과 시 쓰기는 실패한다. 프로세스 총 RSS 한도가 아니며 운영 크기/경보는 부하 측정 후 정한다.
- PostgreSQL/Neo4j는 외부 운영 DB다. 이 파일은 생성·마이그레이션·백업하지 않는다.
- Caddy `/data` 볼륨은 인증서·키 저장소다. Redis와 함께 접근/백업 정책을 정해야 한다.
- 중단은 동일 명령의 `stop`, 재개는 `up -d`. **운영에서 `down --volumes`를 쓰지 않는다.**
- 앱 롤백은 이전 검증 이미지로 `APP_IMAGE`를 바꾸고 `up -d --no-deps app`을 실행한 뒤 `/ready`를 확인한다.
  앱 교체 동안 짧은 중단이 생길 수 있다. DB 호환성은 별도로 확인한다.
- 로그는 컨테이너별 10MB × 3개로 회전한다. #140·#141의 확정 정책에 따라 대화 저장과 학습 이용은 선택 사항이며, 동의한 원문은 30일 보관한다. 운영 로그·백업의 보존 정책은 별도다.

## 외부 서비스 없는 검증

```bash
python3 scripts/smoke_beta.py YOUR_LOCAL_IMAGE
```

고유 프로젝트에 테스트 PostgreSQL/Redis/Caddy를 띄우며 호스트 포트는 열지 않는다.
테스트 전용 CA만 테스트 클라이언트가 신뢰하며 시스템 인증서 저장소를 변경하지 않는다.
로그인 없는 HTTPS, UI/CSS 포함 여부, 요청 본문 상한, 내부 경로 차단, 세션 쿠키, Origin 검증, SSE 전달, GPU/그래프 미연결 시 readiness를 검사하고
자신이 만든 컨테이너/볼륨만 정리한다. 원격 이미지 다운로드 외에는 외부 DB/GPU/API에 연결하지 않는다.
