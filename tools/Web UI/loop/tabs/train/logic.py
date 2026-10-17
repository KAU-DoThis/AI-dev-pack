"""학습 탭 로직 (gradio 없이 테스트 가능). 엔진이 학습을 못 하면 안내만."""

import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from engines.base import EngineUnavailable, TrainParams, TrainStatus, can_train
from loop.core.config import append_record, load_profile, read_records
from loop.core.engine import Engine, get_engine
from loop.core.paths import ROOT
from loop.tabs.dataset.logic import SAMPLE_DATA, ready_problem  # noqa: F401 (SAMPLE_DATA 는 학습 탭 화면이 씀)

NOT_SUPPORTED = "이 엔진은 웹 UI 학습을 지원하지 않아."
TRAININGS_FILE = "trainings.jsonl"  # 프로필 outputs/ 안. 학습 시작 기록 (이름·데이터 폴더)


def auto_name(data_dir: str) -> str:
    """이름을 비우면: <데이터셋 폴더 이름>-<월일-시분초> (겹치지 않게). 영문·숫자·-·_ 만 남긴다."""
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(str(data_dir or "").strip().strip('"')).name).strip("-_")[:40]
    return f"{stem or 'run'}-{datetime.now():%m%d-%H%M%S}"


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
    if problem := ready_problem(data_path(data_dir or "")):
        return f"❌ {problem}"
    name = str(name or "").strip() or auto_name(data_dir)
    params = TrainParams(name=name, data_dir=str(data_path(data_dir)), epochs=int(epochs), lr=float(lr),
                         batch_size=int(batch), effective_batch_size=int(effective_batch), lora_rank=int(rank))
    try:
        engine.start_training(params)
    except EngineUnavailable as error:
        return f"❌ {error}"
    # 평가 탭이 "방금 학습한 모델 + 그 데이터의 시험셋"을 기본으로 고르게 기록
    append_record(TRAININGS_FILE, {"at": datetime.now().isoformat(timespec="seconds"), "name": params.name,
                                   "data_dir": str(data_dir).strip().strip('"')})
    return f"⏳ `{params.name}` 시작했어. 모델 불러오는 데 30초쯤 걸려."


def needs_training(data_dir: str) -> bool:
    """고른 데이터셋으로 아직 학습을 시작한 적이 없으면 True (마지막 학습 기록과 폴더가 다름)."""
    last = last_training()
    return not last or data_path(last["data_dir"]) != data_path(data_dir or "")


def trained_before(data_dir: str) -> bool:
    """이 데이터셋으로 학습을 시작한 적이 한 번이라도 있으면 True (마지막이 아니어도)."""
    target = data_path(data_dir or "")
    return any(data_path(r.get("data_dir", "")) == target for r in read_records(TRAININGS_FILE))


def last_training() -> dict | None:
    """마지막으로 시작한 학습 {at, name, data_dir}. 없으면 None."""
    records = read_records(TRAININGS_FILE)
    return records[-1] if records else None


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
    """그래프 축을 처음부터 고정: x = 0 → 계획한 step, y = 0 부터. 점이 늘어도 그래프 전체가 출렁이지 않게.
    (x, loss 축, nll 축). 정확도 축은 늘 0~1."""
    last_step = max([step for step, _ in s.steps] + [step for step, *_ in s.evals] + [0])
    x = [0, max(s.planned_steps, last_step, 1)]
    top_loss = max([loss for _, loss in s.steps] + [1.0])
    top_eval = max([nll for *_, nll in s.evals] + [1.0])
    return x, [0, round(top_loss * 1.1, 2)], [0, round(top_eval * 1.1, 2)]


def view(s: TrainStatus) -> tuple[str, pd.DataFrame, pd.DataFrame, pd.DataFrame, str]:
    """화면에 넣을 것: 상태 글, loss 표, 개발셋 정확도 표, 개발셋 nll 표, 로그.
    정확도·nll 을 한 그래프에 색으로 나누면 Gradio 6 에서 학습 중에 안 그려져서(빈 표 → 데이터) 표를 따로 낸다."""
    message = s.message
    if s.state == "running" and not s.steps:
        message += "\n\n처음 30초쯤은 모델을 불러오느라 그래프가 비어 있어. 그다음부터 선이 그려져."
    if s.evals:
        step, accuracy, nll = s.evals[-1]
        first = s.evals[0]
        message += (f"\n\n개발셋 (학습에 안 쓰는 확인용) 정확도 {first[1]:.2f} → **{accuracy:.2f}**, "
                    f"nll {first[2]:.3f} → **{nll:.3f}** (step {step})")
    # 빈 표도 숫자 칸으로 만들어야 그래프가 축을 글자로 오해하지 않는다
    loss = pd.DataFrame(s.steps, columns=["step", "loss"]).astype({"step": "int64", "loss": "float64"})
    accuracy = pd.DataFrame([(step, acc) for step, acc, _ in s.evals], columns=["step", "accuracy"])
    accuracy = accuracy.astype({"step": "int64", "accuracy": "float64"})
    nll = pd.DataFrame([(step, value) for step, _, value in s.evals], columns=["step", "nll"])
    nll = nll.astype({"step": "int64", "nll": "float64"})
    if s.state == "done":
        message += "\n\n👉 다음: **③ 평가**에서 처음 보는 문제를 학습 전보다 잘 푸는지 확인해."
    return message, loss, accuracy, nll, s.log
