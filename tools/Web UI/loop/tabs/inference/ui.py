"""추론 테스트 탭: 입력 JSON → 엔진 판단(선택지별 확률).

SD WebUI 처럼: 왼쪽 입력(예시·입력 JSON·선택지 설명), 오른쪽 큰 버튼과 결과, 아래 결과 기록.
"""

import gradio as gr

from loop.core.config import LANGUAGES, PROMPT_FILES, action_names, load_examples, load_profile, load_prompt, save_prompt
from loop.core.engine import get_engine
from loop.core.paths import ROOT
from loop.tabs.inference import logic


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
    """모델 목록 새로 받기. 고르던 게 아직 있으면 그대로, 없으면 맨 앞(기본 = 학습 전)."""
    names = get_engine().models()
    return gr.update(choices=names, value=current if current in names else (names[0] if names else None))


def _judge(raw, rows, instructions, double_check, history, lang, model):
    outcome = logic.run(raw, rows, instructions, double_check, lang=lang, model=model or "")
    history = logic.add_history(history, outcome.row)
    return outcome.label, outcome.status, outcome.detail, history, history


def _judge_all(rows, instructions, double_check, history, lang, model):
    """예시 전부를 지금 설명으로 한 번에 돌린다. 설명을 고친 뒤 비교할 때 쓴다."""
    if not load_examples():
        return None, "⚠️ 이 프로필에는 예시가 없어 (profiles/<이름>/examples.yaml).", None, history, history
    last = None
    for example in reversed(load_examples()):  # 기록 맨 위가 첫 예시가 되게
        last = logic.run(logic.example_json(example["name"]), rows, instructions, double_check, lang=lang,
                         model=model or "")
        if last.row is None:  # 서버 꺼짐 등: 나머지도 똑같이 실패하니 멈춘다
            return None, last.status, None, history, history
        history = logic.add_history(history, last.row)
    done = history[:len(load_examples())]
    hits = sum(1 for row in done if row[6] == "✅")
    status = f"예시 {len(done)}개 실행 ({LANGUAGES[lang]} 설명, 모델 `{model or '기본'}`) · 기대값 일치 {hits}/{len(done)}"
    return last.label, status, last.detail, history, history


def _save(rows, instructions, lang, drafts):
    save_prompt(instructions.strip(), logic.options_from_rows(rows), lang)
    drafts = {k: v for k, v in drafts.items() if k != lang}
    return f"💾 {PROMPT_FILES[lang].relative_to(ROOT).as_posix()} 에 저장했어 ({LANGUAGES[lang]}). 다음에 켜도 이 설명으로 시작해.", drafts


def _reset(lang, drafts):
    drafts = {k: v for k, v in drafts.items() if k != lang}
    return _prompt_rows(lang), load_prompt(lang)["instructions"], f"↩️ 저장된 {LANGUAGES[lang]} 설명으로 되돌렸어.", drafts


def build() -> None:
    names = logic.example_names()
    first = names[0] if names else None
    prompt = load_prompt()
    profile = load_profile()
    engine = get_engine().name  # 화면 말에 들어갈 모델 이름
    item = profile["item"]
    source = f" ({profile['examples_source']})" if profile["examples_source"] else ""

    with gr.Row(equal_height=False):
        with gr.Column(scale=3):
            example = gr.Dropdown(names, value=first, label=f"예시 {item}{source}",
                                  info="고르면 아래 입력이 바뀜. 시연·확인용이고 평가 시험셋에는 안 씀.")
            raw = gr.Code(logic.example_json(first) if first else "{}", language="json",
                          label=f"{item} 입력 (JSON) — 직접 고쳐도 됨", lines=14)
            with gr.Accordion(f"{engine}에 보내는 설명 — 고치고 바로 다시 판단해 볼 수 있음", open=True):
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
            with gr.Row(equal_height=True):
                names = get_engine().models()
                model = gr.Dropdown(names, value=names[0] if names else None, label="모델", scale=4,
                                    info="맨 앞 = 기본(학습 전). 그 뒤 = 학습 탭에서 만든 것. 같은 예시로 바꿔 가며 비교.")
                refresh_btn = gr.Button("🔄", size="sm", scale=0, min_width=40)
            run_btn = gr.Button("판단하기", variant="primary", size="lg")
            with gr.Row():
                run_all_btn = gr.Button("예시 전부 돌리기", size="sm")
                double_check = gr.Checkbox(False, label="순서 뒤집어 한 번 더 (치우침↓, 2배 느림)")
            label = gr.Label(label=f"{engine} 판단 (선택지별 확률)", num_top_classes=10)
            status = gr.Markdown("예시를 고르고 **판단하기**를 눌러 줘.")
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
    outputs = [label, status, detail, history_state, history]
    lang.change(_switch, inputs=[lang, lang_state, instructions, rows, drafts],
                outputs=[instructions, rows, lang_state, drafts, prompt_msg])
    run_btn.click(_judge, inputs=[raw, rows, instructions, double_check, history_state, lang, model], outputs=outputs)
    run_all_btn.click(_judge_all, inputs=[rows, instructions, double_check, history_state, lang, model], outputs=outputs)
    refresh_btn.click(_models, inputs=model, outputs=model)
    model.focus(_models, inputs=model, outputs=model)  # 학습 끝난 뒤 드롭다운만 눌러도 새 모델이 보이게
    save_btn.click(_save, inputs=[rows, instructions, lang, drafts], outputs=[prompt_msg, drafts])
    reset_btn.click(_reset, inputs=[lang, drafts], outputs=[rows, instructions, prompt_msg, drafts])
    clear_btn.click(lambda: ([], []), outputs=[history_state, history])
