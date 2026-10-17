"""엔진 분리 지키기: 루프는 engines/base.py 모양만 알고, 엔진은 루프를 모른다."""

import re

import pytest

import engines
from engines.base import Decision, EngineUnavailable, Health
from engines.jeff.client import START_HINT, JeffClient
from loop.core.engine import status_markdown
from loop.core.paths import ROOT
from loop.tabs.inference import logic

LOOP_FILES = list((ROOT / "loop").rglob("*.py"))
ENGINE_FILES = list((ROOT / "engines").rglob("*.py"))


class OtherEngine:
    """Jeff 가 아닌 엔진. 같은 모양만 맞추면 루프가 그대로 돌아야 한다."""

    name = "다른모델"

    def __init__(self, state="ready"):
        self.state, self.calls = state, []

    def health(self) -> Health:
        return Health(self.state, "테스트용", "memory://")

    def models(self):
        return ["기본모델", "학습된모델"]

    def choose(self, state, options, instructions="", orders=1, model="") -> Decision:
        self.calls.append((state, options, instructions, orders))
        first = next(iter(options))
        probs = {k: (0.9 if k == first else 0.1 / (len(options) - 1)) for k in options}
        return Decision(first, probs, 0.8, 1.0, {"state": state}, {"ok": True})


ROWS = [["auto", "자동 처리", "clear"], ["ask", "사용자에게 질문", "unclear"]]


def test_loop_runs_with_a_non_jeff_engine():
    engine = OtherEngine()
    outcome = logic.run('{"x": 1}', ROWS, "decide", True, engine)
    assert outcome.row[2] == "auto"
    assert engine.calls == [({"x": 1}, {"auto": "clear", "ask": "unclear"}, "decide", 2)]


def test_engine_errors_are_shown_not_raised():
    class Broken(OtherEngine):
        def choose(self, *args, **kwargs):
            raise EngineUnavailable("꺼져 있어")
    outcome = logic.run('{"x": 1}', ROWS, "", False, Broken())
    assert outcome.status == "❌ 꺼져 있어" and outcome.row is None


@pytest.mark.parametrize("state, mark", [("ready", "🟢"), ("loading", "🟡"), ("off", "🔴")])
def test_status_bar_uses_engine_name_and_state(state, mark):
    text = status_markdown(OtherEngine(state))
    assert text.startswith(mark) and "다른모델" in text


def test_unknown_engine_gives_korean_hint():
    with pytest.raises(engines.EngineNotFound, match="있는 엔진: jeff"):
        engines.load("없는엔진", {})


def test_jeff_engine_reads_its_own_settings(monkeypatch):
    monkeypatch.delenv("JEFF_URL", raising=False)
    engine = engines.load("jeff", {"url": "http://example:1/", "model": "m", "timeout": 5})
    assert (engine.url, engine.model, engine.timeout, engine.name) == ("http://example:1", "m", 5.0, "Jeff")
    monkeypatch.setenv("JEFF_URL", "http://override:2")
    assert engines.load("jeff", {"url": "http://example:1"}).url == "http://override:2"


def test_jeff_health_off_when_server_down():
    h = JeffClient("http://127.0.0.1:9", timeout=2).health()  # 아무도 안 듣는 포트
    assert h.state == "off" and h.detail == START_HINT


@pytest.mark.parametrize("path", LOOP_FILES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_loop_does_not_name_an_engine(path):
    hits = [f"{i}: {line.strip()}" for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if re.search(r"jeff", line, re.IGNORECASE)]
    assert not hits, "루프는 엔진 이름을 모름 → engines/<이름>/ 이나 config 로:\n" + "\n".join(hits)


@pytest.mark.parametrize("path", ENGINE_FILES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_engines_do_not_import_loop(path):
    hits = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if re.match(r"\s*(from|import)\s+loop\b", line)]
    assert not hits, "엔진은 루프를 import 하지 않음 (설정은 dict 로 받음):\n" + "\n".join(hits)
