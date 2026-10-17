"""실행기(webui.bat → loop/launch.py): 가짜 엔진·가짜 시계로 '켜고 기다리기'를 확인. 진짜 WSL 은 안 부른다."""

import subprocess

import pytest

import engines
from engines.base import EngineUnavailable, Health, can_serve
from engines.jeff import server as jeff_server
from loop.core.paths import ROOT
from loop.launch import ensure_engine


class FakeEngine:
    """health 가 차례대로 states 를 돌려준다 (끝나면 마지막 값 유지)."""

    name = "가짜"

    def __init__(self, states, start_error=None, exit_reason=""):
        self.states, self.start_error, self.exit_reason = list(states), start_error, exit_reason
        self.started = []

    def health(self):
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return Health(state, "켜는 법: 테스트")

    def start_server(self, log_path):
        if self.start_error:
            raise EngineUnavailable(self.start_error)
        self.started.append(log_path)

    def server_exited(self):
        return self.exit_reason

    def stop_server(self):
        pass


class NoServe:
    name = "직접켜기"

    def health(self):
        return Health("off", "켜는 법: 직접")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run(engine, wait=10):
    said, clock = [], Clock()
    started = ensure_engine(engine, ROOT / "logs" / "x.log", said.append, wait, clock.sleep, clock)
    return started, said


def test_already_running_is_left_alone():
    engine = FakeEngine(["ready"])
    started, said = run(engine)
    assert started is False and engine.started == [] and "이미 켜져" in said[0]


def test_off_is_started_and_waited_for():
    engine = FakeEngine(["off", "off", "loading", "ready"])
    started, said = run(engine)
    assert started is True and len(engine.started) == 1 and "준비됨" in said[-1]


def test_loading_started_elsewhere_is_waited_but_not_owned():
    engine = FakeEngine(["loading", "ready"])
    started, _ = run(engine)
    assert started is False and engine.started == []


def test_engine_that_cannot_serve_shows_hint():
    started, said = run(NoServe())
    assert started is False and "켜는 법: 직접" in said[0]


def test_start_error_still_opens_webui():
    started, said = run(FakeEngine(["off"], start_error="WSL 없음"))
    assert started is False and "WSL 없음" in said[-1]


def test_server_dying_early_stops_waiting():
    started, said = run(FakeEngine(["off"], exit_reason="모델 폴더 없음"))
    assert started is False and "모델 폴더 없음" in said[-1]


def test_timeout_keeps_ownership():
    started, said = run(FakeEngine(["off"]), wait=6)
    assert started is True and "6초" in said[-1]


# ---- Jeff 쪽 ----

def test_jeff_engine_can_serve():
    assert can_serve(engines.load("jeff", {}))


def test_jeff_server_runs_serve_sh_in_wsl(monkeypatch, tmp_path):
    seen = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            seen["command"], seen["kwargs"] = command, kwargs
            self.returncode = 1

        def poll(self):
            return 1

    monkeypatch.setattr(jeff_server.subprocess, "Popen", FakePopen)
    server = jeff_server.WslServer({"distro": "Ubuntu-X", "user": "me"})
    log = tmp_path / "serve.log"
    server.start(log)
    assert seen["command"][:5] == ["wsl.exe", "-d", "Ubuntu-X", "-u", "me"]
    assert seen["command"][-1].endswith("/engines/jeff/wsl/serve.sh")
    assert seen["kwargs"]["stderr"] is subprocess.STDOUT
    log.write_text("시작\nFileNotFoundError: 모델 없음\n", encoding="utf-8")
    assert "모델 없음" in server.exited()


def test_jeff_server_start_failure_is_korean(monkeypatch, tmp_path):
    def broken(*args, **kwargs):
        raise OSError("wsl.exe 없음")
    monkeypatch.setattr(jeff_server.subprocess, "Popen", broken)
    with pytest.raises(EngineUnavailable, match="wsl -l -v"):
        jeff_server.WslServer().start(tmp_path / "serve.log")


def test_serve_script_leaves_pid_for_stopping():
    text = jeff_server.SCRIPT.read_text(encoding="utf-8")
    assert "serve.pid" in text and text.index("serve.pid") < text.index("exec ")


def test_bat_has_windows_line_endings():
    data = (ROOT / "webui.bat").read_bytes()
    assert data.count(b"\r\n") == data.count(b"\n") and b"loop\\launch.py" in data


def test_wsl_kill_commands_avoid_nested_double_quotes(monkeypatch, tmp_path):
    """wsl.exe 를 거치면 `$(cat "...")` 안의 큰따옴표가 깨져 kill 이 실패한다 (10/17 실제로 겪음)."""
    from engines.jeff import trainer as jeff_trainer
    commands = []

    class Done:
        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(jeff_server.subprocess, "run", lambda command, **kwargs: commands.append(command))
    server = jeff_server.WslServer()
    server.process = Done()
    server.stop()
    trainer = jeff_trainer.WslTrainer(runs_dir=tmp_path)
    (tmp_path / "run-1").mkdir()
    (tmp_path / "run-1" / "state").write_text("running\n")
    trainer.stop_training()
    assert len(commands) == 2
    for command in commands:
        assert '"$(' not in command[-1] and "$(cat" in command[-1]
