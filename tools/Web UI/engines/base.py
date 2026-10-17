"""엔진이 맞춰야 하는 모양 (콘센트). 루프는 이 파일에 있는 것만 안다.

필수: '상태 확인'(health)·'판단'(choose)·'고를 수 있는 모델'(models).
선택: 학습(Trainer)·서버 켜기(Server)·데이터 검사·분할(DatasetKit). 없는 엔진이면 그 탭은 안내만 보여준다.
엔진은 loop 를 import 하지 않는다 (설정은 루프가 dict 로 넘겨준다).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol


class EngineUnavailable(Exception):
    """화면에 그대로 보여줄 한국어 안내를 담은 오류. 엔진은 모든 실패를 이걸로 바꿔서 던진다."""


@dataclass
class Decision:
    choice: str
    probabilities: dict[str, float]  # 보낸 선택지 순서 그대로
    confidence: float
    elapsed_ms: float
    request: dict = field(repr=False)   # 엔진에 실제로 보낸 것 (화면 "요청/응답" 칸)
    response: dict = field(repr=False)

    def ranked(self) -> list[tuple[str, float]]:
        return sorted(self.probabilities.items(), key=lambda item: -item[1])


@dataclass
class Health:
    state: Literal["ready", "loading", "off"]
    detail: str = ""   # 준비됨이면 모델 이름 등, 꺼짐이면 켜는 법
    where: str = ""    # 주소 등 (화면 표시용)


class Engine(Protocol):
    name: str  # 화면에 보일 이름 (예: "Jeff")

    def health(self) -> Health:
        """켜졌는지. 실패해도 예외 대신 Health(state="off") 를 돌려준다."""

    def models(self) -> list[str]:
        """판단에 쓸 수 있는 모델 이름들. 맨 앞이 기본(학습 전) 모델. 꺼져 있으면 빈 목록."""

    def choose(self, state: dict, options: dict[str, str], instructions: str = "", orders: int = 1,
               model: str = "") -> Decision:
        """선택지 {id: 설명} 중 state 에 가장 맞는 것과 선택지별 확률. model 이 비면 기본 모델. 실패하면 EngineUnavailable."""


@dataclass
class TrainParams:
    name: str              # 결과 모델 이름 (추론 탭 모델 목록에 이 이름으로 나옴)
    data_dir: str          # train / development / calibration .jsonl 이 있는 폴더 (시험셋은 안 읽음)
    epochs: int = 2
    lr: float = 1e-4
    batch_size: int = 4
    effective_batch_size: int = 8
    lora_rank: int = 16


@dataclass
class TrainStatus:
    state: Literal["idle", "running", "done", "stopped", "failed"]
    message: str = ""                                          # 화면 상태줄 (한국어)
    name: str = ""                                             # 학습 이름
    steps: list[tuple[int, float]] = field(default_factory=list)          # (step, loss)
    evals: list[tuple[int, float, float]] = field(default_factory=list)   # (step, 개발셋 정확도, 개발셋 nll)
    planned_steps: int = 0                                     # 0 = 모름
    selected_step: int | None = None                           # 고른 체크포인트
    log: str = ""                                              # 로그 끝부분


class Trainer(Protocol):
    """학습할 수 있는 엔진이 더 갖추는 것. 한 번에 학습 하나."""

    def start_training(self, params: TrainParams) -> None:
        """백그라운드로 시작하고 바로 돌아온다. 시작 못 하면 EngineUnavailable."""

    def training_status(self) -> TrainStatus:
        """지금(또는 마지막) 학습의 상태. 예외 대신 TrainStatus 로."""

    def stop_training(self) -> None:
        """돌고 있는 학습을 멈춘다. 없으면 아무것도 안 함."""


def can_train(engine: object) -> bool:
    return all(callable(getattr(engine, name, None)) for name in ("start_training", "training_status", "stop_training"))


class Server(Protocol):
    """자기 서버를 켜고 끌 수 있는 엔진이 더 갖추는 것. 실행기(loop/launch.py)가 웹 UI 를 켤 때 쓴다.
    못 켜는 엔진이면 실행기가 켜는 법(health 의 detail)만 보여 준다."""

    def start_server(self, log_path: Path) -> None:
        """백그라운드로 켜고 바로 돌아온다 (준비됐는지는 health 로). 서버 출력은 log_path 에. 못 켜면 EngineUnavailable."""

    def server_exited(self) -> str:
        """start_server 로 켠 서버가 끝나 버렸으면 그 이유(로그 끝부분), 아직 돌고 있으면 빈 문자열."""

    def stop_server(self) -> None:
        """start_server 로 켠 서버를 끈다. 켠 게 없으면 아무것도 안 함."""


def can_serve(engine: object) -> bool:
    return all(callable(getattr(engine, name, None)) for name in ("start_server", "server_exited", "stop_server"))


@dataclass
class KitResult:
    ok: bool
    message: str      # 화면에 보여줄 결과 (도구 출력 그대로 또는 한국어 요약)
    report: str = ""  # 긴 보고서 (markdown). 없으면 빈 문자열


class DatasetKit(Protocol):
    """학습 데이터를 검사·분할하는 도구가 있는 엔진이 더 갖추는 것. 데이터셋 탭이 쓴다.
    split 결과 폴더에는 train / development / calibration / test .jsonl 이 생긴다 (학습은 test 를 안 읽음)."""

    def check_rows(self, file: Path) -> KitResult:
        """줄마다 형식 검사."""

    def split(self, file: Path, out_dir: Path, test: float, development: float, calibration: float,
              seed: int) -> KitResult:
        """묶음(family) 단위로 4개 파일로 나눈다. out_dir 에 이미 나눈 파일이 있으면 실패."""

    def leak_check(self, split_dir: Path) -> KitResult:
        """학습 파일에 평가용 줄(또는 거의 같은 줄)이 섞였는지."""

    def shortcut_report(self, split_dir: Path) -> KitResult:
        """정답이 겉모양(길이·위치·라벨 비율 등)으로 새는지 보고서. 깃발이 있어도 ok (사람이 볼 것)."""


def can_prepare(engine: object) -> bool:
    return all(callable(getattr(engine, name, None))
               for name in ("check_rows", "split", "leak_check", "shortcut_report"))
