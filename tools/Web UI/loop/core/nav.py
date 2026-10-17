"""처음 쓰는 사람 길잡이: 맨 위 진행 단계(① 데이터셋 → ② 학습 → ③ 평가 → ④ 추론)와 탭 사이 "다음 →" 버튼.

main.py 가 탭 묶음(gr.Tabs)을 만든 직후 set_tabs 로 알려 주고, 각 탭은 next_button 으로 다음 탭 버튼을 만든다.
"""

import gradio as gr

from loop.core.config import read_records

STEPS = [("dataset", "데이터셋"), ("train", "학습"), ("evaluate", "평가"), ("inference", "추론 테스트")]
_tabs: gr.Tabs | None = None


def set_tabs(tabs: gr.Tabs) -> None:
    global _tabs
    _tabs = tabs


def next_button(target: str, text: str) -> gr.Button:
    """누르면 target 탭으로 넘어가는 버튼. 처음엔 숨겨 두고, 그 탭의 할 일이 끝나면 보이게 한다."""
    button = gr.Button(text, variant="primary", size="lg", visible=False, elem_classes=["next-step"])
    if _tabs is not None:
        button.click(lambda: gr.Tabs(selected=target), outputs=_tabs)
    return button


def step_states(engine=None) -> list[str]:
    """단계마다 한 줄 상태. ✅ 끝 · ⏳ 진행 중 · 👉 지금 할 차례 · 비어 있으면 아직."""
    from loop.tabs.dataset import logic as dataset   # 탭 로직이 이 파일을 쓰지 않게 여기서만 부른다
    from loop.tabs.evaluate import logic as evaluate
    from loop.tabs.train import logic as train

    # 학습 탭에 나오는 데이터셋만 (누수 실패는 빼기). 5초마다 도니 해시는 안 하고 lock.json 만 읽는다.
    made = [p for p in dataset.folders() if (dataset.read_lock(p) or {}).get("leak_ok") is not False]
    status = train.status(engine)
    trained = status.state == "done" and bool(status.name)
    last = train.last_training()  # 그 학습에 쓴 데이터의 시험셋 — 견본 등 다른 시험셋으로 잰 건 안 침
    test = evaluate.test_key(str(train.data_path(last["data_dir"]) / "test.jsonl")) if last else ""
    evaluated = trained and any((r.get("score") or [""])[0] == status.name and r.get("test") == test
                                for r in read_records(evaluate.EVALS_FILE))
    first = f"✅ {made[0].name}" if made else "👉 할 차례"
    behind = bool(made) and train.needs_training(str(made[0]))  # 가장 최근 데이터셋을 아직 학습 안 함
    if status.state == "running":
        second = "⏳ 학습 중"
    elif trained and not behind:
        second = f"✅ {status.name}"
    else:
        second = "👉 할 차례" if made else ""
        trained = evaluated = False
    third = "✅ 함" if evaluated else ("👉 할 차례" if trained else "")
    fourth = "👉 해 보기" if evaluated else ("가능" if trained else "")
    return [first, second, third, fourth]


def progress_markdown(engine=None) -> str:
    try:
        states = step_states(engine)
    except Exception:  # 길잡이는 화면 장식 — 실패해도 탭은 그대로 쓰게
        states = [""] * len(STEPS)
    parts = []
    for number, ((_, name), state) in enumerate(zip(STEPS, states), 1):
        mark = f" {state}" if state else ""
        parts.append(f"**{'①②③④'[number - 1]} {name}**{mark}")
    return "  →  ".join(parts)
