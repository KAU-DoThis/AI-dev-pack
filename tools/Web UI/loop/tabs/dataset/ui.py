"""데이터셋 탭: 불러오기·검사 → 나누기·검사·잠그기 → "② 학습 →". 만든 데이터셋은 학습 탭에 자동으로 골라진다.

처음 쓰는 사람은 파일과 버튼 두 개만 보면 되게, 이름·비율·seed 는 '고급 설정'에 접어 둔다."""

import gradio as gr

from loop.core import nav
from loop.core.config import action_labels, load_profile
from loop.tabs.dataset import logic


def _split(name, test, development, calibration, seed, token):
    message, report = logic.split(name, test, development, calibration, seed, token=token or "")
    done = message.startswith("✅")
    next_name = logic.default_name() if done else gr.skip()  # 실패하면 적은 이름 그대로
    ready = done and "누수 검사 실패" not in message  # 누수 실패한 데이터는 학습 탭에서 막히니 넘어가라고 안 함
    return message, report or "_보고서 없음_", logic.table(), next_name, gr.update(visible=ready)


def _next_visible():
    return gr.update(visible=any(choice != logic.SAMPLE_DATA for choice in logic.data_choices()))


def build() -> list:
    item = load_profile()["item"]
    gr.Markdown(f"### ① 데이터셋\n**할 일:** 파일 넣기 → **불러오기·검사** → **나누기·검사·잠그기**. "
                f"{item}마다 `state`(내용)와 `label`(정답: {', '.join(f'`{l}`' for l in action_labels())})만 있으면 돼. "
                "나머지 칸은 자동으로 채우고, 학습용·확인용·**시험용**(학습 때 안 보여주는 문제)으로 나눠.")
    with gr.Row(equal_height=False):
        with gr.Column(scale=2):
            file = gr.File(label="데이터 파일 (.jsonl, 한 줄에 하나)", file_types=[".jsonl"], type="filepath")
            load_btn = gr.Button("불러오기·검사", variant="primary", size="lg")
            split_btn = gr.Button("나누기·검사·잠그기", variant="primary", size="lg")
            next_btn = nav.next_button("train", "② 학습하러 가기 →")
            with gr.Accordion("고급 설정 (모르면 그대로 둬도 돼)", open=False):
                name = gr.Textbox(logic.default_name(), label="이름", info="저장 폴더 이름 (영문·숫자·-·_). 같은 이름은 다시 못 씀.")
                test = gr.Slider(0.05, 0.3, value=0.15, step=0.05, label="시험 비율", info="진짜 점수용. 잠가서 학습에 안 씀")
                development = gr.Slider(0.05, 0.3, value=0.15, step=0.05, label="개발 비율",
                                        info="학습 중 제일 좋은 체크포인트 고르기")
                calibration = gr.Slider(0.05, 0.3, value=0.15, step=0.05, label="보정 비율",
                                        info="확률 보정 (0.7 이라 하면 실제로 70% 맞게)")
                seed = gr.Number(7, label="seed", precision=0, info="같은 seed 면 같은 결과로 나뉨")
        with gr.Column(scale=3):
            status = gr.Markdown("파일을 고르고 **불러오기·검사**를 눌러.")
            preview = gr.Dataframe(logic.preview([]), label=f"미리보기 (앞 {logic.PREVIEW_ROWS}줄)", interactive=False,
                                   wrap=True, max_height=320)
            with gr.Accordion("지름길 보고서 (정답이 겉모양으로 새는지)", open=False):
                report = gr.Markdown("_분할하면 여기에 나와._")
    gr.Markdown("#### 만든 데이터셋")
    datasets = gr.Dataframe(logic.table(), interactive=False)

    token = gr.State("")  # ① 이 통과시킨 내용의 해시. 파일을 바꾸면 비워서 ① 부터 다시 하게
    file.change(lambda _: "", inputs=file, outputs=token)
    load_btn.click(logic.load, inputs=file, outputs=[status, preview, token])
    split_btn.click(_split, inputs=[name, test, development, calibration, seed, token],
                    outputs=[status, report, datasets, name, next_btn])
    # 새로고침해도 학습할 수 있는 데이터셋이 있으면 "② 학습 →" 를 보여 둔다
    return [(logic.table, None, datasets), (_next_visible, None, next_btn, "show")]
