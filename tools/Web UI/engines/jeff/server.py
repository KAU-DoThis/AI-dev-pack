"""Jeff 서버 켜기·끄기: 윈도우에서 WSL 안의 jeff-serve 를 띄운다 (engines/jeff/wsl/serve.sh).

- 켜기: wsl.exe 로 serve.sh 를 백그라운드 실행, 출력은 로그 파일로. 준비됐는지는 client 의 /health 로 본다.
- 끄기: serve.sh 가 남긴 serve.pid 로 WSL 안 프로세스를 끈다 (wsl.exe 만 끄면 리눅스 쪽이 남을 수 있음).
WSL 배포판·계정·폴더는 학습과 같은 설정(settings.yaml 의 engines.jeff.train)을 쓴다.
"""

import shlex
import subprocess
from pathlib import Path

from engines.base import EngineUnavailable
from engines.jeff.trainer import DEFAULTS, to_wsl

SCRIPT = Path(__file__).resolve().parent / "wsl" / "serve.sh"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOG_TAIL = 5


class WslServer:
    def __init__(self, settings: dict | None = None):
        self.cfg = {**DEFAULTS, **(settings or {})}
        self.process: subprocess.Popen | None = None
        self.log_path: Path | None = None
        self._log = None

    def _wsl(self, *args: str) -> list[str]:
        return ["wsl.exe", "-d", self.cfg["distro"], "-u", self.cfg["user"], "--", *args]

    def start(self, log_path: Path) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path = log_path
        self._log = open(log_path, "wb")
        try:
            self.process = subprocess.Popen(self._wsl("bash", to_wsl(SCRIPT)), stdin=subprocess.DEVNULL,
                                            stdout=self._log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        except OSError as error:
            self._close_log()
            raise EngineUnavailable(f"WSL 을 못 불렀어 ({error}). `wsl -l -v` 로 {self.cfg['distro']} 가 있는지 봐 줘.") from error

    def exited(self) -> str:
        if not self.process or self.process.poll() is None:
            return ""
        self._close_log()
        tail = [line for line in _read(self.log_path).splitlines() if line.strip()][-LOG_TAIL:]
        return " / ".join(tail) or f"종료 코드 {self.process.returncode}"

    def stop(self) -> None:
        if not self.process:
            return
        pid_file = shlex.quote(f"{self.cfg['root']}/serve.pid")
        try:
            # wsl.exe 를 거치면 $(...) 안의 큰따옴표가 깨진다 → 경로는 shlex.quote(작은따옴표)로
            subprocess.run(self._wsl("bash", "-c", f"kill $(cat {pid_file}) 2>/dev/null; rm -f {pid_file}; true"),
                           capture_output=True, timeout=30, creationflags=NO_WINDOW)
            self.process.wait(timeout=10)
        except (OSError, subprocess.SubprocessError):
            self.process.kill()
        self._close_log()
        self.process = None

    def _close_log(self) -> None:
        if self._log:
            self._log.close()
            self._log = None


def _read(path: Path | None) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace") if path else ""
    except OSError:
        return ""
