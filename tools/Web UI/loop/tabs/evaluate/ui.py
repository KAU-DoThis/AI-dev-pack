"""평가 탭: 시험셋 + 모델들 → 한 문장 판정(학습 전보다 몇 문제 더 맞혔나) + 시험셋별 점수 기록.

탭을 누를 때마다 모델·시험셋을 새로 맞춘다: 학습 전 + 마지막 학습 모델, 그 학습에 쓴 데이터의 시험셋.
처음 쓰는 사람은 평가 실행만 누르면 되게, 표·칸 설명은 '자세히'에 접어 둔다.
문턱 정하기는 보정셋만 쓰고 시험 기록에 안 쌓인다.
"""

import gradio as gr

from loop.core import nav
from loop.core.config import action_labels
from loop.core.engine import get_engine
from loop.tabs.evaluate import logic

COLUMN_HELP = (
    "- **정확도**: 맞힌 비율 (1 = 전부 맞힘). 높을수록 좋음\n"
    "- **nll**: 정답에 낮은 확률을 줄수록 커지는 벌점. 낮을수록 좋음\n"
    "- **평균 확신도**: 모델이 자기 답에 건 확률의 평균. 정확도보다 훨씬 높으면 틀리면서도 자신만만한 것. 문턱을 써도 문턱 적용 전 답 기준\n"
    "- **평균 ms**: 한 문제 판단에 걸린 시간\n"
    "- **라벨별 정밀·재현**: 라벨 정밀도/재현율. 정밀도 = 그 라벨이라고 답한 것 중 맞은 비율, 재현율 = 그 라벨인 것 중 찾아낸 비율"
)
SWEEP_HELP = (
    "**문턱이란:** 대상 라벨 확률이 문턱 이상이면 대상 라벨로 답해. 낮출수록 대상 라벨을 덜 놓치는 대신, 다른 라벨을 대상으로 잘못 보내는 일이 늘어.\n\n"
    "**보정셋으로만** 비교해 (시험셋으로 고르면 시험 점수가 부풀어). 이 비교는 시험 횟수에 안 들어가. "
    "보정셋과 실제 데이터의 성격이 다르면 문턱도 실제에서 안 맞을 수 있으니, 정한 문턱은 시험셋으로 확인해."
)
WRONG_WARNING = "⚠️ 시험셋의 틀린 줄을 보고 데이터·설정을 고치면 이 시험셋은 개발용이 돼. 최종 점수는 새 시험셋으로 내."


def _models(current=None):
    names = get_engine().models()
    keep = [name for name in (current or []) if name in names]
    return gr.update(choices=names, value=keep or logic.default_models(names))


def _tests(current=None):
    choices = logic.test_choices()
    return gr.update(choices=choices, value=current if current in choices else logic.default_test(choices))


def _history(test_file):
    return logic.history(test_file), logic.history_status(test_file)


def _default_sweep_model(names):
    defaults = logic.default_models(names)
    return defaults[-1] if defaults else None


def _sweep_models(current=None):
    names = get_engine().models()
    return gr.update(choices=names, value=current if current in names else _default_sweep_model(names))


def _calibration_for(model):
    choices = logic.calibration_choices()
    return gr.update(choices=choices, value=logic.default_calibration(model or "", choices) or None)


def _show():
    """탭을 열 때: 지금 서버 모델 목록 + 기본 선택, 기본 시험셋, 그 시험셋 기록. 판정·다음 버튼·이번 결과는 비움."""
    names = get_engine().models()
    choices = logic.test_choices()
    test = logic.default_test(choices)
    sweep_model = _default_sweep_model(names)
    return (gr.update(choices=names, value=logic.default_models(names)), gr.update(choices=choices, value=test),
            *_history(test), "", gr.update(visible=False), "", [],
            gr.update(choices=names, value=sweep_model), _calibration_for(sweep_model))


def _threshold_value(value):
    """빈칸·0 = 문턱 안 씀 (모델이 고른 답 그대로)."""
    return float(value) if value else None


def _run(test_file, models, target, threshold):
    report = logic.run(test_file, models, target=target or "", threshold=_threshold_value(threshold))
    names = get_engine().models()
    base = names[0] if names else (models[0] if models else "")
    history, status = _history(test_file)
    if not report.status.startswith("✅"):
        return history, f"{report.status}\n\n{status}", "", gr.update(visible=False), report.details, report.wrong
    return history, status, logic.verdict(report.table, base), gr.update(visible=True), report.details, report.wrong


def build() -> list:
    gr.Markdown("### ③ 평가\n**할 일:** **평가 실행**만 누르면 돼. 시험셋(학습 때 한 번도 안 보여준 문제)을 "
                "학습 전 모델과 방금 학습한 모델이 똑같이 풀어서, 몇 개 맞히는지 비교해.")
    labels = action_labels()
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
                with gr.Row(equal_height=True):
                    target = gr.Dropdown(labels, value=logic.review_label(), label="대상 라벨",
                                         info="합격선과 문턱을 볼 라벨.")
                    threshold = gr.Number(None, label="문턱", minimum=0, maximum=0.99, step=0.05,
                                          info="비우면 모델이 고른 답 그대로. 값은 아래 '문턱 정하기'에서 정하고, 쓸 땐 모델을 하나만 골라.")
        with gr.Column(scale=3):
            result = gr.Markdown("")
            status = gr.Markdown("⏳ 불러오는 중…")
            with gr.Accordion("라벨별 점수·혼동표", open=False):
                details = gr.Markdown("")
            with gr.Accordion("틀린 줄", open=False):
                gr.Markdown(WRONG_WARNING)
                wrong = gr.Dataframe(headers=logic.WRONG_COLUMNS, type="array", interactive=False,
                                     label="이번 평가에서 틀린 줄 (자신 있게 틀린 것 먼저)")
            with gr.Accordion("자세히: 점수 기록과 칸 설명", open=False):
                gr.Markdown(COLUMN_HELP)
                table = gr.Dataframe(headers=logic.HISTORY_COLUMNS, type="array", interactive=False,
                                     label="이 시험셋의 평가 기록 (최근 것 먼저)")

    with gr.Accordion("문턱 정하기 (보정셋)", open=False):
        gr.Markdown(SWEEP_HELP)
        with gr.Row(equal_height=True):
            sweep_model = gr.Dropdown([], label="모델", info="이 모델을 학습한 데이터의 보정셋이 자동으로 골라져.")
            calibration = gr.Dropdown([], label="보정셋", info="데이터셋 탭에서 만든 calibration.jsonl 만.")
        sweep_btn = gr.Button("문턱별로 비교하기", variant="secondary")
        sweep_message = gr.Markdown("")
        sweep_table = gr.Dataframe(headers=logic.SWEEP_COLUMNS, type="array", interactive=False,
                                   label="문턱별 점수 (보정셋)")

    run_btn.click(_run, inputs=[test_file, models, target, threshold],
                  outputs=[table, status, result, next_btn, details, wrong])
    refresh_btn.click(_models, inputs=models, outputs=models)
    models.focus(_models, inputs=models, outputs=models, show_progress="hidden")
    test_file.focus(_tests, inputs=test_file, outputs=test_file, show_progress="hidden")
    test_file.change(_history, inputs=test_file, outputs=[table, status], show_progress="hidden")
    sweep_btn.click(logic.run_sweep, inputs=[calibration, sweep_model, target], outputs=[sweep_table, sweep_message])
    sweep_model.focus(_sweep_models, inputs=sweep_model, outputs=sweep_model, show_progress="hidden")
    sweep_model.change(_calibration_for, inputs=sweep_model, outputs=calibration, show_progress="hidden")
    # 페이지 열 때 + 이 탭을 누를 때마다 ("show") — 다른 탭에서 방금 학습·분할한 것을 바로 고르게
    return [(_show, None, [models, test_file, table, status, result, next_btn, details, wrong, sweep_model, calibration], "show")]
