"""추론 탭 로직 (gradio 없이 테스트 가능)."""

import json
from dataclasses import dataclass
from datetime import datetime

from loop.core.config import LANGUAGES, action_names, load_examples, load_profile
from loop.core.engine import Decision, Engine, EngineUnavailable, get_engine

# 둘째 칸 이름은 프로필 profile.yaml 의 item
HISTORY_COLUMNS = ["시각", load_profile()["item"], "결과", "확률", "확신도", "기대", "일치", "ms", "설명", "모델"]
HISTORY_LIMIT = 50


@dataclass
class Outcome:
    label: dict[str, float] | None  # gr.Label 에 넣을 {표시이름: 확률}
    status: str
    detail: dict | None = None      # 실제로 보낸 요청과 받은 응답
    row: list | None = None         # 결과 기록 한 줄


def example_names() -> list[str]:
    return [example["name"] for example in load_examples()]


def example_json(name: str) -> str:
    for example in load_examples():
        if example["name"] == name:
            return json.dumps(example["state"], ensure_ascii=False, indent=2)
    return ""


def example_for(state: dict) -> dict:
    """입력이 예시와 똑같으면 그 예시. 손으로 고친 입력이면 빈 dict (기대값 비교 안 함)."""
    for example in load_examples():
        if example["state"] == state:
            return example
    return {}


def parse_state(raw: str) -> dict:
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 형식 오류: {e.msg} (줄 {e.lineno}, 칸 {e.colno})") from None
    if not isinstance(state, dict) or not state:
        raise ValueError("JSON 형식 오류: 입력은 { ... } 객체여야 해.")
    return state


def options_from_rows(rows: list[list]) -> dict[str, str]:
    """선택지 표 [[id, 이름, 설명], ...] → {id: 설명}."""
    return {str(row[0]).strip(): str(row[2] or "").strip() for row in rows if row and str(row[0]).strip()}


def display_label(key: str) -> str:
    name = action_names().get(key)
    return f"{key} · {name}" if name else key


def summarize(state: dict) -> str:
    """기록 표에 보여줄 짧은 이름: 프로필 display_keys 중 처음 있는 칸 + 금액."""
    who = next((state[k] for k in load_profile()["display_keys"] if state.get(k)), "?")
    amount = state.get("amount")
    return f"{who} {amount:,}" if isinstance(amount, (int, float)) else str(who)


def run(raw: str, rows: list[list], instructions: str, double_check: bool,
        engine: Engine | None = None, lang: str = "", model: str = "") -> Outcome:
    try:
        state = parse_state(raw)
    except ValueError as e:
        return Outcome(None, str(e))
    options = options_from_rows(rows)
    empty = [key for key, desc in options.items() if not desc]
    engine = engine or get_engine()
    try:
        decision: Decision = engine.choose(state, options, instructions, orders=2 if double_check else 1, model=model)
    except EngineUnavailable as e:
        return Outcome(None, f"❌ {e}")

    example = example_for(state)
    expect = example.get("expect", "")
    match = "" if not expect else ("✅" if decision.choice == expect else "❌")
    status = f"✅ {display_label(decision.choice)} (확률 {decision.probabilities[decision.choice]:.2f}, {decision.elapsed_ms:.0f}ms)"
    if expect and decision.choice != expect:
        status += f" — 예시의 기대값은 `{expect}`"
    if empty:
        status += f" · ⚠️ 설명 없는 선택지: {', '.join(empty)} ({engine.name}는 id 이름만 보고 판단)"
    row = [datetime.now().strftime("%H:%M:%S"), example.get("name") or summarize(state), decision.choice,
           round(decision.probabilities[decision.choice], 3), round(decision.confidence, 3),
           expect, match, round(decision.elapsed_ms), LANGUAGES.get(lang, ""), model or "기본"]
    return Outcome({display_label(k): p for k, p in decision.ranked()}, status,
                   {"request": decision.request, "response": decision.response}, row)


def add_history(history: list[list], row: list | None) -> list[list]:
    if row is None:
        return history
    return ([row] + history)[:HISTORY_LIMIT]
