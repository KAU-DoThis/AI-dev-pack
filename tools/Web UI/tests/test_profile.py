"""프로필 분리 지키기: 루프 코드는 프로젝트를 모르고, 프로젝트 내용은 profiles/<이름>/ 에만 있다."""

import re

import pytest
import yaml

import loop.core.config as config
from loop.core.paths import PROFILES_DIR, ROOT

# 루프와 엔진 코드 둘 다 프로젝트를 몰라야 한다 (엔진은 Jeff 를 알아도 됨)
LOOP_CODE = [*(ROOT / "loop").rglob("*.py"), *(ROOT / "engines").rglob("*.py")]


def project_words() -> list[str]:
    """프로젝트에만 있는 말 = 각 프로필의 이름·대상·입력 칸 이름 (+ profile.yaml 의 loop_forbidden).
    루프 코드에 나오면 프로필로 옮겨야 한다. 예시 프로필은 일반 단어라 뺀다."""
    words = set()
    for folder in PROFILES_DIR.iterdir():
        path = folder / "profile.yaml"
        if folder.name == "example" or not path.exists():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        words |= {data.get("name"), data.get("item"), *data.get("display_keys", []), *data.get("loop_forbidden", [])}
    return sorted(word for word in words if word)


PROJECT_WORDS = re.compile("|".join(map(re.escape, project_words())) or r"(?!x)x", re.IGNORECASE)


@pytest.mark.parametrize("path", LOOP_CODE, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_loop_code_has_no_project_words(path):
    hits = [f"{i}: {line.strip()}" for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if PROJECT_WORDS.search(line)]
    assert not hits, "프로젝트 말은 profiles/<이름>/ 로:\n" + "\n".join(hits)


def test_every_profile_has_the_needed_files():
    profiles = [p for p in PROFILES_DIR.iterdir() if p.is_dir()]
    assert profiles
    for profile in profiles:
        for name in ("profile.yaml", "labels.yaml", "prompt.en.yaml", "examples.yaml"):
            assert (profile / name).exists(), f"{profile.name}/{name} 없음"


def test_profile_values_fill_defaults():
    profile = config.load_profile()
    assert set(config.PROFILE_DEFAULTS) <= set(profile)
    assert isinstance(profile["display_keys"], list)


def test_unknown_profile_gives_korean_hint(monkeypatch):
    monkeypatch.setenv("WEBUI_PROFILE", "없는프로필")
    with pytest.raises(config.ProfileError, match="있는 프로필: .*example"):
        config.profile_dir()


def test_run_all_without_examples_does_not_crash(monkeypatch):
    from loop.tabs.inference import ui
    monkeypatch.setattr(ui, "load_examples", lambda: [])
    label, status, _, history, _ = ui._judge_all([], "", False, [], "en", "")
    assert label is None and "예시가 없어" in status and history == []


def test_prompt_files_live_in_profile():
    for path in config.PROMPT_FILES.values():
        assert path.parent == config.profile_dir()
