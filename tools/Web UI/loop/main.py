"""웹 UI 진입점.

각 탭은 loop/tabs/<기능>/ui.py 의 build() 가 만든다. 여기서는 탭을 모으고 맨 위 상태줄만 둔다.
실행 (웹 UI 폴더 맨 위에서): .venv/Scripts/python loop/main.py  →  http://127.0.0.1:7860 (브라우저 자동)
"""

import os
import sys
from pathlib import Path

# 스크립트로 실행해도 웹 UI 폴더 맨 위 기준으로 loop·engines 를 import 하게
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gradio as gr  # noqa: E402

from loop.core import nav
from loop.core.config import app_title, load_profile
from loop.core.engine import get_engine, status_markdown
from loop.core.style import CSS, FORCE_DARK, THEME, header_html
from loop.tabs.dataset import ui as dataset_ui
from loop.tabs.evaluate import ui as evaluate_ui
from loop.tabs.inference import ui as inference_ui
from loop.tabs.train import ui as train_ui

# 탭 id → 화면. 순서·이름은 nav.STEPS (진행 단계와 같게)
TABS = {"dataset": dataset_ui, "train": train_ui, "evaluate": evaluate_ui, "inference": inference_ui}

STATUS_EVERY = 5  # 초. 상태줄이 엔진 상태를 묻는 간격
PROGRESS_EVERY = 20  # 초. 진행 단계 줄 (탭을 누를 때·페이지 열 때도 새로 그림)


def build_app() -> gr.Blocks:
    profile = load_profile()  # 화면 제목의 프로젝트 이름·말은 profiles/<프로필>/profile.yaml 에서
    engine = get_engine().name
    title = f"{app_title()} · {profile['name']}" if profile["name"] else app_title()
    with gr.Blocks(title=title) as app:
        gr.HTML(header_html(title, f"{profile['title']} 모델 · {engine}", [name for _, name in nav.STEPS]))
        status_bar = gr.Markdown(f"⏳ {engine} 상태 확인 중…", elem_id="status-bar")
        progress = gr.Markdown("", elem_id="progress")  # ① 데이터셋 → ② 학습 → ③ 평가 → ④ 추론 (어디까지 했는지)
        # 탭이 "페이지 열 때 채울 것"을 (함수, 입력, 출력) 으로 돌려줄 수 있다.
        # 넷째 값 "show" 를 붙이면 그 탭을 누를 때마다도 다시 채운다 (다른 탭에서 만든 것을 바로 보이게).
        on_load = []
        with gr.Tabs() as tabs:
            nav.set_tabs(tabs)  # 탭 안의 "다음 →" 버튼이 이 묶음을 바꿔 다음 탭으로 넘긴다
            for key, name in nav.STEPS:
                with gr.Tab(name, id=key) as tab:
                    for fn, inputs, outputs, *when in TABS[key].build() or []:
                        on_load.append((fn, inputs, outputs))
                        if "show" in when:
                            tab.select(fn, inputs=inputs, outputs=outputs, show_progress="hidden")
                tab.select(nav.progress_markdown, outputs=progress, show_progress="hidden")
        app.load(status_markdown, outputs=status_bar, show_progress="hidden")
        app.load(nav.progress_markdown, outputs=progress, show_progress="hidden")
        for fn, inputs, outputs in on_load:
            app.load(fn, inputs=inputs, outputs=outputs, show_progress="hidden")
        gr.Timer(STATUS_EVERY).tick(status_markdown, outputs=status_bar, show_progress="hidden")
        # 진행 단계는 탭을 누를 때 바로 바뀌니 타이머는 느리게 (학습 상태를 WSL 파일에서 읽어 무거움)
        gr.Timer(PROGRESS_EVERY).tick(nav.progress_markdown, outputs=progress, show_progress="hidden")
    return app


def run() -> None:
    """웹 UI 를 띄우고 끌 때까지 기다린다 (Ctrl+C 로 끔). loop/launch.py 도 이걸 부른다."""
    # 127.0.0.1 = 이 컴퓨터에서만 접속. 다른 곳에서 열 땐 포트를 열지 말고 SSH 터널·Tailscale로.
    # 브라우저 자동으로 열기. 이미 창이 열려 있으면 WEBUI_NO_BROWSER=1 로 켜고 그 창에서 F5 (새 창이 계속 쌓이지 않게)
    open_browser = os.environ.get("WEBUI_NO_BROWSER") != "1"
    build_app().launch(server_name="127.0.0.1", server_port=7860, inbrowser=open_browser,
                       theme=THEME, css=CSS, js=FORCE_DARK)


if __name__ == "__main__":
    run()
