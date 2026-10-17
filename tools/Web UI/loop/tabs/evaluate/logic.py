"""평가 탭 로직 (gradio 없이 테스트 가능). 켜져 있는 엔진에 시험셋을 한 줄씩 보내 정답과 비교한다.

시험셋 한 줄 = {"state": {...}, "question": {"instructions": ..., "criteria": {id: 설명}}, "label": 정답 id}
최소 버전: 모델별 정확도·nll·평균 확신도·응답 시간만. 혼동표·틀린 줄 목록·기록은 나중에.
"""

import json
import math
from pathlib import Path

from loop.core.engine import Engine, EngineUnavailable, get_engine
from loop.tabs.train.logic import data_path

SAMPLE_TEST = "logs/smoke-train/split/test.jsonl"
COLUMNS = ["모델", "줄 수", "정확도", "nll", "평균 확신도", "평균 ms"]


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


def run(test_file: str, models: list[str], engine: Engine | None = None) -> tuple[list[list], str]:
    """(점수 표, 상태 글)."""
    engine = engine or get_engine()
    if not models:
        return [], "❌ 모델을 하나 이상 골라 줘."
    path = data_path(test_file)
    if not path.is_file():
        return [], f"❌ 파일이 없어: `{path}`"
    try:
        rows = read_rows(path)
    except ValueError as error:
        return [], f"❌ {error}"
    table = []
    for model in models:
        try:
            table.append(score(engine, rows, model))
        except EngineUnavailable as error:
            return table, f"❌ `{model}` 평가 중 멈춤: {error}"
    return table, (f"✅ `{path.name}` {len(rows)}줄 × 모델 {len(models)}개 평가 끝. "
                   "정확도↑ · nll↓ 이 좋음. 시험셋 점수를 보고 설정을 계속 고치면 시험셋이 오염되니 주의.")
