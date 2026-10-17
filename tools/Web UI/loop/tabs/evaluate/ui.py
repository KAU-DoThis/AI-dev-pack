"""평가 탭: 시험셋 + 모델들 → 한 문장 판정(학습 전보다 몇 문제 더 맞혔나) + 시험셋별 점수 기록.

탭을 누를 때마다 모델·시험셋을 새로 맞춘다: 학습 전 + 마지막 학습 모델, 그 학습에 쓴 데이터의 시험셋.
처음 쓰는 사람은 평가 실행만 누르면 되게, 표·칸 설명은 '자세히'에 접어 둔다.
"""

import gradio as gr

from loop.core import nav
from loop.core.engine import get_engine
from loop.tabs.evaluate import logic

COLUMN_HELP = (
    "- **정확도**: 맞힌 비율 (1 = 전부 맞힘). 높을수록 좋음\n"
    "- **nll**: 정답에 낮은 확률을 줄수록 커지는 벌점. 낮을수록 좋음\n"
    "- **평균 확신도**: 모델이 자기 답에 건 확률의 평균. 정확도보다 훨씬 높으면 틀리면서도 자신만만한 것\n"
    "- **평균 ms**: 한 문제 판단에 걸린 시간"
)


def _models(current=None):
    names = get_engine().models()
    keep = [name for name in (current or []) if name in names]
    return gr.update(choices=names, value=keep or logic.default_models(names))


def _tests(current=None):
    choices = logic.test_choices()
    return gr.update(choices=choices, value=current if current in choices else logic.default_test(choices))


def _history(test_file):
    return logic.history(test_file), logic.history_status(test_file)


def _show():
    """탭을 열 때: 지금 서버 모델 목록 + 기본 선택, 기본 시험셋, 그 시험셋 기록. 판정·다음 버튼은 비움."""
    names = get_engine().models()
    choices = logic.test_choices()
    test = logic.default_test(choices)
    return (gr.update(choices=names, value=logic.default_models(names)), gr.update(choices=choices, value=test),
            *_history(test), "", gr.update(visible=False))


def _run(test_file, models):
    table, message = logic.run(test_file, models)
    names = get_engine().models()
    base = names[0] if names else (models[0] if models else "")
    history, status = _history(test_file)
    if not message.startswith("✅"):
        return history, f"{message}\n\n{status}", "", gr.update(visible=False)
    return history, status, logic.verdict(table, base), gr.update(visible=True)


def build() -> list:
    gr.Markdown("### ③ 평가\n**할 일:** **평가 실행**만 누르면 돼. 시험셋(학습 때 한 번도 안 보여준 문제)을 "
                "학습 전 모델과 방금 학습한 모델이 똑같이 풀어서, 몇 개 맞히는지 비교해.")
    with gr.Row(equal_height=False):
        with gr.Column(scale=2):
            run_btn = gr.Button("평가 실행", variant="primary", size="lg")
            next_btn = nav.next_button("inference", "④ 추론 테스트로 직접 물어보기 →")
            with gr.Accordion("시험셋·모델 바꾸기 (모르면 그대로)", open=False):
                test_file = gr.Dropdown([], label="시험셋 파일 (.jsonl)", allow_custom_value=True,
                                        info="기본 = 마지막 학습에 쓴 데이터셋의 시험셋. 맨 뒤 smoke-train 은 견본(동작 확인용).")
                with gr.Row(equal_height=True):
                    models = gr.Dropdown([], multiselect=True, label="모델", scale=4,
                                         info="맨 앞 = 기본(학습 전). 기본으로 학습 전 + 마지막에 학습한 모델이 골라져 있음.")
                    refresh_btn = gr.Button("🔄", size="sm", scale=0, min_width=40)
        with gr.Column(scale=3):
            result = gr.Markdown("")
            status = gr.Markdown("⏳ 불러오는 중…")
            with gr.Accordion("자세히: 점수 기록과 칸 설명", open=False):
                gr.Markdown(COLUMN_HELP)
                table = gr.Dataframe(headers=logic.HISTORY_COLUMNS, type="array", interactive=False,
                                     label="이 시험셋의 평가 기록 (최근 것 먼저)")

    run_btn.click(_run, inputs=[test_file, models], outputs=[table, status, result, next_btn])
    refresh_btn.click(_models, inputs=models, outputs=models)
    models.focus(_models, inputs=models, outputs=models, show_progress="hidden")
    test_file.focus(_tests, inputs=test_file, outputs=test_file, show_progress="hidden")
    test_file.change(_history, inputs=test_file, outputs=[table, status], show_progress="hidden")
    # 페이지 열 때 + 이 탭을 누를 때마다 ("show") — 다른 탭에서 방금 학습·분할한 것을 바로 고르게
    return [(_show, None, [models, test_file, table, status, result, next_btn], "show")]
