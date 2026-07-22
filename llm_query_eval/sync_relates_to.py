"""CONCERN_EFFECT_MAP(프로덕션 하드코딩 딕셔너리)을 그래프의
(Effect)-[:RELATES_TO]->(Concern) 관계로 그대로 반영한다.

배경: 그래프의 Concern 노드는 15개뿐이고(앱이 쓰는 Concern enum은 26개),
RELATES_TO로 Effect와 연결된 건 7개뿐이었다 — 나머지는 그래프만 봐서는
concern->effect를 찾을 수 없는 상태. 그래서 프로덕션이 이 그래프 관계 대신
CONCERN_EFFECT_MAP을 하드코딩해서 우회하고 있었음.

이 스크립트는 그 우회로를 그래프 자체에 채워 넣어서, LLM(B)이 Concern 노드부터
RELATES_TO를 타고 탐색하는 것도 "데이터가 없어서 못 하는" 게 아니라 "선택"이
되게 만든다. CONCERN_EFFECT_MAP을 유일한 정답으로 보고 **완전히 동기화**한다
(빠진 건 추가, CONCERN_EFFECT_MAP에 없는 기존 RELATES_TO는 삭제 — 예:
POST_ACNE_MARKS가 그래프엔 ANTI_INFLAMMATORY로 연결돼 있었는데 실제 매핑은
아니었음, eval/RESULTS.md §1에 이미 기록된 불일치).

MERGE 기반이라 재실행해도 안전(idempotent). 실행 전/후 카운트를 출력한다.

사용:
    python sync_relates_to.py            # 실제 적용
    python sync_relates_to.py --dry-run  # 뭐가 바뀔지만 출력, 그래프는 안 건드림
"""

import argparse
import sys
from pathlib import Path

from neo4j import GraphDatabase

_APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_APP_ROOT))
from app.core.config import settings  # noqa: E402
from app.services.taxonomy_normalization_service import CONCERN_EFFECT_MAP  # noqa: E402

# taxonomy_normalization_service.py의 그룹 주석에서 가져온 한글 이름
# (csv/nodes/concern.csv에 없던 11개 — 원래 그래프에 노드 자체가 없던 concern).
_KOREAN_NAMES: dict[str, str] = {
    "PORE_CONGESTION": "모공 막힘",
    "ENLARGED_PORES": "모공 확대",
    "FLAKY_SKIN": "각질",
    "ROUGH_TEXTURE": "피부결 거침",
    "UNEVEN_SKIN_TONE": "피부 톤 불균일",
    "BLEMISHES": "잡티",
    "DARK_CIRCLES": "다크서클",
    "SUNBURN": "자외선 손상",
    "WRINKLES": "주름",
    "LOSS_OF_ELASTICITY": "탄력 저하",
    "SAGGING_SKIN": "피부 처짐",
}


def get_driver():
    return GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))


def current_state(driver) -> dict[str, set[str]]:
    with driver.session() as s:
        rows = s.run(
            "MATCH (e:Effect)-[:RELATES_TO]->(c:Concern) RETURN c.concern_code AS concern, e.effect_code AS effect"
        ).data()
    state: dict[str, set[str]] = {}
    for r in rows:
        state.setdefault(r["concern"], set()).add(r["effect"])
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    desired: dict[str, set[str]] = {
        concern.value: {effect.value for effect in effects} for concern, effects in CONCERN_EFFECT_MAP.items()
    }

    driver = get_driver()
    try:
        before = current_state(driver)
        print(f"적용 전: RELATES_TO 있는 concern {len(before)}개, 전체 CONCERN_EFFECT_MAP concern {len(desired)}개")

        to_add: list[tuple[str, str]] = []
        to_remove: list[tuple[str, str]] = []
        for concern, effects in desired.items():
            existing = before.get(concern, set())
            for effect in effects - existing:
                to_add.append((concern, effect))
            for effect in existing - effects:
                to_remove.append((concern, effect))

        print(f"추가할 RELATES_TO: {len(to_add)}건, 삭제할 RELATES_TO: {len(to_remove)}건")
        if args.dry_run:
            for c, e in to_add:
                print(f"  [+] {c} -> {e}")
            for c, e in to_remove:
                print(f"  [-] {c} -> {e}")
            return

        with driver.session() as s:
            for concern, effects in desired.items():
                s.run(
                    "MERGE (c:Concern {concern_code: $concern}) "
                    "ON CREATE SET c.concern_name_ko = $name_ko",
                    concern=concern, name_ko=_KOREAN_NAMES.get(concern, concern),
                )
                for effect in effects:
                    s.run(
                        "MATCH (e:Effect {effect_code: $effect}), (c:Concern {concern_code: $concern}) "
                        "MERGE (e)-[:RELATES_TO]->(c)",
                        effect=effect, concern=concern,
                    )
            for concern, effect in to_remove:
                s.run(
                    "MATCH (e:Effect {effect_code: $effect})-[r:RELATES_TO]->(c:Concern {concern_code: $concern}) "
                    "DELETE r",
                    effect=effect, concern=concern,
                )

        after = current_state(driver)
        print(f"적용 후: RELATES_TO 있는 concern {len(after)}개")
        missing = set(desired) - set(after)
        print("여전히 RELATES_TO 없는 concern:", missing if missing else "없음")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
