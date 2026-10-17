"""설정 읽기. 공용 설정은 config/settings.yaml, 프로젝트별 내용은 profiles/<프로필>/ 에서.

루프 코드(loop/)는 여기 함수로만 프로젝트 내용을 읽는다 → 프로필 폴더만 바꾸면 다른 프로젝트.
"""

import json
import os
from functools import lru_cache
from pathlib import Path

import yaml

from loop.core.paths import CONFIG_DIR, PROFILES_DIR

# 설명 세트는 언어별 파일 하나씩. 추론 탭의 영어/한국어 스위치로 바꿔 가며 비교한다.
LANGUAGES = {"en": "영어", "ko": "한국어"}
PROMPT_HEADER = (
    "# 엔진에 보내는 지시문과 선택지 설명 ({name}). 추론 탭의 \"설명 저장\" 버튼이 이 파일을 덮어쓴다.\n"
    "# 선택지 id 목록은 labels.yaml 이 기준.\n"
)
# profile.yaml 에 없을 때 쓰는 값
PROFILE_DEFAULTS = {"name": "", "title": "판단", "item": "입력", "display_keys": [], "examples_source": ""}


class ProfileError(Exception):
    """프로필 폴더가 없거나 필요한 파일이 빠졌을 때. 메시지는 한국어 안내."""


def _read_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache
def settings() -> dict:
    """config/settings.yaml + (있으면) config/settings.local.yaml. local 은 이 컴퓨터 전용(커밋 안 함)이고 맨 위 키를 덮어쓴다."""
    data = _read_yaml(CONFIG_DIR / "settings.yaml")
    local = CONFIG_DIR / "settings.local.yaml"
    return {**data, **_read_yaml(local)} if local.exists() else data


def profile_dir() -> Path:
    """쓸 프로필 폴더. 환경변수 WEBUI_PROFILE 이 settings.yaml 의 profile 보다 우선."""
    name = os.environ.get("WEBUI_PROFILE") or settings().get("profile") or ""
    path = PROFILES_DIR / name
    if not name or not (path / "labels.yaml").exists():
        found = sorted(p.name for p in PROFILES_DIR.iterdir() if (p / "labels.yaml").exists()) \
            if PROFILES_DIR.exists() else []
        raise ProfileError(f"프로필 '{name}' 을(를) 못 찾았어 ({path}). 있는 프로필: {', '.join(found) or '없음'}. "
                           "config/settings.yaml 의 profile 또는 환경변수 WEBUI_PROFILE 을 확인해 줘.")
    return path


def outputs_dir() -> Path:
    """프로필의 기록 폴더 (학습·평가 기록). 커밋 안 함. 테스트는 이 함수를 임시 폴더로 바꿔 끼운다."""
    return profile_dir() / "outputs"


def append_record(name: str, record: dict) -> None:
    path = outputs_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_records(name: str) -> list[dict]:
    path = outputs_dir() / name
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 쓰다 끊긴 줄은 건너뜀
    return records


@lru_cache
def load_profile() -> dict:
    """화면에 쓸 프로젝트 이름·말 (profile.yaml). 없는 값은 기본값."""
    path = profile_dir() / "profile.yaml"
    data = _read_yaml(path) if path.exists() else {}
    return {**PROFILE_DEFAULTS, **data}


# 언어별 설명 파일. 테스트가 경로를 바꿔 끼울 수 있게 dict 로 둔다.
PROMPT_FILES = {lang: profile_dir() / f"prompt.{lang}.yaml" for lang in LANGUAGES}


@lru_cache
def load_labels() -> dict:
    """프로필의 labels.yaml. 라벨이 바뀌면 코드 말고 이 파일만 고친다."""
    return _read_yaml(profile_dir() / "labels.yaml")


def action_labels() -> list[str]:
    return [item["id"] for item in load_labels()["actions"]]


def action_names() -> dict[str, str]:
    """id → 한국어 이름 (화면 표시용)."""
    return {item["id"]: item["name"] for item in load_labels()["actions"]}


def app_title() -> str:
    return settings().get("title") or "WebUI"


def engine_name() -> str:
    """쓸 엔진 이름. 환경변수 WEBUI_ENGINE 이 settings.yaml 의 engine 보다 우선."""
    return os.environ.get("WEBUI_ENGINE") or settings().get("engine") or ""


def engine_settings(name: str) -> dict:
    """settings.yaml 의 engines.<이름> (엔진이 자기 설정만 받도록)."""
    return (settings().get("engines") or {}).get(name) or {}


def load_prompt(lang: str = "en") -> dict:
    """지시문 + 선택지 설명. 화면에서 고치고 저장하므로 캐시하지 않는다.
    labels.yaml 에 있는데 설명이 없는 라벨은 빈 설명으로 채운다. 파일이 없으면 전부 빈 칸."""
    path = PROMPT_FILES[lang]
    data = (yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None) or {}
    options = data.get("options") or {}
    return {
        "instructions": data.get("instructions") or "",
        "options": {label: options.get(label) or "" for label in action_labels()},
    }


def save_prompt(instructions: str, options: dict[str, str], lang: str = "en") -> None:
    body = yaml.safe_dump({"instructions": instructions, "options": options},
                          allow_unicode=True, sort_keys=False, width=120)
    PROMPT_FILES[lang].write_text(PROMPT_HEADER.format(name=LANGUAGES[lang]) + body, encoding="utf-8")


def load_examples() -> list[dict]:
    path = profile_dir() / "examples.yaml"
    return (_read_yaml(path).get("examples") or []) if path.exists() else []
