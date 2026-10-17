"""Jeff 학습 데이터 검사·분할: Jeff 원본의 jeff-kit 을 그대로 부른다 (LLM 안 씀, Windows 에서 돎).

check-rows → split(family 단위) → leak-check → shortcut-report. 결과는 도구 출력을 그대로 화면에 넘긴다.
jeff-kit 위치는 settings 의 kit (없으면 vendor/jeff/.venv 안).
"""

import os
import subprocess
from pathlib import Path

from engines.base import EngineUnavailable, KitResult

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_KIT = ROOT / "vendor" / "jeff" / ".venv" / "Scripts" / "jeff-kit.exe"
SPLIT_FILES = ("train", "development", "calibration", "test")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TIMEOUT = 300  # 초. 수만 줄이어도 몇 초면 끝남
REPORT_NAME = "shortcut-report.md"


class JeffKit:
    def __init__(self, settings: dict | None = None):
        kit = Path((settings or {}).get("kit") or DEFAULT_KIT)
        self.kit = kit if kit.is_absolute() else ROOT / kit  # 상대 경로는 웹 UI 폴더 기준

    def _run(self, *args: str | Path) -> subprocess.CompletedProcess:
        if not self.kit.exists():
            raise EngineUnavailable(f"jeff-kit 을 못 찾았어 (`{self.kit}`). README 의 Jeff 설치를 먼저 해 줘.")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        try:
            return subprocess.run([str(self.kit), *map(str, args)], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", env=env, timeout=TIMEOUT, creationflags=NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as error:
            raise EngineUnavailable(f"jeff-kit 실행 실패: {error}") from error

    @staticmethod
    def _result(done: subprocess.CompletedProcess) -> KitResult:
        text = "\n".join(part.strip() for part in (done.stdout, done.stderr) if part.strip())
        return KitResult(done.returncode == 0, text)

    def check_rows(self, file: Path) -> KitResult:
        return self._result(self._run("check-rows", file))

    def split(self, file: Path, out_dir: Path, test: float, development: float, calibration: float,
              seed: int) -> KitResult:
        if any((out_dir / f"{name}.jsonl").exists() for name in SPLIT_FILES):
            return KitResult(False, f"`{out_dir}` 에 이미 나눈 파일이 있어. 다른 이름으로 해 줘.")
        return self._result(self._run("split", file, "--out", out_dir, "--test", test, "--development", development,
                                      "--calibration", calibration, "--seed", seed))

    def leak_check(self, split_dir: Path) -> KitResult:
        against = [split_dir / f"{name}.jsonl" for name in SPLIT_FILES[1:]]
        return self._result(self._run("leak-check", "--train", split_dir / "train.jsonl", "--against", *against))

    def shortcut_report(self, split_dir: Path) -> KitResult:
        report = split_dir / REPORT_NAME
        done = self._run("shortcut-report", "--train", split_dir / "train.jsonl", "--test", split_dir / "test.jsonl",
                         "--out", report)
        result = self._result(done)
        if report.exists():
            result.report = report.read_text(encoding="utf-8", errors="replace")
        return result
