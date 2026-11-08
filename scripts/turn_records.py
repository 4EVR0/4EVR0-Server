"""수집된 질문·답변을 응답 방식으로 걸러 본다(개발자용, Tailscale 필요).

    python scripts/turn_records.py --modes                 # 최근 7일 응답 방식별 건수
    python scripts/turn_records.py --mode no_products      # 제품 없음으로 끝난 질문
    python scripts/turn_records.py --mode quality_fallback --mode grounding_fallback --days 3 --limit 20
    python scripts/turn_records.py --mode generated --full # 답변 전체 출력(기본은 앞부분만)

베타 앱의 내부 조회 API(Tailscale 전용)를 부른다. 주소는 INTERNAL_VIEWER_URL로 바꿀 수 있다.
사용자 질문 원문이 나오므로 출력 결과를 이슈·PR·채팅에 그대로 붙이지 않는다.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://evr0-beta-app.tailb70036.ts.net:18080"


def fetch(base: str, path: str, params: list[tuple[str, str]]) -> dict:
    url = f"{base.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        sys.exit(f"조회 실패 HTTP {exc.code}: Tailscale 연결과 베타 서버의 내부 조회 설정을 확인하세요.")
    except urllib.error.URLError as exc:
        sys.exit(f"접속 실패: {exc.reason} — Tailscale에 연결돼 있는지 확인하세요.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", action="append", help="응답 방식(여러 번 지정 가능). 예: no_products")
    parser.add_argument("--modes", action="store_true", help="응답 방식별 건수만 본다")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--full", action="store_true", help="답변 전체를 출력")
    parser.add_argument("--url", default=os.environ.get("INTERNAL_VIEWER_URL", DEFAULT_URL))
    args = parser.parse_args()

    if args.modes:
        data = fetch(args.url, "/internal/turn-records/modes", [("days", str(args.days))])
        print(f"최근 {data['days']}일 응답 방식별 건수")
        for mode, count in data["modes"].items():
            print(f"  {mode:<34} {count:>5}")
        return

    params = [("days", str(args.days)), ("limit", str(args.limit))] + [("mode", m) for m in args.mode or []]
    data = fetch(args.url, "/internal/turn-records", params)
    for record in data["records"]:
        answer = " ".join(record["answer"].split())
        print(f"[{record['created_at'][:16].replace('T', ' ')}] {record['response_mode']}  ({record['turn_id'][:8]})")
        print(f"  Q: {record['question']}")
        print(f"  A: {answer if args.full else answer[:200] + ('…' if len(answer) > 200 else '')}")
        if record["products"]:
            print(f"  제품: {', '.join(record['products'][:3])}{' 외' if len(record['products']) > 3 else ''}")
        print()
    print(f"{data['count']}건 (최근 {data['days']}일{', 응답 방식 ' + ', '.join(data['modes']) if data['modes'] else ''})")


if __name__ == "__main__":
    main()
