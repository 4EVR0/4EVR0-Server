"""프롬프트 버전 관리.

프롬프트를 코드에 하드코딩하지 않고 `app/prompts/*.txt` 로 분리한다.
- `load_prompt(name)` : 텍스트 로드 (캐시)
- `prompt_version(name)` : 내용 해시(sha1[:8]) — 프롬프트가 바뀌면 버전도 바뀜.
  실험 추적(MLflow)에서 "어떤 프롬프트로 측정했나"를 이 버전으로 식별한다.

A/B 비교 시: `profile_extraction.txt` 와 `profile_extraction.v2.txt` 처럼 파일을 두고
`load_prompt("profile_extraction.v2")` 로 불러 비교한다.
"""

import hashlib
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """`app/prompts/<name>.txt` 내용을 반환(끝 공백 제거)."""
    path = _DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"프롬프트 파일 없음: {path}")
    return path.read_text(encoding="utf-8").strip()


@lru_cache(maxsize=None)
def prompt_version(name: str) -> str:
    """프롬프트 내용 해시(sha1 앞 8자). 내용이 바뀌면 값이 바뀐다."""
    return hashlib.sha1(load_prompt(name).encode("utf-8")).hexdigest()[:8]
