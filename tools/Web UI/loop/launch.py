"""실행기: webui.bat 이 부른다. 엔진 서버가 꺼져 있으면 켜고, 준비되면 웹 UI 를 띄운다.

- 이미 켜져 있으면 그대로 쓴다 (끌 때도 안 끔).
- 여기서 켠 서버는 웹 UI 를 끌 때(Ctrl+C 나 창 닫기) 같이 끈다. GPU 메모리를 계속 잡고 있지 않게.
- 서버를 못 켜도 웹 UI 는 켠다. 상태줄이 🔴 와 켜는 법을 보여 준다.
실행 (웹 UI 폴더 맨 위에서): .venv/Scripts/python loop/launch.py
"""

import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engines.base import Engine, EngineUnavailable, can_serve  # noqa: E402
from loop.core.config import engine_name  # noqa: E402
from loop.core.engine import get_engine  # noqa: E402
from loop.core.paths import LOGS_DIR  # noqa: E402
from loop.main import run  # noqa: E402

WAIT = 180  # 초. 처음 켤 땐 WSL 깨우기 + 모델 불러오기로 1분쯤 걸릴 수 있음
POLL = 2


def ensure_engine(engine: Engine, log_path: Path, say: Callable[[str], None] = print, wait: float = WAIT,
                  sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> bool:
    """엔진 서버가 준비될 때까지. 이 함수가 서버를 켰으면 True (끌 때 같이 끄라는 뜻)."""
    health = engine.health()
    if health.state == "ready":
        say(f"🟢 {engine.name} 이미 켜져 있음 · {health.detail}")
        return False
    started = False
    if health.state == "off":
        if not can_serve(engine):
            say(f"🔴 {engine.name} 꺼짐. 직접 켜야 해: {health.detail}")
            return False
        say(f"⏳ {engine.name} 서버 켜는 중… (로그: {log_path})")
        try:
            engine.start_server(log_path)
        except EngineUnavailable as error:
            say(f"❌ {error}\n   웹 UI 는 그대로 켤게 (상태줄 🔴).")
            return False
        started = True
    deadline = clock() + wait
    while clock() < deadline:
        sleep(POLL)
        health = engine.health()
        if health.state == "ready":
            say(f"🟢 {engine.name} 준비됨 · {health.detail}")
            return started
        reason = engine.server_exited() if started else ""
        if reason:
            say(f"❌ {engine.name} 서버가 켜지다가 꺼졌어: {reason}\n   로그: {log_path}")
            return False
    say(f"⚠️ {wait:.0f}초 안에 준비가 안 됐어. 웹 UI 는 켜고, 상태줄로 계속 볼게. 로그: {log_path}")
    return started


def stop_on_window_close(stop: Callable[[], None]) -> None:
    """콘솔 창 X 로 닫아도 서버를 끄게 (윈도우만). Ctrl+C 는 run() 이 끝나면서 처리된다."""
    if sys.platform != "win32":
        return
    import ctypes

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
    def handler(event: int) -> bool:
        if event == 2:  # CTRL_CLOSE_EVENT: 창 닫기. 몇 초 안에 끝내야 윈도우가 기다려 준다
            stop()
        return False

    stop_on_window_close.handler = handler  # 가비지 컬렉션으로 사라지지 않게 붙잡아 둔다
    ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)


def main() -> None:
    engine = get_engine()
    started = ensure_engine(engine, LOGS_DIR / f"{engine_name()}-serve.log")
    stopped = False

    def stop() -> None:
        nonlocal stopped
        if started and not stopped:
            stopped = True
            print(f"⏹️ {engine.name} 서버 끄는 중…")
            engine.stop_server()

    stop_on_window_close(stop)
    try:
        run()
    finally:
        stop()


if __name__ == "__main__":
    main()
