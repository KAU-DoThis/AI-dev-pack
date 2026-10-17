"""학습 탭: 데이터셋 고르기 → 학습 시작 → loss·개발셋 점수 그래프와 로그가 2초마다 갱신. 끝나면 "③ 평가 →" 버튼.

처음 쓰는 사람은 데이터셋과 시작 버튼만 보면 되게, 이름·숫자 설정은 '고급 설정'에 접어 둔다 (이름은 비우면 자동).
시험셋(test.jsonl)은 여기서 읽지 않는다. 그래프는 바뀐 게 있을 때만 다시 그리고, 축은 처음부터 고정해서 선만 늘어나게 한다.
"""

import gradio as gr

from loop.core import nav
from loop.core.engine import get_engine
from loop.tabs.dataset import logic as dataset
from loop.tabs.train import logic

REFRESH_EVERY = 2  # 초
ACCURACY_Y = [0, 1.05]


def _draw(s, data_dir, message=None):
    shown, loss, accuracy, nll, log = logic.view(s)
    x, loss_y, nll_y = logic.axes(s)
    fresh = not logic.trained_before(data_dir) and s.state != "running"
    if fresh:  # 그래프는 지난 학습 것 — 고른 데이터셋은 아직이라고 먼저 말한다
        shown = (f"👉 고른 데이터셋 `{data_dir}` 은(는) 아직 학습 안 했어. **학습 시작**을 눌러."
                 + (f"\n\n_아래 그래프는 지난 학습 `{s.name}`_" if s.name else ""))
    elif logic.needs_training(data_dir) and s.state != "running":  # 전에 학습했지만 마지막 학습은 다른 데이터셋
        shown = f"_이 데이터셋은 전에 학습했어. 아래는 마지막 학습 `{s.name}` (다른 데이터셋)._\n\n{shown}"
    return (message or shown, gr.update(value=loss, x_lim=x, y_lim=loss_y),
            gr.update(value=accuracy, x_lim=x, y_lim=ACCURACY_Y), gr.update(value=nll, x_lim=x, y_lim=nll_y),
            log, (logic.signature(s), data_dir),
            gr.update(visible=s.state == "done" and not fresh))


def _refresh(last, data_dir):
    s = logic.status()
    if (logic.signature(s), data_dir) == last:
        return gr.skip(), gr.skip(), gr.skip(), gr.skip(), gr.skip(), last, gr.skip()
    return _draw(s, data_dir)


def _data_choices(current):
    """드롭다운을 누를 때: 목록 새로, 고르던 건(직접 적은 경로도) 그대로."""
    choices = dataset.data_choices()
    return gr.update(choices=choices, value=current or (choices[0] if choices else logic.SAMPLE_DATA))


def _on_show(current, seen):
    """탭을 열 때: 데이터셋 탭에서 새 데이터셋을 만들었으면 그걸로, 아니면 고르던 것 그대로.
    seen = 지난번에 본 가장 최근 데이터셋 (바뀌었을 때만 옮겨서, 일부러 고른 옛 데이터셋을 덮지 않게)."""
    choices = dataset.data_choices()
    newest = choices[0] if choices else logic.SAMPLE_DATA
    value = newest if newest != seen or not current else current
    return gr.update(choices=choices, value=value), newest


def _start(name, data_dir, *args):
    message = logic.start(name, data_dir, *args)
    return _draw(logic.status(), data_dir, message if message.startswith("❌") else None)


def build() -> list:
    engine = get_engine().name
    gr.Markdown(f"### ② 학습\n**할 일:** 데이터셋을 고르고 **학습 시작**. 1분쯤 걸려. "
                f"{engine}가 학습 파일을 보면서 정답 고르는 법을 배우고, 끝나면 새 모델이 하나 생겨.")
    with gr.Row(equal_height=False):
        with gr.Column(scale=2):
            choices = dataset.data_choices()
            data_dir = gr.Dropdown(choices, value=choices[0] if choices else logic.SAMPLE_DATA, label="데이터셋",
                                   allow_custom_value=True,
                                   info="① 데이터셋 탭에서 만든 것 (맨 위 = 가장 최근). 맨 뒤 smoke-train 은 견본(동작 확인용).")
            with gr.Row():
                start_btn = gr.Button("학습 시작", variant="primary", size="lg")
                stop_btn = gr.Button("멈추기", size="lg")
            next_btn = nav.next_button("evaluate", "③ 평가하러 가기 →")
            with gr.Accordion("고급 설정 (모르면 그대로 둬도 돼)", open=False):
                name = gr.Textbox("", label="이름", placeholder="비우면 자동: <데이터셋>-<날짜시각>",
                                  info="새 모델 이름 (영문·숫자·-·_). 같은 이름은 다시 못 씀.")
                epochs = gr.Slider(1, 10, value=2, step=1, label="에폭", info="데이터를 몇 바퀴 볼지. 많으면 외워 버림(과적합)")
                lr = gr.Number(1e-4, label="학습률", info="크면 빨리 배우지만 망가지기 쉬움")
                batch = gr.Slider(1, 16, value=4, step=1, label="배치", info="GPU에 한 번에 넣는 줄 수. 메모리 부족하면 줄이기")
                effective_batch = gr.Slider(1, 64, value=8, step=1, label="실제 배치", info="여러 번 모아서 한 번 고침")
                rank = gr.Slider(4, 64, value=16, step=4, label="LoRA rank", info="클수록 많이 배우지만 무거움")
        with gr.Column(scale=3):
            status = gr.Markdown("⏳ 상태 확인 중…")
            loss_plot = gr.LinePlot(x="step", y="loss", label="loss (낮을수록 학습 데이터를 잘 맞춤, 매 step 기록)", height=220)
            # 한 그래프에 색으로 나누면 Gradio 6 에서 학습 중에 안 그려짐 → 정확도·nll 따로
            accuracy_plot = gr.LinePlot(x="step", y="accuracy", height=200,
                                        label="개발셋 정확도 (몇 step 마다 시험, 높을수록 좋음)")
            nll_plot = gr.LinePlot(x="step", y="nll", height=200, label="개발셋 nll (낮을수록 좋음)")
            with gr.Accordion("로그 (끝부분)", open=False):
                log = gr.Textbox(show_label=False, lines=10, max_lines=10, interactive=False, autoscroll=True)

    last = gr.State(None)  # 마지막으로 그린 상태 (같으면 안 다시 그림)
    seen = gr.State(choices[0] if choices else logic.SAMPLE_DATA)  # 지난번에 본 가장 최근 데이터셋
    outputs = [status, loss_plot, accuracy_plot, nll_plot, log, last, next_btn]
    start_btn.click(_start, inputs=[name, data_dir, epochs, lr, batch, effective_batch, rank], outputs=outputs)
    stop_btn.click(logic.stop, outputs=status)
    gr.Timer(REFRESH_EVERY).tick(_refresh, inputs=[last, data_dir], outputs=outputs, show_progress="hidden")
    data_dir.change(_refresh, inputs=[last, data_dir], outputs=outputs, show_progress="hidden")
    data_dir.focus(_data_choices, inputs=data_dir, outputs=data_dir, show_progress="hidden")  # 새로 만든 데이터셋 반영
    # 페이지 열자마자 한 번 그리기 (타이머 첫 tick 을 안 기다리게) + 탭을 누를 때마다 데이터셋 목록 새로
    return [(_on_show, [data_dir, seen], [data_dir, seen], "show"), (_refresh, [last, data_dir], outputs)]
