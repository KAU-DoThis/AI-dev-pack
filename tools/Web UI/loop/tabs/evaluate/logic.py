"""평가 탭 로직 (gradio 없이 테스트 가능). 켜져 있는 엔진에 시험셋을 한 줄씩 보내 정답과 비교한다.

시험셋 한 줄 = {"state": {...}, "question": {"instructions": ..., "criteria": {id: 설명}}, "label": 정답 id}
점수는 시험셋마다 문제가 달라 같은 시험셋끼리만 비교한다 → 평가할 때마다 기록(프로필 outputs/evaluations.jsonl)에
쌓고, 화면은 고른 시험셋의 기록만 보여 준다. 같은 시험셋으로 자주 평가하면 오염 경고.
"""

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

from engines.base import can_train
from loop.core.config import append_record, read_records
from loop.core.engine import Engine, EngineUnavailable, get_engine
from loop.tabs.dataset.logic import folders, ready_problem, shown
from loop.tabs.train.logic import data_path, last_training

SAMPLE_TEST = "logs/smoke-train/split/test.jsonl"
COLUMNS = ["모델", "줄 수", "정확도", "nll", "평균 확신도", "평균 ms"]   # score() 한 줄
HISTORY_COLUMNS = ["시각", "시험셋", *COLUMNS]                          # 화면 표
EVALS_FILE = "evaluations.jsonl"
WARN_AFTER = 3  # 같은 시험셋으로 이만큼 평가하면 경고 (점수 보고 설정을 고치면 시험셋에 맞춰 튜닝하게 됨)


def test_choices() -> list[str]:
    """시험셋 후보: 데이터셋 탭에서 만든 것(누수 통과·안 바뀐 것만, 최근 것 먼저) + 견본."""
    made = [shown(folder / "test.jsonl") for folder in folders() if not ready_problem(folder)]
    return made + [SAMPLE_TEST]


def default_test(choices: list[str] | None = None) -> str:
    """기본 시험셋: 마지막 학습에 쓴 데이터 폴더의 test.jsonl (후보에 있으면), 아니면 맨 앞."""
    choices = choices if choices is not None else test_choices()
    last = last_training()
    if last:
        test = test_key(str(data_path(last["data_dir"]) / "test.jsonl"))
        if test in choices:
            return test
    return choices[0] if choices else SAMPLE_TEST


def default_models(names: list[str], engine: Engine | None = None) -> list[str]:
    """기본으로 고를 모델: 맨 앞(학습 전) + 마지막으로 학습한 모델. 학습 기록이 없으면 전부."""
    last = (last_training() or {}).get("name", "")
    if not last:
        engine = engine or get_engine()
        last = engine.training_status().name if can_train(engine) else ""
    if names and last in names[1:]:
        return [names[0], last]
    return names


def test_key(test_file: str) -> str:
    """기록에 쓰는 시험셋 이름: 웹 UI 폴더 기준 경로 (같은 파일이면 어떻게 적어도 같게)."""
    return shown(data_path(test_file or ""))


def test_label(key: str) -> str:
    """표에 보일 짧은 이름: .../domo-1/test.jsonl → domo-1."""
    path = Path(key)
    return path.parent.name if path.name == "test.jsonl" and path.parent.name else path.stem


def read_rows(path: Path) -> list[dict]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{number}번째 줄이 JSON 이 아니야: {error.msg}") from None
        criteria = (row.get("question") or {}).get("criteria") or {}
        if not isinstance(row.get("state"), dict) or row.get("label") not in criteria:
            raise ValueError(f"{number}번째 줄에 state·question.criteria·label(선택지 중 하나)이 없어.")
        rows.append(row)
    if not rows:
        raise ValueError("빈 파일이야.")
    return rows


def score(engine: Engine, rows: list[dict], model: str) -> list:
    hits, nll, confidence, ms = 0, 0.0, 0.0, 0.0
    for row in rows:
        question = row["question"]
        decision = engine.choose(row["state"], question["criteria"], question.get("instructions") or "", model=model)
        hits += decision.choice == row["label"]
        nll -= math.log(max(decision.probabilities.get(row["label"], 0.0), 1e-12))
        confidence += decision.confidence
        ms += decision.elapsed_ms
    n = len(rows)
    return [model, n, round(hits / n, 3), round(nll / n, 3), round(confidence / n, 3), round(ms / n)]


SMALL_TEST = 30  # 문제가 이보다 적으면 한두 개 차이는 우연일 수 있다고 덧붙임


def verdict(table: list[list], base: str) -> str:
    """처음 쓰는 사람용 한 문장 판정: 학습 전(base)보다 학습한 모델이 몇 문제 더 맞혔나."""
    if not table:
        return ""
    n = table[0][1]

    def hits(row):
        return round(row[2] * row[1])

    before = next((row for row in table if row[0] == base), None)
    others = [row for row in table if row[0] != base]
    small = f"\n\n문제가 {n}개뿐이라 한두 개 차이는 우연일 수 있어. 데이터가 많아지면 더 믿을 만해져." if n < SMALL_TEST else ""
    if before and others:
        best = max(others, key=lambda row: (row[2], -row[3]))
        if best[2] > before[2]:
            head = "✅ 학습 효과 있음"
        elif best[2] == before[2]:
            head = "➖ 학습 전과 같음"
        else:
            head = "⚠️ 학습 후가 더 못함"
        return (f"### {head}\n처음 보는 문제 {n}개 중 **{hits(before)}개 → {hits(best)}개** 맞힘 "
                f"(정확도 {before[2]:.0%} → {best[2]:.0%}, `{best[0]}`){small}")
    if len(table) > 1:  # 학습 전 모델 없이 여러 모델끼리
        lines = " · ".join(f"`{row[0]}` **{hits(row)}개** ({row[2]:.0%})" for row in table)
        return f"### 처음 보는 문제 {n}개 중 맞힌 수\n{lines}{small}"
    row = table[0]
    return f"### `{row[0]}`: 처음 보는 문제 {n}개 중 **{hits(row)}개** 맞힘 (정확도 {row[2]:.0%}){small}"


# ---- 기록 ----
def records_for(test_file: str) -> list[dict]:
    key = test_key(test_file)
    return [r for r in read_records(EVALS_FILE) if r.get("test") == key]


def history(test_file: str) -> list[list]:
    """고른 시험셋의 기록, 최근 것 먼저."""
    rows = [[r["at"][5:16].replace("T", " "), test_label(r["test"]), *r["score"]] for r in records_for(test_file)]
    return rows[::-1]


def history_status(test_file: str) -> str:
    records = records_for(test_file)
    if not records:
        return f"`{test_label(test_key(test_file))}` 은(는) 아직 평가 기록이 없어. 모델을 고르고 **평가 실행**."
    runs = len({r["at"] for r in records})
    changed = len({r.get("sha256") for r in records}) > 1
    text = f"`{test_label(records[0]['test'])}` 평가 {runs}번 · 같은 시험셋 기록만 보여 줘 (다른 시험셋과는 점수 비교 안 됨)."
    if changed:
        text += "\n\n⚠️ 이 경로의 파일 내용이 기록 중간에 바뀌었어. 예전 점수와 비교하지 마."
    if runs >= WARN_AFTER:
        text += (f"\n\n⚠️ 이 시험셋으로 {runs}번 평가했어. 점수를 보고 설정·데이터를 계속 고치면 시험셋에 맞춰 튜닝하는 셈이라 "
                 "점수가 부풀어. 설정 고르기는 학습 탭의 개발셋으로 하고, 시험셋은 마지막 확인에만.")
    return text


def run(test_file: str, models: list[str], engine: Engine | None = None) -> tuple[list[list], str]:
    """(이번 점수 표, 상태 글). 끝까지 잰 모델의 점수는 기록에 쌓는다."""
    engine = engine or get_engine()
    if not models:
        return [], "❌ 모델을 하나 이상 골라 줘."
    path = data_path(test_file or "")
    if not path.is_file():
        return [], f"❌ 파일이 없어: `{path}`"
    try:
        rows = read_rows(path)
    except ValueError as error:
        return [], f"❌ {error}"
    at = datetime.now().isoformat(timespec="seconds")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    table, stopped = [], ""
    for model in models:
        try:
            table.append(score(engine, rows, model))
        except EngineUnavailable as error:
            stopped = f"❌ `{model}` 평가 중 멈춤: {error}"
            break
    for row in table:
        append_record(EVALS_FILE, {"at": at, "test": test_key(test_file), "sha256": digest, "score": row})
    if stopped:
        return table, stopped
    return table, f"✅ `{test_label(test_key(test_file))}` {len(rows)}줄 × 모델 {len(models)}개 평가 끝. 정확도↑ · nll↓ 이 좋음."
