"""응답에서 식약처 사용제한 원료 고시 원문(배합한도·사용금지 조건)을 지운다.

그래프의 kr_limit_note는 고시 문구를 그대로 담고 있어 소비자 답변에 그대로 나오면 읽기 어렵고
제품 선택에도 도움이 되지 않는다. 생성 입력에서 이미 빼지만, 모델이 기억으로 비슷한 문구를 쓰는
경우를 막기 위해 출력 단계에서도 해당 문장을 지운다. 성분 규제 상태(kr_reg_status)는 API 필드로만 남는다.
"""

from __future__ import annotations

import re

_REGULATORY_PATTERN = re.compile(
    r"배합\s*한도"
    r"|사용\s*금지"
    r"|<\s*(?:보존제|기타\s*배합\s*한도)\s*>"
    r"|(?:로서|으로서)\s*\d+(?:\.\d+)?\s*%"
    r"|만\s*\d+\s*세\s*이하"
    r"|유효\s*성분으로\s*사용하는\s*경우"
)
# 문장 경계: 마침표류 뒤 공백, 또는 고시 문구의 가운뎃점 글머리(∙·•).
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。])\s+|\s*[∙•]\s*")
_BULLET_ONLY = re.compile(r"^\s*(?:[-*•∙]|\d+[.)])?\s*$")


def _drop_regulatory_parentheses(line: str) -> str:
    """괄호(중첩 포함) 안에 고시 문구가 있으면 그 괄호 묶음만 지운다. 바깥 문장은 남긴다."""
    spans: list[tuple[int, int]] = []
    stack: list[int] = []
    for i, ch in enumerate(line):
        if ch == "(":
            stack.append(i)
        elif ch == ")" and stack:
            start = stack.pop()
            if not stack and _REGULATORY_PATTERN.search(line[start:i + 1]):
                spans.append((start, i + 1))
    for start, end in reversed(spans):
        line = line[:start].rstrip() + line[end:]
    return line


def has_regulatory_text(text: str) -> bool:
    return bool(_REGULATORY_PATTERN.search(text or ""))


def strip_regulatory_text(text: str) -> tuple[str, bool]:
    """고시 원문 문장을 지운다. (정리된 문장, 지웠는지). 해당 문구가 없으면 원문 그대로."""
    if not has_regulatory_text(text):
        return text, False
    out: list[str] = []
    for line in text.split("\n"):
        if not _REGULATORY_PATTERN.search(line):
            out.append(line)
            continue
        line = _drop_regulatory_parentheses(line)
        if not _REGULATORY_PATTERN.search(line):
            out.append(line)
            continue
        bullet = re.match(r"^\s*(?:[-*]|\d+[.)])\s+", line)
        prefix = bullet.group(0) if bullet else ""
        body = line[len(prefix):]
        kept = [s for s in _SENTENCE_SPLIT.split(body) if s and s.strip() and not _REGULATORY_PATTERN.search(s)]
        rebuilt = " ".join(s.strip() for s in kept).strip()
        if rebuilt and not _BULLET_ONLY.match(rebuilt):
            out.append(prefix + rebuilt)
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n")
    return cleaned, True
