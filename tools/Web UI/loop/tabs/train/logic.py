"""학습 탭 로직 (gradio 없이 테스트 가능). 엔진이 학습을 못 하면 안내만."""

import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from engines.base import EngineUnavailable, TrainParams, TrainStatus, can_train
from loop.core.config import load_profile
from loop.core.engine import Engine, get_engine
from loop.core.paths import ROOT

# 데이터셋 탭이 생기기 전까지 쓰는 견본 (학습 도구 동작 확인용 60줄, 시험셋 15줄은 안 씀)
SAMPLE_DATA = "logs/smoke-train/split"
NOT_SUPPORTED = "이 엔진은 웹 UI 학습을 지원하지 않아."


def default_name() -> str:
    prefix = re.sub(r"[^a-z0-9]+", "-", (load_profile()["name"] or "run").lower()).strip("-") or "run"
    return f"{prefix}-{datetime.now():%m%d-%H%M}"


def data_path(text: str) -> Path:
    """화면에 적은 폴더. 상대 경로면 웹 UI 폴더 기준."""
    path = Path(text.strip().strip('"'))
    return path if path.is_absolute() else ROOT / path


def start(name, data_dir, epochs, lr, batch, effective_batch, rank, engine: Engine | None = None) -> str:
    engine = engine or get_engine()
    if not can_train(engine):
        return f"❌ {NOT_SUPPORTED}"
    params = TrainParams(name=str(name).strip(), data_dir=str(data_path(data_dir)), epochs=int(epochs), lr=float(lr),
                         batch_size=int(batch), effective_batch_size=int(effective_batch), lora_rank=int(rank))
    try:
        engine.start_training(params)
    except EngineUnavailable as error:
        return f"❌ {error}"
    return f"⏳ `{params.name}` 시작했어. 모델 불러오는 데 30초쯤 걸려."


def stop(engine: Engine | None = None) -> str:
    engine = engine or get_engine()
    if not can_train(engine):
        return f"❌ {NOT_SUPPORTED}"
    if engine.training_status().state != "running":
        return "멈출 학습이 없어."
    engine.stop_training()
    return "⏹️ 멈추라고 했어. 몇 초 안에 상태가 바뀌어."


def status(engine: Engine | None = None) -> TrainStatus:
    engine = engine or get_engine()
    if not can_train(engine):
        return TrainStatus("idle", NOT_SUPPORTED)
    return engine.training_status()


def signature(s: TrainStatus) -> tuple:
    """화면이 바뀌어야 하는지 가리는 값. 같으면 다시 그리지 않는다 (2초마다 깜빡이지 않게)."""
    return s.state, s.name, s.message, len(s.steps), len(s.evals), s.selected_step, s.log


def axes(s: TrainStatus) -> tuple[list[float], list[float], list[float]]:
    """그래프 축을 처음부터 고정: x = 0 → 계획한 step, y = 0 부터. 점이 늘어도 그래프 전체가 출렁이지 않게."""
    last_step = max([step for step, _ in s.steps] + [step for step, *_ in s.evals] + [0])
    x = [0, max(s.planned_steps, last_step, 1)]
    top_loss = max([loss for _, loss in s.steps] + [1.0])
    top_eval = max([nll for *_, nll in s.evals] + [1.0])
    return x, [0, round(top_loss * 1.1, 2)], [0, round(top_eval * 1.1, 2)]


def view(s: TrainStatus) -> tuple[str, pd.DataFrame, pd.DataFrame, str]:
    """화면에 넣을 것: 상태 글, loss 표, 개발셋 점수 표(긴 형식), 로그."""
    message = s.message
    if s.evals:
        step, accuracy, nll = s.evals[-1]
        first = s.evals[0]
        message += (f"\n\n개발셋 (학습에 안 쓰는 확인용) 정확도 {first[1]:.2f} → **{accuracy:.2f}**, "
                    f"nll {first[2]:.3f} → **{nll:.3f}** (step {step})")
    # 빈 표도 숫자 칸으로 만들어야 그래프가 축을 글자로 오해하지 않는다
    loss = pd.DataFrame(s.steps, columns=["step", "loss"]).astype({"step": "int64", "loss": "float64"})
    evals = pd.DataFrame([(step, "accuracy", acc) for step, acc, _ in s.evals]
                         + [(step, "nll", nll) for step, _, nll in s.evals], columns=["step", "metric", "value"])
    evals = evals.astype({"step": "int64", "metric": "str", "value": "float64"})
    return message, loss, evals, s.log
