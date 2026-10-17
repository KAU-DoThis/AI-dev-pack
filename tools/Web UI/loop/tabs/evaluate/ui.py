"""평가 탭 (최소 버전): 시험셋 파일 + 모델들 → 모델별 점수 표."""

import gradio as gr

from loop.core.engine import get_engine
from loop.tabs.evaluate import logic


def _models(current=None):
    names = get_engine().models()
    keep = [name for name in (current or []) if name in names]
    return gr.update(choices=names, value=keep or names)


def build() -> None:
    gr.Markdown("### 평가\n시험셋(학습·체크포인트 고르기에 안 쓴 데이터)을 한 줄씩 판단시켜 정답과 비교. 모델 여러 개를 한 표로.")
    names = get_engine().models()
    with gr.Row(equal_height=False):
        with gr.Column(scale=2):
            test_file = gr.Textbox(logic.SAMPLE_TEST, label="시험셋 파일 (.jsonl)",
                                   info="지금 값은 견본(동작 확인용, 고객 문의 분류 15줄). 프로젝트 점수가 아님.")
            with gr.Row(equal_height=True):
                models = gr.Dropdown(names, value=names, multiselect=True, label="모델", scale=4,
                                     info="맨 앞 = 기본(학습 전). 학습 탭에서 만든 것과 비교.")
                refresh_btn = gr.Button("🔄", size="sm", scale=0, min_width=40)
            run_btn = gr.Button("평가 실행", variant="primary", size="lg")
        with gr.Column(scale=3):
            status = gr.Markdown("시험셋과 모델을 고르고 **평가 실행**을 눌러 줘.")
            table = gr.Dataframe(headers=logic.COLUMNS, type="array", interactive=False, label="모델별 점수")

    run_btn.click(logic.run, inputs=[test_file, models], outputs=[table, status])
    refresh_btn.click(_models, inputs=models, outputs=models)
