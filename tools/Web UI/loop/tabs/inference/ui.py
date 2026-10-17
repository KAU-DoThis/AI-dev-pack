"""추론 테스트 탭: 입력 JSON → 엔진 판단(선택지별 확률). 학습 전(A)과 학습한 모델(B)을 나란히 비교.

SD WebUI 처럼: 왼쪽 입력(예시·입력 JSON·선택지 설명), 오른쪽 큰 버튼과 A·B 결과, 아래 결과 기록.
"""

import gradio as gr

from loop.core.config import LANGUAGES, PROMPT_FILES, action_names, load_examples, load_profile, load_prompt, save_prompt
from loop.core.engine import get_engine
from loop.core.paths import ROOT
from loop.tabs.evaluate.logic import default_models
from loop.tabs.inference import logic

NO_COMPARE = "(비교 안 함)"


def _prompt_rows(lang: str = "en") -> list[list]:
    prompt, names = load_prompt(lang), action_names()
    return [[key, names.get(key, ""), desc] for key, desc in prompt["options"].items()]


def _switch(new_lang, old_lang, instructions, rows, drafts):
    """언어 전환. 저장 안 한 수정은 언어별로 임시 보관했다가 돌아오면 되살린다 (새로고침하면 사라짐)."""
    drafts = {**drafts, old_lang: (instructions, rows)}
    if new_lang in drafts:
        new_instructions, new_rows = drafts[new_lang]
    else:
        new_instructions, new_rows = load_prompt(new_lang)["instructions"], _prompt_rows(new_lang)
    note = "" if PROMPT_FILES[new_lang].exists() else f"⚠️ {PROMPT_FILES[new_lang].name} 가 없어서 빈 칸이야. 채우고 💾 저장해 줘."
    return new_instructions, new_rows, new_lang, drafts, note


def _models(current=None):
    """A 모델 목록 새로 받기. 고르던 게 아직 있으면 그대로, 없으면 맨 앞(기본 = 학습 전)."""
    names = get_engine().models()
    return gr.update(choices=names, value=current if current in names else (names[0] if names else None))


def _last_trained(names: list[str]) -> str:
    picked = default_models(names)
    return picked[1] if len(picked) == 2 else NO_COMPARE


def _models_b(current=None):
    """B 모델 목록 새로. 고르던 게 있으면 그대로, 없으면 마지막에 학습한 모델."""
    names = get_engine().models()
    choices = [NO_COMPARE, *names[1:]]
    return gr.update(choices=choices, value=current if current in choices else _last_trained(names))


def _show():
    """탭을 누를 때: A = 학습 전, B = 마지막에 학습한 모델 (다른 탭에서 방금 학습한 것)."""
    names = get_engine().models()
    return (gr.update(choices=names, value=names[0] if names else None),
            gr.update(choices=[NO_COMPARE, *names[1:]], value=_last_trained(names)))


def _both(raw, rows, instructions, double_check, lang, model_a, model_b):
    """A·B 둘 다 판단. (A 결과, B 결과 또는 None)."""
    a = logic.run(raw, rows, instructions, double_check, lang=lang, model=model_a or "")
    if a.row is None or not model_b or model_b == NO_COMPARE:
        return a, None
    return a, logic.run(raw, rows, instructions, double_check, lang=lang, model=model_b)


def _judge(raw, rows, instructions, double_check, history, lang, model_a, model_b):
    a, b = _both(raw, rows, instructions, double_check, lang, model_a, model_b)
    history = logic.add_history(logic.add_history(history, a.row), b.row if b else None)
    status = f"**A** {a.status}" + (f"\n\n**B** {b.status}" if b else "")
    return a.label, b.label if b else None, status, (b or a).detail, history, history


def _judge_all(rows, instructions, double_check, history, lang, model_a, model_b):
    """예시 전부를 지금 설명으로 A·B 둘 다 돌린다. 설명을 고친 뒤 비교할 때 쓴다."""
    examples = load_examples()
    if not examples:
        return None, None, "⚠️ 이 프로필에는 예시가 없어 (profiles/<이름>/examples.yaml).", None, history, history
    a = b = None
    hits = {"A": 0, "B": 0}
    for example in reversed(examples):  # 기록 맨 위가 첫 예시가 되게
        a, b = _both(logic.example_json(example["name"]), rows, instructions, double_check, lang, model_a, model_b)
        if a.row is None:  # 서버 꺼짐 등: 나머지도 똑같이 실패하니 멈춘다
            return None, None, a.status, None, history, history
        history = logic.add_history(logic.add_history(history, a.row), b.row if b else None)
        hits["A"] += a.row[6] == "✅"
        hits["B"] += bool(b and b.row and b.row[6] == "✅")
    status = (f"예시 {len(examples)}개 실행 ({LANGUAGES[lang]} 설명) · 기대값 일치 "
              f"A `{model_a or '기본'}` {hits['A']}/{len(examples)}"
              + (f" · B `{model_b}` {hits['B']}/{len(examples)}" if b else ""))
    return a.label, b.label if b else None, status, (b or a).detail, history, history


def _save(rows, instructions, lang, drafts):
    save_prompt(instructions.strip(), logic.options_from_rows(rows), lang)
    drafts = {k: v for k, v in drafts.items() if k != lang}
    return f"💾 {PROMPT_FILES[lang].relative_to(ROOT).as_posix()} 에 저장했어 ({LANGUAGES[lang]}). 다음에 켜도 이 설명으로 시작해.", drafts


def _reset(lang, drafts):
    drafts = {k: v for k, v in drafts.items() if k != lang}
    return _prompt_rows(lang), load_prompt(lang)["instructions"], f"↩️ 저장된 {LANGUAGES[lang]} 설명으로 되돌렸어.", drafts


def build() -> list:
    names = logic.example_names()
    first = names[0] if names else None
    prompt = load_prompt()
    profile = load_profile()
    engine = get_engine().name  # 화면 말에 들어갈 모델 이름
    item = profile["item"]
    source = f" ({profile['examples_source']})" if profile["examples_source"] else ""

    gr.Markdown(f"### ④ 추론 테스트\n**할 일:** 예시 {item}를 고르고 **판단하기**. "
                "**A = 학습 전**, **B = 방금 학습한 모델**이 같은 입력을 나란히 판단해. 입력 JSON 을 직접 고쳐서 물어봐도 돼.")
    with gr.Row(equal_height=False):
        with gr.Column(scale=3):
            example = gr.Dropdown(names, value=first, label=f"예시 {item}{source}",
                                  info="고르면 아래 입력이 바뀜. 시연·확인용이고 평가 시험셋에는 안 씀.")
            raw = gr.Code(logic.example_json(first) if first else "{}", language="json",
                          label=f"{item} 입력 (JSON) — 직접 고쳐도 됨", lines=14)
            with gr.Accordion(f"{engine}에 보내는 설명 고치기 (고급)", open=False):
                lang = gr.Radio([(name, key) for key, name in LANGUAGES.items()], value="en", label="설명 언어",
                                info="언어마다 설명 파일이 따로 있어. 같은 예시로 바꿔 가며 비교하기 (결과 기록 '설명' 칸).")
                instructions = gr.Textbox(prompt["instructions"], label="지시문", lines=3)
                rows = gr.Dataframe(_prompt_rows(), headers=["id", "이름", f"설명 ({engine}가 읽음)"],
                                    datatype=["str", "str", "str"], type="array", interactive=True,
                                    static_columns=[0, 1], wrap=True, column_widths=["12%", "18%", "70%"],
                                    label="선택지 (id·이름은 labels.yaml 에서 고침)")
                with gr.Row():
                    save_btn = gr.Button("💾 설명 저장", size="sm")
                    reset_btn = gr.Button("↩️ 저장된 걸로 되돌리기", size="sm")
                prompt_msg = gr.Markdown()

        with gr.Column(scale=2):
            run_btn = gr.Button("판단하기", variant="primary", size="lg")
            names = get_engine().models()
            with gr.Row(equal_height=False):
                with gr.Column(min_width=160):
                    model = gr.Dropdown(names, value=names[0] if names else None, label="A: 비교 기준",
                                        info="기본 = 학습 전")
                    label = gr.Label(label=f"A: {engine} 판단 (선택지별 확률)", num_top_classes=10)
                with gr.Column(min_width=160):
                    model_b = gr.Dropdown([NO_COMPARE, *names[1:]], value=_last_trained(names), label="B: 학습한 모델",
                                          info="기본 = 마지막에 학습한 것")
                    label_b = gr.Label(label=f"B: {engine} 판단 (선택지별 확률)", num_top_classes=10)
            status = gr.Markdown("예시를 고르고 **판단하기**를 눌러 줘.")
            with gr.Row():
                run_all_btn = gr.Button("예시 전부 돌리기", size="sm")
                refresh_btn = gr.Button("🔄 모델 목록", size="sm")
            double_check = gr.Checkbox(False, label="순서 뒤집어 한 번 더 (치우침↓, 2배 느림)")
            with gr.Accordion("실제로 보낸 요청 / 받은 응답", open=False):
                detail = gr.JSON(label=None)

    history_state = gr.State([])
    lang_state = gr.State("en")   # 전환 직전 언어
    drafts = gr.State({})         # 언어별 저장 안 한 수정
    with gr.Row():
        gr.Markdown("#### 결과 기록 (최근 50개, 새로고침하면 사라짐)")
        clear_btn = gr.Button("기록 지우기", size="sm", scale=0)
    history = gr.Dataframe(headers=logic.HISTORY_COLUMNS, type="array", interactive=False, wrap=True)

    example.change(logic.example_json, inputs=example, outputs=raw)
    outputs = [label, label_b, status, detail, history_state, history]
    lang.change(_switch, inputs=[lang, lang_state, instructions, rows, drafts],
                outputs=[instructions, rows, lang_state, drafts, prompt_msg])
    inputs = [raw, rows, instructions, double_check, history_state, lang, model, model_b]
    run_btn.click(_judge, inputs=inputs, outputs=outputs)
    run_all_btn.click(_judge_all, inputs=inputs[1:], outputs=outputs)
    refresh_btn.click(_models, inputs=model, outputs=model).then(_models_b, inputs=model_b, outputs=model_b)
    model.focus(_models, inputs=model, outputs=model)  # 학습 끝난 뒤 드롭다운만 눌러도 새 모델이 보이게
    model_b.focus(_models_b, inputs=model_b, outputs=model_b)
    save_btn.click(_save, inputs=[rows, instructions, lang, drafts], outputs=[prompt_msg, drafts])
    reset_btn.click(_reset, inputs=[lang, drafts], outputs=[rows, instructions, prompt_msg, drafts])
    clear_btn.click(lambda: ([], []), outputs=[history_state, history])
    # 탭을 누를 때마다 A = 학습 전, B = 마지막에 학습한 모델로 ("show")
    return [(_show, None, [model, model_b], "show")]
