import gradio as gr


def _not_ready(*_):
    return "아직 구현 안 됨"


def build() -> None:
    gr.Markdown("### 데이터셋\n시드 생성 → 불러오기 → 라벨 확인 → 분할")
    with gr.Row():
        with gr.Column():
            file = gr.File(label="데이터셋 파일 (JSONL)", file_types=[".jsonl"])
            split = gr.Slider(0.5, 0.9, value=0.8, step=0.05, label="학습 데이터 비율 (나머지는 검증·테스트)")
            load_btn = gr.Button("불러오기")
        with gr.Column():
            preview = gr.Dataframe(label="미리보기", interactive=False)
            status = gr.Textbox(label="상태", interactive=False)
    load_btn.click(_not_ready, inputs=[file, split], outputs=status)
