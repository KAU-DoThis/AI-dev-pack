"""엔진이 맞춰야 하는 모양 (콘센트). 루프는 이 파일에 있는 것만 안다.

필수: '상태 확인'(health)·'판단'(choose)·'고를 수 있는 모델'(models).
선택: 학습(Trainer). 학습을 못 하는 엔진이면 학습 탭이 안내만 보여준다. 평가는 평가 탭 만들 때 더한다.
엔진은 loop 를 import 하지 않는다 (설정은 루프가 dict 로 넘겨준다).
"""

from dataclasses import dataclass, field
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
