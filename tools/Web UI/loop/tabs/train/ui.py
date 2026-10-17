"""학습 탭: 데이터 폴더·설정 → 학습 시작 → loss·개발셋 점수 그래프와 로그가 2초마다 갱신.

끝나면 결과 모델이 추론 탭 '모델' 목록에 나온다. 시험셋(test.jsonl)은 여기서 읽지 않는다.
그래프는 바뀐 게 있을 때만 다시 그리고, 축은 처음부터 고정해서 선만 늘어나게 한다.
"""

import gradio as gr

from loop.core.engine import get_engine
from loop.tabs.train import logic

REFRESH_EVERY = 2  # 초
EVAL_COLORS = {"accuracy": "#2563eb", "nll": "#f97316"}  # 그래프 제목에 색 이름을 적음 (범례는 잘림). 한글 이름은 선이 안 그려져서 영문


def _draw(s, message=None):
    shown, loss, evals, log = logic.view(s)
    x, loss_y, eval_y = logic.axes(s)
    return (message or shown, gr.update(value=loss, x_lim=x, y_lim=loss_y),
            gr.update(value=evals, x_lim=x, y_lim=eval_y), log, logic.signature(s))


def _refresh(last):
    s = logic.status()
    if logic.signature(s) == last:
        return gr.skip(), gr.skip(), gr.skip(), gr.skip(), last
    return _draw(s)


def _start(*args):
    message = logic.start(*args)
    return _draw(logic.status(), message if message.startswith("❌") else None)


def build() -> list:
    engine = get_engine().name
    gr.Markdown(f"### 학습\n{engine} 베이스에 LoRA 어댑터를 붙여 학습. 끝나면 **추론 탭 → 모델**에서 골라 학습 전과 비교.")
    with gr.Row(equal_height=False):
        with gr.Column(scale=2):
            name = gr.Textbox(logic.default_name(), label="이름", info="결과 모델 이름 (영문·숫자·-·_). 같은 이름은 다시 못 씀.")
            data_dir = gr.Textbox(logic.SAMPLE_DATA, label="데이터 폴더",
                                  info="train / development / calibration .jsonl 이 있는 폴더. 지금 값은 견본(동작 확인용). "
                                       "test.jsonl 은 있어도 안 읽음.")
            with gr.Group():
                epochs = gr.Slider(1, 10, value=2, step=1, label="에폭", info="데이터를 몇 바퀴 볼지. 많으면 외워 버림(과적합)")
                lr = gr.Number(1e-4, label="학습률", info="크면 빨리 배우지만 망가지기 쉬움")
                batch = gr.Slider(1, 16, value=4, step=1, label="배치", info="GPU에 한 번에 넣는 줄 수. 메모리 부족하면 줄이기")
                effective_batch = gr.Slider(1, 64, value=8, step=1, label="실제 배치", info="여러 번 모아서 한 번 고침")
                rank = gr.Slider(4, 64, value=16, step=4, label="LoRA rank", info="클수록 많이 배우지만 무거움")
            with gr.Row():
                start_btn = gr.Button("학습 시작", variant="primary", size="lg")
                stop_btn = gr.Button("멈추기", size="lg")
        with gr.Column(scale=3):
            status = gr.Markdown("⏳ 상태 확인 중…")
            loss_plot = gr.LinePlot(x="step", y="loss", label="loss (낮을수록 학습 데이터를 잘 맞춤)", height=220)
            eval_plot = gr.LinePlot(x="step", y="value", color="metric", y_title="값", color_title="지표",
                                    color_map=EVAL_COLORS, height=270,  # 범례가 아래에 붙어서 loss 보다 높게
                                    label="개발셋: 파랑 = 정확도 (높을수록 좋음) · 주황 = nll (낮을수록 좋음)")
            log = gr.Textbox(label="로그 (끝부분)", lines=10, max_lines=10, interactive=False, autoscroll=True)

    last = gr.State(None)  # 마지막으로 그린 상태 (같으면 안 다시 그림)
    outputs = [status, loss_plot, eval_plot, log, last]
    start_btn.click(_start, inputs=[name, data_dir, epochs, lr, batch, effective_batch, rank], outputs=outputs)
    stop_btn.click(logic.stop, outputs=status)
    gr.Timer(REFRESH_EVERY).tick(_refresh, inputs=last, outputs=outputs, show_progress="hidden")
    return [(_refresh, [last], outputs)]  # 페이지 열자마자 한 번 그리기 (타이머 첫 tick 을 안 기다리게)
