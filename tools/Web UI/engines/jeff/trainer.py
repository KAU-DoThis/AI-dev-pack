"""Jeff 학습: 윈도우에서 WSL 안의 jeff-train 을 돌린다 (jeff-train 은 윈도우에서 안 돎 — fcntl·fsync·심볼릭 링크).

- 시작: wsl.exe 로 engines/jeff/wsl/train.sh 를 백그라운드 실행. 데이터 폴더는 /mnt/c/... 경로로 넘긴다.
- 상태: WSL 파일을 윈도우에서 바로 읽는다 (//wsl.localhost/<배포판>/...): runs/<이름>/state·run/*.jsonl·train.log.
- 끝: train.sh 가 어댑터를 adapters/<이름>/ 로 복사 → 여기서 서버에 /v1/adapters/reload 를 한 번 부른다.
웹 UI 를 껐다 켜도 마지막 학습 상태는 파일에서 다시 읽는다 (학습 자체는 WSL 에서 계속 돎).
"""

import json
import math
import re
import shlex
import subprocess
from pathlib import Path, PureWindowsPath
from typing import Callable

from engines.base import EngineUnavailable, TrainParams, TrainStatus

SCRIPT = Path(__file__).resolve().parent / "wsl" / "train.sh"
SPLIT_FILES = ("train", "development", "calibration")  # 시험셋(test)은 일부러 없음
NAME_RULE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
LOG_TAIL = 40
DEFAULTS = {"distro": "Ubuntu-24.04", "user": "user", "root": "/home/user/jeff-train",
            "base_model": "Qwen/Qwen3.5-0.8B", "revision": "2fc06364715b967f1860aea9cf38778875588b17"}


def to_wsl(path: Path | str) -> str:
    """C:\\a\\b → /mnt/c/a/b"""
    p = PureWindowsPath(Path(path).resolve())
    return f"/mnt/{p.drive[0].lower()}/" + "/".join(p.parts[1:])


class WslTrainer:
    def __init__(self, settings: dict | None = None, reload_adapters: Callable[[], None] | None = None,
                 runs_dir: Path | None = None):
        self.cfg = {**DEFAULTS, **(settings or {})}
        self.reload_adapters = reload_adapters
        # 윈도우에서 본 WSL 안 학습 폴더 (테스트는 임시 폴더로 바꿔 끼움)
        root = "//wsl.localhost/" + self.cfg["distro"] + self.cfg["root"]
        self.runs = runs_dir or Path(root) / "runs"
        self.process: subprocess.Popen | None = None
        self.current = ""        # 이번 웹 UI 에서 시작한 학습 이름
        self.reloaded: set[str] = set()

    # ---- 시작 / 멈춤 ----
    def check(self, params: TrainParams) -> Path:
        """시작 전에 막을 것들. 데이터 폴더(윈도우 경로)를 돌려준다."""
        if not NAME_RULE.match(params.name):
            raise EngineUnavailable("이름은 영문·숫자·`-`·`_`·`.` 만 (64자까지, 첫 글자는 영문·숫자).")
        if self.training_status().state == "running":
            raise EngineUnavailable("이미 학습이 돌고 있어. 끝나거나 멈춘 뒤에 시작해 줘.")
        if self._run_dir(params.name).exists():
            raise EngineUnavailable(f"`{params.name}` 은(는) 이미 쓴 이름이야. 다른 이름으로 해 줘.")
        data = Path(params.data_dir)
        missing = [f"{name}.jsonl" for name in SPLIT_FILES if not (data / f"{name}.jsonl").is_file()]
        if missing:
            raise EngineUnavailable(f"데이터 폴더 `{data}` 에 {', '.join(missing)} 이(가) 없어.")
        if params.epochs < 1 or params.batch_size < 1 or params.effective_batch_size < params.batch_size or params.lora_rank < 1:
            raise EngineUnavailable("에폭·배치·rank 는 1 이상, 실제 배치는 배치 크기 이상이어야 해.")
        if not (0 < params.lr < 1):
            raise EngineUnavailable("학습률은 0 과 1 사이 (보통 1e-4 정도).")
        return data

    def start_training(self, params: TrainParams) -> None:
        data = self.check(params)
        args = [params.name, to_wsl(data), params.epochs, params.batch_size, params.effective_batch_size,
                params.lr, params.lora_rank, self.cfg["base_model"], self.cfg["revision"]]
        command = ["wsl.exe", "-d", self.cfg["distro"], "-u", self.cfg["user"], "--", "bash", to_wsl(SCRIPT),
                   *map(str, args)]
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as error:
            raise EngineUnavailable(f"WSL 을 못 불렀어 ({error}). `wsl -l -v` 로 {self.cfg['distro']} 가 있는지 봐 줘.") from error
        self.current = params.name

    def stop_training(self) -> None:
        status = self.training_status()
        if status.state != "running":
            return
        run = f"{self.cfg['root']}/runs/{status.name}"
        stop, pid = shlex.quote(f"{run}/stop"), shlex.quote(f"{run}/pid")
        # stop 표시를 먼저 남겨야 train.sh 가 '실패' 대신 '멈춤' 으로 적는다.
        # wsl.exe 를 거치면 $(...) 안의 큰따옴표가 깨진다 → 경로는 shlex.quote(작은따옴표)로
        subprocess.run(["wsl.exe", "-d", self.cfg["distro"], "-u", self.cfg["user"], "--", "bash", "-c",
                        f"touch {stop}; kill $(cat {pid}) 2>/dev/null; true"],
                       capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    # ---- 상태 ----
    def _run_dir(self, name: str) -> Path:
        return self.runs / name

    def _latest(self) -> str:
        if self.current:
            return self.current
        try:
            dirs = [p for p in self.runs.iterdir() if (p / "state").exists()]
        except OSError:
            return ""
        return max(dirs, key=lambda p: (p / "state").stat().st_mtime).name if dirs else ""

    def training_status(self) -> TrainStatus:
        name = self._latest()
        if not name:
            return TrainStatus("idle", "아직 학습한 적 없어.")
        run = self._run_dir(name)
        state_text = _read(run / "state").strip()
        if not state_text:  # wsl.exe 가 막 떴고 state 를 아직 안 씀
            if self.process and self.process.poll() not in (None, 0):
                error = (self.process.stderr.read() or b"").decode(errors="replace").strip() if self.process.stderr else ""
                return TrainStatus("failed", f"❌ 시작 못 함: {error[:300] or 'WSL 오류'}", name)
            return TrainStatus("running", "⏳ WSL 에서 준비 중…", name)

        steps = [(row["step"], row["loss"]) for row in _jsonl(run / "run" / "training.jsonl") if "loss" in row]
        evals_raw = _jsonl(run / "run" / "evaluations.jsonl")
        evals = [(row["step"], row["raw"]["accuracy"], row["raw"]["nll"]) for row in evals_raw if row.get("raw")]
        selected = evals_raw[-1].get("selected_step") if evals_raw else None
        config = _json(run / "run" / "config.json") or _json(run / "params.json")  # 시작 직후엔 params.json 만 있음
        planned = _planned(run / "data" / "train.jsonl", config)
        log = "\n".join(_readable_log(_read(run / "train.log"), steps, evals)[-LOG_TAIL:])
        status = TrainStatus("running", "", name, steps, evals, planned, selected, log)

        word = state_text.split()[0]
        if word == "running":
            if self.process and self.process.poll() is not None and name == self.current:
                status.state, status.message = "failed", "❌ WSL 쪽 프로그램이 상태를 못 남기고 끝났어. 로그를 봐 줘."
            else:
                where = f"step {steps[-1][0]}/{planned or '?'}" if steps else "모델 불러오는 중"
                status.message = f"⏳ `{name}` 학습 중 · {where}"
        elif word == "done":
            self._reload_once(name)
            status.state = "done"
            status.message = (f"✅ `{name}` 학습 끝 · 고른 체크포인트 step {selected}. "
                              f"추론 탭 **모델**에서 `{name}` 을 고르면 써 볼 수 있어.")
        elif word == "stopped":
            status.state, status.message = "stopped", f"⏹️ `{name}` 멈춤 (어댑터는 안 만들어짐)."
        else:
            status.state, status.message = "failed", f"❌ `{name}` 실패 ({state_text}). 아래 로그 끝부분을 봐 줘."
        return status

    def _reload_once(self, name: str) -> None:
        """끝난 어댑터를 서버가 다시 읽게 (한 번만). 서버가 꺼져 있으면 다음에 켤 때 알아서 읽는다."""
        if name in self.reloaded or not self.reload_adapters:
            return
        try:
            self.reload_adapters()
            self.reloaded.add(name)
        except EngineUnavailable:
            pass


def _read(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", errors="replace")  # read_text 는 CR 을 줄바꿈으로 바꿔 버림
    except OSError:
        return ""


def _jsonl(path: Path) -> list[dict]:
    rows = []
    for line in _read(path).splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:  # 쓰는 중인 마지막 줄
            pass
    return rows


def _json(path: Path) -> dict:
    try:
        return json.loads(_read(path) or "{}")
    except json.JSONDecodeError:
        return {}


def _planned(train_file: Path, config: dict) -> int:
    rows = len(_read(train_file).splitlines())
    if not rows or not config:
        return 0
    return math.ceil(rows / config.get("effective_batch_size", 1)) * config.get("epochs", 1)


def _readable_log(text: str, steps: list[tuple[int, float]], evals: list[tuple[int, float, float]]) -> list[str]:
    """사람이 읽을 로그: train.log 의 글줄(경고·오류)만 + step 별 한 줄. JSON 덩어리는 그래프·상태에 이미 있음."""
    lines = [line for line in _tail(text, 10_000)
             if not line.startswith((" ", "\t", "{", "}", "[", "]")) and "Loading weights" not in line]
    by_step = {step: f"step {step:>4} · loss {loss:.4f}" for step, loss in steps}
    for step, accuracy, nll in evals:
        by_step[step] = by_step.get(step, f"step {step:>4}") + f" · 개발셋 정확도 {accuracy:.2f} nll {nll:.3f}"
    return lines + [by_step[step] for step in sorted(by_step)]


def _tail(text: str, count: int) -> list[str]:
    # 진행 막대(\r)는 마지막 모습만 남긴다
    lines = [line.rsplit("\r", 1)[-1] for line in text.replace("\r\n", "\n").split("\n")]
    return [line for line in lines if line.strip()][-count:]
